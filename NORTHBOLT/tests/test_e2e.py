"""End-to-end tests: the real host process against the simulated Skriware firmware.

Run from the NORTHBOLT folder:   python -m unittest discover -s tests -v
"""

import collections
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import Rig, file_commands, make_gcode  # noqa: E402


def extrusion_lines(cmds):
    return [c for c in cmds if c.startswith("G1 X") and " E" in c]


class E2EBase(unittest.TestCase):
    speed = 25
    error_rate = 0.0
    settings = None

    env = None

    def setUp(self):
        self.rig = Rig(speed=self.speed, error_rate=self.error_rate, settings=self.settings, env=self.env).start()

    def tearDown(self):
        if os.environ.get("KEEP_LOGS"):
            print(self.rig.tail())
        self.rig.stop()

    def make(self, name="part.gcode", **kw):
        path = os.path.join(self.rig.data, "src-" + name)
        lines = make_gcode(path, **kw)
        status, resp = self.rig.upload(path, name)
        self.assertEqual(status, 200)
        self.rig.wait(lambda s: True, 2)
        # wait for the background metadata scan
        end = time.time() + 20
        while time.time() < end:
            files = self.rig.get("/api/files")["files"]
            f = [x for x in files if x["name"] == name]
            if f and not f[0]["scanning"] and f[0]["est_time_s"]:
                break
            time.sleep(0.2)
        return file_commands(lines)

    def start_print(self, name="part.gcode"):
        self.rig.post("/api/print", {"file": name})
        self.rig.wait(lambda s: s["job"]["state"] in ("printing", "finished"), 20, "print start")

    def wait_layer(self, layer):
        return self.rig.wait(lambda s: s["job"]["state"] == "printing" and (s["job"]["layer"] or 0) >= layer,
                             90, f"layer {layer}")

    def wait_done(self, timeout=180):
        return self.rig.wait(lambda s: s["job"]["state"] in ("finished", "error", "cancelled"), timeout, "job end")

    def assert_each_once(self, expected, history):
        counts = collections.Counter(history)
        missing = [c for c in extrusion_lines(expected) if counts[c] == 0]
        dupes = [c for c in extrusion_lines(expected) if counts[c] > 1]
        self.assertEqual(missing, [], "extrusion moves never executed")
        self.assertEqual(dupes, [], "extrusion moves executed twice")


class PrintTests(E2EBase):
    error_rate = 0.03   # reject about 3% of lines with a checksum error to exercise resends

    def test_print_completes_with_resends(self):
        cmds = self.make(layers=8, segs=20)
        files = self.rig.get("/api/files")["files"]
        self.assertEqual(files[0]["est_time_s"], 3723)
        self.assertEqual(files[0]["filament_g"], 12.34)
        self.assertTrue(files[0]["has_thumbnail"])
        self.start_print()
        st = self.wait_done()
        self.assertEqual(st["job"]["state"], "finished", st["job"])
        self.assertGreater(st["connection"]["stats"]["resends"], 0)
        self.assert_each_once(cmds, self.rig.sim_history())
        self.assertIsNone(st["recovery"])

    def test_heats_at_the_back_left_before_the_file(self):
        self.make(layers=3, segs=8)
        self.start_print()
        self.wait_done()
        hist = self.rig.sim_history()
        start = hist.index("G28 X Y")
        self.assertEqual(hist[start:start + 6], ["G28 X Y", "G1 X10 Y265 F6000", "M140 S60", "M104 T0 S200",
                                                 "M190 S60", "M109 T0 S200"])
        self.assertLess(start + 5, hist.index("G28"), "heated before the file's own start code")


class PauseTests(E2EBase):
    def test_pause_park_resume(self):
        cmds = self.make(layers=12, segs=40)
        self.start_print()
        self.wait_layer(3)
        self.rig.post("/api/job/pause")
        st = self.rig.wait(lambda s: s["job"]["state"] == "paused", 30, "paused")
        sim = self.rig.admin("state")
        self.assertAlmostEqual(sim["pos"]["X"], 10.0, places=2)
        self.assertAlmostEqual(sim["pos"]["Y"], 265.0, places=2)
        z_print = st["job"]["z"]
        self.assertAlmostEqual(sim["logical_z"], z_print + 10, places=2)
        self.rig.post("/api/job/resume")
        st = self.wait_done()
        self.assertEqual(st["job"]["state"], "finished", st["job"])
        self.assert_each_once(cmds, self.rig.sim_history())

    def test_m600_in_file_pauses(self):
        self.make(layers=6, segs=10, m600_at=3)
        self.start_print()
        st = self.rig.wait(lambda s: s["job"]["state"] == "paused", 60, "paused by M600")
        self.assertIn("Filament change", st["job"]["pause_reason"])
        self.rig.post("/api/job/resume")
        self.assertEqual(self.wait_done()["job"]["state"], "finished")

    def test_runout_pauses_and_clears_sensor(self):
        cmds = self.make(layers=12, segs=40)
        self.start_print()
        self.wait_layer(3)
        self.rig.admin("runout 0")
        st = self.rig.wait(lambda s: s["job"]["state"] == "paused", 30, "runout pause")
        self.assertEqual(st["job"]["runout_side"], "left")
        self.assertIn("ran out", st["job"]["pause_reason"])
        self.rig.post("/api/job/resume")
        st = self.wait_done()
        self.assertEqual(st["job"]["state"], "finished")
        hist = self.rig.sim_history()
        self.assertIn("M63 L", hist)
        self.assert_each_once(cmds, hist)

    def test_cancel_stops_and_cools(self):
        cmds = self.make(layers=30, segs=40)
        self.start_print()
        self.wait_layer(3)
        self.rig.post("/api/job/cancel")
        st = self.rig.wait(lambda s: s["job"]["state"] == "cancelled", 40, "cancelled")
        sim = self.rig.admin("state")
        self.assertEqual(sim["temps"]["t0"][1], 0)
        self.assertEqual(sim["temps"]["bed"][1], 0)
        hist = self.rig.sim_history()
        self.assertIn("G28 X Y", hist[-5:])
        executed = set(hist)
        self.assertNotIn(extrusion_lines(cmds)[-1], executed)
        # the link still works after the M411 resync
        resp = self.rig.post("/api/console", {"command": "M114"})
        self.assertTrue(any("X:" in line for line in resp["response"]))
        self.assertIsNone(st["recovery"])


