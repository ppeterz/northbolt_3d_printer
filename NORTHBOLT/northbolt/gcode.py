"""G-code helpers: cleaning, parsing, a print-state tracker and a file metadata scanner."""

import base64
import os
import re
from dataclasses import asdict, dataclass, field

WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
CODE_RE = re.compile(r"\s*([GMTgmt])(\d+)(?:\.\d+)?")
NO_PARAM_CODES = {"M117", "M118", "M23", "M28", "M30", "M32", "M928"}


def clean(line):
    """Strip comments, existing line numbers and checksums. Returns '' for nothing to send."""
    i = line.find(";")
    if i >= 0:
        line = line[:i]
    line = line.strip()
    if not line:
        return ""
    if line[0] in "Nn":
        m = re.match(r"[Nn]\d+\s*", line)
        if m:
            line = line[m.end():]
    star = line.rfind("*")
    if star > 0 and line[star + 1:].strip().isdigit():
        line = line[:star].rstrip()
    return line


def parse(cmd):
    """Return (code, params). code like 'G1', 'M104', 'T1'. Flag params map to None."""
    m = CODE_RE.match(cmd)
    if not m:
        return None, {}
    code = f"{m.group(1).upper()}{int(m.group(2))}"
    if code in NO_PARAM_CODES or code[0] == "T":
        return code, {}
    rest = cmd[m.end():]
    params = {}
    for k, v in WORD_RE.findall(rest):
        params[k.upper()] = float(v)
    for k in re.findall(r"(?:^|\s)([A-Za-z])(?=\s|$)", rest):
        params.setdefault(k.upper(), None)
    return code, params


def host_action(cmd):
    """Commands the host handles itself instead of sending.

    Returns ('pause', reason), ('progress', params), ('message', text) or None.
    M0/M1/M600 have no handler in this firmware build (no LCD, no ADVANCED_PAUSE_FEATURE).
    """
    stripped = cmd.strip()
    if stripped.lower().startswith("@pause"):
        return ("pause", "Pause requested by the G-code file")
    code, params = parse(stripped)
    if code in ("M0", "M1", "M601"):
        return ("pause", "Pause requested by the G-code file")
    if code == "M600":
        return ("pause", "Filament change requested by the G-code file")
    if code == "M73":
        return ("progress", params)
    if code == "M117":
        return ("message", stripped[4:].strip())
    return None


@dataclass
class PrintState:
    x: float = None
    y: float = None
    z: float = None
    e: float = 0.0
    f: float = None
    rel_xyz: bool = False       # G91
    rel_e: bool = False         # M83
    tool: int = 0
    fan: int = 0
    t0: float = 0.0
    t1: float = 0.0
    bed: float = 0.0
    layer: int = 0
    layer_z: float = -1.0
    max_z: float = 0.0

    def copy(self):
        return PrintState(**asdict(self))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def target(self, tool):
        return self.t1 if tool == 1 else self.t0


@dataclass
class FeedResult:
    code: str = None
    is_move: bool = False
    extrudes: bool = False
    layer_change: bool = False


