"""Power-loss recovery without extra hardware.

The job runner saves:
  * a checkpoint at layer changes (after M400, so everything before it is printed), with the
    full print state just before the layer's first line, and
  * a progress mark every few seconds: the last line the firmware acknowledged.

Acknowledged lines run ahead of the nozzle by up to BUFSIZE + BLOCK_BUFFER_SIZE moves, so the
resume point is the progress mark minus that many moves, but never before the checkpoint.
"""

import json
import os
import time
from collections import deque

from .config import atomic_write_json
from .gcode import PrintState, Tracker, clean, host_action


class RecoveryStore:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "recovery.json")
        self.data = None
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, ValueError):
                self.data = None

    def begin(self, file_path, file_name):
        st = os.stat(file_path)
        self.data = {
            "version": 1,
            "file": file_name,
            "path": file_path,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "started": time.time(),
            "checkpoint": None,
            "progress": None,
            "saved": time.time(),
        }
        self._save()

    def checkpoint(self, line_index, offset, state, elapsed):
        """Saved right after M400 at a layer change: every earlier line has been printed."""
        if not self.data:
            return
        self.data["checkpoint"] = {
            "line": line_index,
            "offset": offset,
            "state": state.to_dict(),
            "elapsed": elapsed,
            "time": time.time(),
        }
        self.data["sync"] = {"line": line_index, "offset": offset, "elapsed": elapsed}
        self.data["head"] = {"z": state.z, "tool": state.tool}
        self.data["progress"] = None
        self._save()

    def head(self, z, tool, line_index, offset, elapsed):
        """Saved after M400 around a line that moves Z or changes tool: everything before it has been printed.

        Before the line is sent, z/tool are the lower of the heights before and after it (power may go
        either side of it, and resuming too high is harmless where too low hits the part). A move up or a
        tool change is saved again after a second M400 confirms it ran. Re-sending that line on resume
        is harmless. Acknowledged-line progress alone can't tell the head height: the printer may
        already be a layer or a Z-hop further on than the resume point."""
        if not self.data or not self.data.get("checkpoint"):
            return
        self.data["sync"] = {"line": line_index, "offset": offset, "elapsed": elapsed}
        self.data["head"] = {"z": z, "tool": tool}
        self.data["progress"] = None
        self._save()

    def progress(self, line_index, offset, elapsed):
        """The last line the firmware acknowledged (not synced: the nozzle may be behind it)."""
        if not self.data or not self.data.get("checkpoint"):
            return
        self.data["progress"] = {"line": line_index, "offset": offset, "elapsed": elapsed, "time": time.time()}
        self._save()

    def clear(self):
        self.data = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _save(self):
        self.data["saved"] = time.time()
        atomic_write_json(self.path, self.data)

    def pending(self):
        """Summary for the UI if an interrupted print can be resumed."""
        d = self.data
        if not d or not d.get("checkpoint"):
            return None
        ck = d["checkpoint"]
        return {
            "file": d["file"],
            "layer": ck["state"].get("layer"),
            "z": ck["state"].get("z"),
            # the nozzle at the print when the power went (saved at every swap), not the layer's first one
            "tool": (d.get("head") or {}).get("tool", ck["state"].get("tool", 0)),
            "saved": d.get("saved"),
            "progress": round(100.0 * ((d.get("progress") or d.get("sync") or ck)["offset"]) / max(1, d["size"]), 1),
        }


class ResumePlan:
    def __init__(self, line_index, offset, state, elapsed, head_z=None, head_tool=None):
        self.line_index = line_index   # 1-based index of the first line to send
        self.offset = offset           # byte offset of that line
        self.state = state             # PrintState just before that line
        self.elapsed = elapsed
        self.head_z = state.z if head_z is None else head_z          # where the head physically is
        self.head_tool = state.tool if head_tool is None else head_tool