class PowerLossTests(E2EBase):
    settings = {"recovery": {"min_interval_s": 0, "progress_interval_s": 0.5}}

    @staticmethod
    def _layer(cmds, layer):
        """make_gcode's commands for one layer: from its Z move down to the layer to its Z-hop at the end."""
        start = cmds.index(f"G1 Z{round(0.2 * layer, 3)} F600")
        end = next(i for i in range(start + 1, len(cmds)) if cmds[i].startswith("G1 Z"))
        return cmds[start:end + 1]

    def _power_loss_cycle(self, cmds, tool, when, line, rises=0.0):
        """Print, lose power just `when` ("before"/"after") the printer runs `line`, resume and check.

        rises: how much higher the nozzle may come back than it was. Power lost around a Z move can't be
        told apart from power lost just before or just after it, so the host assumes the lower height:
        the nozzle may then print up to one Z move higher for a moment, but never lower, into the part."""
        self.rig.admin(f"cut {when} {line}")
        self.start_print()
        # Both lose power: the printer board dies around `line`, then the Pi dies mid-write.
        self.rig.wait_sim(lambda s: not s["power"], 120, f"power cut {when} {line}")
        self.rig.kill_host()
        lost_at = self.rig.admin("state")
        offset_before = lost_at["nozzle"] - lost_at["logical_z"]
        old_hist = collections.Counter(self.rig.sim_history())
        self.rig.admin("poweron")
        self.rig.start_host()
        st = self.rig.wait(lambda s: s["recovery"] is not None, 20, "recovery offer")
        self.assertEqual(st["recovery"]["file"], "part.gcode")
        self.assertEqual(st["recovery"]["tool"], tool)
        self.rig.post("/api/recovery/resume")
        self.rig.wait(lambda s: s["job"]["state"] == "printing", 60, "resumed printing")
        time.sleep(0.5)
        after = self.rig.admin("state")
        self.assertEqual(after["tool"], tool)
        # The nozzle-to-G-code-Z relationship must be the same as before the outage (or `rises` higher).
        drift = (after["nozzle"] - after["logical_z"]) - offset_before
        self.assertAlmostEqual(drift, rises, places=3, msg=f"Z drifted by {drift:.3f} mm, expected {rises:.3f} mm")
        st = self.wait_done()
        self.assertEqual(st["job"]["state"], "finished", st["job"])
        hist = self.rig.sim_history()
        # Z was never homed after the outage: only "G28 X Y" in the resumed session.
        self.assertNotIn("G28", hist)
        self.assertIn("G28 X Y", hist)
        # Every extrusion move happened at least once overall, and the overlap is small.
        resumed = collections.Counter(hist)
        ext = extrusion_lines(cmds)
        first_resumed = next(i for i, c in enumerate(ext) if resumed[c])
        self.assertGreater(first_resumed, 0)
        self.assertTrue(all(resumed[c] for c in ext[first_resumed:]), "moves skipped after resume")
        skipped = [c for c in ext if not old_hist[c] and not resumed[c]]
        self.assertEqual(skipped, [], "moves lost across the outage")
        overlap = [c for c in ext if old_hist[c] and resumed[c]]
        self.assertLessEqual(len(overlap), 60, "resume backed off too far")
        self.assertIsNone(st["recovery"])
        return first_resumed, lost_at

    def _mid_layer(self, tool):
        cmds = self.make(layers=25, segs=60, tool=tool)
        layer = self._layer(cmds, 7)
        self._power_loss_cycle(cmds, tool, "after", layer[len(layer) // 2])

    def test_power_loss_resume_left_nozzle(self):
        self._mid_layer(0)

    def test_power_loss_resume_right_nozzle(self):
        self._mid_layer(1)

    # Around Z moves. The host syncs (M400) and saves the head height before each one, so these are the
    # moments a save can't describe exactly: the printer may or may not have run the move yet.
    def test_power_lost_just_before_a_z_hop(self):
        cmds = self.make(layers=8, segs=20)
        self._power_loss_cycle(cmds, 0, "before", self._layer(cmds, 4)[-1])

    def test_power_lost_as_a_z_hop_runs(self):
        cmds = self.make(layers=8, segs=20)
        self._power_loss_cycle(cmds, 0, "after", self._layer(cmds, 4)[-1], rises=0.4)

    def test_power_lost_just_before_a_layer_change_down(self):
        cmds = self.make(layers=8, segs=20)
        self._power_loss_cycle(cmds, 0, "before", self._layer(cmds, 5)[0], rises=0.2)


class ZOffsetTests(E2EBase):
    settings = {"zoffset": {"heat": False}}

    def _lower_to_paper(self):
        st = self.rig.wait(lambda s: s["zoffset"]["state"] == "adjusting", 60, "wizard ready")
        for _ in range(200):
            nozzle = self.rig.admin("state")["nozzle"]
            if abs(nozzle) < 0.004:
                break
            for step in (1.0, 0.1, 0.05, 0.01):
                if nozzle >= step - 1e-6:
                    self.rig.post("/api/zoffset/step", {"delta": -step})
                    break
            else:
                self.rig.post("/api/zoffset/step", {"delta": 0.01})
        self.assertLess(abs(self.rig.admin("state")["nozzle"]), 0.004)
        return st

    def test_left_nozzle_paper_test(self):
        self.rig.post("/api/zoffset/start", {"tool": 0})
        self._lower_to_paper()
        self.assertFalse(self.rig.admin("state")["soft"], "soft endstops should be off while measuring")
        self.rig.post("/api/zoffset/accept")
        st = self.rig.wait(lambda s: s["zoffset"]["state"] == "done", 30, "saved")
        sim = self.rig.admin("state")
        self.assertTrue(sim["soft"])
        self.assertAlmostEqual(sim["eeprom"]["ho"], 1.30, places=3)   # equals the simulated gap
        self.assertAlmostEqual(st["offsets"]["ho"], 1.30, places=2)
        # Corner check goes to Z0
        self.rig.post("/api/zoffset/check", {"point": "front_left"})
        sim = self.rig.admin("state")
        self.assertAlmostEqual(sim["nozzle"], 0.0, places=3)
        self.rig.post("/api/zoffset/finish")
        # A fresh home and a move to Z0 now touches the bed
        self.rig.post("/api/console", {"command": "G28\nG1 Z0 F300\nM400"})
        self.assertAlmostEqual(self.rig.admin("state")["nozzle"], 0.0, places=3)

    def test_right_nozzle_paper_test(self):
        self.rig.post("/api/zoffset/start", {"tool": 1})
        self._lower_to_paper()
        self.assertEqual(self.rig.admin("state")["tool"], 1)
        self.rig.post("/api/zoffset/accept")
        self.rig.wait(lambda s: s["zoffset"]["state"] == "done", 30, "saved")
        sim = self.rig.admin("state")
        self.assertEqual(sim["tool"], 0, "should end on the left nozzle")
        self.assertAlmostEqual(sim["eeprom"]["e1"], 0.95, places=3)   # gap - drop
        self.assertAlmostEqual(sim["eeprom"]["ho"], 0.4, places=3)    # left offset untouched
        self.rig.post("/api/zoffset/finish")
        self.rig.post("/api/console", {"command": "G28\nT1\nG1 Z0 F300\nM400"})
        self.assertAlmostEqual(self.rig.admin("state")["nozzle"], 0.0, places=3)

    def test_cancel_restores_soft_endstops(self):
        self.rig.post("/api/zoffset/start", {"tool": 0})
        self.rig.wait(lambda s: s["zoffset"]["state"] == "adjusting", 60, "wizard ready")
        self.rig.post("/api/zoffset/cancel")
        st = self.rig.wait(lambda s: s["zoffset"]["state"] == "idle", 30, "cancelled")
        self.assertTrue(self.rig.admin("state")["soft"])
        self.assertAlmostEqual(self.rig.admin("state")["eeprom"]["ho"], 0.4)
        self.assertIsNone(st["zoffset"]["error"])


class ApiTests(E2EBase):
    def test_octoprint_upload_and_version(self):
        v = self.rig.get("/api/version")
        self.assertTrue(v["text"].startswith("OctoPrint"))
        path = os.path.join(self.rig.data, "slicer.gcode")
        make_gcode(path, layers=2, segs=4)
        status, resp = self.rig.upload(path, "from slicer.gcode", url="/api/files/local", extra={"print": "true"})
        self.assertEqual(status, 201)
        st = self.wait_done()
        self.assertEqual(st["job"]["state"], "finished")
        self.assertEqual(st["job"]["file"], "from slicer.gcode")

    def test_rejects_bad_names_and_blocks_moves_while_printing(self):
        path = os.path.join(self.rig.data, "x.txt")
        with open(path, "w") as fh:
            fh.write("G28\n")
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self.rig.upload(path, "../../evil.txt")
        self.make(layers=20, segs=40)
        self.start_print()
        self.wait_layer(2)
        r = self.rig.post("/api/move", {"x": 10}, expect_ok=False)
        self.assertEqual(r["status"], 400)
        r = self.rig.post("/api/console", {"command": "G1 X0"}, expect_ok=False)
        self.assertEqual(r["status"], 400)
        self.rig.post("/api/temp", {"heater": "t0", "target": 205})   # allowed while printing
        self.rig.post("/api/speed", {"value": 120})
        self.rig.post("/api/job/cancel")
        self.rig.wait(lambda s: s["job"]["state"] == "cancelled", 40)

    def test_emergency_stop_and_reconnect(self):
        self.rig.post("/api/connection/estop")
        st = self.rig.wait(lambda s: s["connection"]["halted"], 10, "halted")
        self.assertTrue(self.rig.admin("state")["dead"])
        r = self.rig.post("/api/move", {"z": 5}, expect_ok=False)
        self.assertEqual(r["status"], 400)
        self.rig.post("/api/connection/reconnect")
        st = self.rig.wait(lambda s: s["connection"]["ready"] and not s["connection"]["halted"], 30, "reset")
        self.assertFalse(self.rig.admin("state")["dead"])

    def test_touch_dead_zone_setting(self):
        self.assertIsNone(self.rig.state()["config"]["touch_dead_zone"])
        self.rig.post("/api/settings", {"touch_dead_zone": {"top": "200", "bottom": 355}})
        self.assertEqual(self.rig.get("/api/settings")["touch_dead_zone"], {"top": 200, "bottom": 355})
        self.rig.wait(lambda s: s["config"]["touch_dead_zone"] == {"top": 200, "bottom": 355}, 5, "pushed to the screen")
        for bad in ({"top": 300, "bottom": 200}, {"top": 10}, "middle", {"top": -5, "bottom": 40}):
            r = self.rig.post("/api/settings", {"touch_dead_zone": bad}, expect_ok=False)
            self.assertEqual(r["status"], 400, bad)
        self.assertEqual(self.rig.get("/api/settings")["touch_dead_zone"], {"top": 200, "bottom": 355})
        self.rig.post("/api/settings", {"touch_dead_zone": None})
        self.assertIsNone(self.rig.get("/api/settings")["touch_dead_zone"])

    def test_led_settings_and_test(self):
        self.rig.wait(lambda s: s["leds"]["event"] == "idle", 10, "the LED state")    # set once a second
        self.rig.post("/api/settings", {"leds": {"events": {"done": {"color": "#00ff00", "mode": "breathe"}}}})
        leds = self.rig.get("/api/settings")["leds"]
        self.assertEqual(leds["events"]["done"], {"mode": "breathe", "color": "#00ff00"})
        self.assertEqual(leds["events"]["printing"]["color"], "#ffffff")       # the others are untouched
        for bad in ({"events": {"party": {"mode": "on"}}}, {"events": {"done": {"mode": "rainbow"}}},
                    {"events": {"done": {"color": "green"}}}, {"brightness": 3}, {"colour": "#fff"}):
            r = self.rig.post("/api/settings", {"leds": bad}, expect_ok=False)
            self.assertEqual(r["status"], 400, bad)
        self.rig.post("/api/leds/test", {"event": "done"})
        r = self.rig.post("/api/leds/test", {"event": "disco"}, expect_ok=False)
        self.assertEqual(r["status"], 400)

    def test_text_size_setting(self):
        self.assertEqual(self.rig.state()["config"]["text_size"], 75)
        self.rig.post("/api/settings", {"text_size": 90})
        self.rig.wait(lambda s: s["config"]["text_size"] == 90, 5, "pushed to the screen")
        for bad in (10, 300, "big", True):
            r = self.rig.post("/api/settings", {"text_size": bad}, expect_ok=False)
            self.assertEqual(r["status"], 400, bad)
        self.assertEqual(self.rig.get("/api/settings")["text_size"], 90)
        self.assertEqual(self.rig.state()["config"]["ui_size"], 80)
        self.rig.post("/api/settings", {"ui_size": 70})
        self.rig.wait(lambda s: s["config"]["ui_size"] == 70, 5, "pushed to the screen")
        r = self.rig.post("/api/settings", {"ui_size": 20}, expect_ok=False)
        self.assertEqual(r["status"], 400)

    def test_boot_intro_setting(self):
        self.assertTrue(self.rig.state()["config"]["boot_intro"])
        self.rig.post("/api/settings", {"boot_intro": False})
        self.rig.wait(lambda s: s["config"]["boot_intro"] is False, 5, "pushed to the screen")
        for name in ("intro.mp4", "logo-full.png", "logo-wordmark.png", "logo-mark.svg"):
            self.assertTrue(os.path.getsize(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         "northbolt", "static", name)) > 0, name)


class WifiApiTests(E2EBase):
    env = {"NORTHBOLT_FAKE_WIFI": "1"}

    def test_scan_wrong_password_then_connect(self):
        st = self.rig.get("/api/wifi")
        self.assertEqual(st["ssid"], "HomeNet")
        nets = self.rig.get("/api/wifi/networks")["networks"]
        self.assertEqual(nets[0]["ssid"], "HomeNet")      # the connected one comes first
        self.assertTrue(nets[0]["saved"])                # saved under Imager's name "preconfigured"
        self.assertIn("Neighbour's Wi-Fi", [n["ssid"] for n in nets])
        r = self.rig.post("/api/wifi/connect", {"ssid": "Workshop_5G", "password": "wrongpass1"}, expect_ok=False)
        self.assertEqual(r["status"], 400)
        self.assertIn("Wrong password", r["error"])
        nets = self.rig.get("/api/wifi/networks")["networks"]
        self.assertFalse(next(n for n in nets if n["ssid"] == "Workshop_5G")["saved"], "failed attempt left a profile")
        r = self.rig.post("/api/wifi/connect", {"ssid": "Workshop_5G", "password": "short"}, expect_ok=False)
        self.assertIn("8 to 63", r["error"])
        st = self.rig.post("/api/wifi/connect", {"ssid": "Workshop_5G", "password": "northbolt"})
        self.assertEqual(st["ssid"], "Workshop_5G")
        self.assertEqual(st["ip"], "192.168.1.57")
        self.rig.post("/api/wifi/forget", {"ssid": "Workshop_5G"})
        self.assertIsNone(self.rig.get("/api/wifi")["ssid"])
        st = self.rig.post("/api/wifi/connect", {"ssid": "HomeNet"})   # saved: no password needed
        self.assertEqual(st["ssid"], "HomeNet")
        r = self.rig.post("/api/wifi/country", {"code": "x1"}, expect_ok=False)
        self.assertEqual(r["status"], 400)
        self.assertEqual(self.rig.post("/api/wifi/country", {"code": "ng"})["country"], "NG")


class LevelingE2ETests(E2EBase):
    settings = {"leveling": {"heat": False}}

    def wait_level(self, state, timeout=60):
        return self.rig.wait(lambda s: s["leveling"]["state"] == state, timeout, f"leveling {state}")

    def test_measure_turn_screws_and_save(self):
        # Simulated bed: back left 0.30 mm high, back right 0.20 mm low (front is the reference).
        self.rig.post("/api/leveling/start", {"probe": True})
        st = self.wait_level("results")
        r = st["leveling"]["result"]
        front, back_left, back_right = r["screws"]
        self.assertEqual(front["action"], "reference")
        self.assertAlmostEqual(back_left["delta"], 0.30, delta=0.015)
        self.assertEqual((back_left["action"], back_left["direction"]), ("lower", "cw"))
        self.assertAlmostEqual(back_left["turns"], 0.6, delta=0.03)
        self.assertAlmostEqual(back_right["delta"], -0.20, delta=0.015)
        self.assertEqual((back_right["action"], back_right["direction"]), ("raise", "ccw"))
        self.assertFalse(r["level"])
        hist = self.rig.sim_history()
        self.assertIn("G28", hist)
        self.assertIn("G29", hist)
        self.assertIn("M420 S1", hist)
        # Moving is blocked while the assistant is open.
        self.assertEqual(self.rig.post("/api/move", {"x": 5}, expect_ok=False)["status"], 400)

        # Changing the screw thread recalculates without probing again.
        self.rig.post("/api/settings", {"leveling": {"pitch": 0.7}})
        st = self.rig.state()
        self.assertAlmostEqual(st["leveling"]["result"]["screws"][1]["turns"], 0.43, delta=0.03)

        # The user turns the screws; probing again shows a level bed.
        self.rig.admin("bed 0 0 0")
        self.rig.post("/api/leveling/start", {"probe": True})
        st = self.wait_level("results")
        self.assertTrue(st["leveling"]["result"]["level"], st["leveling"]["result"]["screws"])
        self.assertEqual(st["leveling"]["rounds"], 2)
        self.rig.post("/api/leveling/save")
        self.assertIn("M500", self.rig.sim_history()[-3:])
        self.rig.post("/api/leveling/finish")
        self.assertEqual(self.rig.state()["leveling"]["state"], "idle")

    def test_saved_mesh_and_teach_screw_position(self):
        r = self.rig.post("/api/leveling/start", {"probe": False}, expect_ok=True)
        st = self.wait_level("error")
        self.assertIn("Probe the bed", st["leveling"]["error"])
        self.rig.post("/api/leveling/finish")
        # Teach the back-left screw: the head goes there, is nudged 2 mm right, and saved.
        self.rig.post("/api/leveling/teach_start", {"index": 1})
        sim = self.rig.admin("state")
        self.assertAlmostEqual(sim["pos"]["X"], 15.0, places=2)
        self.assertAlmostEqual(sim["pos"]["Y"], 265.0, places=2)
        self.rig.post("/api/leveling/teach_jog", {"dx": 2})
        self.rig.post("/api/leveling/teach_save")
        screws = self.rig.get("/api/settings")["leveling"]["screws"]
        self.assertEqual((screws[1]["x"], screws[1]["y"]), (17.0, 265.0))
        self.assertEqual(screws[0]["name"], "Front")
        self.rig.post("/api/leveling/teach_done")
        self.assertEqual(self.rig.state()["leveling"]["state"], "idle")


class FirmwareUpdateTests(E2EBase):
    """Check a fake GitHub release, download, back up, flash with a fake avrdude, reconnect, restore."""
    release_name = "Marlin 1.1.9.9 (Github)"
    release_tag = "fw-1.1.9.9-NB1"
    extra_fw = {"auto_check": False}
    board_name = "Marlin 1.1.9.9 (Github) old"     # what the board runs before the update

    def setUp(self):
        import hashlib
        import http.server
        import json
        import tempfile
        import threading
        from helpers import fake_firmware, free_port
        self.work = tempfile.mkdtemp(prefix="northbolt-fw-")
        self.release_hex = fake_firmware(self.release_name)
        self.flash = os.path.join(self.work, "board.hex")                      # the "board's" firmware
        if self.board_name:
            self.old_hex = fake_firmware(self.board_name)
        else:   # like the original Skriware firmware: a real program, but no "FIRMWARE_NAME: ... SOURCE_CODE_URL"
            from helpers import make_hex
            self.old_hex = make_hex(b"\x0c\x94" * 35000)
        with open(self.flash, "wb") as fh:
            fh.write(self.old_hex)
        self.log = os.path.join(self.work, "avrdude.log")
        port = free_port()
        digest = "sha256:" + hashlib.sha256(self.release_hex).hexdigest()
        self.digest = digest
        test = self

        class GitHub(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/repos/ppeterz/SkriwareFirmware/releases/latest":
                    body = json.dumps({"tag_name": test.release_tag, "name": test.release_tag,
                                       "body": "Thermal checks tightened.", "published_at": "2026-10-15T08:00:00Z",
                                       "assets": [
                                           {"name": "led-scale-board-NB1.hex", "size": 10,
                                            "browser_download_url": f"http://127.0.0.1:{port}/led.hex"},
                                           {"name": "skriware2-printer-NB1.hex", "size": len(test.release_hex),
                                            "digest": test.digest, "url": f"http://127.0.0.1:{port}/api-asset",
                                            "browser_download_url": f"http://127.0.0.1:{port}/printer.hex"}]}).encode()
                    self.send_response(200)
                elif self.path == "/printer.hex":
                    body = test.release_hex
                    self.send_response(200)
                else:
                    body = b"{}"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), GitHub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        here = os.path.dirname(os.path.abspath(__file__))
        self.settings = {"firmware": {"github_repo": "https://github.com/ppeterz/SkriwareFirmware",
                                      "github_api": f"http://127.0.0.1:{port}",
                                      "avrdude": [sys.executable, os.path.join(here, "fake_avrdude.py")],
                                      **self.extra_fw}}
        # the simulator reports the firmware name found in the "board's" flash file
        self.env = {"FAKE_AVRDUDE_FLASH": self.flash, "FAKE_AVRDUDE_LOG": self.log, "SIM_FLASH_HEX": self.flash,
                    "FAKE_AVRDUDE_CTRL": self.work}
        super().setUp()

    def tearDown(self):
        import shutil
        super().tearDown()
        self.server.shutdown()
        shutil.rmtree(self.work, ignore_errors=True)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [l.split(" ", 1) for l in fh.read().splitlines()]

    def test_check_install_and_restore(self):
        snap = self.rig.post("/api/firmware/check")
        rel = snap["release"]
        self.assertEqual((rel["tag"], rel["asset"]), ("fw-1.1.9.9-NB1", "skriware2-printer-NB1.hex"))  # not the LED board file
        self.rig.post("/api/firmware/install")
        st = self.rig.wait(lambda s: s["firmware_update"]["state"] in ("done", "error"), 60, "update to finish")
        fu = st["firmware_update"]
        self.assertEqual(fu["state"], "done", fu)
        self.assertEqual(fu["message"], "Firmware updated.")
        self.assertTrue(st["connection"]["ready"], "should reconnect after flashing")
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["r", "w"])           # backup first, then write
        with open(self.flash, "rb") as fh:
            self.assertEqual(fh.read(), self.release_hex)              # the board now has the release
        self.assertIsNotNone(fu["backup"])
        # Blocked while it runs is covered by preflight; restore puts the old firmware back.
        self.rig.post("/api/firmware/restore")
        self.rig.wait(lambda s: len(self.calls()) == 3 and s["firmware_update"]["state"] in ("done", "error")
                      and s["connection"]["ready"], 60, "restore to finish")
        self.assertEqual([c[0] for c in self.calls()], ["r", "w", "w"])   # no second backup on restore
        from northbolt.firmware import APP_LIMIT, parse_hex
        with open(self.flash, "rb") as fh:
            restored = parse_hex(fh.read())
        self.assertEqual(restored, parse_hex(self.old_hex))              # the original firmware is back,
        self.assertLess(max(restored), APP_LIMIT)                        # and the bootloader area untouched

    def test_bad_checksum_never_flashes(self):
        self.digest = "sha256:" + "0" * 64
        r = self.rig.post("/api/firmware/check", expect_ok=False)      # the check downloads and verifies it
        self.assertEqual(r["status"], 400)
        self.assertIn("checksum", r["error"])
        r = self.rig.post("/api/firmware/install", expect_ok=False)    # nothing to install
        self.assertEqual(r["status"], 400)
        st = self.rig.state()
        self.assertEqual(st["firmware_update"]["state"], "error")
        self.assertEqual(self.calls(), [])                             # avrdude never ran
        self.assertTrue(st["connection"]["ready"])                     # the printer was never disconnected

    def test_refused_while_printing(self):
        self.make(layers=30, segs=40)
        self.start_print()
        self.rig.post("/api/firmware/check")
        r = self.rig.post("/api/firmware/install", expect_ok=False)
        self.assertEqual(r["status"], 400)
        self.assertIn("printing", r["error"])
        self.rig.post("/api/job/cancel")
        self.rig.wait(lambda s: s["job"]["state"] == "cancelled", 40)


