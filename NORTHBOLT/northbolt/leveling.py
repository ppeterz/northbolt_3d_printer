"""Bed leveling assistant for a bed held by three screws.

It runs auto bed leveling (G29), reads the 5x5 mesh the firmware prints, fits a plane through it
and works out the bed height at each screw. Because three points define a plane, turning each
screw by its height difference to the reference screw makes the bed flat. What is left over
after removing the plane is warp, which screws can't fix (the mesh handles it while printing).

The probe sits 32 mm behind the nozzle, so it can't reach the front screw at Y0 directly; the
plane gives its height anyway.

Mesh output format (Marlin_main.cpp print_2d_array): a header row of column numbers, then one
row per Y index from front to back: " 0 +0.123 -0.045 ..."; unprobed points print "=====".
G29 and M420 V print this grid, then a 13x13 subdivided grid, which is ignored.
"""

import asyncio
import logging
import re
import time

from .protocol import CommandCancelled, LinkError
from .recovery import fmt

log = logging.getLogger("leveling")

ROW_RE = re.compile(r"^\s*(\d+)((?:\s+(?:[+-]\d+\.\d+|=+))+)\s*$")


def parse_grid(lines):
    """The first 'Bilinear Leveling Grid:' in the lines, as rows (front to back) of floats or None."""
    rows = []
    started = False
    for line in lines:
        text = line.strip()
        if text.startswith("echo:"):
            text = text[5:].strip()
        if not started:
            if "Bilinear Leveling Grid" in text:
                started = True
            continue
        m = ROW_RE.match(text)
        if m:
            vals = [None if v.startswith("=") else float(v) for v in m.group(2).split()]
            rows.append(vals)
        elif rows:
            break        # end of the first grid (the subdivided grid follows)
        # else: the header line of column numbers
    if not rows or len({len(r) for r in rows}) != 1:
        return None
    return rows


