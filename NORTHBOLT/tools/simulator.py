"""A fake Skriware 2 printer that speaks the same serial dialect as this firmware.

It reproduces the parts the host depends on:
  * line numbers + checksums, "LINE ACCEPTED:<n>", "BAD COMMAND:", "Resend:" then "ok"
  * a 10-command queue and a 16-move planner, so "ok" runs ahead of the motion
  * M109/M190 waits with temperature lines and "M109 HEATING FINISHED"
  * busy keepalives, auto temperature reports (M155)
  * emergency parser M108/M112/M410/M411
  * the Z offset logic: home_offset, M206 Z/E/I, workspace offset updated only by M206 and G28,
    and the extruder swap code from Skriware_Functions.cpp, with a "true" head height so tests
    can check where the nozzle physically is.
  * runout messages, injected from the admin port.

Run:   python tools/simulator.py --port 5555 --admin 5556 --speed 20
Host:  python -m northbolt --serial socket://127.0.0.1:5555
Admin: send one line, e.g. "runout 0", "state", "powerloss", "history 50"
"""

import argparse
import asyncio
import json
import math
import os
import random
import re
import time

BUFSIZE = 10
PLANNER = 16


def firmware_name_from_hex(path):
    """The FIRMWARE_NAME inside an Intel HEX file: lets tests 'flash' the simulated board."""
    if not path or not os.path.exists(path):
        return None
    image, base = {}, 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith(":"):
                continue
            raw = bytes.fromhex(line[1:])
            count, addr, rtype, data = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
            if rtype == 0:
                for i, b in enumerate(data):
                    image[base + addr + i] = b
            elif rtype == 2:
                base = ((data[0] << 8) | data[1]) << 4
            elif rtype == 4:
                base = ((data[0] << 8) | data[1]) << 16
            elif rtype == 1:
                break
    if not image:
        return None
    blob = bytes(image.get(i, 0xFF) for i in range(max(image) + 1))
    m = re.search(rb"FIRMWARE_NAME:([^\x00]*?) SOURCE_CODE_URL", blob)
    return m.group(1).decode() if m else None


def checksum(text):
    cs = 0
    for ch in text.encode():
        cs ^= ch
    return cs


def words(cmd):
    out = {}
    for k, v in re.findall(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))", cmd[cmd.find(" "):] if " " in cmd else ""):
        out[k.upper()] = float(v)
    for k in re.findall(r"(?:^|\s)([A-Za-z])(?=\s|$)", cmd[cmd.find(" "):] if " " in cmd else ""):
        out.setdefault(k.upper(), None)
    return out


