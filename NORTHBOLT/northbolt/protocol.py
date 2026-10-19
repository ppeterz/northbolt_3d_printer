"""Serial link to the Skriware Marlin 1.1.9 firmware.

Protocol facts this is built around (see Marlin/Marlin_main.cpp get_serial_commands):
  * Lines may carry "N<n> ... *<checksum>". If N is present it must be LastN+1 and the
    checksum is mandatory. The firmware echoes "LINE ACCEPTED:<n>" for each good line.
  * A bad line prints "BAD COMMAND:<line>", an "Error:" line, "Resend: <n>" and "ok".
  * "ok" is sent once a command has been processed. We keep exactly one line in flight.
  * HOST_KEEPALIVE prints "busy: processing" every 2 s during long commands, and
    AUTO_REPORT_TEMPERATURES (M155) prints temperatures while idle.
  * The emergency parser acts on M108/M112/M410/M411 as soon as the bytes arrive.
  * Commands longer than MAX_CMD_SIZE-1 (95) characters are truncated by the firmware.
"""

import asyncio
import logging
import re
import threading
import time
from collections import OrderedDict, deque

import serial

log = logging.getLogger("link")

MAX_LINE = 95
RESEND_RE = re.compile(r"^(?:Resend:|rs)\s*N?:?\s*(\d+)", re.I)
LONG_WAIT_CODES = {"M109", "M190", "M116", "M303", "M48", "G29"}


class LinkError(Exception):
    pass


class CommandCancelled(Exception):
    pass


def checksum(text):
    cs = 0
    for ch in text.encode("ascii", "replace"):
        cs ^= ch
    return cs


def m110_line(n):
    """'N<n> M110 N<n>*<checksum>': sets the firmware's last line number to n. A numbered M110 takes
    effect as soon as Marlin reads the line (get_serial_commands); a plain "M110 N0" only once the
    queue reaches it, so a queue clear (M411) or a long command ahead of it could lose it."""
    body = f"N{n} M110 N{n}"
    return f"{body}*{checksum(body)}"


def find_serial_port():
    import glob
    import os
    import sys
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    if sys.platform.startswith("linux"):
        # Board wired to the Pi's GPIO UART. Never guess further: on a Pi 3, /dev/ttyAMA0 is Bluetooth.
        return "/dev/serial0" if os.path.exists("/dev/serial0") else None
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
        if ports:
            return ports[0]
    except Exception:
        pass
    return None


class _Command:
    __slots__ = ("text", "future", "response", "numbered", "n", "long_wait", "written_at")

    def __init__(self, text, future, numbered):
        self.text = text
        self.future = future
        self.response = []
        self.numbered = numbered
        self.n = None
        code = text.split(None, 1)[0].upper() if text.strip() else ""
        self.long_wait = code in LONG_WAIT_CODES
        self.written_at = 0.0


