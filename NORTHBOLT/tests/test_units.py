import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import make_gcode  # noqa: E402
from northbolt.gcode import PrintState, Tracker, clean, host_action, parse, parse_m114, scan_file  # noqa: E402
from northbolt.hardware import frame  # noqa: E402
from northbolt.protocol import checksum  # noqa: E402
from northbolt.recovery import RecoveryStore, build_plan, resume_commands  # noqa: E402
from northbolt.config import DEFAULTS, deep_merge  # noqa: E402


class GcodeTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean("G1 X10 Y5 ; move"), "G1 X10 Y5")
        self.assertEqual(clean("; only comment"), "")
        self.assertEqual(clean("N12 G1 X1*45"), "G1 X1")
        self.assertEqual(clean("   "), "")

    def test_parse(self):
        self.assertEqual(parse("G28 X Y"), ("G28", {"X": None, "Y": None}))
        self.assertEqual(parse("M104 T1 S200"), ("M104", {"T": 1.0, "S": 200.0}))
        self.assertEqual(parse("T1"), ("T1", {}))
        self.assertEqual(parse("G1 X-1.5 E.4"), ("G1", {"X": -1.5, "E": 0.4}))
        self.assertEqual(parse("M117 Hello X1")[1], {})

    def test_host_actions(self):
        self.assertEqual(host_action("M600")[0], "pause")
        self.assertEqual(host_action("M0")[0], "pause")
        self.assertEqual(host_action("@pause")[0], "pause")
        self.assertEqual(host_action("M73 P50 R10"), ("progress", {"P": 50.0, "R": 10.0}))
        self.assertEqual(host_action("M117 Printing"), ("message", "Printing"))
        self.assertIsNone(host_action("G1 X1"))

    def test_tracker_modes_and_layers(self):
        t = Tracker()
        for c in ["G28", "G90", "M82", "G92 E0", "G1 Z0.2 F600", "G1 X10 Y10 F3000"]:
            t.feed(c)
        self.assertEqual(t.state.layer, 0)
        r = t.feed("G1 X20 Y10 E1.5 F1200")
        self.assertTrue(r.layer_change)
        self.assertEqual(t.state.layer, 1)
        t.feed("G1 E0.7 F2400")          # retract
        t.feed("G1 Z0.6")                # z-hop: no extrusion, no new layer
        t.feed("G1 Z0.4")
        t.feed("G1 E1.5")                # unretract (no XY): no new layer
        self.assertEqual(t.state.layer, 1)
        r = t.feed("G1 X30 E2.0")
        self.assertTrue(r.layer_change)
        self.assertEqual(t.state.layer_z, 0.4)
        t.feed("M83")
        t.feed("G1 X40 E0.5")
        self.assertAlmostEqual(t.state.e, 2.5)
        t.feed("G91")
        t.feed("G1 X1 Y1")
        self.assertEqual((t.state.x, t.state.y), (41.0, 11.0))
        t.feed("M104 T1 S210")
        t.feed("T1")
        t.feed("M106 S128")
        self.assertEqual((t.state.t1, t.state.tool, t.state.fan), (210.0, 1, 128))
        t.feed("G28 X Y")
        self.assertEqual(t.state.tool, 0)
        self.assertIsNone(t.state.x)

    def test_m114(self):
        p = parse_m114(["X:10.00 Y:20.50 Z:0.30 E:12.10 Count X:800 Y:1640 Z:240"])
        self.assertEqual(p, {"x": 10.0, "y": 20.5, "z": 0.3, "e": 12.1})

    def test_scan(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.gcode")
            make_gcode(path, layers=5, segs=6)
            meta = scan_file(path)
            self.assertEqual(meta["est_time_s"], 3723)
            self.assertEqual(meta["layers"], 5)
            self.assertAlmostEqual(meta["max_z"], 1.0)
            self.assertTrue(meta["thumbnail"].startswith("data:image/png;base64,"))


class ProtocolTests(unittest.TestCase):
    def test_checksum(self):
        # Marlin: XOR of every byte before '*'
        self.assertEqual(checksum("N1 G28"), 0x4E ^ 0x31 ^ 0x20 ^ 0x47 ^ 0x32 ^ 0x38)

    def test_i2c_frame(self):
        f = frame(0xCC, 0, 255, 0, 0)
        self.assertEqual(f[:5], [0xCC, 0, 255, 0, 0])
        self.assertEqual(f[5], ((0xCC ^ 0 ^ 255 ^ 0 ^ 0) + 4) & 0xFF)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "p.gcode")
        self.lines = make_gcode(self.path, layers=6, segs=40)
        self.store = RecoveryStore(self.dir.name)
        self.store.begin(self.path, "p.gcode")

    def tearDown(self):
        self.dir.cleanup()

    def _checkpoint_at_layer(self, layer):
        t = Tracker()
        offset = 0
        with open(self.path, "rb") as fh:
            idx = 0
            while True:
                off = fh.tell()
                raw = fh.readline()
                if not raw:
                    break
                idx += 1
                c = clean(raw.decode())
                if not c or host_action(c):
                    continue
                before = t.state.copy()
                r = t.feed(c)
                if r.layer_change and t.state.layer == layer:
                    return idx, off, before
        raise AssertionError("layer not found")

    def test_plan_backs_off_from_progress(self):
        idx, off, before = self._checkpoint_at_layer(3)
        self.store.checkpoint(idx, off, before, 100)
        self.store.progress(idx + 35, 0, 120)
        plan = build_plan(RecoveryStore(self.dir.name).data, 26)
        self.assertGreater(plan.line_index, idx)
        self.assertLess(plan.line_index, idx + 35)
        self.assertIsNotNone(plan.state.x)
        self.assertEqual(plan.state.layer, 3)

    def test_plan_uses_checkpoint_when_progress_is_close(self):
        idx, off, before = self._checkpoint_at_layer(4)
        self.store.checkpoint(idx, off, before, 100)
        self.store.progress(idx + 5, 0, 101)
        plan = build_plan(self.store.data, 26)
        self.assertEqual(plan.line_index, idx)
        self.assertEqual(plan.offset, off)

    def test_synced_head_sets_floor_and_height(self):
        idx, off, before = self._checkpoint_at_layer(3)
        self.store.checkpoint(idx, off, before, 100)
        # A synced Z move 50 lines later: nothing before it may be skipped or re-done.
        self.store.head(before.z + 0.2, 0, idx + 50, 0, 110)
        plan = build_plan(self.store.data, 26)
        self.assertEqual(plan.line_index, idx + 50)
        self.assertAlmostEqual(plan.head_z, before.z + 0.2)
        # Unsynced progress after that backs off, but not below the synced line.
        self.store.progress(idx + 60, 0, 111)
        plan = build_plan(self.store.data, 26)
        self.assertEqual(plan.line_index, idx + 50)
        self.store.progress(idx + 120, 0, 112)
        plan = build_plan(self.store.data, 26)
        self.assertGreater(plan.line_index, idx + 50)
        self.assertLess(plan.line_index, idx + 120)

    def test_changed_file_refused(self):
        idx, off, before = self._checkpoint_at_layer(2)
        self.store.checkpoint(idx, off, before, 10)
        with open(self.path, "a") as fh:
            fh.write("G1 X1\n")
        with self.assertRaises(ValueError):
            build_plan(self.store.data, 26)

    def test_resume_commands(self):
        s = PrintState(x=10, y=20, z=3.2, e=55.5, f=1800, tool=1, fan=255, t0=0, t1=210, bed=60)
        settings = {"recovery": DEFAULTS["recovery"], "z_max": 210, "lift_mechanism": True}
        cmds = resume_commands(s, settings, {"e0": 0.4, "e1": 0.9})
        self.assertIn("G92 Z2.7", cmds)               # 3.2 - (0.9 - 0.4)
        self.assertIn("G28 X Y", cmds)
        self.assertNotIn("G28", cmds)
        self.assertLess(cmds.index("M190 S60"), cmds.index("G92 Z2.7"))
        self.assertLess(cmds.index("G28 X Y"), cmds.index("T1"))
        self.assertEqual(cmds[-3:], ["G92 E55.5", "M106 S255", "G1 F1800"])
        s.tool = 0
        self.assertIn("G92 Z3.2", resume_commands(s, settings, {"e0": 0.4, "e1": 0.9}))


