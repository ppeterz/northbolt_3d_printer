"""Paper-test Z offset wizard.

How the offset works in this firmware (Marlin_main.cpp, Skriware_Functions.cpp):
  * Logical Z (what G-code and M114 use) = machine Z + home_offset[Z].
  * Z homes on the bed probe at the bed centre; the machine Z there is 0 - M851 Z.
  * "M206 Z<v>" sets home_offset[Z] for the left nozzle (and home_offset_E0) immediately.
  * "M206 E<v>" sets home_offset_E1, which the firmware swaps in when it changes to the right
    nozzle with the lift mechanism. "M206 I" reports it as "E:<v>".
So if the paper drags at logical Z = m, the new offset is  old - m  (+ paper thickness if you
want Z0 to be the nozzle touching the bed rather than resting on the paper).
"""

import asyncio
import logging

from .protocol import CommandCancelled, LinkError
from .recovery import fmt

log = logging.getLogger("zoffset")

STEPS = (-1.0, -0.1, -0.05, -0.01, 0.01, 0.05, 0.1, 1.0)


class ZOffsetWizard:
    def __init__(self, svc):
        self.svc = svc
        self._reset()

    def _reset(self):
        self.state = "idle"       # idle, preparing, adjusting, saving, done, checking, error
        self.tool = 0
        self.message = ""
        self.z = None
        self.start_z = None
        self.min_z = None
        self.max_z = None
        self.old_offset = None
        self.new_offset = None
        self.measured = None
        self.heated = False
        self.check_point = None
        self.error = None
        self._task = None
        self._cancelled = False

    @property
    def active(self):
        return self.state not in ("idle", "error")

    def snapshot(self):
        return {
            "state": self.state, "tool": self.tool, "message": self.message, "z": self.z,
            "start_z": self.start_z, "min_z": self.min_z, "old_offset": self.old_offset,
            "new_offset": self.new_offset, "measured": self.measured, "error": self.error,
            "check_point": self.check_point, "steps": STEPS,
            "paper": self.svc.settings["zoffset"]["paper_thickness"],
        }

    def _set(self, state=None, message=None):
        if state is not None:
            self.state = state
        if message is not None:
            self.message = message
        self.svc.notify("zoffset")

    async def _cmd(self, text):
        return await self.svc.link.command(text)

    async def _all(self, cmds):
        for c in cmds:
            if self._cancelled:
                raise CommandCancelled("Wizard cancelled")
            await self._cmd(c)

    # ------------------------------------------------------------------ steps
    def start(self, tool=0, heat=None):
        svc = self.svc
        if not svc.link.ready:
            raise RuntimeError("Printer not connected")
        if svc.job.active:
            raise RuntimeError("Can't calibrate while printing")
        if self.active:
            raise RuntimeError("The wizard is already running")
        if svc.leveling.active:
            raise RuntimeError("Finish bed leveling first")
        if tool not in (0, 1):
            raise RuntimeError("Unknown extruder")
        if tool == 1 and not svc.settings["lift_mechanism"]:
            raise RuntimeError("The right nozzle only has its own offset with the lift mechanism")
        if svc.tool != 0:
            raise RuntimeError("Select the left extruder before starting (Control page)")
        self._reset()
        self.tool = tool
        cfg = svc.settings["zoffset"]
        self.heated = cfg["heat"] if heat is None else bool(heat)
        self._set("preparing", "Reading current offsets")
        self._task = asyncio.ensure_future(self._prepare())

    async def _prepare(self):
        svc = self.svc
        cfg = svc.settings["zoffset"]
        try:
            offs = await svc.read_offsets()
            if self.heated:
                self._set(message="Heating bed and nozzle")
                await self._all([
                    f"M140 S{cfg['bed_temp']}",
                    f"M104 T{self.tool} S{cfg['nozzle_temp']}",
                    f"M190 S{cfg['bed_temp']}",
                    f"M109 T{self.tool} S{cfg['nozzle_temp']}",
                ])
            self._set(message="Homing")
            await self._all(["G28"])
            svc.tool = 0
            svc.homed = True
            if cfg["use_mesh"]:
                await self._all(["M420 S1"])
            if self.tool == 1:
                self._set(message="Switching to the right nozzle")
                await self._all(["T1"])
                svc.tool = 1
                active = offs["e1"]
            else:
                active = offs["ho"]
            self.old_offset = active
            # Machine Z where the probe triggered is -zprobe. Start a few mm above that point.
            trigger_logical = -offs["zprobe"] + active
            self.start_z = round(trigger_logical + cfg["start_height"], 3)
            self.min_z = round(trigger_logical - cfg["max_below_trigger"], 3)
            self.max_z = round(self.start_z + 20, 3)
            self._set(message="Moving to the bed centre")
            await self._all([
                "M211 S0",   # allow going below the normal Z minimum while measuring
                "G90",
                f"G1 Z{fmt(self.start_z)} F600",
                f"G1 X{fmt(cfg['center_x'])} Y{fmt(cfg['center_y'])} F3000",
                "M400",
            ])
            self.z = self.start_z
            self._set("adjusting", "Slide a sheet of paper under the nozzle and lower it until the paper "
                                   "just drags when you move it.")
        except (CommandCancelled, LinkError) as exc:
            if not self._cancelled:   # cancel() does its own clean-up
                await self._abort(str(exc))
        except Exception as exc:
            log.exception("Z offset wizard failed")
            await self._abort(str(exc))

    async def step(self, delta):
        if self.state != "adjusting":
            raise RuntimeError("Not adjusting")
        new = round(min(self.max_z, max(self.min_z, self.z + float(delta))), 3)
        if new == self.z:
            return
        self.z = new
        self.svc.notify("zoffset")
        await self._cmd(f"G1 Z{fmt(new)} F300")
        await self._cmd("M400")

    async def accept(self):
        if self.state != "adjusting":
            raise RuntimeError("Not adjusting")
        svc = self.svc
        paper = svc.settings["zoffset"]["paper_thickness"]
        self.measured = self.z
        new = round(self.old_offset - self.measured + paper, 3)
        if abs(new - self.old_offset) > 10:
            raise RuntimeError("That is more than 10 mm from the current offset; check the nozzle and probe")
        self.new_offset = new
        self._set("saving", "Saving the new offset")
        try:
            if self.tool == 0:
                await self._all([f"M206 Z{fmt(new)}", "G91", "G1 Z5 F600", "G90"])
            else:
                await self._all([f"M206 E{fmt(new)}", "G91", "G1 Z5 F600", "G90", "T0"])
                svc.tool = 0
            await self._all(["M211 S1", "M500"])
            await svc.read_offsets()
            self.z = None
            self._set("done", f"Saved. Z offset changed from {self.old_offset:.2f} to {new:.2f} mm.")
        except (CommandCancelled, LinkError) as exc:
            await self._abort(str(exc))

    async def check(self, point):
        """After saving the left nozzle, lower to Z0 at a corner to check the paper drag there."""
        if self.state not in ("done", "checking") or self.tool != 0:
            raise RuntimeError("Save a left-nozzle offset first")
        cfg = self.svc.settings["zoffset"]
        inset = cfg["check_inset"]
        bx, by = self.svc.settings["bed_x"], self.svc.settings["bed_y"]
        points = {
            "center": (cfg["center_x"], cfg["center_y"]),
            "front_left": (inset, inset),
            "front_right": (bx - inset, inset),
            "back_left": (inset, by - inset),
            "back_right": (bx - inset, by - inset),
        }
        if point not in points:
            raise RuntimeError("Unknown point")
        x, y = points[point]
        z0 = cfg["paper_thickness"]
        self.check_point = point
        self._set("checking", "Slide the paper under the nozzle. It should drag about the same as at the centre.")
        await self._all(["G90", f"G1 Z{fmt(z0 + 2)} F600", f"G1 X{fmt(x)} Y{fmt(y)} F3000",
                         f"G1 Z{fmt(z0)} F300", "M400"])

    async def finish(self):
        if self.state not in ("done", "checking"):
            raise RuntimeError("Nothing to finish")
        try:
            await self._all(["G91", "G1 Z5 F600", "G90"])
            if self.heated:
                await self._all([f"M104 T{self.tool} S0", "M140 S0"])
        finally:
            self._reset()
            self.svc.notify("zoffset")

    async def cancel(self):
        if self.state == "idle":
            return
        self._cancelled = True
        if self._task and not self._task.done():
            self.svc.link.emergency("M108")  # break out of a heat-up wait
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 30)
            except Exception:
                pass
        await self._abort("Cancelled", is_error=False)

    async def _abort(self, reason, is_error=True):
        svc = self.svc
        self._cancelled = False
        try:
            if svc.link.ready:
                await self._all(["M211 S1"])
                if svc.homed:
                    await self._all(["G91", "G1 Z5 F600", "G90"])
                if svc.tool == 1:
                    await self._all(["T0"])
                    svc.tool = 0
                if self.heated:
                    await self._all([f"M104 T{self.tool} S0", "M140 S0"])
        except Exception as exc:
            log.warning("Clean-up after wizard failed: %s", exc)
        tool = self.tool
        self._reset()
        self.tool = tool
        if is_error:
            self.state = "error"
            self.error = reason
            self.message = reason
        self.svc.notify("zoffset")
