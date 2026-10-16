#!/bin/sh
# Install NORTHBOLT on the printer's Raspberry Pi (current Raspberry Pi OS, Lite or Desktop).
#
#   sudo sh deploy/install.sh             # server + full-screen kiosk + boot splash
#   sudo sh deploy/install.sh --no-kiosk  # server only (use it from a phone or computer)
#   sudo sh deploy/install.sh --no-splash # kiosk without the boot splash (start-up text shows)
#
# Run it by hand once. After that, update from the printer's screen (Settings -> System -> Software):
# deploy/northbolt-update.sh fetches the new version and runs this script again with the same choices.
#
# Before the first run: back up the SD card and stop the original Skriware software so the serial
# port is free (see README.md).
set -eu

CONF=/etc/northbolt/install.conf
KIOSK=1
SPLASH=1
APP_USER=""
REPO_URL=""
BRANCH=""
# an update repeats the choices of the first install
if [ -f "$CONF" ]; then
  . "$CONF"
fi
for arg in "$@"; do
  case "$arg" in
    --no-kiosk) KIOSK=0 ;;
    --kiosk) KIOSK=1 ;;
    --no-splash) SPLASH=0 ;;
    --splash) SPLASH=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi

APP_USER="${SUDO_USER:-${APP_USER:-pi}}"
[ "$APP_USER" = "root" ] && APP_USER="${SUDO_USER:-pi}"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$SRC_DIR/.." && pwd)"
DEST=/opt/northbolt
PORT=8080
STATUS_DIR="${NB_STATUS_DIR:-}"     # set by northbolt-update.sh: where to say a restart is needed

echo "Installing for user $APP_USER from $SRC_DIR"

# Install the packages that are missing; refresh the package lists only when something is.
APT_UPDATED=""
need() {
  missing=""
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing="$missing $p"
  done
  [ -z "$missing" ] && return 0
  if [ -z "$APT_UPDATED" ]; then
    apt-get update
    APT_UPDATED=1
  fi
  apt-get install -y $missing
}
reboot_needed() {
  [ -n "$STATUS_DIR" ] && touch "$STATUS_DIR/reboot-needed"
  return 0
}

need python3 python3-aiohttp python3-serial python3-smbus2 python3-gpiozero iw udisks2 avrdude git

# Copy the app
mkdir -p "$DEST"
rm -rf "$DEST/northbolt"
cp -r "$SRC_DIR/northbolt" "$DEST/"
mkdir -p "$APP_HOME/northbolt-data"
chown -R "$APP_USER": "$APP_HOME/northbolt-data"

# Which commit this is, so the screen can tell when GitHub has a newer one
python3 - "$REPO_DIR" "$DEST/northbolt/BUILD" <<'PY'
import json, subprocess, sys
repo, out = sys.argv[1], sys.argv[2]
try:
    text = subprocess.run(["git", "-c", "safe.directory=*", "-C", repo, "log", "-1", "--format=%H%n%ct%n%s"],
                          capture_output=True, text=True, check=True).stdout
    sha, when, subject = (text.split("\n", 2) + ["", ""])[:3]
    with open(out, "w") as fh:
        json.dump({"sha": sha.strip(), "time": int(when), "subject": subject.strip()}, fh)
    print("Build", sha[:7], subject.strip())
except Exception as exc:
    print("No git information, so the screen can't check for NORTHBOLT updates:", exc)
PY

# Where updates come from: this copy's GitHub repository
if [ -z "$REPO_URL" ]; then
  REPO_URL="$(git -c safe.directory='*' -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
fi
REPO_URL="${REPO_URL:-https://github.com/ppeterz/northbolt_3d_printer.git}"
if [ -z "$BRANCH" ]; then
  BRANCH="$(git -c safe.directory='*' -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  case "$BRANCH" in ""|HEAD) BRANCH=main ;; esac
fi

# Serial, I2C and GPIO access
usermod -aG dialout,i2c,gpio,netdev "$APP_USER" || true

# Raspberry Pi Imager's settings (user, Wi-Fi, SSH) are applied by cloud-init on the first start.
# After that it only slows every start-up by several seconds, so switch it off.
if [ -d /etc/cloud ]; then
  touch /etc/cloud/cloud-init.disabled
fi
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
fi

# Let the app shut the Pi down when the power button asks for it
# ...and set the Wi-Fi country from the Wi-Fi screen (the app only passes two letters)
cat > /etc/sudoers.d/northbolt <<EOF
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
$APP_USER ALL=(root) NOPASSWD: /usr/bin/raspi-config nonint do_wifi_country *
EOF
chmod 440 /etc/sudoers.d/northbolt

# Let the app choose Wi-Fi networks through NetworkManager without a desktop login
mkdir -p /etc/polkit-1/rules.d
if command -v nmcli >/dev/null 2>&1; then
  cat > /etc/polkit-1/rules.d/50-northbolt-network.rules <<EOF
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.user == "$APP_USER") {
    return polkit.Result.YES;
  }
});
EOF
else
  echo "NetworkManager (nmcli) not found: the Wi-Fi screen will be unavailable on this OS."
fi

# Let the app open, close and power off USB drives and card readers (read-only mounts).
# Only these udisks actions: no formatting, no partitioning, no system disks.
cat > /etc/polkit-1/rules.d/50-northbolt-usb.rules <<EOF
polkit.addRule(function(action, subject) {
  var allowed = [
    "org.freedesktop.udisks2.filesystem-mount",
    "org.freedesktop.udisks2.filesystem-mount-other-seat",
    "org.freedesktop.udisks2.filesystem-unmount-others",
    "org.freedesktop.udisks2.power-off-drive",
    "org.freedesktop.udisks2.power-off-drive-other-seat",
    "org.freedesktop.udisks2.eject-media",
    "org.freedesktop.udisks2.eject-media-other-seat"
  ];
  if (subject.user == "$APP_USER" && allowed.indexOf(action.id) >= 0) {
    return polkit.Result.YES;
  }
});
EOF