class Firmware:
    def __init__(self, speed, error_rate, eeprom=None, head_z=None):
        self.speed = speed
        self.error_rate = error_rate
        self.writer = None
        self.dead = False
        # EEPROM-persisted values
        self.eeprom = eeprom or {"ho": 0.4, "e0": 0.4, "e1": 0.9, "zprobe": 0.0}
        self.ho = self.eeprom["ho"]          # home_offset[Z]
        self.e0 = self.eeprom["e0"]
        self.e1 = self.eeprom["e1"]
        self.zprobe = self.eeprom["zprobe"]
        self.ws = self.ho                    # workspace_offset[Z], set at boot by settings postprocess
        # physical model
        self.gap = 1.30      # T0 nozzle height above the bed when the probe triggers
        self.drop = 0.35     # how much lower the lowered T1 nozzle sits than T0
        self.head = head_z if head_z is not None else 30.0   # true head height, 0 = probe trigger
        self.lowered = False
        # firmware state (native coordinates)
        self.pos = {"X": 0.0, "Y": 0.0, "Z": self.head - self.zprobe, "E": 0.0}
        self.homed = False
        self.rel = False
        self.rel_e = False
        self.tool = 0
        self.leveling = False
        self.soft = True
        self.temps = {"t0": [22.0, 0.0], "t1": [22.0, 0.0], "bed": [22.0, 0.0]}
        self.auto_report = 0
        self.last_n = 0
        self.queue = asyncio.Queue()
        self.planner_end = time.monotonic()
        self.planner_count = 0
        self.wait_for_heatup = False
        self.sensors = {"L": True, "R": True}
        self.runout = {"L": False, "R": False}
        self.history = []
        # Bed: heights (mm) at the three screws (front, back left, back right) plus a slight bowl.
        self.bed_screws = [(100.0, 0.0, 0.0), (15.0, 265.0, 0.30), (185.0, 265.0, -0.20)]
        self.bed_warp = 0.04
        self.mesh = None
        self.moves = 0
        self.extruded = 0.0
        self.busy = False

    # ------------------------------------------------------------------ output
    def out(self, text):
        if self.writer and not self.dead:
            self.writer.write((text + "\n").encode())

    def temp_line(self):
        t0, t1, b = self.temps["t0"], self.temps["t1"], self.temps["bed"]
        cur = t1 if self.tool == 1 else t0
        return (f"T:{cur[0]:.2f} /{cur[1]:.2f} B:{b[0]:.2f} /{b[1]:.2f} "
                f"T0:{t0[0]:.2f} /{t0[1]:.2f} T1:{t1[0]:.2f} /{t1[1]:.2f} @:0 B@:0")

    def logical_z(self):
        return self.pos["Z"] + self.ws

    def nozzle_height(self):
        h = self.head + self.gap
        if self.tool == 1 and self.lowered:
            h -= self.drop
        return round(h, 4)

    # ------------------------------------------------------------------ input
    def receive(self, line):
        if self.dead:
            return
        line = line.strip()
        if not line:
            return
        bare = re.sub(r"^N\d+\s+", "", line).split("*")[0].strip().upper()
        # emergency parser (runs in the RX interrupt in the real firmware)
        if bare == "M112":
            self.out("Error:Printer halted. kill() called!")
            self.dead = True
            return
        if bare == "M108":
            self.wait_for_heatup = False
        if bare in ("M410", "M411"):
            self.planner_end = time.monotonic()
            self.planner_count = 0
            if bare == "M411":
                while not self.queue.empty():
                    self.queue.get_nowait()
                return
        if line.startswith("N"):
            m = re.match(r"N(\d+)\s+(.*)", line)
            n = int(m.group(1)) if m else -1
            is_m110 = "M110" in line
            if not is_m110 and n != self.last_n + 1:
                self.out(f"BAD COMMAND:{line}")
                self.out(f"Error:Line Number is not Last Line Number+1, Last Line: {self.last_n}")
                self.out(f"Resend: {self.last_n + 1}")
                self.out("ok")
                return
            if "*" not in line:
                self.out(f"BAD COMMAND:{line}")
                self.out(f"Error:No Checksum with line number, Last Line: {self.last_n}")
                self.out(f"Resend: {self.last_n + 1}")
                self.out("ok")
                return
            body, cs = line.rsplit("*", 1)
            corrupt = self.error_rate and random.random() < self.error_rate
            if corrupt or checksum(body) != int(cs):
                self.out(f"BAD COMMAND:{line}")
                self.out(f"Error:checksum mismatch, Last Line: {self.last_n}")
                self.out(f"Resend: {self.last_n + 1}")
                self.out("ok")
                return
            self.out(f"LINE ACCEPTED:{n}")
            self.last_n = n
            cmd = body.split(None, 1)[1] if " " in body else ""
            if is_m110:
                p = words(cmd)
                self.last_n = int(p.get("N") or 0)
        else:
            cmd = line
        if self.queue.qsize() >= BUFSIZE:
            return  # the real firmware stops reading serial; the host never fills it
        self.queue.put_nowait(cmd)

    # ------------------------------------------------------------------ processing
    async def wait_planner(self):
        self.busy = True
        try:
            while time.monotonic() < self.planner_end:
                await asyncio.sleep(0.02)
            self.planner_count = 0
        finally:
            self.busy = False

    async def plan_move(self, target, feed):
        dist = math.sqrt(sum((target[a] - self.pos[a]) ** 2 for a in "XYZ"))
        if dist == 0:
            dist = abs(target["E"] - self.pos["E"])
        duration = dist / max(feed / 60.0, 0.1) / self.speed
        # wait for room in the planner
        while self.planner_count >= PLANNER and time.monotonic() < self.planner_end:
            self.busy = True
            await asyncio.sleep(0.01)
        self.busy = False
        now = time.monotonic()
        if now >= self.planner_end:
            self.planner_count = 0
            self.planner_end = now
        self.planner_end += duration
        self.planner_count += 1
        self.head += target["Z"] - self.pos["Z"]
        de = target["E"] - self.pos["E"]
        if de > 0:
            self.extruded += de
        self.pos = target
        self.moves += 1

    async def execute(self, cmd):
        m = re.match(r"\s*([GMT])(\d+)", cmd, re.I)
        if not m:
            return
        code = m.group(1).upper() + m.group(2)
        p = words(cmd)
        if code in ("G0", "G1", "G2", "G3"):
            target = dict(self.pos)
            for a in "XYZ":
                if p.get(a) is not None:
                    v = p[a]
                    native = (self.pos[a] + v) if self.rel else (v - (self.ws if a == "Z" else 0.0))
                    if a == "Z" and self.soft:
                        native = max(native, -3.0)
                    target[a] = native
            if p.get("E") is not None:
                target["E"] = self.pos["E"] + p["E"] if (self.rel or self.rel_e) else p["E"]
            self.feed = p.get("F") or getattr(self, "feed", 3000)
            await self.plan_move(target, self.feed)
            if target["E"] > self.pos["E"] - 1e-9:
                for side, letter in (("0", "L"), ("1", "R")):
                    if self.runout[letter] == "pending" and self.sensors[letter]:
                        self.runout[letter] = True
                        self.out(f"FILAMENT_RUNOUT_E{side}")
        elif code == "G4":
            await self.busy_sleep((p.get("S") or 0) + (p.get("P") or 0) / 1000.0)
        elif code == "G28":
            await self.wait_planner()
            await self.busy_sleep(1.0)
            self.lowered = False           # Extruder_Up()
            if self.tool == 1:             # fixed firmware: undo T1's Z offset like extruder_swap() does
                self.pos["Z"] += self.e0 - self.ho
                self.ho = self.e0
            self.tool = 0                  # tool_change(0, 0, true)
            self.leveling = False
            axes = [a for a in "XYZ" if a in p] or ["X", "Y", "Z"]
            if "X" in axes:
                self.pos["X"] = 0.0
            if "Y" in axes:
                self.pos["Y"] = 0.0
            if "Z" in axes:
                self.head = 0.0
                self.pos["Z"] = 0.0 - self.zprobe
                self.ws = self.ho          # update_software_endstops(Z)
            self.homed = True
        elif code == "G90":
            self.rel = False
        elif code == "G91":
            self.rel = True
        elif code == "M82":
            self.rel_e = False
        elif code == "M83":
            self.rel_e = True
        elif code == "G92":
            for a in "XYZE":
                if p.get(a) is not None:
                    self.pos[a] = p[a] - (self.ws if a == "Z" else 0.0)
        elif code == "M400":
            await self.wait_planner()
        elif code == "M114":
            await self.wait_planner()
            self.report_position()
        elif code == "M105":
            self.out("ok " + self.temp_line())
            self.history[-1] += "  (answered inline)"
            return await self._skip_ok()
        elif code == "M155":
            self.auto_report = int(p.get("S") or 0)
        elif code == "M115":
            name = firmware_name_from_hex(os.environ.get("SIM_FLASH_HEX")) or "Marlin 1.1.9.9 (Github)"
            self.out(f"FIRMWARE_NAME:{name} SOURCE_CODE_URL:https://github.com/MarlinFirmware/Marlin "
                     "PROTOCOL_VERSION:1.0 MACHINE_TYPE:3D Printer EXTRUDER_COUNT:2 UUID:sim")
            self.out("Cap:AUTOREPORT_TEMP:1")
            self.out("Cap:EMERGENCY_PARSER:1")
        elif code in ("M104", "M109"):
            t = int(p["T"]) if p.get("T") is not None else self.tool
            key = "t1" if t == 1 else "t0"
            if p.get("S") is not None:
                self.temps[key][1] = p["S"]
            if code == "M109":
                await self.heat_wait(key)
                self.out(f"M109 HEATING FINISHED T{t} S{int(self.temps[key][1])}")
        elif code in ("M140", "M190"):
            if p.get("S") is not None:
                self.temps["bed"][1] = p["S"]
            if code == "M190":
                await self.heat_wait("bed")
                self.out(f"M190 HEATING FINISHED S{int(self.temps['bed'][1])}")
        elif code == "M206":
            if "I" in p:
                self.out(f"E:{self.e1:.2f}")
            if p.get("E") is not None:
                self.e1 = p["E"]
            if p.get("Z") is not None:
                self.e0 = p["Z"]
                self.ho = p["Z"]
                self.ws = self.ho          # set_home_offset -> update_software_endstops
            self.report_position()
        elif code == "M503":
            self.out("echo:; Home offset:")
            self.out(f"echo:  M206 X0.00 Y0.00 Z{self.ho:.2f}")
            self.out("echo:; Z-Probe Offset (mm):")
            self.out(f"echo:  M851 Z{self.zprobe:.2f}")
            self.out("echo:E0 INVERT options:00")
        elif code == "M851":
            if p.get("Z") is not None:
                self.zprobe = p["Z"]
        elif code == "M500":
            self.eeprom.update({"ho": self.ho, "e0": self.e0, "e1": self.e1, "zprobe": self.zprobe})
            self.out("echo:Settings Stored (612 bytes; crc 1234)")
        elif code == "M211":
            if p.get("S") is not None:
                self.soft = bool(p["S"])
            self.out(f"echo:Soft endstops: {'On' if self.soft else 'Off'}")
        elif code == "G29":
            await self.wait_planner()
            await self.probe_mesh()
            if self.mesh:
                self.leveling = True
        elif code == "M420":
            if p.get("S") is not None:
                self.leveling = bool(p["S"]) and self.mesh is not None
            if "V" in p and self.mesh:
                self.print_mesh()
            self.out(f"echo:Bed Leveling {'On' if self.leveling else 'Off'}")
        elif code[0] == "T":
            await self.wait_planner()
            new = int(code[1:])
            if new != self.tool:
                self.swap(new)
        elif code == "M80":
            if "U" in p:
                self.lowered = False
            if "D" in p:
                self.lowered = True
        elif code in ("M61", "M62"):
            on = code == "M61"
            for letter in ("L", "R"):
                if letter in p or "A" in p:
                    self.sensors[letter] = on
        elif code == "M63":
            for letter in ("L", "R"):
                if letter in p or "A" in p:
                    self.runout[letter] = False
        elif code == "M64":
            if self.sensors["L"] and self.sensors["R"]:
                self.out(f"L: {0 if self.runout['L'] else 1} R: {0 if self.runout['R'] else 1}")
            else:
                self.out("FILAMENT SENSORS OFF")
        elif code == "M110":
            if p.get("N") is not None:       # gcode_M110: gcode_LastN = N
                self.last_n = int(p["N"])
        elif code in ("M84", "M106", "M107", "M220", "M221", "M999", "M117", "M73", "M18"):
            pass
        else:
            self.out(f'echo:Unknown command: "{cmd}"')

    async def _skip_ok(self):
        # M105 already printed "ok T:..." on its own line; signal the worker to not print another ok
        raise _InlineOk()

    def bed_height(self, x, y):
        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = self.bed_screws
        # plane through the three screws
        ux, uy, uz = x2 - x1, y2 - y1, z2 - z1
        vx, vy, vz = x3 - x1, y3 - y1, z3 - z1
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        z = z1 - (nx * (x - x1) + ny * (y - y1)) / nz
        bowl = ((x - 100) / 100) ** 2 + ((y - 137.5) / 137.5) ** 2
        return z + self.bed_warp * (bowl - 0.5)

    def print_mesh(self):
        self.out("Bilinear Leveling Grid:")
        self.out("      0      1      2      3      4")
        for j, row in enumerate(self.mesh):
            self.out(f" {j} " + " ".join(f"{v:+.3f}" for v in row))
        self.out("Subdivided with CATMULL ROM Leveling Grid:")
        self.out("        0        1        2")
        self.out(" 0 +0.00000 +0.00000 +0.00000")

    async def probe_mesh(self):
        if not self.homed:
            self.out("echo:Home XYZ first")
            return
        await self.busy_sleep(3.0)
        ref = self.bed_height(100 + 20, 137.5 + 32)   # Z was homed with the probe at the bed centre
        xs = [20 + i * 40 for i in range(5)]
        ys = [32 + j * (255 - 32) / 4 for j in range(5)]
        self.mesh = [[round(self.bed_height(x, y) - ref, 3) for x in xs] for y in ys]
        self.print_mesh()

    def swap(self, new):
        """extruder_swap() from Skriware_Functions.cpp with extruder_type 4."""
        tmp_z = self.pos["Z"]
        if new == 1:
            self.head += 3.0
            self.lowered = True
            self.pos["Z"] = tmp_z + 3.0 + self.e1 - self.ho
            self.ho = self.e1
            self.head += tmp_z - self.pos["Z"]
            self.pos["Z"] = tmp_z
        else:
            self.lowered = False
            self.pos["Z"] = tmp_z + self.e0 - self.ho
            self.ho = self.e0
            self.head += tmp_z - self.pos["Z"]
            self.pos["Z"] = tmp_z
        self.tool = new

    def report_position(self):
        x, y, z, e = self.pos["X"], self.pos["Y"], self.logical_z(), self.pos["E"]
        self.out(f"X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} Count X:{int(x*80)} Y:{int(y*80)} Z:{int(self.pos['Z']*800)}")

    async def busy_sleep(self, seconds):
        end = time.monotonic() + seconds / self.speed
        while time.monotonic() < end:
            await asyncio.sleep(0.02)

    async def heat_wait(self, key):
        self.wait_for_heatup = True
        last = 0
        while self.wait_for_heatup and abs(self.temps[key][0] - self.temps[key][1]) > 1.0 and self.temps[key][1] > 0:
            await asyncio.sleep(0.05)
            if time.monotonic() - last > 1.0 / max(1, self.speed / 5):
                last = time.monotonic()
                self.out(self.temp_line() + " W:?")
        self.wait_for_heatup = False

    async def physics(self):
        last = time.monotonic()
        last_report = last
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            dt = (now - last) * self.speed
            last = now
            for t in self.temps.values():
                target = t[1] if t[1] > 0 else 22.0
                step = 8.0 * dt
                if abs(target - t[0]) <= step:
                    t[0] = target if t[1] > 0 else max(22.0, t[0] - step)
                else:
                    t[0] += step if target > t[0] else -step
            if self.auto_report and now - last_report >= self.auto_report and not self.dead:
                last_report = now
                self.out(self.temp_line())
            if self.busy and not self.dead and int(now * 10) % 20 == 0:
                self.out("echo:busy: processing")


