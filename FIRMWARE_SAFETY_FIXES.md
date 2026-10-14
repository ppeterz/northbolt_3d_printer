# Firmware safety fixes (NB1)

Changes to the Skriware 2 firmware in this repository, made with the NORTHBOLT host.
The board reports them in M115 as `Marlin 1.1.9.9 (Github, NB1 safety fixes)`.
The EEPROM layout is unchanged, so flashing keeps your saved settings (Z offsets, mesh, PID).

## Printer board (Marlin, MKS Gen 1.3)

| What | File | Before | After |
|---|---|---|---|
| Heat-up check (catches a thermistor that fell out) | `Marlin/Skriware_Conf_Advanced.h` | 1 °C rise in 45 s | 2 °C in 20 s (Marlin default) |
| Bed heat-up check | same | 1 °C in 90 s | 2 °C in 60 s |
| Thermal runaway allowed drop | same | 12 °C for 45 s | 8 °C for 40 s |
| Extra max-temperature check | `Marlin/temperature.cpp` | used the left nozzle's limit for every heater | each heater's own limit |
| Halt messages for max/min temp and heating failure | same | printed garbage (RAM text sent through a flash print) | readable; Skriware's "Heating Error. Heater ID: n" line kept |
| Extruder lift board not answering | `Marlin/Skriware_Functions.cpp` | retried forever without servicing the heaters; the watchdog reset the board 4 s later with no explanation | 5 tries over about 1 s, then stops with "Extruder lift board not responding. Check its cable." |
| Waits after lifting/lowering the extruder | same | compared the clock with a fixed 500, so after power-on they didn't wait at all | really wait `extruder_change_time_offset` (500 ms, `M80 O<ms>`) while servicing the heaters |
| Right runout sensor debounce | same | reset by the left sensor's pin: with the left spool empty, a brief flicker on the right sensor could pause the print | reset by its own pin |
| G28 after printing with the right nozzle | `Marlin/Marlin_main.cpp` | switched to the left nozzle but kept the right nozzle's Z offset, so the next print used the wrong nozzle height | restores the left nozzle's offset, like a normal tool change |

**Thermal runaway allowed drop:** Skriware raised this from 5 °C to 12 °C in their April 2021 release, probably because of false alarms.
If you get "Thermal Runaway" stops while the part fan runs at full speed and the hotend is healthy, raise
`THERMAL_PROTECTION_HYSTERESIS` to 10.

**Tool changes take longer:** each tool change with the lift mechanism now takes 0.5 to 1 s longer, because the waits really happen.

## LED and scale board (ATmega328, `Atmega_Soft/led_and_scale`)

| What | Before | After |
|---|---|---|
| I2C receive | copied every byte into a 6-byte buffer, so longer messages overwrote other variables | stores at most 6 bytes and ignores anything that isn't exactly 6 |
| Power-off command (0xDD) | ran its 20-second countdown with `delay()` inside the I2C interrupt, where delays don't work | the interrupt only flags it; the main loop runs the countdown, then cuts power |
| Scale reads | waited forever for a scale that never got ready, freezing the main loop | give up after 1.5 s and keep the last reading |

The other LED effects (brighten, fade, the 0x06 sweep) still use `delay()` inside the interrupt. NORTHBOLT doesn't use them.

## Building

Ready-built files are in `firmware-NB1/`:

| File | Board | Flash | RAM |
|---|---|---|---|
| `skriware2-printer-NB1.hex` | MKS Gen 1.3 printer board (ATmega2560) | 112,454 bytes | 5,302 bytes |
| `led-scale-board-NB1.hex` | LED and scale board (ATmega328P) | 9,722 bytes | 348 bytes |

For comparison, the original build uses 112,528 bytes of flash and 5,324 bytes of RAM on the printer board, so the fixes make it slightly smaller.
These were built with avr-gcc 7.3 and the Arduino AVR core, using the same flags as the Arduino IDE. `*.hex` is in `.gitignore`, so they aren't committed.

To rebuild them yourself, use the [PlatformIO](https://platformio.org) configs in this repository:

```bash
pio run -e skriware2
```
```bash
pio run -d Atmega_Soft/led_and_scale -e atmega328
```

The printer firmware ends up in `.pio/build/skriware2/firmware.hex`.

## Installing from the NORTHBOLT screen (easiest)

1. Push this repository to your own GitHub account.
2. Make a release:
   - **Automatically:** tag the commit and push the tag. `.github/workflows/firmware.yml` builds both `.hex` files and publishes the release.
   - **By hand:** create a release on GitHub and attach `firmware-NB1/skriware2-printer-NB1.hex`.

```bash
git tag fw-1.1.9.9-NB1
```
```bash
git push origin fw-1.1.9.9-NB1
```

3. On the printer, go to **Settings → Firmware**. Enter your repository link, then press **Check for update** and **Install on the printer**.

NORTHBOLT then:
- downloads the release's printer `.hex`, the one whose name contains "printer";
- checks it against GitHub's checksum, and checks that it's valid Intel HEX that fits below the bootloader and contains Marlin;
- reads the current firmware off the board as a backup;
- flashes the new firmware with avrdude, reconnects, and shows what the board now reports.

**Automatic checks:** NORTHBOLT checks by itself every 6 hours. It offers an update when the firmware inside the latest release differs from what the board reports.
Release builds put their tag into that name, so a board on `fw-1.1.9.9-NB2` reports `Marlin 1.1.9.9 (Github, fw-1.1.9.9-NB2)`, and every new tag counts as an update.

**Restore backup…** puts the previous firmware back. A private repository needs a read-only GitHub token in the same screen.

## Flashing the printer board by hand

**Before flashing:**
- Don't flash mid-print.
- Save a copy of your settings first, by sending `M503` from the NORTHBOLT console and keeping the output.

**From the Raspberry Pi** (NORTHBOLT stopped so the port is free):

```bash
sudo systemctl stop northbolt
```
```bash
sudo apt-get install -y avrdude
```
```bash
avrdude -p m2560 -c wiring -P /dev/ttyUSB0 -b 115200 -D -U flash:w:skriware2-printer-NB1.hex:i
```
```bash
sudo systemctl start northbolt
```

**From a PC with the board on USB:** run `pio run -e skriware2 -t upload --upload-port COM5`, using your port.

**After flashing:** check NORTHBOLT under Settings → Connection. The firmware line should end in "NB1 safety fixes".

The LED and scale board has no USB port. Flashing it needs an ISP programmer (for example a USBasp) on its programming header,
or whatever method Skriware used. Its fixes matter less than the printer board's, so skip it if you have no programmer.
