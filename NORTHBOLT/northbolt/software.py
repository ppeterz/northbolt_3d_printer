"""Update NORTHBOLT itself (this server and the touch screen UI) from its GitHub repository.

Checking: the newest commit on the branch that touches the NORTHBOLT folder is compared with the
commit this copy was installed from (the BUILD file the installer writes next to this package).

Updating: the app can't replace itself (the files are root's, and the update restarts this very
service), so it asks systemd to start northbolt-update.service. That runs deploy/northbolt-update.sh
as root, outside this process: fetch the repository into a root-owned copy, run the installer from
it, restart the services. Its progress goes to <status_dir>/state and <status_dir>/log, which the
screen shows; once this service comes back on the new code, the screen reloads itself.
"""

import asyncio
import json
import logging
import os
import time

import aiohttp

from .config import github_repo
from .firmware import FirmwareError, parse_repo

log = logging.getLogger("software")

BUILD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BUILD")


class SoftwareError(RuntimeError):
    pass


def read_build(path=None):
    """{"sha", "time", "subject"} of the installed commit, or None for a copy not made by the installer."""
    path = path or os.environ.get("NORTHBOLT_BUILD_FILE") or BUILD_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if data.get("sha") else None
    except (OSError, ValueError):
        return None


def read_status(status_dir):
    """The update service's progress: state (running/done/failed), message, time, and the log's end."""
    out = {}
    try:
        with open(os.path.join(status_dir, "state"), encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.rstrip("\n").partition("=")
                if key:
                    out[key] = value
    except OSError:
        return None
    try:
        with open(os.path.join(status_dir, "log"), encoding="utf-8", errors="replace") as fh:
            out["log"] = [l.rstrip() for l in fh.readlines()[-12:] if l.strip()]
    except OSError:
        out["log"] = []
    try:
        out["time"] = float(out.get("time") or 0)
    except ValueError:
        out["time"] = 0
    return out


class SoftwareUpdater:
    def __init__(self, svc):
        self.svc = svc
        self.build = read_build()
        self.available = None     # {"sha", "short", "subject", "date", "ahead_by", "changes": [...]}
        self.last_check = None
        self.checking = False
        self.error = None
        self.starting = False
        self.snoozed_until = 0.0

    @property
    def cfg(self):
        return self.svc.settings["software"]

    def status(self):
        return read_status(self.cfg["status_dir"])

    @property
    def running(self):
        st = self.status()
        if st and st.get("state") == "running" and time.time() - st["time"] < 3600:
            return True
        return self.starting

    def prompt(self):
        a = self.available
        if not a or self.running or self.svc.job.active:
            return False
        if a["sha"] == self.cfg.get("skipped"):
            return False
        st = self.status()
        if st and st.get("state") in ("done", "failed") and time.time() - st["time"] < 600:
            return False          # an update just ran: don't offer it again straight away
        return time.time() >= self.snoozed_until

    def snapshot(self):
        return {
            "build": self.build, "available": self.available, "checking": self.checking,
            "last_check": self.last_check, "error": self.error, "update": self.status(),
            "running": self.running, "prompt": self.prompt(), "skipped": self.cfg.get("skipped"),
            "auto_check": self.cfg.get("auto_check", True),
        }

    # ------------------------------------------------------------------ GitHub
    def _headers(self):
        h = {"Accept": "application/vnd.github+json", "User-Agent": "NORTHBOLT-printer"}
        token = self.svc.settings["firmware"].get("token")
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _get(self, session, url):
        async with session.get(url, headers=self._headers()) as r:
            if r.status in (401, 403):
                raise SoftwareError("The update server refused the request. Try again in an hour.")
            if r.status == 404:
                return None
            if r.status != 200:
                raise SoftwareError(f"The update server answered {r.status}")
            return await r.json(content_type=None)

    async def _fetch(self):
        """Newest commit touching the NORTHBOLT folder, and how far ahead of the installed one it is."""
        fw = self.svc.settings["firmware"]
        try:
            repo = parse_repo(github_repo(self.svc.settings))
        except FirmwareError as exc:
            raise SoftwareError(str(exc))
        api = f"{fw['github_api'].rstrip('/')}/repos/{repo}"
        cfg = self.cfg
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                commits = await self._get(s, f"{api}/commits?sha={cfg['branch']}&path={cfg['path']}&per_page=1")
                if not commits:
                    raise SoftwareError("No NORTHBOLT version found to update from")
                top = commits[0]
                latest = {
                    "sha": top["sha"], "short": top["sha"][:7],
                    "subject": (top.get("commit", {}).get("message") or "").split("\n", 1)[0][:200],
                    "date": top.get("commit", {}).get("committer", {}).get("date"),
                    "ahead_by": None, "changes": [],
                }
                if not self.build or self.build["sha"] == latest["sha"]:
                    return latest, False
                cmp = await self._get(s, f"{api}/compare/{self.build['sha']}...{latest['sha']}")
        except aiohttp.ClientError as exc:
            raise SoftwareError("Can't reach the update server. Is the printer online?")
        if cmp is None:          # the installed commit isn't on GitHub (a local change): offer the GitHub one
            return latest, True
        if cmp.get("status") in ("behind", "identical"):
            return latest, False
        latest["ahead_by"] = cmp.get("ahead_by")
        latest["changes"] = [(c.get("commit", {}).get("message") or "").split("\n", 1)[0][:160]
                             for c in (cmp.get("commits") or [])][-6:][::-1]
        return latest, True

    async def check(self):
        """'Check for updates' button. Returns the snapshot; raises SoftwareError for the screen."""
        if self.checking:
            raise SoftwareError("Already checking")
        self.checking = True
        self.error = None
        self.svc.notify("software")
        try:
            latest, newer = await self._fetch()
            self.available = latest if newer else None
            self.last_check = time.time()
        except SoftwareError as exc:
            self.error = str(exc)
            raise
        finally:
            self.checking = False
            self.svc.notify("software")
        return self.snapshot()

    async def auto_task(self):
        await asyncio.sleep(self.cfg.get("first_check_s", 90))
        while True:
            try:
                if self.cfg.get("auto_check", True) and self.build and not self.running:
                    try:
                        await self.check()
                        log.info("Software check: %s", f"{self.available['short']} available"
                                 if self.available else "up to date")
                    except SoftwareError as exc:
                        log.info("Software check failed: %s", exc)
                await asyncio.sleep(max(60.0, float(self.cfg.get("check_hours", 6)) * 3600))
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Software check task")
                await asyncio.sleep(3600)

    def snooze(self, hours=24):
        self.snoozed_until = time.time() + hours * 3600
        self.svc.notify("software")

    def skip(self):
        if not self.available:
            raise SoftwareError("There's no update to skip")
        self.svc.settings.update({"software": {"skipped": self.available["sha"]}})
        self.svc.notify("software")

    # ------------------------------------------------------------------ update
    async def start(self):
        if self.running:
            raise SoftwareError("An update is already running")
        if self.svc.job.active:
            raise SoftwareError("Wait for the print to finish: the update restarts the printer's software")
        if self.svc.fw and self.svc.fw.busy:
            raise SoftwareError("A firmware update is running")
        cmd = list(self.cfg["command"])
        self._started_at = time.time()
        self.starting = True
        self.error = None
        self.svc.notify("software")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
        except FileNotFoundError:
            self.starting = False
            self.svc.notify("software")
            raise SoftwareError("This printer can't update itself yet: run the installer once by hand "
                                "(see the README), after that updates work from here")
        except asyncio.TimeoutError:
            self.starting = False
            raise SoftwareError("The update service didn't start")
        if proc.returncode != 0:
            self.starting = False
            self.svc.notify("software")
            text = out.decode(errors="replace").strip().splitlines()
            raise SoftwareError("The update didn't start" + (f": {text[-1]}" if text else "")
                                + ". If this printer was installed before updates from the screen existed, "
                                  "run the installer once by hand.")
        log.info("Software update started")
        asyncio.ensure_future(self._until_running())
        return self.snapshot()

    async def _until_running(self):
        """Stay "starting" until the update service writes its first status (or 30 s pass). Any new
        status counts: a quick failure may already have replaced "running" with "failed"."""
        for _ in range(30):
            st = self.status()
            if st and st["time"] >= self._started_at - 1:
                break
            await asyncio.sleep(1)
        self.starting = False
        self.svc.notify("software")