class WifiTests(unittest.TestCase):
    def test_split_terse(self):
        from northbolt.wifi import split_terse
        self.assertEqual(split_terse("*:My\:Net:80:WPA2"), ["*", "My:Net", "80", "WPA2"])
        self.assertEqual(split_terse(r" :back\\slash:5:--"), [" ", "back\\slash", "5", "--"])
        self.assertEqual(split_terse("a::b"), ["a", "", "b"])

    def test_real_backend_builds_safe_commands(self):
        import asyncio
        from northbolt.wifi import Wifi, WifiError
        calls = []

        async def runner(*args, timeout=20):
            calls.append(args)
            if args[:5] == ("nmcli", "-t", "-f", "NAME,TYPE", "connection"):
                return 0, "", ""
            if "connect" in args:
                return 4, "", "Error: Connection activation failed: Secrets were required, but not provided."
            return 0, "", ""

        w = Wifi(runner=runner)
        with self.assertRaises(WifiError) as ctx:
            asyncio.run(w.connect("Cafe; rm -rf /", "password123"))
        self.assertIn("Wrong password", str(ctx.exception))
        connect = next(c for c in calls if "connect" in c)
        # The SSID and password are separate argv items: no shell, nothing to inject.
        self.assertEqual(connect[-4:], ("connect", "Cafe; rm -rf /", "password", "password123"))
        self.assertIn(("nmcli", "connection", "delete", "id", "Cafe; rm -rf /"), calls)
        with self.assertRaises(WifiError):
            asyncio.run(w.set_country("GB; reboot"))


