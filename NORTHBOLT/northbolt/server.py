"""aiohttp web server: static UI, REST API, WebSocket state push, OctoPrint-style upload."""

import asyncio
import base64
import json
import logging
import os
import tempfile

from aiohttp import WSMsgType, web

from . import __version__
from .gcode import quick_scan
from .printer import safe_filename

log = logging.getLogger("server")
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_UPLOAD = 1024 * 1024 * 1024
LOCAL = ("127.0.0.1", "::1", "localhost")
EDITABLE = {"firmware", "leveling", "presets", "filament", "pause", "recovery", "runout", "zoffset", "leds", "scales",
            "lift_mechanism", "serial_port", "baudrate", "auto_connect", "power_button_gpio",
            "require_key_remote", "touch_dead_zone", "boot_intro"}


def write_boot_js(settings):
    """<data>/boot.js tells the screen's start-up page (static/boot.html, opened before this server
    answers) whether to play the intro."""
    path = os.path.join(settings.data_dir, "boot.js")
    text = "window.NB_BOOT = %s;\n" % json.dumps({"intro": bool(settings["boot_intro"])})
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)


def check_dead_zone(value):
    """None, or {"top": int, "bottom": int} with 0 <= top < bottom <= 4000 (screen pixels)."""
    if value is None:
        return None
    try:
        top, bottom = int(value["top"]), int(value["bottom"])
    except (TypeError, KeyError, ValueError):
        raise ValueError("touch_dead_zone needs top and bottom in pixels") from None
    if not 0 <= top < bottom <= 4000:
        raise ValueError("touch_dead_zone: top must be above bottom")
    return {"top": top, "bottom": bottom}


def json_error(status, message):
    return web.json_response({"error": message}, status=status)


@web.middleware
async def auth_and_errors(request, handler):
    path = request.path
    svc = request.app["svc"]
    if path.startswith("/api") or path == "/ws":
        s = svc.settings
        if s["require_key_remote"] and request.remote not in LOCAL:
            key = request.headers.get("X-Api-Key") or request.query.get("apikey")
            if key != s["api_key"]:
                return json_error(403, "API key required")
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (RuntimeError, ValueError) as exc:
        return json_error(400, str(exc))
    except Exception as exc:
        log.exception("Request %s failed", path)
        return json_error(500, f"Internal error: {exc}")


async def body(request):
    if request.can_read_body:
        try:
            return await request.json()
        except ValueError:
            return {}
    return {}


def ok(data=None):
    return web.json_response(data if data is not None else {"ok": True})


