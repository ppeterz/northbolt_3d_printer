"""Settings with defaults, deep-merged over a JSON file in the data directory."""

import copy
import json
import os
import secrets
import tempfile

DEFAULTS = {
    # Serial link to the MKS Gen 1.3 board. "auto" picks the first /dev/ttyUSB* or /dev/ttyACM*.
    # pyserial URLs work too, e.g. "socket://localhost:5555" for the simulator.
    "serial_port": "auto",
    "baudrate": 115200,
    "auto_connect": True,

    "http_host": "0.0.0.0",
    "http_port": 8080,
    "api_key": "",              # generated on first run
    "require_key_remote": True,  # requests not from this Pi need X-Api-Key

    # Machine geometry (matches Skriware_Configuration.h)
    "bed_x": 200,
    "bed_y": 275,
    "z_max": 210,
    "lift_mechanism": True,      # right extruder has its own Z offset (MOVING_EXTRUDER, M206 E)

    "presets": [
        {"name": "PLA", "hotend": 200, "bed": 60},
        {"name": "PETG", "hotend": 235, "bed": 80},
        {"name": "ABS", "hotend": 240, "bed": 100},
    ],

    "filament": {
        "load_length": 60,       # mm pushed when loading
        "load_speed": 5,         # mm/s
        "purge_length": 20,      # mm extruded slowly after loading
        "unload_length": 80,     # mm pulled back when unloading
        "unload_speed": 30,      # mm/s
        "min_temp": 180,         # loading/unloading heats to at least this
    },

    "pause": {
        "retract": 2.0,          # mm
        "lift": 10.0,            # mm
        "park_x": 10.0,          # NOZZLE_PARK_POINT X_MIN+10
        "park_y": 265.0,         # NOZZLE_PARK_POINT Y_MAX-10
        "heater_timeout_min": 15,  # switch hotends off after this long paused (0 = never)
    },

    "recovery": {
        "enabled": True,
        "layer_sync": True,      # send M400 at layer changes so checkpoints are exact
        "min_interval_s": 15,    # at most one synced checkpoint per this many seconds
        "progress_interval_s": 2,
        "sync_z_moves": True,    # M400 + save before each Z move / tool change so head height is known
        "backoff_moves": 26,     # BUFSIZE 10 + BLOCK_BUFFER_SIZE 16
        "z_lift": 5.0,
        "prime_length": 5.0,
        "use_mesh": True,        # send M420 S1 after homing X/Y (G28 turns leveling off)
    },

    "runout": {"left": True, "right": True},

    "zoffset": {
        "center_x": 100.0,
        "center_y": 137.5,
        "heat": True,
        "bed_temp": 60,
        "nozzle_temp": 150,
        "use_mesh": True,
        "paper_thickness": 0.0,   # 0 = Z0 where the paper drags (classic paper test)
        "start_height": 5.0,      # mm above the Z trigger point to start lowering from
        "max_below_trigger": 5.0, # never go further than this below the trigger point
        "check_inset": 25.0,
    },

    "leveling": {
        # Bed screws, in nozzle coordinates. The front one is at Y0; set exact spots with
        # "Screw positions" on the Calibrate page (it moves the nozzle over each screw).
        "screws": [
            {"name": "Front", "x": 100.0, "y": 0.0},
            {"name": "Back left", "x": 15.0, "y": 265.0},
            {"name": "Back right", "x": 185.0, "y": 265.0},
        ],
        "reference": 0,            # the screw left alone; the others are turned to match it
        "pitch": 0.5,              # mm per turn: M3 0.5, M4 0.7, M5 0.8
        "clockwise_lowers": True,  # turning clockwise (seen from above) lowers that corner
        "tolerance": 0.05,         # mm; closer than this counts as level
        "heat": True,
        "bed_temp": 60,
        "park_z": 30.0,            # lift after probing so there is room for hands
        # Probe area of the mesh (LEFT/RIGHT/FRONT/BACK_PROBE_BED_POSITION in Skriware_Configuration.h)
        "grid": {"left": 20.0, "right": 180.0, "front": 32.0, "back": 255.0},
    },

    "firmware": {
        "github_repo": "",          # e.g. https://github.com/you/SkriwareFirmware
        "token": "",                # only for a private repository (read-only fine-grained token)
        "asset_match": "printer",   # release .hex whose name contains this
        "github_api": "https://api.github.com",
        "avrdude": ["avrdude"],
        "programmer": "wiring",     # the MKS Gen 1.3 stk500v2 bootloader
        "baud": 115200,
        "auto_check": True,         # look for a new release by itself and offer it on the screen
        "check_hours": 6,
        "first_check_s": 60,        # first look this long after start-up
        "skipped_tag": "",          # "Skip this version" remembers the release here
    },

    "leds": {"enabled": True, "progress": True, "brightness": 0.6},

    # A strip of the Pi's touch panel that doesn't respond, in screen pixels from the top:
    # {"top": 200, "bottom": 355}. The printer's own screen then keeps every button out of it.
    # Measured and saved by Settings -> System -> Touch test. None: the whole panel works.
    "touch_dead_zone": None,

    "scales": {
        "enabled": True,
        "interval_s": 5,
        "left": {"tare": None, "counts_per_gram": None, "spool_g": 250},
        "right": {"tare": None, "counts_per_gram": None, "spool_g": 250},
    },

    "i2c_bus": 1,
    "i2c_addr": 8,
    "power_button_gpio": None,   # BCM pin wired to the ATmega SlaveFlag line, if any
    "shutdown_command": ["sudo", "-n", "systemctl", "poweroff"],
    "power_off_color": [0, 0, 80],
}


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def atomic_write_json(path, data):
    """Write JSON so a power cut leaves either the old file or the new one, never half of one."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if hasattr(os, "O_DIRECTORY"):
        try:
            dfd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


class Settings:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "settings.json")
        self.user = {}
        self.overrides = {}          # command-line values, never written to the file
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.user = json.load(fh)
        self.values = deep_merge(DEFAULTS, self.user)
        if not self.values.get("api_key"):
            self.update({"api_key": secrets.token_hex(16)})

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, changes):
        """Merge changes into the user file and the live values, then save."""
        self.user = deep_merge(self.user, changes)
        self.values = deep_merge(deep_merge(DEFAULTS, self.user), self.overrides)
        atomic_write_json(self.path, self.user)

    def override(self, key, value):
        self.overrides[key] = value
        self.values[key] = value

    def public(self):
        return copy.deepcopy(self.values)
