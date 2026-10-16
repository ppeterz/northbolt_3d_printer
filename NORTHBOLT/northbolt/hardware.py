"""The ATmega328 LED/scale/power board on I2C, and the power button signal on a GPIO.

I2C frame (Atmega_Soft/led_and_scale/led_and_scale.ino, address 8):
    | function | mode | R | G | B | checksum |   checksum = (XOR of first 5 bytes) + 4
Functions used here:
    0xCC lights (mode 0 on, 1 off, 4 breathe, 5 flash; modes 2/3/6 block inside the ISR, avoided)
    0x0B side pads lit from 0 to N (progress), 0xC1 / 0x1C centre bar on / off
    0xAF / 0xCF measure left / right scale, then read an ASCII number (raw HX711 counts)
    0xDF software version, then read 16 bytes
    0xDD power off the printer board (LED animation, about 20 s, then cuts power)
    0xF0 cancel a power-off request
Power button: the ATmega holds its SlaveFlag pin HIGH while the board is powered and pulses it
LOW for 100 ms when the button is pressed. Check it is level-shifted to 3.3 V before wiring.
"""

import logging
import threading
import time

log = logging.getLogger("hardware")

LIGHTS = 0xCC
PADS_UP_TO = 0x0B
CENTER_ON = 0xC1
CENTER_OFF = 0x1C
SCALE_LEFT = 0xAF   # the sketch's header comment says 0x0F, the code uses 0xAF
SCALE_RIGHT = 0xCF
VERSION = 0xDF
POWER_OFF = 0xDD
CANCEL_POWER_OFF = 0xF0
NLED_SIDE = 12


def frame(function, mode=0, r=0, g=0, b=0):
    data = [function & 0xFF, mode & 0xFF, r & 0xFF, g & 0xFF, b & 0xFF]
    cs = 0
    for byte in data:
        cs ^= byte
    return data + [(cs + 4) & 0xFF]


class SkriwareBoard:
    def __init__(self, bus_number=1, address=8):
        self.address = address
        self.bus = None
        self.available = False
        self._lock = threading.Lock()
        self._warned = False
        try:
            from smbus2 import SMBus, i2c_msg  # noqa: F401
            self._i2c_msg = i2c_msg
            self.bus = SMBus(bus_number)
            self.available = True
        except Exception as exc:
            log.info("I2C board not available (%s); LEDs and scales disabled", exc)

    def _send(self, data):
        if not self.available:
            return False
        try:
            with self._lock:
                self.bus.write_i2c_block_data(self.address, data[0], data[1:])
            return True
        except OSError as exc:
            if not self._warned:
                log.warning("I2C write failed: %s", exc)
                self._warned = True
            return False

    def _read(self, length):
        msg = self._i2c_msg.read(self.address, length)
        with self._lock:
            self.bus.i2c_rdwr(msg)
        return bytes(msg)

    def lights(self, mode, rgb=(0, 0, 0)):
        return self._send(frame(LIGHTS, mode, *rgb))

    def pads(self, count, rgb):
        return self._send(frame(PADS_UP_TO, count, *rgb))

    def center(self, rgb):
        if rgb is None or not any(rgb):
            return self._send(frame(CENTER_OFF))
        return self._send(frame(CENTER_ON, 0, *rgb))

    def power_off(self, rgb=(0, 0, 80)):
        return self._send(frame(POWER_OFF, 0, *rgb))

    def cancel_power_off(self):
        return self._send(frame(CANCEL_POWER_OFF))

    def read_scale(self, side):
        """Raw HX711 average for 'left' or 'right', or None."""
        if not self.available:
            return None
        if not self._send(frame(SCALE_LEFT if side == "left" else SCALE_RIGHT)):
            return None
        time.sleep(0.02)
        try:
            raw = self._read(16)
        except OSError:
            return None
        text = ""
        for ch in raw.decode("ascii", "replace"):
            if ch.isdigit() or (ch == "-" and not text):
                text += ch
            else:
                break
        try:
            return int(text)
        except ValueError:
            return None

    def version(self):
        if not self.available or not self._send(frame(VERSION)):
            return None
        time.sleep(0.02)
        try:
            return self._read(16)[0]
        except OSError:
            return None


def scale(rgb, brightness):
    return tuple(int(max(0, min(255, c * brightness))) for c in rgb)


