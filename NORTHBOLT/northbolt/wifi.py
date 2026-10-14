"""Wi-Fi setup through NetworkManager (nmcli), as used by current Raspberry Pi OS.

The app runs as a normal user. deploy/install.sh adds a polkit rule that lets that user manage
NetworkManager, and a sudoers entry for setting the Wi-Fi country with raspi-config.
Set NORTHBOLT_FAKE_WIFI=1 to use a pretend backend (for development on a PC).
"""

import asyncio
import logging
import os
import re
import shutil

log = logging.getLogger("wifi")


class WifiError(RuntimeError):
    pass


def split_terse(line):
    """Split one line of `nmcli -t` output. Colons inside values are escaped as '\\:'."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def friendly_error(text):
    low = text.lower()
    if "property 'psk' is invalid" in low or "key-mgmt" in low:
        return "That password isn't valid for this network (WPA passwords are 8 to 63 characters)."
    if "secrets were required" in low or "802-1x" in low:
        return "Wrong password, or the network refused it."
    if "no network with ssid" in low:
        return "That network isn't in range."
    if "not authorized" in low:
        return "This app isn't allowed to change Wi-Fi. Run deploy/install.sh again."
    if "timeout" in low or "timed out" in low:
        return "The network didn't answer in time. Move the printer closer to the router or try again."
    return text.strip().replace("Error: ", "") or "Wi-Fi command failed"


class Wifi:
    def __init__(self, runner=None):
        self._runner = runner or self._exec
        self.available = runner is not None or shutil.which("nmcli") is not None
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

    async def _nmcli(self, *args, timeout=20, check=True):
        rc, out, err = await self._runner("nmcli", *args, timeout=timeout)
        if check and rc != 0:
            raise WifiError(friendly_error(err or out))
        return out

    def _require(self):
        if not self.available:
            raise WifiError("Wi-Fi setup needs NetworkManager (Raspberry Pi OS Bookworm or newer).")

    # ------------------------------------------------------------------ reading
    async def status(self):
        if not self.available:
            return {"available": False}
        radio = (await self._nmcli("-t", "-f", "WIFI", "radio", check=False)).strip()
        device = state = connection = None
        for line in (await self._nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", check=False)).splitlines():
            f = split_terse(line)
            if len(f) >= 4 and f[1] == "wifi":
                device, state, connection = f[0], f[2], f[3] or None
                break
        ssid = signal = ip = None
        if device and state and state.startswith("connected"):
            for line in (await self._nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list",
                                           "--rescan", "no", check=False)).splitlines():
                f = split_terse(line)
                if len(f) >= 3 and f[0] == "yes":
                    ssid, signal = f[1], int(f[2] or 0)
                    break
            addr = (await self._nmcli("-g", "IP4.ADDRESS", "device", "show", device, check=False)).strip()
            ip = addr.split("|")[0].split("/")[0] or None
        country = await self._country()
        return {
            "available": True, "radio": radio == "enabled", "device": device, "state": state,
            "connection": connection, "ssid": ssid, "signal": signal, "ip": ip, "country": country,
        }

    async def _country(self):
        rc, out, _ = await self._runner("iw", "reg", "get", timeout=5)
        if rc != 0:
            return None
        m = re.search(r"country\s+([A-Z0-9]{2})", out)
        code = m.group(1) if m else None
        return None if code in (None, "00") else code

    async def _saved(self):
        """Map SSID -> saved connection name (names can differ, e.g. Imager's 'preconfigured')."""
        saved = {}
        out = await self._nmcli("-t", "-f", "NAME,TYPE", "connection", "show", check=False)
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[1] in ("802-11-wireless", "wifi"):
                ssid = (await self._nmcli("-g", "802-11-wireless.ssid", "connection", "show", f[0],
                                          check=False)).strip()
                saved[ssid or f[0]] = f[0]
        return saved

    async def networks(self, rescan=True):
        self._require()
        out = await self._nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                                "--rescan", "yes" if rescan else "auto", timeout=30, check=False)
        saved = await self._saved()
        best = {}
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 4 or not f[1]:
                continue   # hidden networks have no name
            ssid, security = f[1], f[3].strip()
            item = {
                "ssid": ssid,
                "signal": int(f[2] or 0),
                "security": "" if security in ("", "--") else security,
                "secure": security not in ("", "--"),
                "in_use": f[0].strip() == "*",
                "saved": ssid in saved,
            }
            prev = best.get(ssid)
            if prev is None or item["in_use"] or (item["signal"] > prev["signal"] and not prev["in_use"]):
                best[ssid] = item
        return sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"], n["ssid"].lower()))

    # ------------------------------------------------------------------ changing
    async def connect(self, ssid, password=None, hidden=False):
        self._require()
        ssid = (ssid or "").strip()
        if not ssid or len(ssid.encode()) > 32:
            raise WifiError("Network names are 1 to 32 characters.")
        if password is not None and password != "" and not 8 <= len(password) <= 63:
            raise WifiError("Wi-Fi passwords are 8 to 63 characters.")
        async with self._lock:
            saved = await self._saved()
            if ssid in saved and not password:
                await self._nmcli("--wait", "40", "connection", "up", "id", saved[ssid], timeout=50)
                return await self.status()
            if ssid in saved:
                # A new password for a saved network: replace the old profile so stale secrets don't win.
                await self._nmcli("connection", "delete", "id", saved[ssid], check=False)
            args = ["--wait", "40", "device", "wifi", "connect", ssid]
            if password:
                args += ["password", password]
            if hidden:
                args += ["hidden", "yes"]
            rc, out, err = await self._runner("nmcli", *args, timeout=50)
            if rc != 0:
                # A failed attempt leaves a half-made profile behind; remove it.
                await self._nmcli("connection", "delete", "id", ssid, check=False)
                raise WifiError(friendly_error(err or out))
            log.info("Connected to Wi-Fi %s", ssid)
            return await self.status()

    async def forget(self, ssid):
        self._require()
        async with self._lock:
            saved = await self._saved()
            if ssid not in saved:
                raise WifiError("That network isn't saved.")
            await self._nmcli("connection", "delete", "id", saved[ssid])

    async def set_radio(self, on):
        self._require()
        await self._nmcli("radio", "wifi", "on" if on else "off")

    async def set_country(self, code):
        code = (code or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise WifiError("Use a two-letter country code, for example GB, US, DE or NG.")
        rc, out, err = await self._runner("sudo", "-n", "/usr/bin/raspi-config", "nonint", "do_wifi_country", code,
                                          timeout=30)
        if rc != 0:
            raise WifiError(friendly_error(err or out) if err or out else "Could not set the Wi-Fi country.")
        return code


class FakeWifi(Wifi):
    """Pretend networks for trying the screens on a PC. The password for every network is 'northbolt'."""

    def __init__(self):
        super().__init__(runner=self._fake)
        self.nets = [
            {"ssid": "HomeNet", "signal": 82, "security": "WPA2"},
            {"ssid": "Workshop_5G", "signal": 64, "security": "WPA2 WPA3"},
            {"ssid": "PrinterLab", "signal": 55, "security": "WPA2"},
            {"ssid": "Neighbour's Wi-Fi", "signal": 38, "security": "WPA1 WPA2"},
            {"ssid": "CafeFree", "signal": 30, "security": ""},
        ]
        self.saved = {"HomeNet": "preconfigured"}
        self.active = "HomeNet"
        self.radio = True
        self.country = "GB"

    async def _fake(self, *args, timeout=20):
        await asyncio.sleep(0.05)
        a = list(args)
        if a[0] == "iw":
            return 0, f"global\ncountry {self.country}: DFS-ETSI\n", ""
        if a[0] == "sudo":
            self.country = a[-1]
            return 0, "", ""
        a = a[1:]
        if a[:2] == ["--wait", "40"]:
            a = a[2:]
        if a == ["-t", "-f", "WIFI", "radio"]:
            return 0, "enabled\n" if self.radio else "disabled\n", ""
        if a[:3] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            state = "connected" if self.active else "disconnected"
            return 0, f"wlan0:wifi:{state}:{self.saved.get(self.active, '') if self.active else ''}\neth0:ethernet:unavailable:\n", ""
        if a[:3] == ["-t", "-f", "ACTIVE,SSID,SIGNAL"]:
            return 0, "".join(f"{'yes' if n['ssid'] == self.active else 'no'}:{n['ssid']}:{n['signal']}\n" for n in self.nets), ""
        if a[:2] == ["-g", "IP4.ADDRESS"]:
            return 0, "192.168.1.57/24\n" if self.active else "", ""
        if a[:3] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"]:
            if not self.radio:
                return 0, "", ""
            esc = lambda s: s.replace("\\", "\\\\").replace(":", "\\:")
            return 0, "".join(f"{'*' if n['ssid'] == self.active else ' '}:{esc(n['ssid'])}:{n['signal']}:{n['security'] or '--'}\n"
                              for n in self.nets), ""
        if a[:4] == ["-t", "-f", "NAME,TYPE", "connection"]:
            return 0, "".join(f"{name}:802-11-wireless\n" for name in self.saved.values()) + "Wired connection 1:802-3-ethernet\n", ""
        if a[:2] == ["-g", "802-11-wireless.ssid"]:
            name = a[-1]
            return 0, next((s for s, n in self.saved.items() if n == name), "") + "\n", ""
        if a[:3] == ["connection", "up", "id"]:
            ssid = next(s for s, n in self.saved.items() if n == a[3])
            await asyncio.sleep(1.0)
            self.active = ssid
            return 0, "Connection successfully activated", ""
        if a[:3] == ["connection", "delete", "id"]:
            name = a[3]
            for s, n in list(self.saved.items()):
                if n == name:
                    del self.saved[s]
                    if self.active == s:
                        self.active = None
                    return 0, "", ""
            return 10, "", "Error: unknown connection"
        if a[:3] == ["device", "wifi", "connect"]:
            ssid = a[3]
            await asyncio.sleep(1.5)
            net = next((n for n in self.nets if n["ssid"] == ssid), None)
            hidden = "hidden" in a
            if net is None and not hidden:
                return 10, "", f"Error: No network with SSID '{ssid}' found."
            pw = a[a.index("password") + 1] if "password" in a else None
            if (net is None or net["security"]) and pw != "northbolt":
                self.saved[ssid] = ssid   # nmcli leaves a profile behind on failure
                return 4, "", "Error: Connection activation failed: Secrets were required, but not provided."
            self.saved[ssid] = ssid
            self.active = ssid
            return 0, f"Device 'wlan0' successfully activated", ""
        if a[:2] == ["radio", "wifi"]:
            self.radio = a[2] == "on"
            if not self.radio:
                self.active = None
            return 0, "", ""
        return 1, "", f"fake nmcli: unsupported {a}"


def make_wifi():
    return FakeWifi() if os.environ.get("NORTHBOLT_FAKE_WIFI") else Wifi()
