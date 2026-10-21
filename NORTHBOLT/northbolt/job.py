"""Streams a G-code file to the printer with pause/resume, runout handling and checkpoints."""

import asyncio
import logging
import os
import time

from .gcode import Tracker, clean, host_action, parse_m114, start_temps
from .protocol import CommandCancelled, LinkError
from .recovery import fmt, resume_commands

log = logging.getLogger("job")

ACTIVE = ("starting", "printing", "pausing", "paused", "resuming", "cancelling")


class JobRunner:
    def __init__(self, svc):
        self.svc = svc
        self.state = "idle"
        self.file = None
        self.path = None
        self.meta = {}
        self.size = 0
        self.bytes_done = 0
        self.line_index = 0
        self.started = None
        self.base_elapsed = 0.0
        self.finished_at = None
        self.tracker = Tracker()
        self.pause_reason = None
        self.runout_side = None
        self.error = None
        self.message = None
        self.m73_progress = None
        self.m73_remaining_min = None
        self.heaters_off_for_pause = False
        self._pause_req = None
        self._resume_evt = asyncio.Event()
        self._cancel = False
        self._task = None
        self._streaming = False
        self._last_checkpoint = 0.0
        self._last_progress_save = 0.0
        self._paused_state = None
        self._timeout_task = None

    # ------------------------------------------------------------------ public API
    @property
    def active(self):
        return self.state in ACTIVE

    def snapshot(self):
        elapsed = self.elapsed()
        progress = self.progress()
        remaining = None
        if self.m73_remaining_min is not None:
            remaining = self.m73_remaining_min * 60
        elif progress and progress > 2:
            est = self.meta.get("est_time_s")
            by_rate = elapsed * (100.0 - progress) / progress
            if est:
                w = progress / 100.0  # trust the slicer early, the measured rate later
                remaining = est * (1 - w) * (1 - w) + by_rate * w
            else:
                remaining = by_rate
        st = self.tracker.state
        return {
            "state": self.state,
            "file": self.file,
            "progress": round(progress, 1) if progress is not None else None,
            "elapsed": int(elapsed),
            "remaining": int(remaining) if remaining is not None else None,
            "est_time_s": self.meta.get("est_time_s"),
            "layer": st.layer,
            "layers": self.meta.get("layers"),
            "z": st.z,
            "tool": st.tool,
            "pause_reason": self.pause_reason,
            "runout_side": self.runout_side,
            "heaters_off_for_pause": self.heaters_off_for_pause,
            "error": self.error,
            "message": self.message,
            "has_thumbnail": bool(self.meta.get("thumbnail")),
        }

    def elapsed(self):
        if self.started is None:
            return 0.0
        end = self.finished_at or time.monotonic()
        return self.base_elapsed + end - self.started

    def progress(self):
        if self.m73_progress is not None:
            return float(self.m73_progress)
        if not self.size:
            return None
        return 100.0 * self.bytes_done / self.size

    async def start(self, path, name, meta, plan=None):
        if self.active:
            raise RuntimeError("A print is already running")
        self.file = name
        self.path = path
        self.meta = meta or {}
        self.size = os.path.getsize(path)
        self.bytes_done = plan.offset if plan else 0
        self.line_index = (plan.line_index - 1) if plan else 0
        self.started = time.monotonic()
        self.finished_at = None
        self.base_elapsed = plan.elapsed if plan else 0.0
        self.tracker = Tracker(plan.state if plan else None)
        self.pause_reason = self.runout_side = self.error = self.message = None
        self.m73_progress = self.m73_remaining_min = None
        self.heaters_off_for_pause = False
        self._pause_req = None
        self._cancel = False
        self._resume_evt = asyncio.Event()
        self._last_checkpoint = 0.0
        self._last_progress_save = time.monotonic()
        if plan is None and self.svc.settings["recovery"]["enabled"]:
            self.svc.recovery.begin(path, name)
        self._set_state("resuming" if plan else "printing")
        self._task = asyncio.ensure_future(self._run(plan))

    def pause(self, reason="Paused", runout_side=None):
        if self.state not in ("printing", "resuming", "starting"):
            return False
        if self._pause_req is None:
            self._pause_req = reason
            self.runout_side = runout_side
            self.svc.notify("job")
        return True

    def resume(self):
        if self.state != "paused":
            return False
        self._resume_evt.set()
        return True

    async def cancel(self):
        if not self.active or self._cancel:
            return False
        self._cancel = True
        was_streaming = self._streaming
        self._set_state("cancelling")
        self._resume_evt.set()
        if was_streaming and self.svc.link.connected:
            # Emergency parser: M108 breaks a heat-up wait, M411 quick-stops and drops the queue.
            self.svc.link.emergency("M108")
            self.svc.link.emergency("M411")
            await self.svc.link.resync()
        return True

    def fail(self, reason):
        """Called by the service when the printer halts or disconnects mid-print."""
        if self.active:
            self.error = reason
            self._resume_evt.set()

    # ------------------------------------------------------------------ internals
    def _set_state(self, state):
        self.state = state
        self.svc.notify("job")

    async def _cmd(self, text, priority=False):
        return await self.svc.link.command(text, priority=priority)

    async def _send_all(self, cmds):
        for c in cmds:
            await self._cmd(c)

    async def _run(self, plan):
        self._streaming = True
        try:
            if plan:
                log.info("Resuming %s at line %d", self.file, plan.line_index)
                park, back = resume_commands(plan.state, self.svc.settings, self.svc.offsets,
                                             plan.head_z, plan.head_tool)
                self.message = "Freeing the nozzle, then heating at the back corner"
                await self._send_all(park)
                self.message = None
                if self.svc.settings["recovery"].get("clean_pause", True) and not (self._cancel or self.error):
                    await self._wait_for_clean(plan.state)
                if self._cancel or self.error:
                    return
                await self._send_all(back)
                self._set_state("printing")
            elif self.svc.settings["start"].get("heat_at_park", True):
                await self._heat_at_park()
            await self._stream(plan)
            if not self._cancel and not self.error:
                await self._cmd("M400")
                self.finished_at = time.monotonic()
                self.svc.recovery.clear()
                self._set_state("finished")
                self.svc.on_job_finished()
        except CommandCancelled:
            if not self._cancel:
                self.error = self.error or "Command queue was cleared"
        except LinkError as exc:
            self.error = self.error or str(exc)
        except Exception as exc:
            log.exception("Print failed")
            self.error = self.error or f"Internal error: {exc}"
        finally:
            self._streaming = False
            if self._timeout_task:
                self._timeout_task.cancel()
            if self._cancel:
                try:
                    await self._end_after_cancel()
                except Exception as exc:
                    log.warning("Cancel clean-up failed: %s", exc)
                self.svc.recovery.clear()
                self.finished_at = time.monotonic()
                self._set_state("cancelled")
            elif self.error:
                self.finished_at = time.monotonic()
                log.error("Print stopped: %s", self.error)
                self._set_state("error")

    async def _heat_at_park(self):
        """Like the original Skriware software: home X and Y, move to the park point (back left, off the
        bed's print area) and heat the bed and nozzles to the file's own temperatures there, so the head
        doesn't wait (and ooze) over the middle of the bed. The file then starts as sliced; its own heat-up
        waits return at once."""
        temps = await asyncio.get_running_loop().run_in_executor(None, start_temps, self.path)
        bed, tools = temps["bed"], temps["tools"]
        if bed is None and not tools:
            return
        p = self.svc.settings["pause"]
        self.message = "Heating at the back corner"
        self._set_state("starting")
        cmds = ["G90", "G28 X Y", f"G1 X{fmt(p['park_x'])} Y{fmt(p['park_y'])} F6000"]
        if bed:
            cmds.append(f"M140 S{fmt(bed)}")
        cmds += [f"M104 T{t} S{fmt(temp)}" for t, temp in sorted(tools.items())]   # all heat at once,
        if bed:
            cmds.append(f"M190 S{fmt(bed)}")                                        # then wait for each
        cmds += [f"M109 T{t} S{fmt(temp)}" for t, temp in sorted(tools.items())]
        log.info("Heating at the park point before %s: %s", self.file, ", ".join(cmds[3:]))
        for c in cmds:
            if self._cancel or self.error:
                return
            await self._cmd(c)
        self.message = None
        self._set_state("printing")

    async def _stream(self, plan):
        rec = self.svc.settings["recovery"]
        with open(self.path, "rb") as fh:
            if plan:
                fh.seek(plan.offset)
            while True:
                if self._pause_req and not self._cancel and not self.error:
                    await self._do_pause()
                if self._cancel or self.error:
                    return
                offset = fh.tell()
                raw = fh.readline()
                if not raw:
                    return
                self.line_index += 1
                cmd = clean(raw.decode("utf-8", "replace"))
                if not cmd:
                    self.bytes_done = fh.tell()
                    continue
                action = host_action(cmd)
                if action:
                    kind, value = action
                    if kind == "pause":
                        self.pause(value)
                    elif kind == "progress":
                        if value.get("P") is not None:
                            self.m73_progress = value["P"]
                        if value.get("R") is not None:
                            self.m73_remaining_min = value["R"]
                    elif kind == "message":
                        self.message = value
                    self.bytes_done = fh.tell()
                    continue

                before = self.tracker.state.copy()
                res = self.tracker.feed(cmd)
                confirm = None
                if res.layer_change and rec["enabled"]:
                    now = time.monotonic()
                    if now - self._last_checkpoint >= rec["min_interval_s"]:
                        self._last_checkpoint = now
                        if rec["layer_sync"]:
                            self._streaming = True
                            await self._cmd("M400")
                        self.svc.recovery.checkpoint(self.line_index, offset, before, self.elapsed())
                        self._last_progress_save = now
                elif rec["enabled"] and rec.get("sync_z_moves", True):
                    after = self.tracker.state
                    z_moves = before.z is not None and after.z is not None and abs(after.z - before.z) > 1e-6
                    tool_change = after.tool != before.tool and after.z is not None
                    if z_moves or tool_change:
                        self._streaming = True
                        await self._cmd("M400")
                        # Power can go just before this line runs or just after, and no save can tell which:
                        # until the line is confirmed, record the lower height. Resuming too high prints in
                        # the air for a moment; resuming too low would drive the nozzle into the part.
                        if z_moves and not tool_change and after.z < before.z:
                            self.svc.recovery.head(after.z, after.tool, self.line_index, offset, self.elapsed())
                        else:
                            self.svc.recovery.head(before.z, before.tool, self.line_index, offset, self.elapsed())
                            confirm = (after.z, after.tool)
                        self._last_progress_save = time.monotonic()

                self._streaming = True
                await self._cmd(cmd)
                self.bytes_done = fh.tell()
                if confirm:
                    # The move up or tool change has run: the head is at its new height.
                    await self._cmd("M400")
                    self.svc.recovery.head(*confirm, self.line_index, offset, self.elapsed())
                    self._last_progress_save = time.monotonic()

                if rec["enabled"]:
                    now = time.monotonic()
                    if now - self._last_progress_save >= rec["progress_interval_s"]:
                        self._last_progress_save = now
                        self.svc.recovery.progress(self.line_index, offset, self.elapsed())
                if res.layer_change:
                    self.svc.notify("job")

    async def _do_pause(self):
        reason = self._pause_req
        self._set_state("pausing")
        p = self.svc.settings["pause"]
        await self._cmd("M400")
        pos = parse_m114(await self._cmd("M114"))
        st = self.tracker.state.copy()
        x = st.x if st.x is not None else (pos or {}).get("x")
        y = st.y if st.y is not None else (pos or {}).get("y")
        z = st.z if st.z is not None else (pos or {}).get("z")
        self._paused_state = {"st": st, "x": x, "y": y, "z": z}
        lift = max(0.0, min(p["lift"], self.svc.settings["z_max"] - (z or 0) - 1))
        await self._send_all([
            "M107",
            "M83",
            f"G1 E-{fmt(p['retract'])} F2400",
            "G91",
            f"G1 Z{fmt(lift)} F600",
            "G90",
            f"G1 X{fmt(p['park_x'])} Y{fmt(p['park_y'])} F6000",
        ])
        self._streaming = False
        self.pause_reason = reason
        self._resume_evt = asyncio.Event()
        self._set_state("paused")
        log.info("Paused: %s", reason)
        timeout = p.get("heater_timeout_min") or 0
        if timeout > 0:
            self._timeout_task = asyncio.ensure_future(self._heater_timeout(timeout * 60))
        await self._resume_evt.wait()
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None
        if self._cancel or self.error:
            return
        self._set_state("resuming")
        await self._unpark()
        self._pause_req = None
        self.pause_reason = None
        self._set_state("printing")

    async def _wait_for_clean(self, st):
        """After a power cut: the head is parked off the print, hot and primed. Whatever oozed or froze
        onto the nozzle would go onto the part, so wait until someone has wiped it."""
        self._streaming = False
        self.pause_reason = ("Power is back. The nozzle is at the back left corner: wipe off any "
                             "filament on it, then press Resume to continue the print")
        self._resume_evt = asyncio.Event()
        self._set_state("paused")
        timeout = self.svc.settings["pause"].get("heater_timeout_min") or 0
        if timeout > 0:
            self._timeout_task = asyncio.ensure_future(self._heater_timeout(timeout * 60))
        await self._resume_evt.wait()
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None
        self.pause_reason = None
        if self._cancel or self.error:
            return
        self._set_state("resuming")
        if self.heaters_off_for_pause:     # waited too long: heat again, still at the park point
            self.heaters_off_for_pause = False
            cmds = [f"M104 T{t} S{fmt(temp)}" for t, temp in ((0, st.t0), (1, st.t1)) if temp > 0]
            if st.target(st.tool) > 0:
                cmds.append(f"M109 T{st.tool} S{fmt(st.target(st.tool))}")
            await self._send_all(cmds)

    async def _heater_timeout(self, seconds):
        try:
            await asyncio.sleep(seconds)
            log.info("Paused too long, switching hotends off")
            self.heaters_off_for_pause = True
            await self._send_all(["M104 T0 S0", "M104 T1 S0"])
            self.svc.notify("job")
        except asyncio.CancelledError:
            pass

    async def _unpark(self):
        ps = self._paused_state
        st = ps["st"]
        p = self.svc.settings["pause"]
        cmds = []
        for t, temp in ((0, st.t0), (1, st.t1)):
            if temp > 0:
                cmds.append(f"M104 T{t} S{fmt(temp)}")
        target = st.target(st.tool)
        current = self.svc.temps.get(f"t{st.tool}", {}).get("actual", 0)
        if target > 0 and (self.heaters_off_for_pause or current < target - 5):
            cmds.append(f"M109 T{st.tool} S{fmt(target)}")
        self.heaters_off_for_pause = False
        if self.runout_side == "left":
            cmds.append("M63 L")
        elif self.runout_side == "right":
            cmds.append("M63 R")
        self.runout_side = None
        cmds += ["G90"]
        if ps["x"] is not None and ps["y"] is not None:
            cmds.append(f"G1 X{fmt(ps['x'])} Y{fmt(ps['y'])} F6000")
        if ps["z"] is not None:
            cmds.append(f"G1 Z{fmt(ps['z'])} F600")
        cmds += ["M83", f"G1 E{fmt(p['retract'])} F1800"]
        if not st.rel_e:
            cmds += ["M82", f"G92 E{fmt(st.e)}"]
        if st.f:
            cmds.append(f"G1 F{fmt(st.f)}")
        if st.fan:
            cmds.append(f"M106 S{st.fan}")
        if st.rel_xyz:
            cmds.append("G91")
        await self._send_all(cmds)

    async def _end_after_cancel(self):
        if not self.svc.link.connected:
            return
        z = self.tracker.state.z or 0
        lift = max(0.0, min(10.0, self.svc.settings["z_max"] - z - 1))
        await self._send_all([
            "M104 T0 S0", "M104 T1 S0", "M140 S0", "M107",
            "G91", f"G1 Z{fmt(lift)} F600", "G90",
            "G28 X Y", "M84",
        ])
