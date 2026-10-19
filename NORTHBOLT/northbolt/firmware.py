"""Update the printer board's firmware from a GitHub release, from the screen.

Flow: check the configured repository's latest release, download the printer .hex asset, check it
(Intel HEX checksums, fits below the bootloader, really is Marlin), read the current firmware off
the board as a backup, flash the new one with avrdude over the USB serial port, reconnect and
confirm what the board now reports. A failed flash leaves the bootloader alone, so the board can
always be flashed again (Retry, or Restore the backup).

Releases come from your own repository: .github/workflows/firmware.yml builds the .hex when you
push a tag like fw-1.1.9.9-NB1 and attaches it to a release (or upload one by hand).
"""

import asyncio
import glob
import hashlib
import logging
import os
import re
import time

import aiohttp

from .config import github_repo

log = logging.getLogger("firmware")

APP_LIMIT = 0x3E000          # ATmega2560 with the 8 KB Arduino bootloader: application ends here
MAX_DOWNLOAD = 2 * 1024 * 1024


class FirmwareError(RuntimeError):
    pass


def parse_repo(text):
    """'owner/repo', 'https://github.com/owner/repo(.git)' or 'github.com/owner/repo' -> 'owner/repo'."""
    t = (text or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", t)
    if not m:
        raise FirmwareError("Use your repository link, like https://github.com/you/SkriwareFirmware")
    return f"{m.group(1)}/{m.group(2)}"


def check_hex(data):
    """Validate Intel HEX text for the ATmega2560. Returns facts about the image or raises."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise FirmwareError("That file isn't a .hex firmware file")
    base = 0
    image = {}
    end_seen = False
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":") or len(line) < 11:
            raise FirmwareError(f"Line {n} isn't Intel HEX")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError:
            raise FirmwareError(f"Line {n} isn't Intel HEX")
        count, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
        if len(raw) != count + 5 or sum(raw) & 0xFF:
            raise FirmwareError(f"Checksum error on line {n}: the file is damaged")
        payload = raw[4:4 + count]
        if rtype == 0x00:
            for i, b in enumerate(payload):
                image[base + addr + i] = b
        elif rtype == 0x01:
            end_seen = True
            break
        elif rtype == 0x02:
            base = ((payload[0] << 8) | payload[1]) << 4
        elif rtype == 0x04:
            base = ((payload[0] << 8) | payload[1]) << 16
        elif rtype in (0x03, 0x05):
            pass
        else:
            raise FirmwareError(f"Unknown record type on line {n}")
    if not end_seen or not image:
        raise FirmwareError("The file is incomplete")
    top = max(image) + 1
    if top > APP_LIMIT:
        raise FirmwareError("The firmware is too big: it would overwrite the bootloader")
    blob = bytes(image.get(i, 0xFF) for i in range(top))
    m = re.search(rb"FIRMWARE_NAME:([^\x00]*?) SOURCE_CODE_URL", blob)
    if not m:
        raise FirmwareError("That isn't printer firmware (no Marlin firmware name inside). "
                            "The LED board's program can't be flashed from here.")
    return {"size": len(image), "end": top, "name": m.group(1).decode("ascii", "replace")}


class FirmwareUpdater:
    def __init__(self, svc):
        self.svc = svc
        self.dir = os.path.join(svc.data_dir, "firmware")
        os.makedirs(self.dir, exist_ok=True)
        self.state = "idle"     # idle, checking, ready, working, done, error
        self.phase = None       # download, backup, write, verify, reconnect
        self.progress = None
        self.message = ""
        self.release = None
        self.result = None
        self.error = None
        self._task = None
        self.available = None      # newer release than the board runs: {tag, name, firmware, current}
        self.last_check = None
        self.snoozed_until = 0.0
        self._name_cache = {}

    @property
    def busy(self):
        return self.state in ("checking", "working")

    def backup(self):
        files = sorted(glob.glob(os.path.join(self.dir, "backup-*.hex")))
        if not files:
            return None
        return {"file": os.path.basename(files[-1]), "time": os.path.getmtime(files[-1])}

    def snapshot(self):
        cfg = self.svc.settings["firmware"]
        return {
            "state": self.state, "phase": self.phase, "progress": self.progress, "message": self.message,
            "release": self.release, "result": self.result, "error": self.error, "backup": self.backup(),
            "available": self.available, "prompt": self.prompt(),
            "last_check": self.last_check, "auto_check": cfg.get("auto_check", True),
            "skipped_tag": cfg.get("skipped_tag"),
        }

    def _set(self, state=None, phase=None, progress=None, message=None):
        if state is not None:
            self.state = state
        if phase is not None:
            self.phase = phase
        self.progress = progress
        if message is not None:
            self.message = message
        self.svc.notify("firmware")

    # ------------------------------------------------------------------ GitHub
    def _headers(self, accept="application/vnd.github+json"):
        h = {"Accept": accept, "User-Agent": "NORTHBOLT-printer"}
        token = self.svc.settings["firmware"].get("token")
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _fetch_release(self):
        """The latest release and its printer .hex asset. Raises FirmwareError; doesn't touch the UI state."""
        cfg = self.svc.settings["firmware"]
        repo = parse_repo(github_repo(self.svc.settings))
        url = f"{cfg['github_api'].rstrip('/')}/repos/{repo}/releases/latest"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(url, headers=self._headers()) as r:
                    if r.status == 404:
                        raise FirmwareError("No firmware release has been published yet")
                    if r.status in (401, 403):
                        raise FirmwareError("The update server refused the request. Try again in an hour.")
                    if r.status != 200:
                        raise FirmwareError(f"The update server answered {r.status}")
                    rel = await r.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise FirmwareError("Can't reach the update server. Is the printer online?")
        hexes = [a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".hex")]
        match = (cfg.get("asset_match") or "printer").lower()
        picked = [a for a in hexes if match in a["name"].lower()] or (hexes if len(hexes) == 1 else [])
        if not picked:
            raise FirmwareError(f"The latest release ({rel.get('tag_name')}) has no printer .hex file "
                                f"(looking for a name containing '{match}').")
        a = picked[0]
        return {
            "repo": repo, "tag": rel.get("tag_name"), "name": rel.get("name") or rel.get("tag_name"),
            "notes": (rel.get("body") or "")[:2000], "published": rel.get("published_at"),
            "asset": a["name"], "size": a.get("size"), "digest": a.get("digest"),
            "download": a.get("browser_download_url"), "api_url": a.get("url"),
            "asset_id": a.get("id"), "updated": a.get("updated_at"),
        }

    async def _release_firmware_name(self, rel):
        """The firmware name inside the release's .hex (what M115 will report once installed). Cached."""
        key = (rel.get("download"), rel.get("digest"), rel.get("size"), rel.get("updated"))
        if self._name_cache.get("key") == key:
            return self._name_cache["name"]
        info = check_hex(await self._download(rel, report=False))
        self._name_cache = {"key": key, "name": info["name"]}
        return info["name"]

    def _board_firmware_name(self):
        fw = self.svc.firmware or ""
        m = re.search(r"FIRMWARE_NAME:(.*?) SOURCE_CODE_URL", fw)
        return m.group(1) if m else None

    async def _compare(self, rel):
        """Is the release different from what the board runs? Sets self.available."""
        rel_name = await self._release_firmware_name(rel)
        board = self._board_firmware_name()
        self.last_check = time.time()
        if board is None:
            self.available = None            # can't tell without the board's M115
        elif rel_name != board:
            self.available = {"tag": rel["tag"], "name": rel["name"], "firmware": rel_name, "current": board}
        else:
            self.available = None
        self.svc.notify("firmware")
        return self.available

    async def check(self):
        """'Check for update' button: fetch the release, show it, and work out if it's new."""
        if self.busy:
            raise FirmwareError("Busy")
        self.error = None
        try:
            self._set("checking", message="Checking for new firmware")
            rel = await self._fetch_release()
            await self._compare(rel)         # downloads and checks the .hex, so a bad file is caught here
            self.release = rel
        except FirmwareError as exc:
            self.error = str(exc)
            self._set("error", message="")
            raise
        self._set("ready", message="")
        return self.release

    async def auto_check(self):
        """Background check: never changes the screen except to offer an update."""
        if self.busy or not self.svc.link.ready or self.svc.job.active:
            return
        try:
            rel = await self._fetch_release()
            await self._compare(rel)
            if self.available:
                self.release = rel           # so Install works straight from the prompt
                if self.state == "idle":
                    self.state = "ready"     # the Firmware tab shows the release card
            log.info("Update check: %s", f"{self.available['tag']} available" if self.available else "up to date")
        except FirmwareError as exc:
            log.info("Update check failed: %s", exc)

    async def auto_task(self):
        cfg = self.svc.settings["firmware"]
        await asyncio.sleep(cfg.get("first_check_s", 60))
        while True:
            cfg = self.svc.settings["firmware"]
            try:
                if cfg.get("auto_check", True):
                    # wait until the board has reported its firmware, so there is something to compare
                    for _ in range(120):
                        if self.svc.link.ready and self.svc.firmware:
                            break
                        await asyncio.sleep(5)
                    await self.auto_check()
                await asyncio.sleep(max(60.0, float(cfg.get("check_hours", 6)) * 3600))
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Update check task")
                await asyncio.sleep(3600)

    def prompt(self):
        """Should the screen offer the update now?"""
        a = self.available
        if not a or self.busy or self.svc.job.active:
            return False
        if a["tag"] == self.svc.settings["firmware"].get("skipped_tag"):
            return False
        return time.time() >= self.snoozed_until

    def snooze(self, hours=24):
        self.snoozed_until = time.time() + hours * 3600
        self.svc.notify("firmware")

    def skip(self):
        if not self.available:
            raise FirmwareError("There's no update to skip")
        self.svc.settings.update({"firmware": {"skipped_tag": self.available["tag"]}})
        self.svc.notify("firmware")

    async def _download(self, rel=None, report=True):
        rel = rel or self.release
        cfg = self.svc.settings["firmware"]
        private = bool(cfg.get("token"))
        url = rel["api_url"] if private and rel.get("api_url") else rel["download"]
        headers = self._headers("application/octet-stream")
        data = b""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    raise FirmwareError(f"Download failed ({r.status})")
                async for chunk in r.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > MAX_DOWNLOAD:
                        raise FirmwareError("The download is far too big for printer firmware")
                    if report and rel.get("size"):
                        self._set(progress=min(100, int(100 * len(data) / rel["size"])))
        digest = rel.get("digest") or ""
        if digest.startswith("sha256:"):
            if hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1].lower():
                raise FirmwareError("The download is damaged (checksum mismatch). Try again.")
        return data

    # ------------------------------------------------------------------ flashing
    def install(self):
        if self.busy:
            raise FirmwareError("Busy")
        if not self.release:
            raise FirmwareError("Check for an update first")
        self._preflight()
        self._task = asyncio.ensure_future(self._run(download=True))

    def restore(self):
        if self.busy:
            raise FirmwareError("Busy")
        if not self.backup():
            raise FirmwareError("There is no backup yet")
        self._preflight()
        self._task = asyncio.ensure_future(self._run(download=False))

    def _preflight(self):
        svc = self.svc
        if svc.job.active:
            raise FirmwareError("Not while printing")
        if svc.wizard.active or svc.leveling.active:
            raise FirmwareError("Finish the calibration first")
        if not self._port():
            raise FirmwareError("The printer board's serial port isn't known. Connect to the printer once first.")

    def _port(self):
        port = self.svc.link.port or self.svc.settings["serial_port"]
        if port in (None, "", "auto"):
            from .protocol import find_serial_port
            port = find_serial_port()
        return port

    async def _avrdude(self, op, path, phase_names):
        """Run avrdude, turning its progress bars into phase/progress updates."""
        cfg = self.svc.settings["firmware"]
        cmd = list(cfg["avrdude"]) + ["-p", "m2560", "-c", cfg["programmer"], "-P", self._port(),
                                      "-b", str(cfg["baud"]), "-D", "-U", f"flash:{op}:{path}:i"]
        log.info("Running %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out = b""
        phase_i = -1
        hashes = 0
        while True:
            chunk = await proc.stdout.read(64)
            if not chunk:
                break
            out += chunk
            text = chunk.decode("ascii", "replace")
            for ch in re.split(r"(Reading \||Writing \||#)", text):
                if ch in ("Reading |", "Writing |"):
                    phase_i = min(phase_i + 1, len(phase_names) - 1)
                    hashes = 0
                    self._set(phase=phase_names[phase_i], progress=0)
                elif ch == "#" and phase_i >= 0:
                    hashes += 1
                    self._set(phase=phase_names[phase_i], progress=min(100, hashes * 2))
        rc = await proc.wait()
        if rc != 0:
            tail = out.decode("ascii", "replace").strip().splitlines()[-3:]
            raise FirmwareError("avrdude failed: " + " / ".join(t.strip() for t in tail))
        return out.decode("ascii", "replace")

    async def _run(self, download):
        svc = self.svc
        self.error = None
        self.result = None
        flashed = False
        new_path = None
        try:
            if download:
                self._set("working", phase="download", progress=0, message=f"Downloading {self.release['asset']}")
                data = await self._download()
                info = check_hex(data)
                new_path = os.path.join(self.dir, "new.hex")
                with open(new_path, "wb") as fh:
                    fh.write(data)
                label = self.release["tag"]
            else:
                new_path = os.path.join(self.dir, self.backup()["file"])
                with open(new_path, "rb") as fh:
                    info = check_hex(fh.read())
                label = "the backup"
            self._set("working", phase="prepare", message=f"Installing {info['name']}")
            if svc.link.ready:
                for c in ("M104 T0 S0", "M104 T1 S0", "M140 S0"):
                    try:
                        await svc.link.command(c, priority=True)
                    except Exception:
                        pass
            await svc.disconnect()              # frees the port and stops auto-reconnect
            await asyncio.sleep(1.0)
            if download:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup = os.path.join(self.dir, f"backup-{stamp}.hex")
                self._set(phase="backup", progress=0, message="Saving the current firmware as a backup")
                await self._avrdude("r", backup, ["backup"])
                with open(backup, "rb") as fh:
                    check_hex(fh.read())        # make sure the backup itself is usable
                for old in sorted(glob.glob(os.path.join(self.dir, "backup-*.hex")))[:-3]:
                    os.unlink(old)
            self._set(phase="write", progress=0, message=f"Writing {label} to the printer board. Don't turn it off.")
            flashed = True
            await self._avrdude("w", new_path, ["write", "verify"])
            self._set(phase="reconnect", progress=None, message="Reconnecting")
            await asyncio.sleep(2.0)
            await svc.connect()
            fw = svc.firmware or ""
            self.result = {"expected": info["name"], "reported": fw.replace("FIRMWARE_NAME:", "").split(" SOURCE_CODE_URL")[0]}
            ok = info["name"] in fw
            if self.release:                 # after an update (or a restore) recompare with the release
                try:
                    await self._compare(self.release)
                except FirmwareError:
                    pass
            self._set("done", message="Firmware updated." if ok else
                      "Flashed, but the board reports a different firmware name. Check Settings → Connection.")
            log.info("Firmware now: %s", fw)
        except Exception as exc:
            log.exception("Firmware update failed")
            self.error = str(exc)
            if flashed:
                self.error += (" The bootloader is untouched, so you can Retry or Restore the backup.")
            self._set("error", message="")
            try:
                if not svc.link.connected:
                    await svc.connect()
            except Exception:
                pass