class UsbTests(unittest.TestCase):
    LSBLK = {"blockdevices": [
        # the Pi's own SD card
        {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk", "tran": None, "rm": False, "hotplug": False,
         "children": [{"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "type": "part", "fstype": "ext4", "mountpoint": "/"}]},
        # a flash drive with a partition table (older lsblk prints "1" instead of true)
        {"name": "sda", "path": "/dev/sda", "type": "disk", "tran": "usb", "rm": "1", "hotplug": "1", "model": "Cruzer",
         "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "vfat", "label": "PRINTS",
                       "size": 16000000000, "mountpoint": None}]},
        # a card reader with an unformatted-looking partition and a "superfloppy" stick
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "tran": "usb", "rm": True, "fstype": "exfat", "label": None,
         "model": "SD Reader", "size": 64000000000, "mountpoint": None},
        # booting from a USB SSD: must never be offered
        {"name": "sdc", "path": "/dev/sdc", "type": "disk", "tran": "usb", "rm": False, "hotplug": True,
         "children": [{"name": "sdc1", "path": "/dev/sdc1", "type": "part", "fstype": "vfat", "mountpoint": "/boot/firmware"},
                      {"name": "sdc2", "path": "/dev/sdc2", "type": "part", "fstype": "ext4", "mountpoint": "/"}]},
    ]}

    def test_parse_lsblk(self):
        from northbolt.usb import parse_lsblk
        vols = parse_lsblk(self.LSBLK)
        self.assertEqual([v["id"] for v in vols], ["sda1", "sdb"])
        self.assertEqual(vols[0]["label"], "PRINTS")
        self.assertEqual(vols[0]["disk"], "/dev/sda")
        self.assertEqual(vols[1]["label"], "SD Reader")

    def test_paths_stay_on_the_drive(self):
        from northbolt.usb import UsbError, resolve_inside
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            self.assertEqual(resolve_inside(d, "sub"), os.path.realpath(os.path.join(d, "sub")))
            self.assertEqual(resolve_inside(d, "/sub"), os.path.realpath(os.path.join(d, "sub")))
            for bad in ("..", "../etc", "sub/../../x"):
                with self.assertRaises(UsbError):
                    resolve_inside(d, bad)

    def test_unique_name(self):
        from northbolt.usb import unique_name
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.gcode")
            with open(src, "w") as fh:
                fh.write("G28\n")
            dest_dir = os.path.join(d, "printer")
            os.makedirs(dest_dir)
            self.assertEqual(unique_name(dest_dir, "part.gcode", src), ("part.gcode", False))
            with open(os.path.join(dest_dir, "part.gcode"), "w") as fh:
                fh.write("G28\n")
            self.assertEqual(unique_name(dest_dir, "part.gcode", src), ("part.gcode", True))     # identical: reuse
            with open(os.path.join(dest_dir, "part.gcode"), "w") as fh:
                fh.write("G1 X1\n")
            self.assertEqual(unique_name(dest_dir, "part.gcode", src), ("part (2).gcode", False))

    def test_quick_scan(self):
        from northbolt.gcode import quick_scan
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.gcode")
            make_gcode(path, layers=5, segs=6)
            meta = quick_scan(path)
            self.assertEqual(meta["est_time_s"], 3723)       # from the end of the file
            self.assertEqual(meta["filament_g"], 12.34)
            self.assertTrue(meta["thumbnail"].startswith("data:image/png;base64,"))