class MarlinLink:
    """Owns the serial port. Call command() to run G-code and get its response lines."""

    def __init__(self, on_line, on_connection):
        self.on_line = on_line              # callback(line) for every received line
        self.on_connection = on_connection  # callback(connected: bool, reason: str)
        self.ser = None
        self.port = None
        self.loop = None
        self.connected = False
        self.ready = False
        self._reader = None
        self._stop_reader = threading.Event()
        self._write_lock = threading.Lock()
        self._prio = deque()
        self._norm = deque()
        self._inflight = None
        self._next_n = 1
        self._history = OrderedDict()
        self._resend = deque()
        self._resyncing = False
        self._start_seen = asyncio.Event()
        self._last_rx = 0.0
        self._last_busy = 0.0
        self._last_tickle = 0.0
        self._watchdog = None
        self.stats = {"resends": 0, "tickles": 0, "stray_oks": 0}

    # ------------------------------------------------------------------ connection
    async def connect(self, port, baudrate):
        if self.connected:
            return
        self.loop = asyncio.get_running_loop()
        if port in (None, "", "auto"):
            port = find_serial_port()
            if not port:
                raise LinkError("No serial port found (is the printer board powered?)")
        self.port = port

        def _open():
            return serial.serial_for_url(port, baudrate=baudrate, timeout=0.2, write_timeout=5)

        self.ser = await self.loop.run_in_executor(None, _open)
        self.connected = True
        self.ready = False
        self._reset_state()
        self._start_seen = asyncio.Event()
        self._stop_reader.clear()
        self._reader = threading.Thread(target=self._read_thread, name="serial-reader", daemon=True)
        self._reader.start()
        self._watchdog = asyncio.ensure_future(self._watchdog_task())
        log.info("Opened %s at %d baud", port, baudrate)

        # Opening a real port toggles DTR, which resets the ATmega2560. Wait for "start".
        wait = 2.0 if port.startswith("socket://") else 10.0
        try:
            await asyncio.wait_for(self._start_seen.wait(), wait)
            await asyncio.sleep(0.5 if port.startswith("socket://") else 2.0)
        except asyncio.TimeoutError:
            log.info("No 'start' from firmware, assuming it is already running")
        await self.command(m110_line(0), numbered=False)
        self.ready = True
        self.on_connection(True, "connected")

    async def disconnect(self, reason="disconnected"):
        if not self.connected:
            return
        self.connected = False
        self.ready = False
        self._stop_reader.set()
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        try:
            self.ser.close()
        except Exception:
            pass
        self._fail_all(LinkError(reason))
        self.on_connection(False, reason)

    def _reset_state(self):
        self._prio.clear()
        self._norm.clear()
        self._inflight = None
        self._next_n = 1
        self._history.clear()
        self._resend.clear()
        self._resyncing = False
        self._last_rx = time.monotonic()

    def _fail_all(self, exc):
        pending = list(self._prio) + list(self._norm)
        if self._inflight:
            pending.append(self._inflight)
        self._prio.clear()
        self._norm.clear()
        self._inflight = None
        self._resend.clear()
        for cmd in pending:
            if not cmd.future.done():
                cmd.future.set_exception(exc)

    def _read_thread(self):
        ser = self.ser
        while not self._stop_reader.is_set():
            try:
                raw = ser.readline()
            except Exception as exc:  # port vanished (USB unplugged, board powered off)
                if not self._stop_reader.is_set():
                    self.loop.call_soon_threadsafe(
                        lambda e=exc: asyncio.ensure_future(self.disconnect(f"Serial error: {e}")))
                return
            if not raw:
                continue
            line = raw.decode("ascii", "replace").strip()
            if line:
                self.loop.call_soon_threadsafe(self._handle_line, line)

    # ------------------------------------------------------------------ sending
    def command(self, text, priority=False, numbered=True):
        """Queue a command. Returns a future resolving to its response lines."""
        text = text.strip()
        fut = self.loop.create_future() if self.loop else asyncio.get_event_loop().create_future()
        if not self.connected:
            fut.set_exception(LinkError("Printer not connected"))
            return fut
        cmd = _Command(text, fut, numbered)
        (self._prio if priority else self._norm).append(cmd)
        self._dispatch()
        return fut

    def emergency(self, text):
        """Write a command immediately, bypassing the queue (emergency parser commands)."""
        if not self.connected:
            return
        self._write((text.strip() + "\n").encode("ascii"))
        log.warning("Emergency command %s", text)

    async def resync(self, drain=1.0, quiet=2.5, limit=120.0):
        """After M411/M410 the firmware drops its queued lines and their oks. It does that only once
        the command it is running has finished, and a line sent before then is wiped as well. That
        command reports "busy" every 2 s, so wait until those stop, then start the numbering over."""
        self._resyncing = True
        self._fail_all(CommandCancelled("Command queue cleared"))
        start = time.monotonic()
        await asyncio.sleep(drain)
        while self.connected and time.monotonic() - start < limit and time.monotonic() - self._last_busy < quiet:
            await asyncio.sleep(0.1)
        self._next_n = 1
        self._history.clear()
        self._resyncing = False
        await self.command(m110_line(0), priority=True, numbered=False)

    def _write(self, data):
        with self._write_lock:
            self.ser.write(data)

    def _dispatch(self):
        if not self.connected or self._resyncing or self._inflight is not None:
            return
        if self._prio:
            cmd = self._prio.popleft()
        elif self._norm:
            cmd = self._norm.popleft()
        else:
            return
        if cmd.future.done():  # cancelled while queued
            return self._dispatch()
        if cmd.numbered:
            n = self._next_n
            body = f"N{n} {cmd.text}"
            line = f"{body}*{checksum(body)}"
        else:
            n = None
            line = cmd.text
        if len(line) > MAX_LINE:
            cmd.future.set_exception(LinkError(f"Line too long for firmware ({len(line)} > {MAX_LINE}): {cmd.text[:40]}"))
            return self._dispatch()
        if cmd.numbered:
            self._next_n += 1
            cmd.n = n
            self._history[n] = line
            while len(self._history) > 256:
                self._history.popitem(last=False)
        data = (line + "\n").encode("ascii", "replace")
        self._inflight = cmd
        cmd.written_at = time.monotonic()
        try:
            self._write(data)
        except Exception as exc:
            asyncio.ensure_future(self.disconnect(f"Write failed: {exc}"))

    # ------------------------------------------------------------------ receiving
    def _handle_line(self, line):
        now = time.monotonic()
        self._last_rx = now
        low = line.lower()

        if line == "start":
            self._start_seen.set()
            if self.ready:
                # The board rebooted underneath us (reset, brown-out, watchdog).
                log.error("Firmware restarted unexpectedly")
                self._fail_all(LinkError("Printer board restarted"))
                self._next_n = 1
                self._history.clear()
                asyncio.ensure_future(self._after_restart())
        elif low.startswith("busy:") or low.startswith("echo:busy:"):
            self._last_busy = now
        elif line.startswith("LINE ACCEPTED:"):
            pass
        else:
            m = RESEND_RE.match(line)
            if m:
                self._on_resend(int(m.group(1)))
            elif low.startswith("ok"):
                self._on_ok(line[2:].strip())
            elif self._inflight is not None and not line.startswith("BAD COMMAND:"):
                self._inflight.response.append(line)

        try:
            self.on_line(line)
        except Exception:
            log.exception("on_line handler failed")

    async def _after_restart(self):
        await asyncio.sleep(1.5)
        try:
            await self.command(m110_line(0), priority=True, numbered=False)
        except Exception:
            pass

    def _on_resend(self, n):
        self.stats["resends"] += 1
        last = self._next_n - 1
        if n == last + 1:
            # It has every line we sent (the watchdog repeated one it already had). Nothing to send;
            # the "ok" that follows answers the line in flight.
            log.info("Firmware already has line %d", last)
            return
        if n not in self._history:
            # Its numbering isn't ours: our reset (M110) never reached it, so it rejected every line
            # since. Renumber it to just before the first of those, then send them again.
            first = self._inflight.n if self._inflight is not None and self._inflight.n else last + 1
            self._resend = deque([m110_line(first - 1)] +
                                 [self._history[k] for k in range(first, last + 1) if k in self._history])
            log.warning("Firmware wants line %d but we are at %d: renumbering it to %d", n, last, first - 1)
            return
        self._resend = deque(self._history[k] for k in range(n, last + 1) if k in self._history)
        log.warning("Resend requested from line %d (%d lines)", n, len(self._resend))

    def _on_ok(self, rest):
        if self._resyncing:
            return
        if self._resend:
            line = self._resend.popleft()
            if self._inflight is not None:
                self._inflight.written_at = time.monotonic()
            self._write((line + "\n").encode("ascii", "replace"))
            return
        cmd = self._inflight
        if cmd is None:
            self.stats["stray_oks"] += 1
            return
        if rest:
            cmd.response.append(rest)
        self._inflight = None
        if not cmd.future.done():
            cmd.future.set_result(cmd.response)
        self._dispatch()

    async def _watchdog_task(self, after=4.0, quiet=3.0):
        """Recover from a lost line or a lost 'ok': if nothing but temperature reports arrives for a
        while, send the line in flight again. If the firmware never got it (a byte lost on the wire
        can turn a line into a comment, which gets no answer), it runs it now. If it did, it asks for
        the next line instead (see _on_resend), and the "ok" after that answers this one. An
        unnumbered poke (M105) would get an "ok" that looks like this line's answer, and the next
        line would be rejected for the missing one. A working board reports "busy" every 2 s, so
        a few seconds of silence is enough: every second the head stands still leaves a blob."""
        try:
            while True:
                await asyncio.sleep(1.0)
                cmd = self._inflight
                if cmd is None or cmd.long_wait or self._resend:
                    continue
                now = time.monotonic()
                waited = now - cmd.written_at
                busy_recent = now - self._last_busy < quiet
                if waited > after and not busy_recent and now - self._last_tickle > after:
                    self._last_tickle = now
                    self.stats["tickles"] += 1
                    line = self._history.get(cmd.n) if cmd.numbered else None
                    log.warning("No 'ok' for %s after %.0fs, sending %s", cmd.text, waited,
                                "it again" if line else "M105")
                    self._write((line + "\n").encode("ascii", "replace") if line else b"M105\n")
        except asyncio.CancelledError:
            pass

    @property
    def busy(self):
        return self._inflight is not None or bool(self._prio) or bool(self._norm)
