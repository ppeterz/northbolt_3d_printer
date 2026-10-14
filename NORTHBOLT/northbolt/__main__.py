"""Run with:  python -m northbolt [--data DIR] [--serial PORT] [--port 8080]"""

import argparse
import logging
import os

from aiohttp import web

from .config import Settings
from .printer import PrinterService
from .server import make_app


def main():
    ap = argparse.ArgumentParser(description="NORTHBOLT: touch UI and print server for the Skriware 2")
    ap.add_argument("--data", default=os.path.expanduser("~/northbolt-data"), help="settings, uploads, recovery")
    ap.add_argument("--serial", help="serial port or pyserial URL (overrides settings)")
    ap.add_argument("--host", help="HTTP bind address (overrides settings)")
    ap.add_argument("--port", type=int, help="HTTP port (overrides settings)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    settings = Settings(args.data)
    if args.serial:
        settings.override("serial_port", args.serial)
    svc = PrinterService(settings)
    app = make_app(svc)
    web.run_app(app, host=args.host or settings["http_host"], port=args.port or settings["http_port"],
                print=lambda *_: logging.info("Serving on http://%s:%s", args.host or settings["http_host"],
                                              args.port or settings["http_port"]))


if __name__ == "__main__":
    main()