def _solve3(m, v):
    """Solve a 3x3 linear system with Gaussian elimination (no numpy on the Pi)."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("The mesh points are all in a line")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(3):
            if r != col:
                f = a[r][col] / a[col][col]
                for c in range(col, 4):
                    a[r][c] -= f * a[col][c]
    return [a[i][3] / a[i][i] for i in range(3)]


def fit_plane(points):
    """Least-squares plane z = a + b*x + c*y through (x, y, z) points."""
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sz = sum(p[2] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    syy = sum(p[1] * p[1] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    sxz = sum(p[0] * p[2] for p in points)
    syz = sum(p[1] * p[2] for p in points)
    return _solve3([[n, sx, sy], [sx, sxx, sxy], [sy, sxy, syy]], [sz, sxz, syz])


def analyze(mesh, cfg):
    """Turn a mesh into per-screw instructions using the leveling settings."""
    area = cfg["grid"]
    ny, nx = len(mesh), len(mesh[0])
    xs = [area["left"] + i * (area["right"] - area["left"]) / (nx - 1) for i in range(nx)]
    ys = [area["front"] + j * (area["back"] - area["front"]) / (ny - 1) for j in range(ny)]
    points = [(xs[i], ys[j], z) for j, row in enumerate(mesh) for i, z in enumerate(row) if z is not None]
    if len(points) < 3:
        raise ValueError("Not enough probed points in the mesh")
    a, b, c = fit_plane(points)
    plane = lambda x, y: a + b * x + c * y  # noqa: E731
    residual = [z - plane(x, y) for x, y, z in points]
    screws = cfg["screws"]
    ref = max(0, min(len(screws) - 1, int(cfg.get("reference", 0))))
    heights = [plane(s["x"], s["y"]) for s in screws]
    pitch = float(cfg.get("pitch", 0.5)) or 0.5
    tol = float(cfg.get("tolerance", 0.05))
    out = []
    for i, (s, h) in enumerate(zip(screws, heights)):
        delta = h - heights[ref]
        turns = abs(delta) / pitch
        if i == ref:
            action = "reference"
        elif abs(delta) < tol:
            action = "ok"
        else:
            action = "lower" if delta > 0 else "raise"
        clockwise = (action == "lower") == bool(cfg.get("clockwise_lowers", True))
        out.append({
            "name": s.get("name", f"Screw {i + 1}"), "x": s["x"], "y": s["y"],
            "height": round(h, 3), "delta": round(delta, 3), "turns": round(turns, 2),
            "degrees": int(round(turns * 360)), "action": action,
            "direction": None if action in ("ok", "reference") else ("cw" if clockwise else "ccw"),
        })
    zs = [p[2] for p in points]
    return {
        "screws": out,
        "reference": ref,
        "level": all(s["action"] in ("ok", "reference") for s in out),
        "tilt": round(max(heights) - min(heights), 3),
        "warp": round(max(residual) - min(residual), 3),
        "range": round(max(zs) - min(zs), 3),
        "mesh": mesh, "xs": xs, "ys": ys,
        "pitch": pitch, "tolerance": tol,
        "time": time.time(),
    }


class BedLevelWizard:
    def __init__(self, svc):
        self.svc = svc
        self.state = "idle"      # idle, running, results, teach, error
        self.message = ""
        self.result = None
        self.error = None
        self.heated = False
        self.started = None
        self.teach_index = 0
        self.teach_pos = None
        self.rounds = 0
        self._task = None
        self._cancelled = False

    @property
    def active(self):
        return self.state in ("running", "results", "teach")

    def snapshot(self):
        cfg = self.svc.settings["leveling"]
        return {
            "state": self.state, "message": self.message, "result": self.result, "error": self.error,
            "elapsed": int(time.monotonic() - self.started) if self.started and self.state == "running" else None,
            "screws": cfg["screws"], "reference": cfg["reference"], "pitch": cfg["pitch"],
            "clockwise_lowers": cfg["clockwise_lowers"], "teach_index": self.teach_index,
            "teach_pos": self.teach_pos, "rounds": self.rounds, "heated": self.heated,
        }

    def _set(self, state=None, message=None):
        if state is not None:
            self.state = state
        if message is not None:
            self.message = message
        self.svc.notify("leveling")

    async def _all(self, cmds):
        out = []
        for c in cmds:
            if self._cancelled:
                raise CommandCancelled("Cancelled")
            out += await self.svc.link.command(c)
        return out

    def _check_ready(self):
        svc = self.svc
        if not svc.link.ready:
            raise RuntimeError("Printer not connected")
        if svc.job.active:
            raise RuntimeError("Can't level the bed while printing")
        if svc.wizard.active:
            raise RuntimeError("Finish the Z offset test first")
        if svc.tool != 0:
            raise RuntimeError("Select the left extruder first (Control page)")

    # ------------------------------------------------------------------ measuring
    def start(self, probe=True, heat=None):
        if self.state in ("running", "teach"):
            raise RuntimeError("Finish what's on screen first")
        self._check_ready()
        if self.state != "results":          # a fresh start, not "probe again" after turning screws
            cfg = self.svc.settings["leveling"]
            self.heated = cfg["heat"] if heat is None else bool(heat)
            self.rounds = 0
        self.error = None
        self._cancelled = False
        self.started = time.monotonic()
        self._set("running", "Starting")
        self._task = asyncio.ensure_future(self._measure(probe))

    async def _measure(self, probe):
        svc = self.svc
        cfg = svc.settings["leveling"]
        try:
            if self.heated:
                self._set(message=f"Heating the bed to {cfg['bed_temp']} °C")
                await self._all([f"M140 S{cfg['bed_temp']}", f"M190 S{cfg['bed_temp']}"])
            self._set(message="Homing")
            await self._all(["G28"])
            svc.tool = 0
            svc.homed = True
            if probe:
                self._set(message="Probing the bed (25 points, about 3 minutes)")
                lines = await self._all(["G29"])
            else:
                self._set(message="Reading the saved bed mesh")
                lines = await self._all(["M420 V"])
            mesh = parse_grid(lines)
            if mesh is None:
                raise RuntimeError("The printer didn't report a bed mesh" +
                                   ("" if probe else ". Probe the bed instead."))
            await self._all(["M420 S1", "G90", f"G1 Z{fmt(cfg['park_z'])} F600",
                             f"G1 X{fmt(svc.settings['bed_x'] / 2)} Y{fmt(svc.settings['bed_y'] / 2)} F3000"])
            self.result = analyze(mesh, cfg)
            self.rounds += 1
            self._set("results", "")
        except (CommandCancelled, LinkError) as exc:
            if not self._cancelled:
                self._fail(str(exc))
        except Exception as exc:
            log.exception("Bed leveling failed")
            self._fail(str(exc))

    def _fail(self, reason):
        self.error = reason
        self._set("error", reason)

    def recompute(self):
        """Settings changed (pitch, direction, reference, screw positions): redo the numbers."""
        if self.result:
            self.result = analyze(self.result["mesh"], self.svc.settings["leveling"])
            self.svc.notify("leveling")

    async def save(self):
        if self.state != "results":
            raise RuntimeError("Nothing to save")
        await self._all(["M500"])
        self._set(message="Bed mesh saved")

    async def cancel(self):
        if self._task and not self._task.done():
            self._cancelled = True
            self.svc.link.emergency("M108")   # breaks a bed heat-up wait; G29 itself runs to the end
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 600)
            except Exception:
                pass
        await self.finish()

    async def finish(self):
        try:
            if self.heated and self.svc.link.ready:
                await self.svc.link.command("M140 S0")
        except Exception:
            pass
        self.state = "idle"
        self.message = ""
        self.result = None
        self.error = None
        self.heated = False
        self._cancelled = False
        self.svc.notify("leveling")

    # ------------------------------------------------------------------ teaching screw positions
    async def teach_start(self, index=0):
        if self.state == "running":
            raise RuntimeError("Wait for the measurement to finish")
        if self.state == "idle" or self.state == "error":
            self._check_ready()
        self._return_state = self.state if self.state == "results" else "idle"
        self.state = "teach"
        await self.teach_go(index)

    async def teach_go(self, index):
        svc = self.svc
        screws = svc.settings["leveling"]["screws"]
        self.teach_index = max(0, min(len(screws) - 1, int(index)))
        s = screws[self.teach_index]
        self._set(message=f"Moving over the {s['name'].lower()} screw")
        if not svc.homed:
            await self._all(["G28"])
            svc.homed = True
            svc.tool = 0
        await self._all(["G90", "G1 Z10 F600", f"G1 X{fmt(s['x'])} Y{fmt(s['y'])} F3000"])
        await self._read_pos()
        self._set(message="Move the nozzle until it is right above the middle of the screw, then save.")

    async def teach_jog(self, dx=0.0, dy=0.0):
        if self.state != "teach":
            raise RuntimeError("Not setting screw positions")
        parts = [f"{a}{fmt(v)}" for a, v in (("X", dx), ("Y", dy)) if v]
        if parts:
            await self._all(["G91", f"G1 {' '.join(parts)} F1500", "G90"])
        await self._read_pos()

    async def _read_pos(self):
        from .gcode import parse_m114
        pos = parse_m114(await self._all(["M400", "M114"]))
        if pos:
            self.teach_pos = {"x": round(pos["x"], 1), "y": round(pos["y"], 1)}
        self.svc.notify("leveling")

    async def teach_save(self):
        if self.state != "teach" or not self.teach_pos:
            raise RuntimeError("Not setting screw positions")
        screws = [dict(s) for s in self.svc.settings["leveling"]["screws"]]
        screws[self.teach_index].update(self.teach_pos)
        self.svc.settings.update({"leveling": {"screws": screws}})
        self.recompute()
        self._set(message=f"Saved the {screws[self.teach_index]['name'].lower()} screw at "
                          f"X {self.teach_pos['x']:g}, Y {self.teach_pos['y']:g}.")

    async def teach_done(self):
        if self.state != "teach":
            return
        await self._all(["G91", "G1 Z10 F600", "G90"])
        self.teach_pos = None
        self._set(getattr(self, "_return_state", "idle"), "")
