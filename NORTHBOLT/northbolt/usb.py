"""USB flash drives and USB SD card readers plugged into the Pi.

Drives are found with lsblk and mounted read-only through udisks2 (udisksctl), so pulling a
drive out can never corrupt it. deploy/install.sh installs udisks2 and a polkit rule that lets
the app's user mount, unmount and power off removable drives (nothing else).

Printing from a drive copies the file to the Pi first, so the drive can be removed once the
print starts and power-loss recovery still has the file.

Set NORTHBOLT_FAKE_USB=<folder> to treat a local folder as a plugged-in drive (development).
"""

import asyncio
import filecmp
import json
import logging
import os
import shutil
import time
from collections import OrderedDict

from .gcode import quick_scan

log = logging.getLogger("usb")

GCODE_EXT = (".gcode", ".gco", ".g")
SUPPORTED_FS = {"vfat", "exfat", "ntfs", "ntfs3", "ext4", "ext3", "ext2"}
SYSTEM_MOUNTS = {"/", "/boot", "/boot/firmware", "[SWAP]"}
SKIP_NAMES = {"System Volume Information", "$RECYCLE.BIN", "LOST.DIR", "lost+found", "FOUND.000"}
MAX_ENTRIES = 500


class UsbError(RuntimeError):
    pass


def _flag(value):
    return value in (True, 1, "1", "true", "True")


def parse_lsblk(data):
    """Pick removable USB volumes out of `lsblk -J` output. Skips the disk the system runs from."""
    volumes = []
    for disk in data.get("blockdevices", []):
        if disk.get("type") != "disk":
            continue
        name = disk.get("name", "")
        if name.startswith(("mmcblk", "zram", "loop", "ram")):
            continue   # the Pi's own SD card and RAM disks
        removable = disk.get("tran") == "usb" or _flag(disk.get("hotplug")) or _flag(disk.get("rm"))
        if not removable:
            continue
        children = disk.get("children") or []
        mounts = {c.get("mountpoint") for c in children} | {disk.get("mountpoint")}
        if mounts & SYSTEM_MOUNTS:
            continue   # booting from a USB SSD: never touch it
        parts = children if children else [disk]   # "superfloppy" sticks have no partition table
        for part in parts:
            fstype = (part.get("fstype") or "").lower()
            if fstype not in SUPPORTED_FS:
                continue
            mp = part.get("mountpoint")
            if mp and not mp.startswith(("/media/", "/run/media/", "/mnt/")):
                continue
            label = part.get("label") or disk.get("model") or "USB drive"
            volumes.append({
                "id": part.get("name"),
                "dev": part.get("path") or f"/dev/{part.get('name')}",
                "disk": disk.get("path") or f"/dev/{name}",
                "label": str(label).strip() or "USB drive",
                "size": int(part.get("size") or 0),
                "fstype": fstype,
                "mount": mp,
            })
    return volumes


