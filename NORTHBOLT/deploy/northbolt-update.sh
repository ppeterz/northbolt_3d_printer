#!/bin/sh
# Update NORTHBOLT from its GitHub repository. Installed as /usr/local/sbin/northbolt-update and run
# as root by northbolt-update.service, which the printer's screen starts (Settings -> System -> Software).
#
# 1. fetch the branch into a root-owned copy (/opt/northbolt-src; only the NORTHBOLT folder),
# 2. run its installer with the choices of the first install (/etc/northbolt/install.conf),
# 3. restart the server (and the screen, if its service changed).
# Progress goes to /run/northbolt-update/state and .../log, which the screen shows.
set -u

CONF=/etc/northbolt/install.conf
SRC=/opt/northbolt-src
STATUS=/run/northbolt-update
REPO_URL=https://github.com/ppeterz/northbolt_3d_printer.git
BRANCH=main
if [ -f "$CONF" ]; then
  . "$CONF"
fi

mkdir -p "$STATUS"
chmod 755 "$STATUS"
: > "$STATUS/log"
chmod 644 "$STATUS/log"
rm -f "$STATUS/reboot-needed"

state() {   # state=running|done|failed, and one line for the screen
  printf 'state=%s\nmessage=%s\ntime=%s\n' "$1" "$2" "$(date +%s)" > "$STATUS/state.tmp"
  chmod 644 "$STATUS/state.tmp"
  mv "$STATUS/state.tmp" "$STATUS/state"
}
fail() {
  echo "FAILED: $1"
  state failed "$1"
  exit 1
}
exec >>"$STATUS/log" 2>&1

state running "Downloading the new version"
echo "== $(date) updating from $REPO_URL ($BRANCH)"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" remote set-url origin "$REPO_URL"
  git -C "$SRC" fetch --depth 1 --filter=blob:none origin "$BRANCH" \
    || fail "Couldn't download from GitHub. Is the printer online?"
  git -C "$SRC" reset --hard FETCH_HEAD || fail "Couldn't unpack the download"
else
  rm -rf "$SRC"
  # only the NORTHBOLT folder: the firmware sources aren't needed on the Pi
  if git clone --depth 1 --branch "$BRANCH" --filter=blob:none --sparse "$REPO_URL" "$SRC"; then
    git -C "$SRC" sparse-checkout set NORTHBOLT || fail "Couldn't unpack the download"
  else
    rm -rf "$SRC"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC" \
      || fail "Couldn't download from GitHub. Is the printer online?"
  fi
fi
NEW="$(git -C "$SRC" log -1 --format='%h %s')"
echo "== got $NEW"
[ -f "$SRC/NORTHBOLT/deploy/install.sh" ] || fail "The download has no NORTHBOLT installer"

state running "Installing $NEW"
kiosk_before="$(md5sum /etc/systemd/system/northbolt-kiosk.service 2>/dev/null || true)"
NB_STATUS_DIR="$STATUS" sh "$SRC/NORTHBOLT/deploy/install.sh" \
  || fail "The installer stopped with an error (the log below says where)"
kiosk_after="$(md5sum /etc/systemd/system/northbolt-kiosk.service 2>/dev/null || true)"

if [ -f "$STATUS/reboot-needed" ]; then
  state done "Updated to $NEW. Restart the printer once to finish: the start-up screen changed."
else
  state done "Updated to $NEW"
fi
echo "== $(date) restarting NORTHBOLT"
systemctl restart northbolt.service
if [ "$kiosk_before" != "$kiosk_after" ]; then
  systemctl restart northbolt-kiosk.service
fi
echo "== done"
