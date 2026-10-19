# NORTHBOLT

A replacement touch UI and print server for the Skriware 2, running on the printer's Raspberry Pi 3.
It talks to the existing Skriware Marlin 1.1.9 firmware in this repository, so the printer board
is not reflashed.

It runs the screen on the printer and is also usable from a phone or computer on the same network.

## Features

- **Printing**: upload G-code from the screen, a browser, or straight from PrusaSlicer/OrcaSlicer
  (OctoPrint-compatible upload). Shows slicer thumbnails, print time, filament, layer, height and finish time.
- **Print from a USB flash drive or SD card reader**: plug it into the Pi and a green dot appears on Files.
  The Files page gets a USB tab where you can browse folders and see thumbnails, print time and filament.
  Print copies the file to the Pi first, so you can pull the drive out once printing starts and power-loss resume still works.
  Drives are opened read-only, so pulling one out can't damage it. Eject is there too.
- **Pause, resume, cancel**: pause parks the head, retracts and lifts. Resume puts everything back,
  including the E position and fan. Nozzles switch off after 15 minutes paused, and Resume heats them again.
- **Filament runout**: the firmware only prints `FILAMENT_RUNOUT_E0/E1`. The host pauses the print, shows
  what happened, lets you load filament, and clears the sensor flag (`M63`) on resume.
- **M600, M0, M1 and @pause in G-code**: these have no handler in this firmware build. The host turns them into a pause.
- **Bed leveling assistant** (Calibrate → Bed level): probes the bed with auto bed leveling, then shows which of the three
  bed screws to turn, which way and how far. It shows a picture of the bed seen from above, plus a dial for each screw.
  Turn the screws, probe again, and repeat until all show a tick, then save the mesh.
- **Z offset paper test** (Calibrate → Z offset): a guided wizard for the left nozzle and, with the lift mechanism, the right nozzle.
  It includes a corner check afterwards.
- **Power-loss recovery** without extra hardware: resume after a power cut from just before the last
  confirmed move, without homing Z into the part.
- **Control**: jog, home, motors off, temperatures with presets, extrude and retract, tool select, extruder lift, fan, speed and flow.
- **Filament**: load and unload per nozzle, runout sensors on and off, and a sensor check.
- **LED strips and spool scales** via the ATmega board on I2C: progress on the side LEDs, and grams left on each
  spool after a one-time calibration.
- **Your own light colours** (Settings → LEDs). Each moment has its own colour and effect (solid, breathe, flash or off):
  Ready, heating, printing, paused, done, cancelled, error, filament, calibrating and the power button. **Test** shows
  one on the printer for a few seconds. There's also a work light (one colour all the time), the progress colours,
  brightness, and how long "done" stays lit.
- **Power button**: a press on the printer asks before turning off, with a countdown when idle. It then shuts the Pi down cleanly.
- **Wi-Fi setup on the screen**: the Wi-Fi button in the sidebar shows signal strength, "No Wi-Fi" in amber, or "Wi-Fi off".
  Tapping it lists nearby networks. Tap one and an on-screen keyboard opens for the password.
  You can also join hidden networks, forget saved ones, turn Wi-Fi on or off, and set the Wi-Fi country.
- **On-screen keyboard** for every text box on the Pi's screen, which has no system keyboard. Phones and computers use their own keyboard.
- **Firmware updates from GitHub** (Settings → Firmware): it installs the latest release's printer firmware over the USB
  cable. Nobody types a repository link: it's the one NORTHBOLT was installed from (recorded in `/etc/northbolt/install.conf`),
  else `https://github.com/ppeterz/northbolt_3d_printer`; `firmware.github_repo` in `settings.json` overrides both. It saves a backup of the current firmware first, and can restore it.
  It also checks by itself, shortly after start-up and then every 6 hours. When the latest release differs from what the board runs,
  it shows a prompt (never during a print) with **Later**, **Skip this version** and **Install**, plus an amber dot on Settings.
  See `FIRMWARE_SAFETY_FIXES.md` for how releases are made.
- **Console** with quick buttons for M114, M503, M420 V, G29 and others.
- **Emergency stop** on every screen.

## Install on the printer (on a new SD card)