class FirmwareWriteSafetyTests(FirmwareUpdateTests):
    """What broke the printer's board on 2026-10-19: something opened the serial port while avrdude was
    writing, the write stopped halfway, and Retry then tried to back up the half-written board."""

    def ctrl(self, name):
        open(os.path.join(self.work, name), "w").close()

    def test_connect_refused_while_writing(self):
        self.ctrl("slow")
        self.rig.post("/api/firmware/check")
        self.rig.post("/api/firmware/install")
        st = self.rig.wait(lambda s: s["firmware_update"]["phase"] == "write", 60, "the write")
        self.assertTrue(st["firmware_update"]["owns_port"])
        for action in ("connect", "reconnect"):
            r = self.rig.post(f"/api/connection/{action}", expect_ok=False)
            self.assertEqual(r["status"], 400, action)
            self.assertIn("would break the write", r["error"])
        st = self.rig.wait(lambda s: s["firmware_update"]["state"] in ("done", "error"), 60, "update to finish")
        self.assertEqual(st["firmware_update"]["state"], "done", st["firmware_update"])
        self.assertEqual([c[0] for c in self.calls()], ["r", "w"])

    def test_interrupted_write_then_install_again(self):
        from northbolt.firmware import parse_hex
        self.ctrl("break")
        self.rig.post("/api/firmware/check")
        self.rig.post("/api/firmware/install")
        st = self.rig.wait(lambda s: s["firmware_update"]["state"] in ("done", "error"), 60, "the broken write")
        fu = st["firmware_update"]
        self.assertEqual(fu["state"], "error")
        self.assertIn("didn't finish", fu["error"])
        self.assertTrue(fu["incomplete"])
        good_backup = fu["backup"]["file"]
        # Install again: no backup of the half-written board, straight to writing
        self.rig.post("/api/firmware/install")
        st = self.rig.wait(lambda s: s["firmware_update"]["state"] in ("done", "error") and len(self.calls()) == 3,
                           60, "the second write")
        fu = st["firmware_update"]
        self.assertEqual(fu["state"], "done", fu)
        self.assertEqual([c[0] for c in self.calls()], ["r", "w", "w"])
        self.assertFalse(fu["incomplete"])
        self.assertEqual(fu["backup"]["file"], good_backup)               # the original is still the backup
        with open(self.flash, "rb") as fh:
            self.assertEqual(parse_hex(fh.read()), parse_hex(self.release_hex))