def make_app(svc):
    app = web.Application(middlewares=[auth_and_errors], client_max_size=4 * 1024 * 1024)
    app["svc"] = svc
    r = app.router

    async def index(request):
        return web.FileResponse(os.path.join(STATIC, "index.html"),
                                headers={"Cache-Control": "no-cache"})

    async def websocket(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        svc.clients.add(ws)
        await ws.send_str(json.dumps(svc.snapshot()))
        await ws.send_str(json.dumps({"type": "log", "entries": list(svc.console)[-150:]}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            svc.clients.discard(ws)
        return ws

    async def state(request):
        return ok(svc.snapshot())

    # ---------------------------------------------------------------- files
    async def files(request):
        return ok({"files": svc.list_files(), "free": _free_bytes(svc.gcode_dir)})

    async def save_upload(request):
        reader = await request.multipart()
        saved = None
        start = False
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                name = safe_filename(part.filename)
                fd, tmp = tempfile.mkstemp(dir=svc.gcode_dir, prefix=".upload-")
                size = 0
                try:
                    with os.fdopen(fd, "wb") as fh:
                        while True:
                            chunk = await part.read_chunk(256 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > MAX_UPLOAD:
                                raise ValueError("File too large")
                            fh.write(chunk)
                        fh.flush()
                        os.fsync(fh.fileno())
                    if svc.job.active and svc.job.file == name:
                        raise RuntimeError("That file is printing; rename it before uploading")
                    os.replace(tmp, os.path.join(svc.gcode_dir, name))
                except BaseException:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
                saved = name
                try:
                    os.unlink(svc._meta_path(name))
                except FileNotFoundError:
                    pass
                svc.schedule_scan(name)
            elif part.name == "print":
                start = (await part.text()).strip().lower() in ("true", "1", "yes")
            else:
                await part.release()
        if not saved:
            raise ValueError("No file in upload")
        svc.notify("files")
        return saved, start

    async def upload(request):
        name, start = await save_upload(request)
        if start:
            await svc.start_print(name)
        return ok({"name": name})

    async def delete_file(request):
        svc.delete_file(request.match_info["name"])
        return ok()

    async def thumb(request):
        name = safe_filename(request.match_info["name"])
        meta = svc.read_meta(name)
        if meta is None and os.path.exists(os.path.join(svc.gcode_dir, name)):
            meta = await asyncio.get_running_loop().run_in_executor(
                None, quick_scan, os.path.join(svc.gcode_dir, name))
        data_url = (meta or {}).get("thumbnail")
        if not data_url:
            raise web.HTTPNotFound()
        header, b64 = data_url.split(",", 1)
        mime = header[5:].split(";")[0]
        return web.Response(body=base64.b64decode(b64), content_type=mime,
                            headers={"Cache-Control": "max-age=3600"})

    # ---------------------------------------------------------------- jobs
    async def print_file(request):
        await svc.start_print((await body(request)).get("file"))
        return ok()

    async def job_action(request):
        action = request.match_info["action"]
        if action == "pause":
            if not svc.job.pause("Paused from the screen"):
                raise RuntimeError("Nothing to pause")
        elif action == "resume":
            if not svc.job.resume():
                raise RuntimeError("Not paused")
        elif action == "cancel":
            if not await svc.job.cancel():
                raise RuntimeError("Nothing to cancel")
        else:
            raise web.HTTPNotFound()
        return ok()

    async def recovery(request):
        action = request.match_info["action"]
        if action == "resume":
            await svc.resume_recovery()
        elif action == "discard":
            svc.discard_recovery()
        else:
            raise web.HTTPNotFound()
        return ok()

    # ---------------------------------------------------------------- control
    async def temp(request):
        b = await body(request)
        await svc.set_temp(b.get("heater"), b.get("target", 0))
        return ok()

    async def cooldown(request):
        for h in ("t0", "t1", "bed"):
            await svc.set_temp(h, 0)
        return ok()

    async def move(request):
        b = await body(request)
        await svc.move(b.get("x", 0), b.get("y", 0), b.get("z", 0), b.get("feed"))
        return ok()

    async def home(request):
        await svc.home((await body(request)).get("axes", ""))
        return ok()

    async def motors_off(request):
        await svc.motors_off()
        return ok()

    async def extrude(request):
        b = await body(request)
        await svc.extrude(int(b.get("tool", 0)), float(b.get("amount", 5)), float(b.get("speed", 3)))
        return ok()

    async def filament(request):
        b = await body(request)
        action = b.get("action")
        if action not in ("load", "unload"):
            raise ValueError("action must be load or unload")
        await svc.filament(action, int(b.get("tool", 0)), b.get("temp"))
        return ok()

    async def tool(request):
        await svc.select_tool(int((await body(request)).get("tool", 0)))
        return ok()

    async def lift(request):
        await svc.lift((await body(request)).get("direction", "up"))
        return ok()

    async def fan(request):
        await svc.set_fan(int((await body(request)).get("value", 0)))
        return ok()

    async def speed(request):
        await svc.set_speed(int((await body(request)).get("value", 100)))
        return ok()

    async def flow(request):
        await svc.set_flow(int((await body(request)).get("value", 100)))
        return ok()

    async def runout(request):
        b = await body(request)
        await svc.set_runout(b.get("side"), bool(b.get("enabled")))
        return ok()

    async def runout_query(request):
        return ok({"status": await svc.query_runout()})

    async def console(request):
        resp = await svc.send_console((await body(request)).get("command", ""))
        return ok({"response": resp})

    # ---------------------------------------------------------------- z offset wizard
    async def zoffset(request):
        action = request.match_info["action"]
        b = await body(request)
        w = svc.wizard
        if action == "start":
            w.start(int(b.get("tool", 0)), b.get("heat"))
        elif action == "step":
            await w.step(float(b.get("delta", 0)))
        elif action == "accept":
            await w.accept()
        elif action == "check":
            await w.check(b.get("point", "center"))
        elif action == "finish":
            await w.finish()
        elif action == "cancel":
            await w.cancel()
        else:
            raise web.HTTPNotFound()
        return ok(w.snapshot())

    # ---------------------------------------------------------------- bed leveling
    async def leveling(request):
        action = request.match_info["action"]
        b = await body(request)
        lv = svc.leveling
        if action == "start":
            lv.start(probe=b.get("probe", True) is not False, heat=b.get("heat"))
        elif action == "cancel":
            await lv.cancel()
        elif action == "finish":
            await lv.finish()
        elif action == "save":
            await lv.save()
        elif action == "teach_start":
            await lv.teach_start(int(b.get("index", 0)))
        elif action == "teach_go":
            await lv.teach_go(int(b.get("index", 0)))
        elif action == "teach_jog":
            await lv.teach_jog(float(b.get("dx", 0)), float(b.get("dy", 0)))
        elif action == "teach_save":
            await lv.teach_save()
        elif action == "teach_done":
            await lv.teach_done()
        else:
            raise web.HTTPNotFound()
        return ok(lv.snapshot())

    # ---------------------------------------------------------------- connection and system
    async def connection(request):
        action = request.match_info["action"]
        if action == "connect":
            await svc.connect()
        elif action == "disconnect":
            await svc.disconnect()
        elif action == "reconnect":
            await svc.reconnect()
        elif action == "m999":
            await svc.restart_m999()
        elif action == "estop":
            await svc.emergency_stop()
        else:
            raise web.HTTPNotFound()
        return ok()

    async def dismiss(request):
        svc.dismiss_alert(int((await body(request)).get("id", 0)))
        return ok()

    async def scales(request):
        action = request.match_info["action"]
        b = await body(request)
        side = b.get("side")
        if side not in ("left", "right"):
            raise ValueError("side must be left or right")
        if action == "tare":
            raw = await svc.scale_tare(side)
        elif action == "calibrate":
            raw = await svc.scale_calibrate(side, float(b.get("grams", 0)))
        else:
            raise web.HTTPNotFound()
        return ok({"raw": raw})

    async def power(request):
        action = request.match_info["action"]
        if action == "off":
            await svc.power_off(bool((await body(request)).get("force")))
        elif action == "cancel":
            svc.cancel_power()
        elif action == "request":
            svc.on_power_request()
        else:
            raise web.HTTPNotFound()
        return ok()

    async def system(request):
        local = request.remote in LOCAL
        return ok({"version": __version__, "ips": _local_ips(), "port": request.url.port or svc.settings["http_port"],
                   "api_key": svc.settings["api_key"] if local else None, "local": local,
                   "require_key_remote": svc.settings["require_key_remote"]})

    async def get_settings(request):
        values = svc.settings.public()
        if request.remote not in LOCAL:
            values["api_key"] = None  # only shown on the printer's own screen
        if values.get("firmware", {}).get("token"):
            values["firmware"]["token"] = "••••"   # never sent back; the screen only shows that one is set
        return ok(values)

    async def post_settings(request):
        changes = await body(request)
        if changes.get("firmware", {}).get("token") == "••••":
            del changes["firmware"]["token"]    # the masked value came back unchanged
        bad = set(changes) - EDITABLE
        if bad:
            raise ValueError(f"Not editable: {', '.join(sorted(bad))}")
        if "touch_dead_zone" in changes:
            changes["touch_dead_zone"] = check_dead_zone(changes["touch_dead_zone"])
        svc.settings.update(changes)
        if "boot_intro" in changes:
            write_boot_js(svc.settings)
        if "leveling" in changes:
            svc.leveling.recompute()
        if "runout" in changes and svc.link.ready:
            await svc.apply_runout_settings()
        svc.notify("settings")
        return ok(svc.settings.public())

    # ---------------------------------------------------------------- Wi-Fi
    async def wifi_status(request):
        return ok(await svc.refresh_wifi())

    async def wifi_networks(request):
        rescan = request.query.get("rescan", "1") != "0"
        return ok({"networks": await svc.wifi.networks(rescan)})

    async def wifi_action(request):
        action = request.match_info["action"]
        b = await body(request)
        if action == "connect":
            st = await svc.wifi.connect(b.get("ssid"), b.get("password") or None, bool(b.get("hidden")))
            return ok(await svc.refresh_wifi(st))
        if action == "forget":
            await svc.wifi.forget(b.get("ssid"))
        elif action == "radio":
            await svc.wifi.set_radio(bool(b.get("on")))
        elif action == "country":
            await svc.wifi.set_country(b.get("code"))
        else:
            raise web.HTTPNotFound()
        return ok(await svc.refresh_wifi())

    # ---------------------------------------------------------------- USB drives
    async def usb_state(request):
        return ok(svc.usb.snapshot())

    async def usb_list(request):
        return ok(svc.usb.list(request.match_info["drive"], request.query.get("path", "")))

    async def usb_meta(request):
        meta = await asyncio.get_running_loop().run_in_executor(
            None, svc.usb.meta, request.match_info["drive"], request.query.get("path", ""))
        out = {k: v for k, v in meta.items() if k != "thumbnail"}
        out["has_thumbnail"] = bool(meta.get("thumbnail"))
        return ok(out)

    async def usb_thumb(request):
        meta = await asyncio.get_running_loop().run_in_executor(
            None, svc.usb.meta, request.match_info["drive"], request.query.get("path", ""))
        if not meta.get("thumbnail"):
            raise web.HTTPNotFound()
        header, b64 = meta["thumbnail"].split(",", 1)
        return web.Response(body=base64.b64decode(b64), content_type=header[5:].split(";")[0],
                            headers={"Cache-Control": "max-age=600"})

    async def usb_action(request):
        drive = request.match_info["drive"]
        action = request.match_info["action"]
        b = await body(request)
        if action in ("print", "copy"):
            name = await svc.usb.copy_to_printer(drive, b.get("path", ""), then_print=action == "print")
            return ok({"name": name})
        if action == "eject":
            await svc.usb.eject(drive)
            return ok()
        raise web.HTTPNotFound()

    # ---------------------------------------------------------------- firmware updates
    async def firmware(request):
        action = request.match_info["action"]
        if action == "check":
            await svc.fw.check()
        elif action == "install":
            svc.fw.install()
        elif action == "restore":
            svc.fw.restore()
        elif action == "skip":
            svc.fw.skip()
        elif action == "snooze":
            svc.fw.snooze(float((await body(request)).get("hours", 24)))
        else:
            raise web.HTTPNotFound()
        return ok(svc.fw.snapshot())

    # ---------------------------------------------------------------- OctoPrint compatibility
    async def octo_version(request):
        return ok({"api": "0.1", "server": "1.10.0", "text": f"OctoPrint 1.10.0 (NORTHBOLT {__version__})"})

    async def octo_upload(request):
        name, start = await save_upload(request)
        if start:
            await svc.start_print(name)
        return web.json_response({"done": True, "files": {"local": {"name": name, "origin": "local"}}},
                                 status=201)

    r.add_get("/", index)
    r.add_get("/ws", websocket)
    r.add_get("/api/state", state)
    r.add_get("/api/files", files)
    r.add_post("/api/files", upload)
    r.add_delete("/api/files/{name}", delete_file)
    r.add_get("/api/thumb/{name}", thumb)
    r.add_post("/api/print", print_file)
    r.add_post("/api/job/{action}", job_action)
    r.add_post("/api/recovery/{action}", recovery)
    r.add_post("/api/temp", temp)
    r.add_post("/api/cooldown", cooldown)
    r.add_post("/api/move", move)
    r.add_post("/api/home", home)
    r.add_post("/api/motors_off", motors_off)
    r.add_post("/api/extrude", extrude)
    r.add_post("/api/filament", filament)
    r.add_post("/api/tool", tool)
    r.add_post("/api/lift", lift)
    r.add_post("/api/fan", fan)
    r.add_post("/api/speed", speed)
    r.add_post("/api/flow", flow)
    r.add_post("/api/runout", runout)
    r.add_post("/api/runout/query", runout_query)
    r.add_post("/api/console", console)
    r.add_post("/api/zoffset/{action}", zoffset)
    r.add_post("/api/leveling/{action}", leveling)
    r.add_post("/api/firmware/{action}", firmware)
    r.add_post("/api/connection/{action}", connection)
    r.add_post("/api/alerts/dismiss", dismiss)
    r.add_post("/api/scales/{action}", scales)
    r.add_post("/api/power/{action}", power)
    r.add_get("/api/system", system)
    r.add_get("/api/settings", get_settings)
    r.add_post("/api/settings", post_settings)
    r.add_get("/api/usb", usb_state)
    r.add_get("/api/usb/{drive}/list", usb_list)
    r.add_get("/api/usb/{drive}/meta", usb_meta)
    r.add_get("/api/usb/{drive}/thumb", usb_thumb)
    r.add_post("/api/usb/{drive}/{action}", usb_action)
    r.add_get("/api/wifi", wifi_status)
    r.add_get("/api/wifi/networks", wifi_networks)
    r.add_post("/api/wifi/{action}", wifi_action)
    r.add_get("/api/version", octo_version)
    r.add_post("/api/files/local", octo_upload)
    r.add_static("/static", STATIC, append_version=False)

    async def on_startup(app):
        write_boot_js(svc.settings)
        await svc.start()

    async def on_cleanup(app):
        await svc.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def _free_bytes(path):
    try:
        import shutil
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _local_ips():
    import socket
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # no packet is sent; this just picks the LAN interface
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in ips and not info[4][0].startswith("127."):
                ips.append(info[4][0])
    except OSError:
        pass
    return ips