NORTHBOLT goes on a new SD card that replaces the original one in the Pi. Keep the original card somewhere safe
as a backup. It's the only copy of the stock Skriware software, and putting it back returns the printer to how it was.

### 1. Keep the original card safe

- **Label it.** Then, whenever it is in a computer, slide the SD adapter's lock switch to *Lock* if it has one.
- **Never format it.** Windows can't read its Linux partition and will offer to format it. Always click **Cancel**.
- **Never copy files from it to the new card.** `cmdline.txt` names the card's own Linux partition, and
  `config.txt` must match the system on the card. Copied over, either one stops the new card from starting.

### 2. The screen needs no settings

Current Raspberry Pi OS asks the Skriware 2's 800×480 HDMI panel which mode it wants and uses it. Don't add
`hdmi_cvt`, `hdmi_group`, `hdmi_mode` or `video=` lines: with the KMS display driver they stop this panel showing a
picture (black or white screen). The older stock Skriware card needed them, the new one doesn't.

### 3. Prepare the new card

In **Raspberry Pi Imager**, choose *Raspberry Pi 3* and **Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)**.
Not the first "Raspberry Pi OS" entry: that one has a desktop, which fights the kiosk for the screen and fills an 8 GB card.
Under **Edit settings**, set:

- Hostname `northbolt`, a user name and a password. Write the password down.
- Your Wi-Fi. A **Pi 3 Model B only has 2.4 GHz Wi-Fi**. Use a 2.4 GHz network, or a network cable.
- **Services: Enable SSH** with password authentication.

Write the card and don't edit anything on it afterwards.

### 4. First start and install

**Power the Pi from the printer.** A laptop USB port can't power the Pi and the screen together: the Pi resets or
stops part-way through starting, which looks like a black or white screen and no network.

Put the card in the Pi, switch the printer on and wait about 5 minutes without touching it. The first start sets up the
user, Wi-Fi and SSH, may restart once, and ends at a `northbolt login:` prompt. Then log in over SSH
(`ssh <user>@northbolt.local`, or PuTTY) and run, one at a time:

```bash
sudo apt update && sudo apt install -y git
```
```bash
git clone https://github.com/ppeterz/northbolt_3d_printer.git
```
```bash
cd northbolt_3d_printer/NORTHBOLT && sudo sh deploy/install.sh
```
```bash
sudo reboot
```

### Updating NORTHBOLT

After the first install, NORTHBOLT updates itself from the GitHub repository it was installed from (the same one as the
firmware, so nothing to set up):

- **It checks by itself** every 6 hours, for new commits that change the `NORTHBOLT` folder. When one is newer than the
  installed copy, the screen offers it (**Later**, **Skip this version**, **Update**), and Settings shows an orange dot.
- **Or by hand**: **Settings → System → NORTHBOLT software → Check now / Update now**.

An update takes a minute or two and refuses to start while printing. The screen shows its progress and reloads itself
when it's done. Settings and files in `~/northbolt-data` are kept. If the boot splash or the start-up line changed, it
asks you to restart the printer once.

**How it works:** the app may start one root service, `northbolt-update.service`, and nothing else (a polkit rule). That
service runs `deploy/northbolt-update.sh`, outside the app. It fetches only the `NORTHBOLT` folder into a root-owned copy
(`/opt/northbolt-src`), then runs the installer from that copy with the choices of the first install
(`/etc/northbolt/install.conf`), then restarts NORTHBOLT. Its log is in `/run/northbolt-update/log` and on the screen.

Printers installed before this feature need one last update by hand. Then the screen can update itself:

```bash
cd ~/northbolt_3d_printer && git pull && cd NORTHBOLT && sudo sh deploy/install.sh
```

Use `--no-kiosk` after `install.sh` if you only want the server, with no full-screen UI on the attached screen.
After the reboot the screen shows the UI. From a phone, open the address shown under **Settings → System**.
Press the printer's power button so the ATmega powers the printer board. The UI connects by itself.

**If the printer board is wired to the Pi's GPIO pins** instead of a USB cable, the UI shows "No serial port found".
Turn off the Linux login console on that port, otherwise it sends text to the printer board. Then enable the UART and reboot:

```bash
sudo raspi-config nonint do_serial_cons 1 && sudo raspi-config nonint do_serial_hw 0 && sudo reboot
```

### 5. Read the power-button pin from the original card (optional)

Do this before you put the original card away. With the new card running, plug the original card into the Pi through a USB card reader. Mount it **read-only**
(`noload` stops Linux replaying its journal, which would write to it), search the Skriware software, then unmount:

```bash
sudo mkdir -p /mnt/legacy && sudo mount -o ro,noload /dev/sda2 /mnt/legacy
```
```bash
sudo grep -rIn -i -E "gpio|ttyUSB|ttyAMA|serial0|ttyS0" /mnt/legacy/home /mnt/legacy/opt /mnt/legacy/etc 2>/dev/null | grep -v -i -E "\.(html|css|md):" | head -40
```
```bash
sudo umount /mnt/legacy
```

Put the power-button pin in `power_button_gpio` in `~/northbolt-data/settings.json`, then restart the server:

```bash
sudo systemctl restart northbolt
```

Then take the original card out of the reader and put it away.

### 6. Before the first print

- **Run the Z offset paper test once** (Z offset page) on each nozzle you use. The offset is stored on the printer board,
  not the SD card, so the old value carries over. But the Skriware software may have set values while it ran that were never saved to the board.
- **Check the first layer** of your first print.

The installer uses Raspberry Pi OS packages (`python3-aiohttp`, `python3-serial`, `python3-smbus2`,
`python3-gpiozero`). It also installs `cage` and Chromium for the kiosk. It enables I2C, adds the user to `dialout`, `i2c` and
`gpio`, and allows `systemctl poweroff` without a password.

For USB drives, the installer adds `udisks2` and a polkit rule that lets the app's user mount, unmount and
power off removable drives. It can't format or partition anything. The Pi's own SD card, and a USB disk the
system boots from, are never offered.

For the Wi-Fi screen, the installer adds a polkit rule so the app's user can manage NetworkManager. It also allows
`raspi-config nonint do_wifi_country` without a password. Wi-Fi setup needs NetworkManager, which current Raspberry Pi OS uses.
On older images that use `dhcpcd`, the Wi-Fi tab says it isn't available.

Settings live in `~/northbolt-data/settings.json`. Uploaded files are in `~/northbolt-data/gcodes/`.

### Start-up splash

From power-on to the UI, the printer's screen shows no start-up text:

1. **Boot splash** (Plymouth, theme in `deploy/plymouth/northbolt`): the first frame of the intro, a point of light on
   black, breathing while the Pi starts. The installer also adds `quiet splash …` to `cmdline.txt` (keeping a copy as
   `cmdline.txt.before-northbolt`) and `disable_splash=1` to `config.txt`, which removes the rainbow square.
2. **Intro**: Chromium takes about 20 s to start on a Pi 3, so the kiosk starts it straight away, alongside the
   server. Its first page is `static/boot.html`, opened from the SD card. It plays `static/intro.mp4` from just after that
   frame while the NORTHBOLT logo forms, waits on the logo until the server answers, then opens the UI, which fades into
   Home. Tap to skip it. Turn it off in **Settings → System**. The server writes that choice to
   `~/northbolt-data/boot.js` for the start-up page.

The kiosk keeps cage's and Chromium's messages in the journal (`journalctl -u northbolt-kiosk`), not on the screen.
It paints Chromium's window black and uses an invisible mouse pointer (`deploy/cursor`). The installer also switches
off cloud-init once it has applied the Imager settings, because it adds several seconds to every start.

Phones and computers only see the logo for a moment while the page loads.

Install without the boot splash with `sudo sh deploy/install.sh --no-splash`. To remove it later, copy
`cmdline.txt.before-northbolt` back over `cmdline.txt` on the `bootfs` drive. That brings the start-up text back.

### If part of the touch panel doesn't respond

Open **Settings → System → Touch test** on the printer's screen and slide a finger slowly from the top edge to the
bottom edge in two or three places. Strips that respond turn green. **Save** stores the strip that never responded,
and the printer's screen then keeps every button out of it:

- **Side bar:** Home, Files and Control sit above the strip, Filament and Calibrate below it.
- **Top bar:** Wi-Fi and Settings are always here.
- **Home:** buttons above or below the strip, information inside it.
- **Dialogs:** content above the strip, buttons below it. The on-screen keyboard splits around it.
- **Messages:** they go behind the status label in the top bar (tap it), so they don't push the page into the strip.
- **Other pages:** can be dragged up or down to move any button out of the strip.

Phones and computers keep the normal layout. **Clear** on the same card uses the whole screen again.

### Things to set on first run

| Setting | Where | Why |
|---|---|---|
| Serial port | Settings → Connection | `auto` takes the first USB serial port, else the Pi's GPIO serial port (`/dev/serial0`). The firmware runs at 115200. |
| Power button GPIO | `power_button_gpio` in `settings.json` | The Pi pin wired to the ATmega `SlaveFlag` line (A3) isn't in this repo. Check that it is level-shifted to 3.3 V. Leave `null` to disable. |
| Lift mechanism | Settings → General → Machine | On means the right nozzle has its own Z offset (`M206 E`). |
| Load and unload lengths | Settings → General → Filament | Depends on your filament path. The defaults are a guess. |
| Scales | Settings → Scales | Tare the empty holder, then calibrate with a known weight. |
| Park position | `pause.park_x`, `pause.park_y` | Defaults to the firmware's park point (10, 265). |

### Slicer upload

In PrusaSlicer or OrcaSlicer, add a physical printer of type **OctoPrint**. Use `http://<pi-address>:8080`
and the API key from **Settings → System**. "Upload and print" then starts the print directly.

## How the Z offset works on this firmware

Logical Z, as used by G-code and `M114`, is machine Z plus `home_offset[Z]`.

- **Left nozzle**: `M206 Z<v>` sets the offset immediately and `M500` saves it.
- **Right nozzle with the lift mechanism**: the firmware swaps in `home_offset_E1` when it switches to T1.
  `M206 E<v>` sets it and `M206 I` reports it.

The wizard homes, moves to the bed centre a few millimetres above the probe trigger point, and turns
software endstops off so you can go lower. You lower in 1, 0.1, 0.05 and 0.01 mm steps. When the paper drags at
logical Z = m, the new offset is the old offset minus m. Raise **Paper thickness** in settings if you want Z 0
to be the nozzle touching the bed rather than resting on the paper. The default is 0, the classic paper test.

## How power-loss recovery works

With no voltage sensing, the Pi records progress as it prints:

1. **At each layer change**, it waits for the printer to finish (`M400`) and saves a checkpoint with the full state.
2. **Before every Z move or tool change**, it waits for the printer to finish and saves where the head is about to be.
   After a power cut, the head height is therefore known exactly. Without this, the head could already be a layer
   or a Z-hop further up than the resume point. Each Z-hop gets a stop of about 20 ms.
3. **Every 2 seconds**, it saves the last line the firmware acknowledged. Up to 26 moves may still be queued at that point.

All saves are atomic (write, flush, rename), so a power cut never leaves a half-written file.

After a power cut, the UI offers to resume. The sequence:

1. Heat the bed and nozzles.
2. Set Z to the saved head height with `G92` (never home Z, because the probe sits under the part).
3. Lift, home X and Y, re-enable the bed mesh, select the tool, and prime.
4. Return to the resume point and continue.

The resume point is the last safe line, at most 26 moves before the last acknowledged one. Expect a small seam
and a few millimetres printed twice.

**Limits:**
- A power cut during the brief Z move itself leaves the height uncertain by that move. Z-hops are 0.2 to 0.6 mm.
- Long outages let ABS and PETG cool and lift off the bed.
- Resuming inside the first few millimetres skips Skriware's E-fade setup (`M58`/`M60`), so those layers may extrude slightly differently.
- If the right nozzle was printing, check the extruder lift is raised before resuming. The firmware forgets the lift state on reboot.

A UPS for the Pi, and ideally the printer, turns an outage into a pause and removes the SD-card risk entirely.

## How the bed leveling assistant works