def hex_rgb(text, fallback=(255, 255, 255)):
    """'#ff8800' -> (255, 136, 0)."""
    t = str(text or "").strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    try:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4)) if len(t) == 6 else fallback
    except ValueError:
        return fallback


LED_MODES = ("on", "breathe", "flash", "off")
# the printer states that have their own light, in the order the screen lists them
LED_EVENTS = ("idle", "heating", "printing", "paused", "done", "cancelled", "error", "filament",
              "calibrating", "power")


class LedPolicy:
    """Turns the printer's state into the colour and effect set for it under settings["leds"]
    and only talks to the board when that changes."""

    BOARD_MODE = {"on": 0, "off": 1, "breathe": 4, "flash": 5}

    def __init__(self, board, settings):
        self.board = board
        self.settings = settings
        self.current = None
        self.current_pads = None
        self._resend_at = None
        self._test = None            # (event, until): Settings -> LEDs -> Test shows one for a moment

    def test(self, event, seconds=6.0):
        if event not in LED_EVENTS and event != "override":
            raise ValueError(f"Unknown light: {event}")
        self._test = (event, time.monotonic() + seconds)

    def look(self, event):
        """(mode, rgb) for an event, from the settings."""
        cfg = self.settings["leds"]
        if event == "override":
            ev = cfg.get("override") or {}
        else:
            ev = (cfg.get("events") or {}).get(event) or (cfg.get("events") or {}).get("idle") or {}
        mode = ev.get("mode") if ev.get("mode") in LED_MODES else "on"
        return mode, hex_rgb(ev.get("color"))

    def apply(self, status, progress=None):
        cfg = self.settings["leds"]
        now = time.monotonic()
        testing = self._test is not None and now < self._test[1]
        if not testing:
            self._test = None
        show_progress = False
        if testing:
            mode, rgb = self.look(self._test[0])
        elif not cfg["enabled"]:
            mode, rgb = "off", (0, 0, 0)
        elif (cfg.get("override") or {}).get("enabled"):
            mode, rgb = self.look("override")
        else:
            mode, rgb = self.look(status)
            show_progress = status == "printing" and cfg["progress"] and progress is not None
        bright = cfg["brightness"]
        key = (mode, rgb, bright)
        # The ATmega finishes a breathe or flash cycle (about 8 s, blocking its loop) before it
        # notices a new pattern, and that cycle ends dark. Send the new pattern again once it is over.
        resend = self._resend_at is not None and now >= self._resend_at
        if key != self.current or resend:
            was_animated = self.current is not None and self.current[0] in ("breathe", "flash")
            self._resend_at = now + 9.0 if (was_animated and not resend) else None
            self.current = key
            self.current_pads = None
            if mode == "off":
                self.board.lights(1)
            else:
                self.board.lights(self.BOARD_MODE[mode], scale(rgb, bright))
        if show_progress and mode != "off":
            count = max(0, min(NLED_SIDE, int(round(progress / 100.0 * NLED_SIDE))))
            if count != self.current_pads:
                self.current_pads = count
                self.board.pads(count, scale(hex_rgb(cfg.get("progress_color"), (0, 160, 255)), bright))
                self.board.center(scale(hex_rgb(cfg.get("center_color")), bright))


class PowerButton:
    """Watches the ATmega SlaveFlag line. Calls on_request() for a short LOW pulse."""

    def __init__(self, gpio, on_request, on_board_off):
        self.gpio = gpio
        self.on_request = on_request
        self.on_board_off = on_board_off
        self.device = None
        self._low_since = None
        if gpio is None:
            return
        try:
            from gpiozero import DigitalInputDevice
            self.device = DigitalInputDevice(gpio, pull_up=False)
            self.device.when_deactivated = self._fell
            self.device.when_activated = self._rose
            log.info("Watching power button signal on GPIO%s", gpio)
        except Exception as exc:
            log.warning("Power button GPIO%s unavailable: %s", gpio, exc)

    def _fell(self):
        self._low_since = time.monotonic()
        threading.Timer(1.5, self._check_still_low).start()

    def _rose(self):
        if self._low_since is not None and time.monotonic() - self._low_since < 1.0:
            self.on_request()
        self._low_since = None

    def _check_still_low(self):
        if self._low_since is not None and time.monotonic() - self._low_since >= 1.4:
            self.on_board_off()