class UnknownBoardFirmwareTests(FirmwareUpdateTests):
    """The board's flash holds no Marlin firmware name (half-written, or something else entirely): it is
    never saved as a backup, nothing is written, and the read is kept aside as bad-read-*.hex."""
    board_name = None

    def test_check_install_and_restore(self):
        self.rig.post("/api/firmware/check")
        self.rig.post("/api/firmware/install")
        st = self.rig.wait(lambda s: s["firmware_update"]["state"] in ("done", "error"), 60, "the refusal")
        fu = st["firmware_update"]
        self.assertEqual(fu["state"], "error")
        self.assertIn("isn't complete Marlin firmware", fu["error"])
        self.assertIsNone(fu["backup"])
        self.assertEqual([c[0] for c in self.calls()], ["r"])              # nothing written
        fw_dir = os.path.join(self.rig.data, "firmware")
        self.assertTrue(any(n.startswith("bad-read-") for n in os.listdir(fw_dir)))
        self.rig.wait(lambda s: s["connection"]["ready"], 30, "reconnecting to the untouched board")


class FirmwareAutoCheckTests(FirmwareUpdateTests):
    """The printer notices a newer release by itself and offers it."""
    release_name = "Marlin 1.1.9.9 (Github, fw-1.1.9.9-NB2)"
    release_tag = "fw-1.1.9.9-NB2"
    extra_fw = {"auto_check": True, "first_check_s": 1}

    def wait_prompt(self):
        return self.rig.wait(lambda s: (s["firmware_update"] or {}).get("prompt"), 30, "update prompt")

    def test_check_install_and_restore(self):    # covered by the parent class
        pass

    def test_bad_checksum_never_flashes(self):
        pass

    def test_refused_while_printing(self):
        pass

    def test_prompt_later_and_skip(self):
        st = self.wait_prompt()
        a = st["firmware_update"]["available"]
        self.assertEqual(a["tag"], "fw-1.1.9.9-NB2")
        self.assertEqual(a["current"], "Marlin 1.1.9.9 (Github) old")
        self.assertEqual(a["firmware"], "Marlin 1.1.9.9 (Github, fw-1.1.9.9-NB2)")
        self.assertEqual(self.calls(), [])                         # nothing flashed without asking
        self.rig.post("/api/firmware/snooze", {"hours": 24})
        st = self.rig.state()
        self.assertFalse(st["firmware_update"]["prompt"])
        self.assertIsNotNone(st["firmware_update"]["available"])   # still listed in Settings
        self.rig.post("/api/firmware/skip")
        self.assertEqual(self.rig.get("/api/settings")["firmware"]["skipped_tag"], "fw-1.1.9.9-NB2")
        self.assertFalse(self.rig.state()["firmware_update"]["prompt"])

    def test_install_from_prompt(self):
        self.wait_prompt()
        self.rig.post("/api/firmware/install")
        st = self.rig.wait(lambda s: s["firmware_update"]["state"] in ("done", "error"), 60, "update to finish")
        self.assertEqual(st["firmware_update"]["state"], "done", st["firmware_update"])
        self.assertEqual(st["firmware_update"]["message"], "Firmware updated.")
        self.assertIsNone(st["firmware_update"]["available"])      # the board now runs the release
        self.assertFalse(st["firmware_update"]["prompt"])
        self.assertIn("fw-1.1.9.9-NB2", st["connection"]["firmware"])

    def test_no_prompt_while_printing(self):
        self.make(layers=40, segs=40)
        self.start_print()
        time.sleep(4)                                              # the first check has run by now
        self.assertFalse(self.rig.state()["firmware_update"]["prompt"])
        self.rig.post("/api/job/cancel")
        self.rig.wait(lambda s: s["job"]["state"] == "cancelled", 40)