class LevelingTests(unittest.TestCase):
    G29_OUTPUT = [
        "echo:busy: processing",
        "Bilinear Leveling Grid:",
        "      0      1      2      3      4",
        " 0 +0.010 +0.020 +0.030 +0.040 +0.050",
        " 1 +0.060 +0.070 +0.080 +0.090 +0.100",
        " 2 +0.110 +0.120  ===== +0.140 +0.150",
        " 3 +0.160 +0.170 +0.180 +0.190 +0.200",
        " 4 +0.210 +0.220 +0.230 +0.240 -0.250",
        "Subdivided with CATMULL ROM Leveling Grid:",
        "        0        1        2",
        " 0 +0.01000 +0.01417 +0.01833",
        "echo:Bed Leveling On",
    ]

    def cfg(self, **kw):
        from northbolt.config import DEFAULTS
        import copy
        c = copy.deepcopy(DEFAULTS["leveling"])
        c.update(kw)
        return c

    def test_parse_grid_takes_only_the_real_grid(self):
        from northbolt.leveling import parse_grid
        mesh = parse_grid(self.G29_OUTPUT)
        self.assertEqual(len(mesh), 5)
        self.assertEqual(mesh[0], [0.01, 0.02, 0.03, 0.04, 0.05])
        self.assertIsNone(mesh[2][2])              # "=====" is an unprobed point
        self.assertEqual(mesh[4][4], -0.25)
        self.assertIsNone(parse_grid(["echo:Bed Leveling Off"]))

    def plane_mesh(self, fn):
        cfg = self.cfg()
        xs = [20 + i * 40 for i in range(5)]
        ys = [32 + j * (255 - 32) / 4 for j in range(5)]
        return [[round(fn(x, y), 4) for x in xs] for y in ys], cfg

    def test_screw_instructions(self):
        from northbolt.leveling import analyze
        # Bed rises 0.002 mm per mm towards the back and 0.001 mm per mm towards the right.
        mesh, cfg = self.plane_mesh(lambda x, y: 0.001 * (x - 100) + 0.002 * y)
        r = analyze(mesh, cfg)
        front, back_left, back_right = r["screws"]
        self.assertEqual(front["action"], "reference")
        # Back left (x 15, y 265): 0.002*265 + 0.001*(15-100) = 0.445 higher than front (x 100, y 0)
        self.assertAlmostEqual(back_left["delta"], 0.445, places=3)
        self.assertEqual(back_left["action"], "lower")
        self.assertEqual(back_left["direction"], "cw")
        self.assertAlmostEqual(back_left["turns"], 0.89, places=2)      # 0.445 / 0.5 mm per turn
        self.assertAlmostEqual(back_right["delta"], 0.615, places=3)
        self.assertLess(r["warp"], 0.001)
        self.assertFalse(r["level"])
        # A raised corner turns the other way; flipping the direction setting flips it again.
        mesh, cfg = self.plane_mesh(lambda x, y: -0.001 * y)
        s = analyze(mesh, cfg)["screws"][1]
        self.assertEqual((s["action"], s["direction"]), ("raise", "ccw"))
        s = analyze(mesh, self.cfg(clockwise_lowers=False))["screws"][1]
        self.assertEqual(s["direction"], "cw")

    def test_flat_bed_with_warp_is_level(self):
        from northbolt.leveling import analyze
        mesh, cfg = self.plane_mesh(lambda x, y: 0.03 * (((x - 100) / 80) ** 2 + ((y - 143.5) / 111.5) ** 2))
        r = analyze(mesh, cfg)
        self.assertTrue(r["level"], r["screws"])
        self.assertGreater(r["warp"], 0.03)


class FirmwareTests(unittest.TestCase):
    def test_check_hex(self):
        from helpers import fake_firmware, make_hex
        from northbolt.firmware import FirmwareError, check_hex
        info = check_hex(fake_firmware("Marlin 1.1.9.9 (Github, NB1 safety fixes)"))
        self.assertEqual(info["name"], "Marlin 1.1.9.9 (Github, NB1 safety fixes)")
        self.assertGreater(info["end"], 65536)                 # extended segment records handled
        bad = bytearray(fake_firmware())
        i = bad.index(b"\n") + 12                               # flip a data digit on line 2
        bad[i:i + 1] = b"0" if bad[i:i + 1] != b"0" else b"1"
        with self.assertRaises(FirmwareError):
            check_hex(bytes(bad))
        with self.assertRaises(FirmwareError) as ctx:          # e.g. the LED board's sketch
            check_hex(make_hex(b"\x0c\x94" * 4000))
        self.assertIn("isn't printer firmware", str(ctx.exception))
        with self.assertRaises(FirmwareError):                 # would overwrite the bootloader
            check_hex(fake_firmware(size=0x3E000 + 16))
        with self.assertRaises(FirmwareError):
            check_hex(b"PK\x03\x04 not a hex file")

    def test_real_build_if_present(self):
        from northbolt.firmware import check_hex
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "firmware-NB1", "skriware2-printer-NB1.hex")
        if not os.path.exists(path):
            self.skipTest("no local build")
        with open(path, "rb") as fh:
            info = check_hex(fh.read())
        self.assertIn("NB1 safety fixes", info["name"])

    def test_parse_repo(self):
        from northbolt.firmware import FirmwareError, parse_repo
        for text in ("https://github.com/ppeterz/SkriwareFirmware", "github.com/ppeterz/SkriwareFirmware.git",
                     "ppeterz/SkriwareFirmware", "https://github.com/ppeterz/SkriwareFirmware/"):
            self.assertEqual(parse_repo(text), "ppeterz/SkriwareFirmware")
        with self.assertRaises(FirmwareError):
            parse_repo("https://example.com/not a repo")