class _InlineOk(Exception):
    pass


class Simulator:
    def __init__(self, args):
        self.args = args
        self.fw = None
        self.eeprom = None
        self.head = None
        self.tasks = []
        self.client = None

    def new_firmware(self):
        fw = Firmware(self.args.speed, self.args.error_rate, self.eeprom, self.head)
        self.eeprom = fw.eeprom
        return fw

    async def handle_client(self, reader, writer):
        if self.client:
            self.client.close()
        for t in self.tasks:
            t.cancel()
        self.client = writer
        old = self.fw
        self.head = old.head if old else None
        self.fw = fw = self.new_firmware()   # opening the port resets the board (DTR)
        if old:
            fw.head = old.head
            fw.pos["Z"] = old.head - fw.zprobe
        fw.writer = writer
        self.tasks = [asyncio.ensure_future(self.worker(fw)), asyncio.ensure_future(fw.physics())]
        fw.out("start")
        fw.out("echo:Marlin 1.1.9.9")
        fw.out("echo: Last Updated: 2018-08-01 | Author: (none, default config)")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                fw.receive(raw.decode(errors="replace"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if self.client is writer:
                self.client = None

    async def worker(self, fw):
        while True:
            cmd = await fw.queue.get()
            if fw.dead:
                continue
            fw.history.append(cmd)
            fw.history = fw.history[-5000:]
            try:
                await fw.execute(cmd)
            except _InlineOk:
                continue
            except Exception as exc:
                fw.out(f"echo:simulator error {exc!r}")
            if not fw.dead:
                fw.out("ok")

    async def handle_admin(self, reader, writer):
        line = (await reader.readline()).decode().strip()
        parts = line.split()
        fw = self.fw
        reply = {"ok": True}
        if not parts:
            reply = {"ok": False}
        elif parts[0] == "runout" and fw:
            letter = "L" if parts[1] == "0" else "R"
            fw.runout[letter] = "pending"
        elif parts[0] == "state" and fw:
            reply = {"pos": fw.pos, "logical_z": round(fw.logical_z(), 4), "head": round(fw.head, 4),
                     "nozzle": fw.nozzle_height(), "tool": fw.tool, "lowered": fw.lowered,
                     "ho": fw.ho, "ws": fw.ws, "e0": fw.e0, "e1": fw.e1, "eeprom": fw.eeprom,
                     "temps": fw.temps, "soft": fw.soft, "leveling": fw.leveling, "moves": fw.moves,
                     "extruded": round(fw.extruded, 3), "dead": fw.dead, "last_n": fw.last_n,
                     "sensors": fw.sensors, "runout": fw.runout}
        elif parts[0] == "history" and fw:
            n = int(parts[1]) if len(parts) > 1 else 50
            reply = {"history": fw.history[-n:]}
        elif parts[0] == "powerloss":
            # The printer loses power: connection drops, RAM is gone, the head stays where it is.
            if self.client:
                self.client.close()
            for t in self.tasks:
                t.cancel()
            if fw:
                self.head = fw.head
                fw.dead = True
        elif parts[0] == "bed" and fw and len(parts) == 4:
            # "bed <front> <back_left> <back_right>": screw heights, e.g. after "turning" them
            hs = [float(v) for v in parts[1:]]
            fw.bed_screws = [(x, y, h) for (x, y, _), h in zip(fw.bed_screws, hs)]
        elif parts[0] == "set" and fw and len(parts) == 3:
            setattr(fw, parts[1], float(parts[2]))
        else:
            reply = {"ok": False, "error": "unknown"}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()

    async def main(self):
        server = await asyncio.start_server(self.handle_client, "127.0.0.1", self.args.port)
        admin = await asyncio.start_server(self.handle_admin, "127.0.0.1", self.args.admin)
        print(f"Simulated Skriware 2 on socket://127.0.0.1:{self.args.port} (admin {self.args.admin})", flush=True)
        async with server, admin:
            await asyncio.gather(server.serve_forever(), admin.serve_forever())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--admin", type=int, default=5556)
    ap.add_argument("--speed", type=float, default=1.0, help="time acceleration for moves and heating")
    ap.add_argument("--error-rate", type=float, default=0.0, help="fraction of lines to reject with a checksum error")
    args = ap.parse_args()
    try:
        asyncio.run(Simulator(args).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
