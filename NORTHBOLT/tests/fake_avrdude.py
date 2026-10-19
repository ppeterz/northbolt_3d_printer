"""Stands in for avrdude in tests: prints the same progress bars and logs what it was asked to do.

Reading (-U flash:r:FILE:i) writes what a real ATmega2560 read gives: the "board's" firmware from
$FAKE_AVRDUDE_FLASH, then empty (0xFF) flash, then the 8 KB bootloader at 0x3E000.
Writing (-U flash:w:FILE:i) copies FILE into $FAKE_AVRDUDE_FLASH, then "verifies".
Each call is appended to $FAKE_AVRDUDE_LOG as "r FILE" or "w FILE".
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from northbolt.firmware import APP_LIMIT, emit_hex, parse_hex  # noqa: E402

BOOTLOADER = bytes((0x0C, 0x94, 0x3E, 0xF0) * 2048)     # 8 KB that isn't empty flash


def bar(label):
    sys.stderr.write(f"\n{label} | ")
    for _ in range(50):
        sys.stderr.write("#")
        sys.stderr.flush()
        time.sleep(0.004)
    sys.stderr.write(" | 100% 0.20s\n")


args = sys.argv[1:]
op = args[args.index("-U") + 1]          # flash:<r|w>:<file>:i  (the file may contain "C:")
mode = op.split(":")[1]
path = op[len("flash:x:"):-len(":i")]
flash = os.environ["FAKE_AVRDUDE_FLASH"]
with open(os.environ["FAKE_AVRDUDE_LOG"], "a") as fh:
    fh.write(f"{mode} {path}\n")
sys.stderr.write("avrdude: AVR device initialized and ready to accept instructions\n")
if mode == "r":
    bar("Reading")
    with open(flash, "rb") as fh:
        image = parse_hex(fh.read())
    top = max(image) + 1
    image.update({a: 0xFF for a in range(top, APP_LIMIT)})              # erased flash after the firmware
    image.update({APP_LIMIT + i: b for i, b in enumerate(BOOTLOADER)})   # the bootloader at the top
    with open(path, "wb") as fh:
        fh.write(emit_hex(image))
elif mode == "w":
    bar("Writing")
    shutil.copyfile(path, flash)
    bar("Reading")
    sys.stderr.write("avrdude: verifying ... flash verified\n")
sys.stderr.write("avrdude done.  Thank you.\n")