class UpToDateTests(FirmwareUpdateTests):
    """The release matches what the board runs: no prompt."""
    release_name = "Marlin 1.1.9.9 (Github) old"
    extra_fw = {"auto_check": True, "first_check_s": 1}

    def test_check_install_and_restore(self):
        pass

    def test_bad_checksum_never_flashes(self):
        pass

    def test_refused_while_printing(self):
        pass

    def test_up_to_date(self):
        st = self.rig.wait(lambda s: (s["firmware_update"] or {}).get("last_check"), 30, "first check")
        self.assertIsNone(st["firmware_update"]["available"])
        self.assertFalse(st["firmware_update"]["prompt"])


class SoftwareUpdateTests(E2EBase):
    """NORTHBOLT's own update: compare the installed commit with GitHub, then run the update service."""
    installed = "a" * 40
    newest = "b" * 40
    fail = False

    def setUp(self):
        import http.server
        import json
        import tempfile
        import threading
        from helpers import free_port
        self.work = tempfile.mkdtemp(prefix="northbolt-sw-")
        build = os.path.join(self.work, "BUILD")
        with open(build, "w") as fh:
            json.dump({"sha": self.installed, "time": 1792000000, "subject": "Old screen"}, fh)
        self.status_dir = os.path.join(self.work, "status")
        port = free_port()
        test = self

        class GitHub(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                api = "/repos/ppeterz/northbolt_3d_printer"
                if self.path == f"{api}/commits?sha=main&path=NORTHBOLT&per_page=1":
                    data = [{"sha": test.newest, "commit": {"message": "New splash\n\nLonger text",
                                                             "committer": {"date": "2026-10-16T18:00:00Z"}}}]
                elif self.path == f"{api}/compare/{test.installed}...{test.newest}":
                    data = {"status": "ahead", "ahead_by": 2, "commits": [
                        {"commit": {"message": "Smaller text"}}, {"commit": {"message": "New splash\n\nbody"}}]}
                else:
                    data = None
                body = json.dumps(data or {}).encode()
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), GitHub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(here, "fake_update.py"), self.status_dir] + (["FAIL"] if self.fail else [])
        self.settings = {
            "firmware": {"github_repo": "https://github.com/ppeterz/northbolt_3d_printer",
                         "github_api": f"http://127.0.0.1:{port}", "auto_check": False},
            "software": {"auto_check": False, "command": cmd, "status_dir": self.status_dir},
        }
        self.env = {"NORTHBOLT_BUILD_FILE": build}
        super().setUp()

    def tearDown(self):
        import shutil
        super().tearDown()
        self.server.shutdown()
        shutil.rmtree(self.work, ignore_errors=True)

    def test_check_offer_and_update(self):
        st = self.rig.state()["software"]
        self.assertEqual(st["build"]["sha"], self.installed)
        self.assertIsNone(st["available"])
        snap = self.rig.post("/api/software/check")
        a = snap["available"]
        self.assertEqual((a["short"], a["subject"], a["ahead_by"]), ("bbbbbbb", "New splash", 2))
        self.assertEqual(a["changes"], ["New splash", "Smaller text"])    # newest first, first lines only
        self.assertTrue(self.rig.state()["software"]["prompt"])
        self.rig.post("/api/software/snooze", {"hours": 1})
        self.assertFalse(self.rig.state()["software"]["prompt"])
        self.rig.post("/api/software/update")
        st = self.rig.wait(lambda s: (s["software"]["update"] or {}).get("state") == "done"
                           and not s["software"]["running"], 30, "update to finish")
        self.assertEqual(st["software"]["update"]["message"], "Updated to bbbbbbb New splash")
        self.assertTrue(any("got bbbbbbb" in l for l in st["software"]["update"]["log"]))

    def test_settings_api_cannot_change_the_update_command(self):
        r = self.rig.post("/api/settings", {"software": {"command": ["sh", "-c", "anything"]}}, expect_ok=False)
        self.assertEqual(r["status"], 400)
        self.rig.post("/api/settings", {"software": {"auto_check": True}})
        self.assertTrue(self.rig.get("/api/settings")["software"]["auto_check"])

    def test_skip_this_version(self):
        self.rig.post("/api/software/check")
        self.rig.post("/api/software/skip")
        st = self.rig.state()["software"]
        self.assertEqual(st["skipped"], self.newest)
        self.assertFalse(st["prompt"])