def resolve_inside(root, rel):
    """Join rel onto root and refuse anything that escapes it (.., absolute paths, symlinks)."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, rel))
    if full != root_real and not full.startswith(root_real + os.sep):
        raise UsbError("That path is outside the drive")
    return full


def unique_name(directory, name, src):
    """Reuse an identical file already on the Pi, otherwise pick 'name (2).gcode' and so on."""
    stem, ext = os.path.splitext(name)
    candidate = name
    n = 1
    while True:
        dest = os.path.join(directory, candidate)
        if not os.path.exists(dest):
            return candidate, False
        try:
            if os.path.getsize(dest) == os.path.getsize(src) and filecmp.cmp(dest, src, shallow=False):
                return candidate, True
        except OSError:
            pass
        n += 1
        candidate = f"{stem} ({n}){ext}"


class UsbManager:
    def __init__(self, svc, runner=None, fake_root=None):
        self.svc = svc
        self.fake_root = fake_root
        self._runner = runner or self._exec
        if fake_root:
            self.available = True
        else:
            self.available = runner is not None or (shutil.which("lsblk") is not None and shutil.which("udisksctl") is not None)
        self.drives = OrderedDict()      # id -> volume dict (with "mount" and "error")
        self.ejected = set()             # ids ejected by the user, ignored until unplugged
        self.copy = None                 # progress of a running copy
        self._meta_cache = OrderedDict()
        self._lock = asyncio.Lock()

    async def _exec(self, *args, timeout=20):
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "LANG": "C", "LC_ALL": "C"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", "timed out"
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    # ------------------------------------------------------------------ detection
    def snapshot(self):
        return {
            "available": self.available,
            "drives": [{k: d.get(k) for k in ("id", "label", "size", "fstype", "error")} | {"ready": bool(d.get("mount"))}
                       for d in self.drives.values()],
            "copy": self.copy,
        }

    async def scan(self):
        if self.fake_root:
            found = [] if "fake" in self.ejected or not os.path.isdir(self.fake_root) else [{
                "id": "fake", "dev": "/dev/fake", "disk": "/dev/fake", "label": "USB DRIVE",
                "size": 32 * 1024 ** 3, "fstype": "vfat", "mount": self.fake_root}]
        else:
            rc, out, err = await self._runner(
                "lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,TRAN,RM,HOTPLUG,FSTYPE,LABEL,MODEL,SIZE,MOUNTPOINT", timeout=10)
            if rc != 0:
                return
            found = parse_lsblk(json.loads(out or "{}"))
        ids = {v["id"] for v in found}
        self.ejected &= ids | ({"fake"} if self.fake_root else set())
        changed = False
        for vid in list(self.drives):
            if vid not in ids:
                log.info("Drive %s removed", self.drives[vid]["label"])
                del self.drives[vid]
                changed = True
        for vol in found:
            if vol["id"] in self.ejected:
                continue
            known = self.drives.get(vol["id"])
            if known is None:
                vol["error"] = None
                self.drives[vol["id"]] = vol
                changed = True
                log.info("Drive %s (%s) plugged in", vol["label"], vol["dev"])
                if not vol["mount"]:
                    await self._mount(vol)
            elif vol["mount"] and known.get("mount") != vol["mount"]:
                known["mount"] = vol["mount"]
                changed = True
        if changed:
            self.svc.notify("usb")

    async def _mount(self, vol):
        rc, out, err = await self._runner("udisksctl", "mount", "-b", vol["dev"], "-o", "ro",
                                          "--no-user-interaction", timeout=30)
        text = (out or "") + (err or "")
        if rc == 0 and " at " in text:
            path = text.split(" at ", 1)[1].strip().splitlines()[0].strip()
            if not os.path.isdir(path) and path.endswith("."):
                path = path[:-1]
            vol["mount"] = path
            vol["error"] = None
        else:
            msg = text.strip().splitlines()[-1] if text.strip() else "Could not open the drive"
            if "not authorized" in msg.lower():
                msg = "Not allowed to open drives. Run deploy/install.sh again."
            vol["error"] = msg
            log.warning("Mount of %s failed: %s", vol["dev"], msg)

    async def task(self):
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("USB scan failed")
            await asyncio.sleep(3)

    def _drive(self, drive_id):
        d = self.drives.get(drive_id)
        if not d:
            raise UsbError("That drive isn't plugged in any more")
        if not d.get("mount"):
            raise UsbError(d.get("error") or "The drive is still being opened")
        return d

    # ------------------------------------------------------------------ browsing
    def list(self, drive_id, rel=""):
        d = self._drive(drive_id)
        root = d["mount"]
        full = resolve_inside(root, rel)
        if not os.path.isdir(full):
            raise UsbError("That folder doesn't exist")
        entries = []
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith(".") or e.name in SKIP_NAMES:
                    continue
                try:
                    target = os.path.realpath(e.path)
                    if target != os.path.realpath(root) and not target.startswith(os.path.realpath(root) + os.sep):
                        continue   # symlink pointing off the drive
                    if e.is_dir():
                        entries.append({"name": e.name, "type": "dir"})
                    elif e.is_file() and e.name.lower().endswith(GCODE_EXT):
                        st = e.stat()
                        entries.append({"name": e.name, "type": "file", "size": st.st_size, "date": st.st_mtime})
                except OSError:
                    continue
                if len(entries) >= MAX_ENTRIES:
                    break
        dirs = sorted((x for x in entries if x["type"] == "dir"), key=lambda x: x["name"].lower())
        gfiles = sorted((x for x in entries if x["type"] == "file"), key=lambda x: -x["date"])
        rel_norm = os.path.relpath(full, os.path.realpath(root)).replace(os.sep, "/")
        rel_norm = "" if rel_norm == "." else rel_norm
        return {"drive": drive_id, "label": d["label"], "path": rel_norm, "entries": dirs + gfiles}

    def file_path(self, drive_id, rel):
        d = self._drive(drive_id)
        full = resolve_inside(d["mount"], rel)
        if not os.path.isfile(full) or not full.lower().endswith(GCODE_EXT):
            raise UsbError("That file isn't on the drive")
        return full

    def meta(self, drive_id, rel):
        """Quick metadata, cached by path, size and time so thumbnails don't re-read the drive."""
        full = self.file_path(drive_id, rel)
        st = os.stat(full)
        key = (full, st.st_size, st.st_mtime)
        if key in self._meta_cache:
            self._meta_cache.move_to_end(key)
            return self._meta_cache[key]
        meta = quick_scan(full)
        meta["date"] = st.st_mtime
        self._meta_cache[key] = meta
        while len(self._meta_cache) > 200:
            self._meta_cache.popitem(last=False)
        return meta

    # ------------------------------------------------------------------ copying and ejecting
    async def copy_to_printer(self, drive_id, rel, then_print=False):
        svc = self.svc
        src = self.file_path(drive_id, rel)
        if self.copy:
            raise UsbError("Another file is being copied")
        if then_print:
            svc.require_ready()
            if svc.recovery.pending():
                raise UsbError("Resume or discard the interrupted print first")
        from .printer import safe_filename
        name, identical = unique_name(svc.gcode_dir, safe_filename(os.path.basename(src)), src)
        total = os.path.getsize(src)
        self.copy = {"name": name, "done": 0, "total": total, "print": then_print}
        svc.notify("usb")
        loop = asyncio.get_running_loop()

        def _copy():
            dest = os.path.join(svc.gcode_dir, name)
            tmp = os.path.join(svc.gcode_dir, f".copy-{int(time.time() * 1000)}")
            last = 0.0
            try:
                with open(src, "rb") as fin, open(tmp, "wb") as fout:
                    while True:
                        chunk = fin.read(1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)
                        self.copy["done"] += len(chunk)
                        now = time.monotonic()
                        if now - last > 0.4:
                            last = now
                            loop.call_soon_threadsafe(svc.notify, "usb")
                    fout.flush()
                    os.fsync(fout.fileno())
                os.replace(tmp, dest)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        try:
            if identical:
                self.copy["done"] = total
            else:
                await loop.run_in_executor(None, _copy)
                svc.schedule_scan(name)
            log.info("Copied %s from %s", name, self.drives.get(drive_id, {}).get("label"))
            if then_print:
                await svc.start_print(name)
            return name
        except OSError as exc:
            raise UsbError(f"Copy failed: {exc.strerror or exc}. Was the drive removed?")
        finally:
            self.copy = None
            svc.notify("usb")

    async def eject(self, drive_id):
        d = self.drives.get(drive_id)
        if not d:
            raise UsbError("That drive isn't plugged in")
        if self.copy:
            raise UsbError("Wait for the copy to finish")
        if not self.fake_root:
            if d.get("mount"):
                rc, out, err = await self._runner("udisksctl", "unmount", "-b", d["dev"], "--no-user-interaction", timeout=30)
                if rc != 0:
                    raise UsbError((err or out).strip() or "Could not eject the drive")
            # Spin the drive down so the light goes off; harmless if the drive doesn't support it.
            await self._runner("udisksctl", "power-off", "-b", d["disk"], "--no-user-interaction", timeout=30)
        self.ejected.add(drive_id)
        del self.drives[drive_id]
        self.svc.notify("usb")


def make_usb(svc):
    fake = os.environ.get("NORTHBOLT_FAKE_USB")
    return UsbManager(svc, fake_root=fake) if fake else UsbManager(svc)