class Tracker:
    """Follows the G-code stream to know positions, modes, tool, fan and temperatures."""

    def __init__(self, state=None):
        self.state = state.copy() if state else PrintState()

    def feed(self, cmd):
        s = self.state
        code, p = parse(cmd)
        res = FeedResult(code=code)
        if code is None:
            return res
        if code in ("G0", "G1", "G2", "G3"):
            res.is_move = True
            moved_xy = False
            for axis in ("x", "y", "z"):
                key = axis.upper()
                if key in p and p[key] is not None:
                    cur = getattr(s, axis)
                    if s.rel_xyz:
                        new = None if cur is None else cur + p[key]
                    else:
                        new = p[key]
                    if axis != "z" and new != cur:
                        moved_xy = True
                    setattr(s, axis, new)
            if code in ("G2", "G3"):
                moved_xy = True
            if "E" in p and p["E"] is not None:
                if s.rel_xyz or s.rel_e:
                    de = p["E"]
                    s.e += de
                else:
                    de = p["E"] - s.e
                    s.e = p["E"]
                res.extrudes = de > 1e-6 and moved_xy
            if "F" in p and p["F"] is not None:
                s.f = p["F"]
            if res.extrudes and s.z is not None:
                s.max_z = max(s.max_z, s.z)   # printed height, ignoring travel and end-code lifts
            if res.extrudes and s.z is not None and s.z > s.layer_z + 0.005:
                s.layer += 1
                s.layer_z = s.z
                res.layer_change = True
        elif code == "G92":
            if not p:
                s.x = s.y = s.z = 0.0
                s.e = 0.0
            for axis in ("x", "y", "z", "e"):
                key = axis.upper()
                if key in p and p[key] is not None:
                    setattr(s, axis, p[key])
        elif code == "G28":
            axes = [a for a in ("X", "Y", "Z") if a in p]
            for a in axes or ["X", "Y", "Z"]:
                setattr(s, a.lower(), None)
            s.tool = 0  # this firmware's G28 always switches to T0
        elif code == "G90":
            s.rel_xyz = False
        elif code == "G91":
            s.rel_xyz = True
        elif code == "M82":
            s.rel_e = False
        elif code == "M83":
            s.rel_e = True
        elif code[0] == "T":
            s.tool = int(code[1:])
        elif code in ("M104", "M109"):
            temp = p.get("S") if p.get("S") is not None else p.get("R")
            if temp is not None:
                tool = int(p["T"]) if p.get("T") is not None else s.tool
                if tool == 1:
                    s.t1 = temp
                else:
                    s.t0 = temp
        elif code in ("M140", "M190"):
            temp = p.get("S") if p.get("S") is not None else p.get("R")
            if temp is not None:
                s.bed = temp
        elif code == "M106":
            s.fan = int(p["S"]) if p.get("S") is not None else 255
        elif code == "M107":
            s.fan = 0
        return res


M114_RE = re.compile(r"X:\s*(-?[\d.]+)\s+Y:\s*(-?[\d.]+)\s+Z:\s*(-?[\d.]+)\s+E:\s*(-?[\d.]+)")


def parse_m114(lines):
    for line in lines:
        m = M114_RE.search(line)
        if m:
            x, y, z, e = (float(v) for v in m.groups())
            return {"x": x, "y": y, "z": z, "e": e}
    return None


# ---------------------------------------------------------------------- file metadata
TIME_PATTERNS = [
    re.compile(r";\s*estimated printing time(?: \(normal mode\))?\s*=\s*(.+)", re.I),
    re.compile(r";\s*total estimated time\s*[:=]\s*(.+)", re.I),
    re.compile(r";\s*model printing time\s*[:=]\s*([^;]+)", re.I),
]
CURA_TIME = re.compile(r";TIME:(\d+)")
FIL_MM = re.compile(r";\s*filament used \[mm\]\s*=\s*([\d.]+)", re.I)
FIL_G = re.compile(r";\s*(?:total )?filament used \[g\]\s*=\s*([\d.]+)", re.I)
CURA_FIL = re.compile(r";Filament used:\s*([\d.]+)m", re.I)
THUMB_BEGIN = re.compile(r";\s*thumbnail(?:_(PNG|JPG|QOI))?\s+begin\s+(\d+)x(\d+)", re.I)


def parse_duration(text):
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*([dhms])", text.lower()):
        found = True
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


SLICERS = ("prusaslicer", "orcaslicer", "superslicer", "bambu", "cura", "simplify3d", "slic3r")
THUMB_END = re.compile(r";\s*thumbnail(?:_\w+)?\s+end", re.I)


def _comment_meta(line, meta):
    """Pull slicer name, print time and filament use out of one comment line."""
    if meta["slicer"] is None:
        low = line.lower()
        for name in SLICERS:
            if name in low:
                meta["slicer"] = name
                break
    if meta["est_time_s"] is None:
        m = CURA_TIME.match(line)
        if m:
            meta["est_time_s"] = int(m.group(1))
        else:
            for pat in TIME_PATTERNS:
                m = pat.match(line)
                if m:
                    meta["est_time_s"] = parse_duration(m.group(1))
                    break
    for pat, key, scale in ((FIL_MM, "filament_mm", 1), (FIL_G, "filament_g", 1), (CURA_FIL, "filament_mm", 1000)):
        m = pat.match(line)
        if m and meta[key] is None:
            try:
                meta[key] = float(m.group(1)) * scale
            except ValueError:
                pass