def build_plan(data, backoff_moves):
    """Work out where to restart. Raises ValueError if the print can't be resumed."""
    path = data["path"]
    if not os.path.exists(path):
        raise ValueError(f"The G-code file {data['file']} is gone")
    st = os.stat(path)
    if st.st_size != data["size"] or abs(st.st_mtime - data["mtime"]) > 1:
        raise ValueError("The G-code file has changed since the print started")
    ck = data.get("checkpoint")
    if not ck:
        raise ValueError("No checkpoint was saved (power was lost before the first layer)")

    # Lines before `floor` are known to be printed (an M400 completed after them).
    sync = data.get("sync") or {}
    floor = max(ck["line"], sync.get("line", ck["line"]))
    # Lines up to `target` were acknowledged, but up to `backoff_moves` of them may not have run.
    target = max(floor, (data.get("progress") or {}).get("line", floor))

    tracker = Tracker(PrintState.from_dict(ck["state"]))
    moves = deque(maxlen=backoff_moves + 1)
    floor_entry = (ck["line"], ck["offset"], PrintState.from_dict(ck["state"]))
    with open(path, "rb") as fh:
        fh.seek(ck["offset"])
        line_index = ck["line"] - 1
        while line_index < target:
            offset = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            line_index += 1
            cmd = clean(raw.decode("utf-8", "replace"))
            if not cmd or host_action(cmd):
                continue
            before = tracker.state.copy()
            if line_index == floor:
                floor_entry = (line_index, offset, before)
            res = tracker.feed(cmd)
            if res.is_move and line_index >= floor:
                moves.append((line_index, offset, before))

    if len(moves) == moves.maxlen:
        line, offset, state = moves[0]
    else:
        line, offset, state = floor_entry
    if state.x is None or state.y is None or state.z is None:
        raise ValueError("The saved position is incomplete, so the print can't be resumed safely")
    elapsed = (data.get("progress") or sync or ck).get("elapsed", 0)
    head = data.get("head") or {}
    return ResumePlan(line, offset, state, elapsed, head.get("z"), head.get("tool"))


def fmt(v):
    return f"{v:.3f}".rstrip("0").rstrip(".")


def resume_commands(state, settings, offsets, head_z=None, head_tool=None):
    """G-code that brings a cold, unhomed printer back to `state` without homing Z, in two parts:
    (park, back). `park` frees the nozzle from the part, moves the head to the park point (back
    left, off the print) and heats and primes it there; the screen then asks to clean the nozzle.
    `back` returns to the part and continues.

    The nozzle is never heated to printing temperature over the part: it would ooze a blob onto it
    while it heats. It only warms to recovery.soften_temp (soft enough to come away from the plastic
    it froze into, too cool to run) before the bed drops away.

    head_z/head_tool: where the head physically stopped (logical Z for that tool). Defaults to
    the state's own Z and tool.
    offsets: {"e0": home_offset_E0, "e1": home_offset_E1} read from the firmware, or None.
    """
    head_z = state.z if head_z is None else head_z
    head_tool = state.tool if head_tool is None else head_tool
    rec = settings["recovery"]
    p = settings.get("pause") or {}
    z_max = settings["z_max"]
    tool = state.tool
    cmds = []
    if state.bed > 0:                   # the bed first, while the nozzle stays cold
        cmds += [f"M140 S{fmt(state.bed)}", f"M190 S{fmt(state.bed)}"]
    soften = min(state.target(head_tool), float(rec.get("soften_temp", 150)))
    if soften > 0:
        cmds.append(f"M109 T{head_tool} S{fmt(soften)}")

    # Z is still where the print stopped (lead screws hold it). Tell the firmware, don't home it.
    # With T1 active the firmware's Z includes the E1-E0 offset difference; we boot on T0.
    z_set = head_z
    if head_tool == 1 and settings["lift_mechanism"] and offsets:
        z_set = head_z - (offsets["e1"] - offsets["e0"])
    lift = max(0.0, min(rec["z_lift"], z_max - head_z - 1))
    cmds += [
        "G90",
        f"G92 Z{fmt(z_set)}",
        "G91",
        f"G1 Z{fmt(lift)} F300",
        "G90",
        "G28 X Y",                     # also switches to T0 and raises the extruder lift
    ]
    if rec.get("use_mesh", True):
        cmds.append("M420 S1")          # G28 turns bed leveling off in this firmware
    if p.get("park_x") is not None and p.get("park_y") is not None:
        cmds.append(f"G1 X{fmt(p['park_x'])} Y{fmt(p['park_y'])} F3000")
    for t, temp in ((0, state.t0), (1, state.t1)):
        if temp > 0:
            cmds.append(f"M104 T{t} S{fmt(temp)}")
    if state.target(tool) > 0:
        cmds.append(f"M109 T{tool} S{fmt(state.target(tool))}")
    for t, temp in ((0, state.t0), (1, state.t1)):
        if temp > 0 and t != tool:
            cmds.append(f"M109 T{t} S{fmt(temp)}")
    if tool == 1:
        cmds.append("T1")
    prime = rec["prime_length"]
    cmds += ["M83"]
    if prime > 0:
        cmds += [f"G1 E{fmt(prime)} F200", "G1 E-1 F1800"]
    park = cmds
    cmds = [
        f"G1 X{fmt(state.x)} Y{fmt(state.y)} F3000",
        f"G1 Z{fmt(state.z)} F300",
    ]
    if prime > 0:
        cmds.append("G1 E1 F1800")
    if not state.rel_e:
        cmds.append("M82")
    cmds.append(f"G92 E{fmt(state.e)}")
    cmds.append(f"M106 S{state.fan}" if state.fan else "M107")
    if state.f:
        cmds.append(f"G1 F{fmt(state.f)}")
    if state.rel_xyz:
        cmds.append("G91")
    return park, cmds