# Updates from the screen: the app may start northbolt-update.service (and nothing else). That runs
# as root, outside the app, fetches the repository into a root-owned copy and runs this script from it.
cp "$SRC_DIR/deploy/northbolt-update.sh" /usr/local/sbin/northbolt-update.new
chmod 755 /usr/local/sbin/northbolt-update.new
mv /usr/local/sbin/northbolt-update.new /usr/local/sbin/northbolt-update   # a new file: a running update keeps the old one
cp "$SRC_DIR/deploy/northbolt-update.service" /etc/systemd/system/northbolt-update.service
cat > /etc/polkit-1/rules.d/50-northbolt-update.rules <<EOF
polkit.addRule(function(action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "$APP_USER" &&
      action.lookup("unit") == "northbolt-update.service" && action.lookup("verb") == "start") {
    return polkit.Result.YES;
  }
});
EOF
mkdir -p /etc/northbolt
cat > "$CONF" <<EOF
# Written by deploy/install.sh. Updates from the screen (deploy/northbolt-update.sh) repeat these choices.
APP_USER='$APP_USER'
KIOSK=$KIOSK
SPLASH=$SPLASH
REPO_URL='$REPO_URL'
BRANCH='$BRANCH'
EOF

# Server service
sed -e "s#@USER@#$APP_USER#g" -e "s#@HOME@#$APP_HOME#g" -e "s#@DEST@#$DEST#g" -e "s#@PORT@#$PORT#g" \
  "$SRC_DIR/deploy/northbolt.service" > /etc/systemd/system/northbolt.service

systemctl daemon-reload
systemctl enable --now northbolt.service

if [ "$KIOSK" -eq 1 ]; then
  need cage curl
  need fonts-inter || echo "fonts-inter not available, the screen uses the default font"
  if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    need chromium || need chromium-browser
  fi
  BROWSER="$(command -v chromium || command -v chromium-browser)"
  cat > /etc/pam.d/cage <<EOF
auth           required        pam_unix.so nullok
account        required        pam_unix.so
session        required        pam_unix.so
session        required        pam_systemd.so
EOF
  # invisible mouse pointer for cage (the kiosk unit points XCURSOR_PATH here)
  mkdir -p /usr/share/northbolt/cursors
  rm -rf /usr/share/northbolt/cursors/default
  cp -r "$SRC_DIR/deploy/cursor/default" /usr/share/northbolt/cursors/
  sed -e "s#@USER@#$APP_USER#g" -e "s#@BROWSER@#$BROWSER#g" -e "s#@PORT@#$PORT#g" \
    -e "s#@DEST@#$DEST#g" -e "s#@HOME@#$APP_HOME#g" \
    "$SRC_DIR/deploy/northbolt-kiosk.service" > /etc/systemd/system/northbolt-kiosk.service
  systemctl daemon-reload
  systemctl disable getty@tty1.service || true
  systemctl enable northbolt-kiosk.service
  systemctl set-default graphical.target
  echo "Kiosk installed. It starts on the next boot (or: sudo systemctl start northbolt-kiosk)."

  if [ "$SPLASH" -eq 1 ]; then
    # Boot splash: hides the start-up text behind the first frame of the intro video until the UI
    # takes over (northbolt-kiosk.service ends it). Undo: put back cmdline.txt.before-northbolt.
    need plymouth
    THEME=/usr/share/plymouth/themes/northbolt
    mkdir -p "$THEME"
    theme_before="$(cat "$THEME"/* 2>/dev/null | md5sum)"
    cp "$SRC_DIR"/deploy/plymouth/northbolt/* "$THEME"/
    theme_after="$(cat "$THEME"/* | md5sum)"
    BOOT=/boot/firmware
    [ -f "$BOOT/cmdline.txt" ] || BOOT=/boot
    [ -f "$BOOT/cmdline.txt.before-northbolt" ] || cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.before-northbolt"
    cmdline_before="$(md5sum < "$BOOT/cmdline.txt")"
    # cmdline.txt must stay one line: add the quiet-boot words that are missing, keep everything else
    python3 - "$BOOT/cmdline.txt" <<'PY'
import sys
path = sys.argv[1]
with open(path) as fh:
    words = fh.read().split()
words = [w for w in words if not w.startswith("loglevel=")]
for w in ("quiet", "splash", "plymouth.ignore-serial-consoles", "logo.nologo", "loglevel=3",
          "vt.global_cursor_default=0", "consoleblank=0"):
    if w not in words:
        words.append(w)
with open(path, "w") as fh:
    fh.write(" ".join(words) + "\n")
PY
    [ "$cmdline_before" = "$(md5sum < "$BOOT/cmdline.txt")" ] || reboot_needed
    if ! grep -q '^disable_splash=1' "$BOOT/config.txt"; then
      printf '\n# NORTHBOLT: no rainbow square at power-on\ndisable_splash=1\n' >> "$BOOT/config.txt"
      reboot_needed
    fi
    # set the theme and rebuild the initramfs (slow on a Pi 3) only when something changed
    if [ "$theme_before" != "$theme_after" ] || [ "$(plymouth-set-default-theme 2>/dev/null)" != "northbolt" ]; then
      plymouth-set-default-theme -R northbolt
      reboot_needed
    fi
    echo "Boot splash installed."
  fi
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. Open http://${IP:-<pi-address>}:$PORT from a phone or computer."
echo "Logs: journalctl -u northbolt -f"
if [ -z "$STATUS_DIR" ]; then
  echo "Log out and back in (or reboot) so the new group memberships apply."
fi