class _Thumbs:
    """Collects slicer thumbnails ("; thumbnail begin WxH ..." blocks) line by line."""

    def __init__(self):
        self.found = []
        self.cur = None

    def feed(self, line):
        """Returns True if the line belonged to a thumbnail block."""
        if self.cur is not None:
            if THUMB_END.match(line):
                self.found.append(self.cur)
                self.cur = None
            else:
                self.cur["data"].append(line[1:].strip())
            return True
        m = THUMB_BEGIN.match(line)
        if m:
            self.cur = {"fmt": (m.group(1) or "PNG").upper(), "w": int(m.group(2)), "h": int(m.group(3)), "data": []}
            return True
        return False

    def best(self):
        usable = [t for t in self.found if t["fmt"] in ("PNG", "JPG") and t["w"] <= 400]
        if not usable:
            return None
        best = max(usable, key=lambda t: t["w"])
        mime = "image/png" if best["fmt"] == "PNG" else "image/jpeg"
        data = "".join(best["data"])
        try:
            base64.b64decode(data, validate=True)
        except Exception:
            return None
        return f"data:{mime};base64,{data}"


def _empty_meta(size):
    return {"size": size, "est_time_s": None, "filament_mm": None, "filament_g": None, "layers": 0,
            "max_z": 0.0, "tools": [], "slicer": None, "thumbnail": None, "lines": 0}


def scan_file(path):
    """Read a G-code file once and pull out what the UI shows. Safe on big files."""
    meta = _empty_meta(os.path.getsize(path))
    tools = set()
    thumbs = _Thumbs()
    marker_layers = 0
    tracker = Tracker()
    with open(path, "rb") as fh:
        for raw in fh:
            meta["lines"] += 1
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            if line[0] == ";":
                if thumbs.feed(line):
                    continue
                if line.startswith(";LAYER_CHANGE") or line.startswith(";LAYER:"):
                    marker_layers += 1
                _comment_meta(line, meta)
                continue
            cmd = clean(line)
            if not cmd:
                continue
            c0 = cmd[0].upper()
            if c0 == "T" and cmd[1:2].isdigit():
                tools.add(int(cmd[1]))
            if c0 in "GTM":
                tracker.feed(cmd)
    meta["max_z"] = round(tracker.state.max_z, 3)
    meta["layers"] = marker_layers or tracker.state.layer
    meta["tools"] = sorted(tools) or [0]
    meta["thumbnail"] = thumbs.best()
    return meta


def start_temps(path, limit=512 * 1024):
    """The first bed and nozzle temperatures a file sets before it starts printing:
    {"bed": 60.0, "tools": {0: 210.0}}. Reads only the start of the file."""
    bed = None
    tools = {}
    tool = 0
    with open(path, "rb") as fh:
        text = fh.read(limit).decode("utf-8", "replace")
    for raw in text.splitlines():
        cmd = clean(raw)
        if not cmd:
            continue
        code, p = parse(cmd)
        if code in ("G0", "G1") and (p.get("E") or 0) > 0:
            break                                    # it's printing (or purging): heating is set by now
        if code[:1] == "T" and code[1:].isdigit():
            tool = int(code[1:])
        elif code in ("M104", "M109"):
            temp = p.get("S") if p.get("S") is not None else p.get("R")
            t = int(p["T"]) if p.get("T") is not None else tool
            if temp and temp > 0 and t not in tools:
                tools[t] = temp
        elif code in ("M140", "M190"):
            temp = p.get("S") if p.get("S") is not None else p.get("R")
            if temp and temp > 0 and bed is None:
                bed = temp
    return {"bed": bed, "tools": tools}


def quick_scan(path, head=1024 * 1024, tail=192 * 1024):
    """Thumbnail, print time and filament from the start and end of a file only.

    Slicers put thumbnails at the top and their estimates at the top or bottom, so this is
    enough for listings and is fast on a USB drive. Layer count and height need scan_file."""
    size = os.path.getsize(path)
    meta = _empty_meta(size)
    meta["layers"] = None
    meta["max_z"] = None
    meta["tools"] = None
    with open(path, "rb") as fh:
        head_bytes = fh.read(head)
        tail_bytes = b""
        if size > head:
            fh.seek(max(head, size - tail))
            tail_bytes = fh.read()
    thumbs = _Thumbs()
    for chunk, use_thumbs in ((head_bytes, True), (tail_bytes, False)):
        for line in chunk.decode("utf-8", "replace").splitlines():
            if not line or line[0] != ";":
                continue
            if use_thumbs and thumbs.feed(line):
                continue
            _comment_meta(line, meta)
    meta["thumbnail"] = thumbs.best()
    return meta
