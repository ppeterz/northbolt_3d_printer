"""Stands in for avrdude in tests: prints the same progress bars and logs what it was asked to do.

Reading (-U flash:r:FILE:i) writes the contents of $FAKE_AVRDUDE_FLASH (the "board's" firmware).
Writing (-U flash:w:FILE:i) copies FILE into $FAKE_AVRDUDE_FLASH, then "verifies".
Each call is appended to $FAKE_AVRDUDE_LOG as "r FILE" or "w FILE".
"""
import os
import shutil
import sys
import time


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
    shutil.copyfile(flash, path)
elif mode == "w":
    bar("Writing")
    shutil.copyfile(path, flash)
    bar("Reading")
    sys.stderr.write("avrdude: verifying ... flash verified\n")
sys.stderr.write("avrdude done.  Thank you.\n")
