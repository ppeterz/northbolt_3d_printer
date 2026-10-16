"""PrinterService: one object that owns the printer and everything the UI can see or do."""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from collections import deque

from .gcode import quick_scan, scan_file
from .hardware import LedPolicy, PowerButton, SkriwareBoard
from .firmware import FirmwareUpdater
from .software import SoftwareUpdater
from .job import JobRunner
from .leveling import BedLevelWizard
from .protocol import LinkError, MarlinLink
from .recovery import RecoveryStore, build_plan
from .usb import make_usb
from .wifi import make_wifi
from .zoffset import ZOffsetWizard

log = logging.getLogger("printer")

TEMP_RE = re.compile(r"\b(T\d?|B):\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")
GCODE_EXT = (".gcode", ".gco", ".g")
SAFE_NAME = re.compile(r"[^\w\-. ()+]")


def safe_filename(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = SAFE_NAME.sub("_", name)
    if not name or name.startswith(".") or not name.lower().endswith(GCODE_EXT):
        raise ValueError("Only .gcode files can be uploaded")
    return name[:120]


class PrinterService:
    def __init__(self, settings):
        self.settings = settings
        self.data_dir = settings.data_dir
        self.gcode_dir = os.path.join(self.data_dir, "gcodes")
        self.meta_dir = os.path.join(self.gcode_dir, ".meta")
        os.makedirs(self.meta_dir, exist_ok=True)

        self.link = MarlinLink(self._on_line, self._on_connection)
        self.job = JobRunner(self)
        self.wizard = ZOffsetWizard(self)
        self.leveling = BedLevelWizard(self)
        self.fw = None   # created in start(): needs data_dir and settings only
        self.recovery = RecoveryStore(self.data_dir)
        if self.recovery.data and not self.recovery.data.get("checkpoint"):
            self.recovery.clear()   # power went before the first layer: nothing to resume

        self.wifi = make_wifi()
        self.wifi_state = {"available": self.wifi.available}
        self.usb = make_usb(self)
        self.board = SkriwareBoard(settings["i2c_bus"], settings["i2c_addr"])
        self.leds = LedPolicy(self.board, settings)
        self.power_button = None

        self.temps = {"t0": {"actual": None, "target": 0}, "t1": {"actual": None, "target": 0},
                      "bed": {"actual": None, "target": 0}}
        self.tool = 0
        self.homed = False
        self.offsets = None
        self.firmware = None
        self.halted = None
        self.stopped = False
        self.connection_error = None
        self.manual_disconnect = False
        self.alerts = []
        self._alert_id = 0
        self.console = deque(maxlen=400)
        self.op = None
        self.runout_status = None
        self.scales = {"left": None, "right": None}
        self.power_request = None
        self.speed = 100
        self.flow = 100
        self.fan = 0
        self.done_since = None
        self.led_event = None          # the LED event showing now (Settings -> LEDs marks it)
        self._led_job_state = None
        self._led_job_since = time.monotonic()
        self.scanning = set()

        self.clients = set()
        self._dirty = True
        self._last_error_line = None
        self._tasks = []
        self.loop = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.fw = FirmwareUpdater(self)
        self.software = SoftwareUpdater(self)
        self.power_button = PowerButton(
            self.settings["power_button_gpio"],
            lambda: self.loop.call_soon_threadsafe(self.on_power_request),
            lambda: self.loop.call_soon_threadsafe(self.on_board_off))
        self._tasks = [
            asyncio.ensure_future(self._connection_task()),
            asyncio.ensure_future(self._broadcast_task()),
            asyncio.ensure_future(self._led_task()),
            asyncio.ensure_future(self._scale_task()),
            asyncio.ensure_future(self._wifi_task()),
            asyncio.ensure_future(self.usb.task()),
            asyncio.ensure_future(self.fw.auto_task()),
            asyncio.ensure_future(self.software.auto_task()),
        ]
        for name in self.list_file_names():
            if not os.path.exists(self._meta_path(name)):
                self.schedule_scan(name)

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await self.link.disconnect("shutting down")

    async def _connection_task(self):
        while True:
            try:
                if not self.link.connected and self.settings["auto_connect"] and not self.manual_disconnect:
                    await self.connect()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                msg = str(exc)
                if msg != self.connection_error:
                    log.info("Connect failed: %s", msg)
                self.connection_error = msg
                self.notify("connection")
            await asyncio.sleep(5)

    async def connect(self):
        self.manual_disconnect = False
        self.halted = None
        self.stopped = False
        await self.link.connect(self.settings["serial_port"], self.settings["baudrate"])
        self.connection_error = None
        self.tool = 0
        self.homed = False
        await self.link.command("M155 S2")
        resp = await self.link.command("M115")
        for line in resp:
            if "FIRMWARE_NAME" in line:
                self.firmware = line
        try:
            await self.read_offsets()
        except Exception as exc:
            log.warning("Could not read offsets: %s", exc)
        await self.apply_runout_settings()
        self.notify("connection")

    async def disconnect(self):
        self.manual_disconnect = True
        await self.link.disconnect("disconnected by user")

    async def reconnect(self):
        """Close and reopen the port. On USB this resets the board (needed after a halt)."""
        await self.link.disconnect("reconnecting")
        await asyncio.sleep(1.0)
        await self.connect()

    def _on_connection(self, connected, reason):
        if not connected:
            self.job.fail(f"Printer disconnected ({reason})")
            for t in self.temps.values():
                t["actual"] = None
            self.connection_error = reason if reason not in ("reconnecting", "disconnected by user", "shutting down") else None
        self.notify("connection")

    # ------------------------------------------------------------------ incoming lines
    def _on_line(self, line):
        is_temp = False
        if "T:" in line or "B:" in line:
            found = TEMP_RE.findall(line)
            if found:
                is_temp = True
                for name, actual, target in found:
                    key = {"T": None, "T0": "t0", "T1": "t1", "B": "bed"}.get(name)
                    if name == "T" and not any(n == "T0" for n, _, _ in found):
                        key = "t0"
                    if key:
                        self.temps[key] = {"actual": float(actual), "target": float(target)}
                self.notify("temps")
        stripped = line.strip()
        if not is_temp and not stripped.startswith("LINE ACCEPTED") and stripped not in ("ok",) \
                and not stripped.startswith("echo:busy") and not stripped.startswith("busy:"):
            self._log("<", line)

        if stripped.startswith("FILAMENT_RUNOUT_E"):
            side = "left" if stripped.endswith("0") else "right"
            if self.job.state in ("printing", "resuming"):
                self.job.pause(f"Filament ran out on the {side} extruder", runout_side=side)
            else:
                self.alert("warning", f"The {side} filament sensor reports no filament.")
        elif stripped.startswith("Error:") or stripped.startswith("ERROR:"):
            self._on_error(stripped)
        elif "Heating Error" in stripped:
            self._last_error_line = stripped
        elif "EXTRUDER BOARD CONNECTION FAIL" in stripped:
            self.alert("warning", "The extruder lift board did not answer (check its cable).", key="extboard")
        elif "Corrupted matrix point" in stripped:
            self.alert("warning", "The bed mesh has a bad point. Run bed leveling (G29) again.", key="mesh")

    def _on_error(self, line):
        text = line.split(":", 1)[1].strip() if ":" in line else line
        low = text.lower()
        if "calibration data corrupted" in low:
            self.alert("warning", "The saved bed mesh is corrupted. Run bed leveling (G29) again.", key="mesh")
            return
        if "printer halted" in low or "kill() called" in low:
            reason = self._last_error_line or "The firmware stopped the printer"
            self.halted = reason
            self.link_fail(f"Printer halted: {reason}")
            self.alert("error", f"Printer halted: {reason}. Fix the cause, then press Reconnect to reset the board.",
                       key="halted")
            return
        if "stopped due to errors" in low or "m999" in low:
            self.stopped = True
            self.link_fail("Printer stopped due to errors")
            self.alert("error", "The printer stopped because of an error. Press Restart (M999) when it is fixed.",
                       key="stopped")
            return
        if "checksum" in low or "line number" in low or "no checksum" in low or "last line" in low:
            return  # handled by the resend logic
        self._last_error_line = text
        if any(w in low for w in ("thermal runaway", "mintemp", "maxtemp", "heating failed", "heating error")):
            self.alert("error", f"Temperature error: {text}", key="thermal")

    def link_fail(self, reason):
        self.job.fail(reason)
        self.link._fail_all(LinkError(reason))

    def _log(self, direction, text):
        entry = {"t": time.time(), "d": direction, "text": text}
        self.console.append(entry)
        for ws in list(self.clients):
            asyncio.ensure_future(self._send_ws(ws, {"type": "log", "entries": [entry]}))

    # ------------------------------------------------------------------ alerts and state
    def alert(self, level, text, key=None):
        for a in self.alerts:
            if key and a.get("key") == key:
                a.update(level=level, text=text, time=time.time())
                self.notify("alerts")
                return
        self._alert_id += 1
        self.alerts.append({"id": self._alert_id, "level": level, "text": text, "time": time.time(), "key": key})
        self.alerts = self.alerts[-20:]
        log.log(logging.ERROR if level == "error" else logging.WARNING, text)
        self.notify("alerts")

    def dismiss_alert(self, alert_id):
        self.alerts = [a for a in self.alerts if a["id"] != alert_id]
        self.notify("alerts")

    def notify(self, _kind=None):
        self._dirty = True

    def snapshot(self):
        s = self.settings
        return {
            "type": "state",
            "connection": {
                "connected": self.link.connected,
                "ready": self.link.ready,
                "port": self.link.port,
                "error": self.connection_error,
                "halted": self.halted,
                "stopped": self.stopped,
                "firmware": self.firmware,
                "stats": self.link.stats,
            },
            "temps": self.temps,
            "tool": self.tool,
            "homed": self.homed,
            "offsets": self.offsets,
            "fan": self.fan,
            "speed": self.speed,
            "flow": self.flow,
            "job": self.job.snapshot(),
            "zoffset": self.wizard.snapshot(),
            "leveling": self.leveling.snapshot(),
            "firmware_update": self.fw.snapshot() if self.fw else None,
            "software": self.software.snapshot() if self.fw else None,
            "recovery": self.recovery.pending() if not self.job.active else None,
            "alerts": self.alerts,
            "op": self.op,
            "leds": {"event": self.led_event, "board": self.board.available},
            "runout": {"enabled": s["runout"], "status": self.runout_status},
            "scales": self.scales,
            "wifi": self.wifi_state,
            "usb": self.usb.snapshot(),
            "power_request": self.power_request,
            "hardware": {"i2c": self.board.available, "power_button": self.settings["power_button_gpio"] is not None},
            "config": {
                "presets": s["presets"], "bed_x": s["bed_x"], "bed_y": s["bed_y"], "z_max": s["z_max"],
                "lift_mechanism": s["lift_mechanism"], "filament": s["filament"],
                "touch_dead_zone": s["touch_dead_zone"], "boot_intro": s["boot_intro"], "text_size": s["text_size"],
            },
        }

    async def _send_ws(self, ws, data):
        try:
            await ws.send_str(json.dumps(data))
        except Exception:
            self.clients.discard(ws)

    async def _broadcast_task(self):
        last_full = 0
        while True:
            try:
                await asyncio.sleep(0.25)
                now = time.monotonic()
                if (self._dirty or now - last_full > 2) and self.clients:
                    self._dirty = False
                    last_full = now
                    data = json.dumps(self.snapshot())
                    for ws in list(self.clients):
                        try:
                            await ws.send_str(data)
                        except Exception:
                            self.clients.discard(ws)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Broadcast failed")

    # ------------------------------------------------------------------ LEDs and scales
    def led_status(self):
        """Which of the LED events (hardware.LED_EVENTS) the printer is in right now."""
        js = self.job.state
        if js != self._led_job_state:            # remember when the job last changed state
            self._led_job_state = js
            self._led_job_since = time.monotonic()
        if self.halted or self.stopped:
            return "error"
        if self.power_request:
            return "power"
        if js in ("paused", "pausing"):
            return "paused"
        heating = False
        for key in ("t0", "t1", "bed"):
            t = self.temps[key]
            if t["target"] and t["actual"] is not None and t["actual"] < t["target"] - 3:
                heating = True
        if js in ("printing", "resuming", "starting", "cancelling"):
            return "heating" if heating else "printing"
        if js == "error":
            return "error"
        if js in ("finished", "cancelled"):
            minutes = float(self.settings["leds"].get("done_minutes", 30))
            if time.monotonic() - self._led_job_since < minutes * 60:
                return "done" if js == "finished" else "cancelled"
        if self.op:
            return "filament"
        if self.wizard.active or self.leveling.active:
            return "calibrating"
        return "heating" if heating else "idle"

    async def _led_task(self):
        while True:
            try:
                await asyncio.sleep(1.0)
                status = self.led_status()
                if status != self.led_event:
                    self.led_event = status
                    self.notify("leds")
                if self.board.available and self.link.ready:
                    await self.loop.run_in_executor(None, self.leds.apply, status, self.job.progress())
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("LED update failed")

    async def _wifi_task(self):
        """Refresh the Wi-Fi state for the sidebar icon every 10 seconds."""
        while True:
            try:
                await self.refresh_wifi()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Wi-Fi status failed")
                await asyncio.sleep(10)

    async def refresh_wifi(self, status=None):
        if not self.wifi.available:
            return self.wifi_state
        st = status or await self.wifi.status()
        slim = {k: st.get(k) for k in ("available", "radio", "ssid", "signal", "ip", "country")}
        if slim != self.wifi_state:
            self.wifi_state = slim
            self.notify("wifi")
        return st

    async def _scale_task(self):
        while True:
            try:
                cfg = self.settings["scales"]
                await asyncio.sleep(max(2, cfg["interval_s"]))
                if not (cfg["enabled"] and self.board.available and self.link.ready):
                    continue
                for side in ("left", "right"):
                    raw = await self.loop.run_in_executor(None, self.board.read_scale, side)
                    self.scales[side] = self._scale_reading(side, raw)
                self.notify("scales")
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Scale read failed")

    def _scale_reading(self, side, raw):
        cal = self.settings["scales"][side]
        grams = None
        if raw is not None and cal["tare"] is not None and cal["counts_per_gram"]:
            grams = round((raw - cal["tare"]) / cal["counts_per_gram"] - (cal["spool_g"] or 0), 0)
        return {"raw": raw, "grams": grams, "calibrated": bool(cal["counts_per_gram"])}

    async def scale_tare(self, side):
        raw = await self.loop.run_in_executor(None, self.board.read_scale, side)
        if raw is None:
            raise RuntimeError("No reading from the scale")
        self.settings.update({"scales": {side: {"tare": raw}}})
        return raw

    async def scale_calibrate(self, side, grams):
        cal = self.settings["scales"][side]
        if cal["tare"] is None:
            raise RuntimeError("Tare the empty holder first")
        raw = await self.loop.run_in_executor(None, self.board.read_scale, side)
        if raw is None:
            raise RuntimeError("No reading from the scale")
        if grams <= 0 or raw == cal["tare"]:
            raise RuntimeError("Put a known weight on the holder first")
        self.settings.update({"scales": {side: {"counts_per_gram": (raw - cal["tare"]) / float(grams)}}})
        return raw

    # ------------------------------------------------------------------ printer commands
    def require_ready(self, allow_paused=False):
        if self.fw and self.fw.busy:
            raise RuntimeError("A firmware update is running")
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        if self.halted:
            raise RuntimeError("Printer halted. Press Reconnect to reset it.")
        if self.job.active and not (allow_paused and self.job.state == "paused"):
            raise RuntimeError("Not available while printing")
        # These own the head while open; jogging underneath them would spoil their measurements.
        if self.wizard.active:
            raise RuntimeError("Finish the Z offset wizard first")
        if self.leveling.active:
            raise RuntimeError("Finish bed leveling first")

    async def run(self, cmds, log_it=True):
        out = []
        for c in cmds:
            if log_it:
                self._log(">", c)
            out += await self.link.command(c, priority=True)
            code = c.split()[0].upper() if c.strip() else ""
            if code == "G28":
                self.homed = True
                self.tool = 0
            elif code[:1] == "T" and code[1:].isdigit():
                self.tool = int(code[1:])
        return out

    async def send_console(self, text):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        cmds = [c.strip() for c in text.replace("\r", "").split("\n") if c.strip()]
        upper = [c.split()[0].upper() for c in cmds]
        for c, code in zip(cmds, upper):
            if code in ("M112", "M108", "M410"):
                self.link.emergency(c)
                self._log(">", c)
                if code == "M112":
                    self.halted = "Emergency stop"
                    self.link_fail("Emergency stop")
                return []
        if self.job.active:
            moving = {"G0", "G1", "G2", "G3", "G28", "G29", "G92", "T0", "T1", "M80"}
            if any(code in moving for code in upper) and self.job.state != "paused":
                raise RuntimeError("Movement commands are blocked while printing")
        return await self.run(cmds)

    async def set_temp(self, heater, target):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        target = max(0, min(285 if heater != "bed" else 120, float(target)))
        cmd = {"t0": f"M104 T0 S{target:g}", "t1": f"M104 T1 S{target:g}", "bed": f"M140 S{target:g}"}[heater]
        await self.run([cmd])

    async def move(self, x=0, y=0, z=0, feed=None):
        self.require_ready(allow_paused=True)
        parts = []
        for axis, v in (("X", x), ("Y", y), ("Z", z)):
            if v:
                parts.append(f"{axis}{float(v):g}")
        if not parts:
            return
        feed = feed or (600 if z and not (x or y) else 6000)
        await self.run(["G91", f"G1 {' '.join(parts)} F{feed:g}", "G90"])

    async def home(self, axes=""):
        self.require_ready()
        axes = "".join(a for a in axes.upper() if a in "XYZ")
        await self.run([f"G28 {' '.join(axes)}".strip()])

    async def extrude(self, tool, amount, speed=3):
        self.require_ready(allow_paused=True)
        key = f"t{tool}"
        actual = self.temps[key]["actual"] or 0
        if actual < self.settings["filament"]["min_temp"] - 10:
            raise RuntimeError("Heat the nozzle first")
        cmds = []
        if tool != self.tool and not self.job.active:
            cmds.append(f"T{tool}")
        cmds += ["M83", f"G1 E{float(amount):g} F{float(speed) * 60:g}"]
        if not self.job.active:
            cmds.append("M82")
        await self.run(cmds)

    async def filament(self, action, tool, temp=None):
        """Load or unload filament: heat, then push or pull. Runs as a background op."""
        self.require_ready(allow_paused=True)
        if self.op:
            raise RuntimeError("Another filament operation is running")
        f = self.settings["filament"]
        key = f"t{tool}"
        target = max(float(temp or 0), self.temps[key]["target"] or 0, f["min_temp"])

        async def _work():
            try:
                self.op = {"name": action, "tool": tool, "message": f"Heating to {target:g} °C"}
                self.notify("op")
                cmds = []
                if tool != self.tool and not self.job.active:
                    cmds.append(f"T{tool}")
                cmds += [f"M104 T{tool} S{target:g}", f"M109 T{tool} S{target:g}", "M83"]
                await self.run(cmds)
                if action == "load":
                    self.op["message"] = "Loading filament"
                    self.notify("op")
                    await self.run([f"G1 E{f['load_length']:g} F{f['load_speed'] * 60:g}",
                                    f"G1 E{f['purge_length']:g} F120"])
                else:
                    self.op["message"] = "Unloading filament"
                    self.notify("op")
                    await self.run(["G1 E5 F300", f"G1 E-{f['unload_length']:g} F{f['unload_speed'] * 60:g}"])
                if not self.job.active:
                    await self.run(["M82"])
            except Exception as exc:
                self.alert("warning", f"Filament {action} failed: {exc}")
            finally:
                self.op = None
                self.notify("op")

        asyncio.ensure_future(_work())

    async def select_tool(self, tool):
        self.require_ready()
        await self.run([f"T{int(tool)}"])

    async def lift(self, direction):
        self.require_ready()
        await self.run(["M80 U" if direction == "up" else "M80 D"])

    async def set_fan(self, value):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        value = int(max(0, min(255, value)))
        await self.run([f"M106 S{value}" if value else "M107"])
        self.fan = value

    async def set_speed(self, percent):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        self.speed = int(max(10, min(300, percent)))
        await self.run([f"M220 S{self.speed}"])

    async def set_flow(self, percent):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        self.flow = int(max(50, min(200, percent)))
        await self.run([f"M221 S{self.flow} T{self.tool}"])

    async def motors_off(self):
        self.require_ready()
        await self.run(["M84"])
        self.homed = False

    async def apply_runout_settings(self):
        r = self.settings["runout"]
        cmds = []
        for side, letter in (("left", "L"), ("right", "R")):
            cmds.append(f"M61 {letter}" if r[side] else f"M62 {letter}")
        await self.run(cmds, log_it=False)

    async def set_runout(self, side, enabled):
        if side not in ("left", "right"):
            raise ValueError("side must be left or right")
        self.settings.update({"runout": {side: bool(enabled)}})
        if self.link.ready:
            await self.run([f"M6{1 if enabled else 2} {'L' if side == 'left' else 'R'}"])

    async def query_runout(self):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        resp = await self.run(["M64"], log_it=False)
        status = None
        for line in resp:
            m = re.search(r"L:\s*(\d)\s*R:\s*(\d)", line)
            if m:
                status = {"left": m.group(1) == "1", "right": m.group(2) == "1"}
            elif "SENSORS OFF" in line.upper():
                status = {"off": True}
        self.runout_status = status
        self.notify("runout")
        return status

    async def read_offsets(self):
        """Home offset (M206 Z, left nozzle), right nozzle offset (M206 I) and probe offset (M851)."""
        resp = await self.run(["M503"], log_it=False)
        ho = zprobe = None
        for line in resp:
            m = re.search(r"M206\s+X(-?[\d.]+)\s+Y(-?[\d.]+)\s+Z(-?[\d.]+)", line)
            if m:
                ho = float(m.group(3))
            m = re.search(r"M851\s+Z(-?[\d.]+)", line)
            if m:
                zprobe = float(m.group(1))
        e1 = None
        if self.settings["lift_mechanism"]:
            for line in await self.run(["M206 I"], log_it=False):
                m = re.match(r"^E:\s*(-?[\d.]+)\s*$", line.strip())
                if m:
                    e1 = float(m.group(1))
        if ho is None:
            raise RuntimeError("The firmware did not report its home offset (M503)")
        e0 = ho if self.tool == 0 else (self.offsets or {}).get("e0", ho)
        self.offsets = {"ho": ho, "e0": e0, "e1": e1 if e1 is not None else e0, "zprobe": zprobe or 0.0}
        self.notify("offsets")
        return self.offsets

    async def emergency_stop(self):
        if self.link.connected:
            self.link.emergency("M112")
        self.halted = "Emergency stop"
        self.link_fail("Emergency stop")
        self.alert("error", "Emergency stop. Press Reconnect to reset the board.", key="halted")

    async def restart_m999(self):
        if not self.link.ready:
            raise RuntimeError("Printer not connected")
        await self.link.command("M999", priority=True, numbered=False)
        self.stopped = False
        self.alerts = [a for a in self.alerts if a.get("key") != "stopped"]
        self.notify("connection")

    # ------------------------------------------------------------------ jobs and recovery
    async def start_print(self, name):
        self.require_ready()
        if self.recovery.pending():
            raise RuntimeError("Resume or discard the interrupted print first")
        name = safe_filename(name)
        path = os.path.join(self.gcode_dir, name)
        if not os.path.exists(path):
            raise RuntimeError("File not found")
        self.done_since = None
        self.alerts = [a for a in self.alerts if a["level"] != "error"]
        meta = self.read_meta(name)
        if meta is None:   # just copied or uploaded: the full scan is still running
            meta = await self.loop.run_in_executor(None, quick_scan, path)
        await self.job.start(path, name, meta)

    async def resume_recovery(self):
        self.require_ready()
        data = self.recovery.data
        if not data or not self.recovery.pending():
            raise RuntimeError("Nothing to resume")
        plan = await self.loop.run_in_executor(
            None, build_plan, data, self.settings["recovery"]["backoff_moves"])
        if self.offsets is None:
            await self.read_offsets()
        self.done_since = None
        await self.job.start(data["path"], data["file"], self.read_meta(data["file"]) or {}, plan=plan)

    def discard_recovery(self):
        self.recovery.clear()
        self.notify("recovery")

    def on_job_finished(self):
        self.done_since = time.monotonic()

    # ------------------------------------------------------------------ files
    def list_file_names(self):
        try:
            return sorted(n for n in os.listdir(self.gcode_dir)
                          if n.lower().endswith(GCODE_EXT) and not n.startswith("."))
        except FileNotFoundError:
            return []

    def _meta_path(self, name):
        return os.path.join(self.meta_dir, name + ".json")

    def read_meta(self, name):
        try:
            with open(self._meta_path(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def list_files(self):
        out = []
        for name in self.list_file_names():
            path = os.path.join(self.gcode_dir, name)
            st = os.stat(path)
            meta = self.read_meta(name) or {}
            out.append({
                "name": name, "size": st.st_size, "date": st.st_mtime,
                "est_time_s": meta.get("est_time_s"), "filament_g": meta.get("filament_g"),
                "filament_mm": meta.get("filament_mm"), "layers": meta.get("layers"),
                "max_z": meta.get("max_z"), "tools": meta.get("tools"),
                "has_thumbnail": bool(meta.get("thumbnail")), "scanning": name in self.scanning,
            })
        out.sort(key=lambda f: f["date"], reverse=True)
        return out

    def schedule_scan(self, name):
        if name in self.scanning:
            return
        self.scanning.add(name)

        async def _scan():
            try:
                meta = await self.loop.run_in_executor(None, scan_file, os.path.join(self.gcode_dir, name))
                from .config import atomic_write_json
                atomic_write_json(self._meta_path(name), meta)
            except Exception as exc:
                log.warning("Could not scan %s: %s", name, exc)
            finally:
                self.scanning.discard(name)
                self.notify("files")

        asyncio.ensure_future(_scan())

    def delete_file(self, name):
        name = safe_filename(name)
        if self.job.active and self.job.file == name:
            raise RuntimeError("That file is printing")
        if self.recovery.data and self.recovery.data.get("file") == name:
            raise RuntimeError("That file belongs to an interrupted print. Discard it first.")
        os.unlink(os.path.join(self.gcode_dir, name))
        try:
            os.unlink(self._meta_path(name))
        except FileNotFoundError:
            pass
        self.notify("files")

    # ------------------------------------------------------------------ power
    def on_power_request(self):
        self.power_request = {"since": time.time(), "printing": self.job.active}
        self.notify("power")

    def on_board_off(self):
        log.info("ATmega reports the printer board is off")

    async def power_off(self, force=False):
        if self.job.active and not force:
            raise RuntimeError("A print is running")
        self.power_request = {"since": time.time(), "shutting_down": True}
        self.notify("power")
        await asyncio.sleep(0.5)
        if self.board.available:
            await self.loop.run_in_executor(None, self.board.power_off, tuple(self.settings["power_off_color"]))
        cmd = self.settings["shutdown_command"]
        if cmd and os.name != "nt":
            subprocess.Popen(cmd)
        else:
            log.warning("Shutdown requested (command not run on this system)")

    def cancel_power(self):
        self.power_request = None
        if self.board.available:
            self.board.cancel_power_off()
        self.notify("power")
