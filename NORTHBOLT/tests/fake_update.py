"""Stands in for `systemctl start northbolt-update.service` in the tests: writes the status files the
real deploy/northbolt-update.sh writes, from "running" to "done" (or "failed" with FAIL in argv)."""
import os
import sys
import time

status_dir = sys.argv[1]
fail = "FAIL" in sys.argv[2:]
os.makedirs(status_dir, exist_ok=True)


def state(s, message):
    tmp = os.path.join(status_dir, "state.tmp")
    with open(tmp, "w") as fh:
        fh.write(f"state={s}\nmessage={message}\ntime={time.time()}\n")
    os.replace(tmp, os.path.join(status_dir, "state"))


with open(os.path.join(status_dir, "log"), "w") as fh:
    fh.write("== fake update from GitHub\n== got bbbbbbb New splash\n")
state("running", "Installing bbbbbbb New splash")
time.sleep(1.5)
if fail:
    state("failed", "Couldn't download from GitHub. Is the printer online?")
else:
    state("done", "Updated to bbbbbbb New splash")