The bed sits on three screws: one at the front (Y0) and two at the back. G29 probes a 5×5 mesh between X 20–180 and
Y 32–255. The probe is 32 mm behind the nozzle, so it can't reach the front screw directly. Instead, the assistant
fits a plane through the mesh and reads the bed height at each screw from it. Three points define a plane, so turning
each screw by its height difference to the reference screw makes the bed flat. Whatever doesn't fit the plane is
warp: the screws can't fix it, but the mesh makes up for it while printing.

**Set these once (Calibrate → Bed level):**
- **Screw positions…**: moves the nozzle over each screw so you can line it up with the jog buttons and save it.
  The defaults are front (100, 0), back left (15, 265) and back right (185, 265).
- **Options…**:
  - **Thread**: the screw size. M3 moves 0.5 mm per turn, M4 0.7 mm and M5 0.8 mm.
  - **Direction**: whether turning clockwise, seen from above, lowers or raises the bed. Check it once by turning a screw a little.
  - **Reference screw**: the one left alone. It defaults to the front screw.

## Firmware quirks the host works around

- G28 always switches to T0 and raises the lift. The stock firmware didn't restore T0's Z offset if T1 was active.
  The NB1 safety fixes in this repository correct that (see `FIRMWARE_SAFETY_FIXES.md`). The wizards still refuse to start
  on T1 in case the board runs the stock firmware. The host also sends `M420 S1` after homing, because G28 turns leveling off.
- Custom M-codes replace standard ones: `M80` is the extruder lift, `M206 E/I` is the T1 offset,
  `M61`/`M62`/`M63`/`M64` are the runout sensors, and `M666` erases EEPROM. Don't send `M666`.
- Every numbered line is echoed as `LINE ACCEPTED:<n>`, and bad lines as `BAD COMMAND:`. The host ignores both and handles resends.
- Cancel uses the emergency parser's `M411` (quick stop and clear queue), then resynchronises line numbers.

## Development

No printer needed. The simulator speaks the same serial dialect as this firmware, including the queue and planner
depth, the Z offset and extruder-swap code, runout messages and the emergency parser.

```bash
python tools/simulator.py --port 5555 --admin 5556 --speed 5
```
```bash
python -m northbolt --data ./dev-data --serial socket://127.0.0.1:5555 --port 8090
```

Then open http://localhost:8090. Opening it as `localhost` makes the page behave like the Pi's screen, with the on-screen keyboard.
Add `?osk=1` to force that keyboard from another address. Set `NORTHBOLT_FAKE_WIFI=1` before starting the server to try the Wi-Fi screen with
pretend networks. The password for all of them is `northbolt`. Set `NORTHBOLT_FAKE_USB=<folder>` to treat a folder
as a plugged-in USB drive. Send `runout 0`, `state` or `powerloss` to the admin port to inject events.

Tests (unit, plus end-to-end runs of the real host against the simulator):

```bash
python -m unittest discover -s tests -v
```

The end-to-end tests cover printing with injected checksum errors and resends, pause and resume, M600, runout,
cancel, emergency stop, slicer upload, the paper test on both nozzles, and power loss on both nozzles.
The power-loss tests check that the nozzle-to-Z relationship after resume is the same as before the outage, and that no move was skipped.

## Not yet verified on the real printer

Everything above was tested against the simulator, not the Skriware 2 itself. Check these on the machine:

1. The extruder lift's real behaviour when resuming a right-nozzle print after a reboot.
2. Reading the scales over I2C.
3. The power button GPIO wiring.
4. Chromium kiosk performance on the Pi 3 with your screen.
5. Joining Wi-Fi through NetworkManager on the Pi. It was tested against a pretend backend.
6. Mounting real USB drives with udisks2 on the Pi. Drive detection was tested with sample `lsblk` output, and browsing,
   copying and printing with a folder standing in for a drive.
7. The bed leveling assistant on the real bed. It was tested against a simulated tilted bed.
   Check the screw positions, thread and turning direction once.
8. Firmware updates with real avrdude and the real board. They were tested with a fake avrdude and a fake GitHub server.
   Do the first one with the printer idle and stand by it.
9. The boot splash and its hand-over to the kiosk on the Pi. The intro and its fade into the UI were tested in a
   desktop browser, not in Chromium on the Pi 3.

Do the first resume test with a small, sacrificial print, standing at the printer with a hand near the power switch.
