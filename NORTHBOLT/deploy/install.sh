#!/bin/sh
# Install NORTHBOLT on the printer's Raspberry Pi (current Raspberry Pi OS, Lite or Desktop).
#
#   sudo sh deploy/install.sh             # server + full-screen kiosk + boot splash
#   sudo sh deploy/install.sh --no-kiosk  # server only (use it from a phone or computer)
#   sudo sh deploy/install.sh --no-splash # kiosk without the boot splash (start-up text shows)
#
# Before running: back up the SD card and stop the original Skriware software so the serial
# port is free (see README.md).
set -eu

KIOSK=1
SPLASH=1
for arg in "$@"; do
  case "$arg" in
    --no-kiosk) KIOSK=0 ;;
    --no-splash) SPLASH=0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi

APP_USER="${SUDO_USER:-pi}"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/northbolt
PORT=8080

echo "Installing for user $APP_USER from $SRC_DIR"

apt-get update
apt-get install -y python3 python3-aiohttp python3-serial python3-smbus2 python3-gpiozero iw udisks2 avrdude

# Copy the app
mkdir -p "$DEST"
rm -rf "$DEST/northbolt"
cp -r "$SRC_DIR/northbolt" "$DEST/"
mkdir -p "$APP_HOME/northbolt-data"
chown -R "$APP_USER": "$APP_HOME/northbolt-data"

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
if command -v nmcli >/dev/null 2>&1; then
  mkdir -p /etc/polkit-1/rules.d
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
mkdir -p /etc/polkit-1/rules.d
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

# Server service
sed -e "s#@USER@#$APP_USER#g" -e "s#@HOME@#$APP_HOME#g" -e "s#@DEST@#$DEST#g" -e "s#@PORT@#$PORT#g" \
  "$SRC_DIR/deploy/northbolt.service" > /etc/systemd/system/northbolt.service

systemctl daemon-reload
systemctl enable --now northbolt.service

if [ "$KIOSK" -eq 1 ]; then
  apt-get install -y cage curl
  apt-get install -y fonts-inter || echo "fonts-inter not available, the screen uses the default font"
  apt-get install -y chromium || apt-get install -y chromium-browser
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
    apt-get install -y plymouth
    mkdir -p /usr/share/plymouth/themes/northbolt
    cp "$SRC_DIR"/deploy/plymouth/northbolt/* /usr/share/plymouth/themes/northbolt/
    BOOT=/boot/firmware
    [ -f "$BOOT/cmdline.txt" ] || BOOT=/boot
    [ -f "$BOOT/cmdline.txt.before-northbolt" ] || cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.before-northbolt"
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
    grep -q '^disable_splash=1' "$BOOT/config.txt" || \
      printf '\n# NORTHBOLT: no rainbow square at power-on\ndisable_splash=1\n' >> "$BOOT/config.txt"
    # set the theme and rebuild the initramfs, so the splash shows early in the boot
    plymouth-set-default-theme -R northbolt
    echo "Boot splash installed."
  fi
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. Open http://${IP:-<pi-address>}:$PORT from a phone or computer."
echo "Logs: journalctl -u northbolt -f"
echo "Log out and back in (or reboot) so the new group memberships apply."