class ConfigTests(unittest.TestCase):
    def test_deep_merge(self):
        merged = deep_merge(DEFAULTS, {"pause": {"lift": 5}})
        self.assertEqual(merged["pause"]["lift"], 5)
        self.assertEqual(merged["pause"]["retract"], DEFAULTS["pause"]["retract"])


class LedTests(unittest.TestCase):
    """The LED policy sends the colour and effect set for each printer state, only when it changes."""

    class Board:
        def __init__(self):
            self.calls = []

        def lights(self, mode, rgb=(0, 0, 0)):
            self.calls.append(("lights", mode, tuple(rgb)))

        def pads(self, count, rgb):
            self.calls.append(("pads", count, tuple(rgb)))

        def center(self, rgb):
            self.calls.append(("center", tuple(rgb)))

    def make(self, **leds):
        from northbolt.hardware import LedPolicy
        settings = deep_merge(DEFAULTS, {"leds": dict({"brightness": 1.0}, **leds)})
        board = self.Board()
        return LedPolicy(board, settings), board, settings

    def test_each_state_uses_its_own_colour_and_effect(self):
        from northbolt.hardware import hex_rgb
        policy, board, _ = self.make(events={"printing": {"mode": "on", "color": "#ffffff"},
                                             "done": {"mode": "breathe", "color": "#00ff00"},
                                             "error": {"mode": "flash", "color": "#ff0000"}})
        policy.apply("printing")
        policy.apply("printing")                       # unchanged: nothing sent again
        policy.apply("done")
        policy.apply("error")
        self.assertEqual(board.calls[0], ("lights", 0, (255, 255, 255)))
        self.assertEqual(board.calls[1], ("lights", 4, (0, 255, 0)))     # breathe
        self.assertEqual(board.calls[2], ("lights", 5, (255, 0, 0)))     # flash
        self.assertEqual(len(board.calls), 3)
        self.assertEqual(hex_rgb("#0af"), (0, 170, 255))
        self.assertEqual(hex_rgb("nonsense", (1, 2, 3)), (1, 2, 3))

    def test_a_new_colour_for_the_same_state_is_sent_at_once(self):
        policy, board, settings = self.make()
        policy.apply("idle")
        settings["leds"]["events"]["idle"]["color"] = "#123456"
        policy.apply("idle")
        self.assertEqual(board.calls[-1], ("lights", 0, (0x12, 0x34, 0x56)))

    def test_off_effect_brightness_and_master_switch(self):
        policy, board, settings = self.make(brightness=0.5, events={"idle": {"mode": "off"}})
        policy.apply("idle")
        self.assertEqual(board.calls[-1], ("lights", 1, (0, 0, 0)))
        policy.apply("printing")
        self.assertEqual(board.calls[-1], ("lights", 0, (127, 127, 127)))   # white at half brightness
        settings["leds"]["enabled"] = False
        policy.apply("printing")
        self.assertEqual(board.calls[-1][:2], ("lights", 1))

    def test_work_light_overrides_and_test_previews(self):
        policy, board, settings = self.make(override={"enabled": True, "mode": "on", "color": "#ff00ff"})
        policy.apply("printing", progress=50)
        self.assertEqual(board.calls, [("lights", 0, (255, 0, 255))])     # no progress bar over the work light
        settings["leds"]["override"]["enabled"] = False
        policy.test("done", seconds=60)
        policy.apply("printing", progress=50)
        self.assertEqual(board.calls[-1], ("lights", 0, (0, 255, 60)))    # the test wins while it lasts
        with self.assertRaises(ValueError):
            policy.test("disco")

    def test_progress_uses_its_colours(self):
        policy, board, _ = self.make(progress_color="#00ff00", center_color="#0000ff")
        policy.apply("printing", progress=50)
        self.assertIn(("pads", 6, (0, 255, 0)), board.calls)
        self.assertIn(("center", (0, 0, 255)), board.calls)


if __name__ == "__main__":
    unittest.main()