class SoftwareUpToDateTests(SoftwareUpdateTests):
    installed = "b" * 40

    def test_check_offer_and_update(self):
        snap = self.rig.post("/api/software/check")
        self.assertIsNone(snap["available"])
        self.assertIsNotNone(snap["last_check"])

    def test_skip_this_version(self):
        self.rig.post("/api/software/check")
        r = self.rig.post("/api/software/skip", expect_ok=False)
        self.assertEqual(r["status"], 400)


class SoftwareUpdateFailsTests(SoftwareUpdateTests):
    fail = True

    def test_check_offer_and_update(self):
        self.rig.post("/api/software/check")
        self.rig.post("/api/software/update")
        st = self.rig.wait(lambda s: (s["software"]["update"] or {}).get("state") == "failed", 30, "update to fail")
        self.assertIn("Couldn't download", st["software"]["update"]["message"])
        self.assertFalse(st["software"]["running"])


class UsbTests(E2EBase):
    def setUp(self):
        import tempfile
        self.drive = tempfile.mkdtemp(prefix="northbolt-usb-")
        os.makedirs(os.path.join(self.drive, "Benchy"))
        os.makedirs(os.path.join(self.drive, "System Volume Information"))
        make_gcode(os.path.join(self.drive, "Benchy", "boat.gcode"), layers=3, segs=6)
        make_gcode(os.path.join(self.drive, "cube.gcode"), layers=3, segs=6)
        with open(os.path.join(self.drive, "notes.txt"), "w") as fh:
            fh.write("not g-code")
        self.env = {"NORTHBOLT_FAKE_USB": self.drive}
        super().setUp()

    def tearDown(self):
        import shutil
        super().tearDown()
        shutil.rmtree(self.drive, ignore_errors=True)

    def test_browse_copy_print_eject(self):
        st = self.rig.wait(lambda s: s["usb"]["drives"], 15, "USB drive")
        drive = st["usb"]["drives"][0]
        self.assertTrue(drive["ready"])
        top = self.rig.get(f"/api/usb/{drive['id']}/list")
        names = [(e["name"], e["type"]) for e in top["entries"]]
        self.assertEqual(names, [("Benchy", "dir"), ("cube.gcode", "file")])   # no .txt, no system folder
        sub = self.rig.get(f"/api/usb/{drive['id']}/list?path=Benchy")
        self.assertEqual([e["name"] for e in sub["entries"]], ["boat.gcode"])
        r = self.rig.api("GET", f"/api/usb/{drive['id']}/list?path=../..", expect_ok=False)
        self.assertEqual(r["status"], 400)
        meta = self.rig.get(f"/api/usb/{drive['id']}/meta?path=Benchy/boat.gcode")
        self.assertEqual(meta["est_time_s"], 3723)
        self.assertTrue(meta["has_thumbnail"])

        # Print straight from the drive: it is copied to the printer, then printed.
        r = self.rig.post(f"/api/usb/{drive['id']}/print", {"path": "Benchy/boat.gcode"})
        self.assertEqual(r["name"], "boat.gcode")
        st = self.wait_done()
        self.assertEqual(st["job"]["state"], "finished", st["job"])
        self.assertEqual(st["job"]["file"], "boat.gcode")
        files = [f["name"] for f in self.rig.get("/api/files")["files"]]
        self.assertIn("boat.gcode", files)

        # The same file again is reused, a different file with the same name gets "(2)".
        self.assertEqual(self.rig.post(f"/api/usb/{drive['id']}/copy", {"path": "Benchy/boat.gcode"})["name"], "boat.gcode")
        make_gcode(os.path.join(self.drive, "boat.gcode"), layers=4, segs=6)
        self.assertEqual(self.rig.post(f"/api/usb/{drive['id']}/copy", {"path": "boat.gcode"})["name"], "boat (2).gcode")

        self.rig.post(f"/api/usb/{drive['id']}/eject")
        self.rig.wait(lambda s: not s["usb"]["drives"], 10, "drive gone after eject")


if __name__ == "__main__":
    unittest.main()
