from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import Engine, GameError, KINDS, require
from bot import ChatBot
from task_rules import TASKS

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("AMONGUS_DATA", str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = DATA / "config.json"
DEFAULT = {"mode": "demo", "bot_token": "", "bot_username": "", "app_short_name": "", "public_url": "", "admin_ids": [], "port": 8765}
cfg = {**DEFAULT, **(json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else {})}
if os.environ.get("AMONGUS_PORT"):
    cfg["port"] = int(os.environ["AMONGUS_PORT"])
if os.environ.get("AMONGUS_MODE"):
    cfg["mode"] = os.environ["AMONGUS_MODE"]
if "AMONGUS_PUBLIC_URL" in os.environ:
    cfg["public_url"] = os.environ["AMONGUS_PUBLIC_URL"]
BOOT_KEY = os.environ.get("AMONGUS_BOOT_KEY") or secrets.token_urlsafe(32)
engine = Engine(DATA / "game.sqlite3")
engine.ensure_catalog()
chat_bot = ChatBot(engine, cfg)
sessions = {}
scan_receipts = {}
attempts = {}
bot_status = {"state": "demo" if cfg["mode"] == "demo" else "connecting", "detail": ""}
log = logging.getLogger("amongus")


def local(request):
    # Uvicorn is launched with proxy_headers=False. Never trust X-Forwarded-For.
    host = request.headers.get("host", "").split(":")[0].lower()
    return (request.client is not None and request.client.host in ("127.0.0.1", "::1", "testclient")
            and host in ("127.0.0.1", "localhost", "testserver")
            and not any(request.headers.get(h) for h in ("x-forwarded-for", "forwarded", "cf-connecting-ip")))


def rate(request, group, limit=40):
    client_ip = request.client.host if request.client else "?"
    # Only our loopback Cloudflare origin and the configured public hostname
    # may use Cloudflare's per-visitor IP. Otherwise 40 polling players would
    # all share one localhost rate limit.
    if (client_ip in ("127.0.0.1", "::1") and cfg.get("public_url")
            and request.url.hostname == urlparse(cfg["public_url"]).hostname):
        try:
            client_ip = str(ipaddress.ip_address(request.headers.get("cf-connecting-ip", "")))
        except ValueError:
            pass
    key = (client_ip, group)
    now = time.time()
    entries = [t for t in attempts.get(key, []) if t > now - 60]
    require(len(entries) < limit, "Слишком много запросов. Подождите минуту")
    attempts[key] = entries + [now]
    if len(attempts) > 2000:
        for k in list(attempts):
            if not any(t > now - 60 for t in attempts[k]):
                del attempts[k]


def issue(uid, host=False, local_only=False, **scope):
    key = secrets.token_urlsafe(32)
    sessions[key] = {"uid": str(uid), "host": host, "local": local_only, "expires": time.time() + 86400}
    sessions[key].update(scope)
    # Bound abandoned sessions without evicting current players.
    for k in list(sessions):
        if sessions[k]["expires"] < time.time():
            del sessions[k]
    return key


def auth(request):
    key = request.headers.get("authorization", "").removeprefix("Bearer ")
    s = sessions.get(key)
    require(s and s["expires"] > time.time(), "Войдите в приложение заново")
    require(not s["local"] or local(request), "Локальная сессия недоступна извне")
    if s.get('room_scope'):
        code = s['room_scope']; r = engine.room(code)
        require(r.get('test_mode') and r['players'].get(s['uid'], {}).get('bot'), 'Тестовый бот больше недоступен')
        engine.admin(r, s['controller'], s.get('controller_host', False))
        allowed = {'scan', 'task_open', 'ghost_task_open', 'task_finish', 'kill', 'report', 'emergency', 'meeting_join', 'vote',
                   'sabotage_start', 'sabotage_open', 'sabotage_fix', 'reactor_hold', 'repair_heartbeat', 'repair_release', 'test_objects'}
        path = request.url.path
        prefix = '/api/rooms/' + code
        require(path in ('/api/me', '/api/qr', prefix) or path.startswith(prefix + '/') and path[len(prefix)+1:] in allowed,
                'Сессия тестового бота действует только для игры в его комнате')
    return s


def host_auth(request):
    s = auth(request)
    require(s["host"], "Нужны права владельца сервера")
    return s


def validate_init_data(raw, token, now=None):
    require(isinstance(raw, str) and 0 < len(raw) < 16384 and token, "Нет данных авторизации Telegram")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    require(len(pairs) == len(dict(pairs)), "Повторяющиеся поля авторизации")
    values = dict(pairs)
    received = values.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    require(hmac.compare_digest(expected, received), "Подпись Telegram неверна")
    stamp = int(values.get("auth_date", 0))
    delta = (time.time() if now is None else now) - stamp
    require(-30 <= delta <= 3600, "Авторизация устарела. Закройте и откройте Mini App")
    user = json.loads(values.get("user", "{}"))
    require(type(user.get("id")) is int and user["id"] > 0, "Пользователь Telegram не определён")
    return user


def deep_link(payload):
    if cfg["mode"] == "telegram" and cfg["bot_username"]:
        return f"https://t.me/{cfg['bot_username']}?{urlencode({'start': payload})}"
    return f"http://127.0.0.1:{cfg['port']}/?{urlencode({'start': payload})}"


def scan_qr(code, uid, payload):
    with engine.lock:
        r = engine.room(code)
        result = engine.inspect_scan(code, uid, payload)
        now = time.time()
        for key, receipt in list(scan_receipts.items()):
            if receipt["expires"] < now or (receipt["uid"] == uid and receipt["room"] == r["code"]):
                del scan_receipts[key]
        receipt = secrets.token_urlsafe(32)
        scan_receipts[receipt] = {"uid": uid, "room": r["code"], "epoch": r["epoch"], "expires": now + 30,
                                  "target": result["target"], "actions": result["actions"]}
        return {k: v for k, v in result.items() if k != "target"} | {"receipt": receipt, "expires": now + 30}


def scanned_action(code, uid, action, data):
    with engine.lock:
        r = engine.room(code)
        receipt_key = str(data.get("receipt", ""))
        receipt = scan_receipts.get(receipt_key)
        require(receipt and receipt["uid"] == uid and receipt["room"] == r["code"]
                and receipt["expires"] >= time.time() and receipt["epoch"] == r["epoch"]
                and action in receipt["actions"], "Сначала отсканируйте QR рядом с объектом. Разрешение действует 30 секунд")
        if action == "task_open":
            result = engine.task_open(code, uid, receipt["target"])
        elif action == "emergency":
            result = engine.emergency(code, uid)
        elif action == 'meeting_join':
            result = engine.meeting_join(code, uid)
        elif action == 'sabotage_open':
            result = engine.sabotage_open(code, uid, receipt['target'])
        else:
            result = getattr(engine, action)(code, uid, receipt["target"])
        del scan_receipts[receipt_key]
        return result


async def tg(client, method, data):
    response = await client.post(f"https://api.telegram.org/bot{cfg['bot_token']}/{method}", json=data)
    result = response.json()
    if not result.get("ok"):
        # Never log the request URL: it includes the bot token.
        raise RuntimeError(f"Telegram {method}: {result.get('error_code', 'error')}")
    return result["result"]


async def bot_loop():
    if cfg["mode"] != "telegram" or not cfg["bot_token"]:
        return
    async with httpx.AsyncClient(timeout=40) as client:
        try:
            me = await tg(client, "getMe", {})
            cfg["bot_username"] = me["username"]
            await tg(client, "deleteWebhook", {"drop_pending_updates": False})
            await tg(client, "setMyCommands", {"commands": [{"command": "start", "description": "Главное меню"}, {"command": "create", "description": "Создать комнату"}, {"command": "join", "description": "Войти по коду"}, {"command": "room", "description": "Моя комната"}, {"command": "leave", "description": "Выйти из лобби"}, {"command": "whoami", "description": "Мой Telegram ID"}]})
            if cfg["public_url"]:
                await tg(client, "setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Открыть игру", "web_app": {"url": cfg["public_url"]}}})
            bot_status.update(state="online", detail="Бот подключён")
        except Exception:
            bot_status.update(state="error", detail="Не удалось подключить бота. Проверьте токен и интернет; затем перезапустите")
            return
        with engine.lock:
            row = engine.db.execute("SELECT value FROM meta WHERE key='bot_offset'").fetchone()
        offset = int(row[0]) if row else 0
        while True:
            try:
                updates = await tg(client, "getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
                for update in updates:
                    message = update.get("message", {})
                    sender = message.get("from", {})
                    if message.get("chat", {}).get("type") == "private" and sender.get("id"):
                        uid = str(sender["id"])
                        replies = chat_bot.process(uid, sender.get("first_name", "Игрок"), message.get("text", ""), update["update_id"])
                        for reply in replies:
                            await tg(client, "sendMessage", {"chat_id": uid, **{k: v for k, v in reply.items() if k in ("text", "reply_markup")}})
                    offset = update["update_id"] + 1
                    with engine.lock:
                        engine.db.execute("INSERT OR REPLACE INTO meta VALUES('bot_offset',?)", (str(offset),))
                        engine.db.commit()
                bot_status.update(state="online", detail="Бот подключён")
            except asyncio.CancelledError:
                raise
            except Exception:
                bot_status.update(state="retry", detail="Проблема связи с Telegram; повторное подключение")
                await asyncio.sleep(5)


async def background():
    async with httpx.AsyncClient(timeout=12) as client:
        while True:
            try:
                if cfg["mode"] == "telegram" and cfg["bot_token"]:
                    with engine.lock:
                        rows = engine.db.execute("SELECT id,uid,text,tries FROM outbox WHERE next_try<=? ORDER BY id LIMIT 15", (time.time(),)).fetchall()
                    for ident, uid, text, tries in rows:
                        try:
                            await tg(client, "sendMessage", {"chat_id": uid, "text": text})
                            with engine.lock:
                                engine.db.execute("DELETE FROM outbox WHERE id=?", (ident,))
                                engine.db.commit()
                        except Exception:
                            with engine.lock:
                                # A blocked bot must not prevent timers or notifications to others.
                                engine.db.execute("UPDATE outbox SET tries=?,next_try=? WHERE id=?", (tries + 1, time.time() + min(3600, 10 * 2 ** min(tries, 8)), ident))
                                engine.db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка игрового таймера")
            await asyncio.sleep(1)


async def timer_loop():
    while True:
        try:
            engine.tick()
        except Exception:
            log.exception("Ошибка игрового таймера")
        await asyncio.sleep(.5)


@asynccontextmanager
async def lifespan(app):
    jobs = [asyncio.create_task(bot_loop()), asyncio.create_task(background()), asyncio.create_task(timer_loop())]
    yield
    for job in jobs:
        job.cancel()
    for job in jobs:
        with contextlib.suppress(asyncio.CancelledError):
            await job


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(GameError)
async def game_error(request, exc):
    message = str(exc)
    return JSONResponse({"error": message}, status_code=401 if message == "Войдите в приложение заново" else 400)


@app.get("/api/health")
def health():
    return {"ok": True, "mode": cfg["mode"], "server_time": time.time(), "local": False,
            "instance": os.environ.get("AMONGUS_INSTANCE", "")}


@app.get("/api/info")
def info(request: Request):
    return {"bot_username": cfg["bot_username"], "mode": cfg["mode"], "local": local(request), "kinds": KINDS,
            'task_types': TASKS, "bot": bot_status if local(request) else None}


@app.post("/api/auth/local")
async def login_local(request: Request):
    rate(request, "auth", 20)
    body = await request.json()
    require(local(request) and hmac.compare_digest(str(body.get("key", "")), BOOT_KEY), "Неверный локальный ключ владельца")
    engine.register("host", "Ведущий")
    return {"token": issue("host", True, True)}


@app.post("/api/auth/demo")
async def login_demo(request: Request):
    rate(request, "auth", 40)
    require(cfg["mode"] == "demo" and local(request), "Деморежим доступен только на этом компьютере")
    body = await request.json()
    uid = str(body.get("id", ""))
    require(re.fullmatch(r"demo_[1-9][0-9]?", uid), "Выберите демоигрока от 1 до 99")
    with engine.lock:
        name = engine.state["users"].get(uid, {}).get("name", f"Игрок {uid[5:]}")
    engine.register(uid, body.get("name") or name)
    return {"token": issue(uid, False, True)}


@app.post("/api/auth/telegram")
async def login_telegram(request: Request):
    rate(request, "auth", 40)
    require(cfg["mode"] == "telegram", "Сервер работает в деморежиме. Подключите Telegram в настройках")
    try:
        user = validate_init_data((await request.json()).get("initData"), cfg["bot_token"])
    except (ValueError, TypeError, KeyError):
        raise GameError("Неверные данные авторизации Telegram")
    uid = str(user["id"])
    engine.register(uid, str(user.get("first_name", "Игрок"))[:60])
    return {"token": issue(uid, uid in [str(x) for x in cfg["admin_ids"]])}


@app.get("/api/me")
def me(request: Request):
    s = auth(request)
    with engine.lock:
        current = engine.current_room(s["uid"])
        return {"user": engine.state["users"][s["uid"]], "host": s["host"], "rooms": engine.listing(s["uid"], s["host"]), "current_room": current["code"] if current else s.get('room_scope'), "time": time.time(), "mode": cfg["mode"], 'test_controller': bool(s.get('room_scope'))}


@app.get("/api/settings")
def settings(request: Request):
    host_auth(request)
    saved = {**DEFAULT, **(json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else cfg)}
    return {**{k: v for k, v in saved.items() if k != "bot_token"}, "token_set": bool(saved["bot_token"]), "bot": bot_status, "restart_required": saved != cfg}


@app.post("/api/settings")
async def save_settings(request: Request):
    host_auth(request)
    require(local(request), "Токен и адрес сервера меняются только с локального компьютера")
    data = await request.json()
    saved = {**DEFAULT, **(json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else cfg)}
    require(data.get("mode") in ("demo", "telegram"), "Неизвестный режим")
    saved["mode"] = data["mode"]
    for key in ("public_url", "bot_username", "app_short_name"):
        saved[key] = str(data.get(key, "")).strip().strip("@") if key == "bot_username" else str(data.get(key, "")).strip().rstrip("/")
    require(not saved["bot_username"] or re.fullmatch(r"[A-Za-z0-9_]{5,32}", saved["bot_username"]), "Проверьте имя бота")
    require(not saved["app_short_name"] or re.fullmatch(r"[A-Za-z0-9_]{3,64}", saved["app_short_name"]), "Проверьте короткое имя Mini App")
    ids = data.get("admin_ids", [])
    require(isinstance(ids, list) and len(ids) <= 20 and all(str(x).isdigit() and int(x) > 0 for x in ids), "Введите числовые Telegram ID администраторов")
    saved["admin_ids"] = [str(x) for x in ids]
    if data.get("bot_token"):
        token = str(data["bot_token"]).strip()
        require(re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,100}", token), "Проверьте формат токена бота")
        saved["bot_token"] = token
    if saved["mode"] == "telegram":
        require(saved["bot_token"] and saved["bot_username"], "Укажите токен и имя бота")
        u = urlparse(saved["public_url"])
        require(u.scheme == "https" and u.hostname and not u.username and not u.query and not u.fragment and u.path in ("", "/"), "Нужен публичный HTTPS-адрес без пути, параметров и пароля")
    temp = CONFIG.with_suffix(".tmp")
    temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(CONFIG)
    return {"ok": True, "restart_required": True}


@app.get("/api/catalog")
def catalog(request: Request):
    s = auth(request)
    with engine.lock:
        can_manage = s["host"] or any(s["uid"] in r["admins"] for r in engine.state["rooms"].values())
        require(can_manage, "Каталог доступен администраторам")
        return {"stations": [engine.station_view(s) for s in engine.state['stations'].values()], "kinds": KINDS}


@app.post("/api/stations")
async def create_station(request: Request):
    s = auth(request)
    with engine.lock:
        require(s["host"] or any(s["uid"] in r["admins"] for r in engine.state["rooms"].values()), "Нужны права администратора")
    data = await request.json()
    return engine.station(data.get("name", ""), data.get("kind"))


@app.post("/api/rooms")
async def create_room(request: Request):
    s = host_auth(request)
    require(cfg["mode"] == "demo" and local(request), "Создайте комнату в чате бота: /create")
    return engine.create_room(s["uid"], (await request.json()).get("name", "Новая сессия"))


@app.post("/api/join")
async def join(request: Request):
    s = auth(request)
    require(cfg["mode"] == "demo" and local(request), "Введите код в чате бота: /join КОД")
    return {"code": engine.join((await request.json()).get("code", ""), s["uid"])["code"]}


@app.get("/api/rooms/{code}")
def room_view(code: str, request: Request):
    s = auth(request)
    engine.tick()
    return {**engine.view(code, s["uid"], s["host"]), "time": time.time()}


@app.post("/api/rooms/{code}/{action}")
async def room_action(code: str, action: str, request: Request):
    s = auth(request)
    rate(request, "game:" + s["uid"], 240)
    data = await request.json()
    engine.tick()
    uid, host = s["uid"], s["host"]
    if action == "configure":
        engine.configure(code, uid, data, host)
    elif action == "start":
        engine.start(code, uid, host)
    elif action == 'add_bots':
        engine.add_bots(code, uid, data.get('count', 1), host)
    elif action == 'bot_action':
        return {'ok': True, 'result': engine.bot_action(code, uid, str(data.get('player', '')), data.get('action'), host)}
    elif action == 'bot_session':
        player = str(data.get('player', ''))
        engine.bot_manual(code, uid, player, host)
        return {'token': issue(player, False, s['local'], room_scope=engine.room(code)['code'], controller=uid, controller_host=host)}
    elif action == 'test_objects':
        r = engine.room(code)
        require(r.get('test_mode') and (host or uid in r['admins'] or s.get('room_scope') == r['code']), 'Тестовые QR доступны только хосту и управляемым им ботам')
        p = r['players'].get(uid, {})
        items = [{'name': engine.state['stations'][st]['name'], 'payload': 's_' + st} for st, t in p.get('tasks', {}).items() if not t['done']]
        items += [{'name': 'Бейдж · ' + pl['name'], 'payload': 'b_' + engine.state['users'][pid]['badge']} for pid, pl in r['players'].items() if pid != uid]
        items += [{'name': 'QR совещания', 'payload': 'm_' + engine.state['meeting_code']}]
        items += engine.sabotage_qrs()
        return items
    elif action == "leave":
        engine.leave(code, uid)
        return {"ok": True}
    elif action == "control":
        engine.control(code, uid, data.get("action"), data, host)
    elif action == "undo":
        engine.undo(code, uid, host)
    elif action == "scan":
        return scan_qr(code, uid, data.get("payload"))
    elif action == "demo_objects":
        require(cfg["mode"] == "demo" and local(request), "Имитация сканирования отключена в настоящей игре")
        with engine.lock:
            r = engine.room(code)
            engine.member(r, uid)
            return [{"name": "Станция совещания", "payload": "m_" + engine.state["meeting_code"]}] + [
                {"name": p["name"], "payload": "b_" + engine.state["users"][pid]["badge"]}
                for pid, p in r["players"].items() if pid != uid]
    elif action == "task_open":
        return scanned_action(code, uid, action, data)
    elif action == 'ghost_task_open':
        return engine.task_open(code, uid, data.get('station'), ghost=True)
    elif action == "task_finish":
        return engine.task_finish(code, uid, data.get("station"), data.get("id"), data.get("epoch"), data.get("answer"))
    elif action in ("kill", "report"):
        scanned_action(code, uid, action, data)
    elif action in ("emergency", "meeting_join"):
        scanned_action(code, uid, action, data)
    elif action == 'sabotage_start':
        return engine.sabotage_start(code, uid, data.get('kind'))
    elif action == 'sabotage_open':
        return scanned_action(code, uid, action, data)
    elif action == 'sabotage_fix':
        return engine.sabotage_fix(code, uid, data)
    elif action in ('repair_heartbeat', 'repair_release'):
        return getattr(engine, action)(code, uid, data)
    elif action == 'reactor_hold':
        return engine.reactor_hold(code, uid, data)
    elif action == "vote":
        engine.vote(code, uid, data.get("meeting"), str(data.get("target", "")))
    else:
        raise GameError("Неизвестное действие")
    return {"ok": True}


@app.post("/api/qr")
async def qr(request: Request):
    s = auth(request)
    data = await request.json()
    payload = str(data.get("payload", ""))
    with engine.lock:
        if payload.startswith("s_"):
            engine.station_target(payload[2:])
            require(s["host"] or any(s["uid"] in r["admins"] for r in engine.state["rooms"].values()), "Печать станций доступна администраторам")
        elif payload.startswith("b_"):
            target = next((u for u in engine.state["users"].values() if u["badge"] == payload[2:]), None)
            require(target and (target["id"] == s["uid"] or s["host"] or any(s["uid"] in r["admins"] and target["id"] in r["players"] for r in engine.state["rooms"].values())), "Бейдж недоступен")
        elif payload.startswith("j_"):
            r = engine.room(payload[2:])
            engine.admin(r, s["uid"], s["host"])
        elif payload.startswith("m_"):
            require(payload[2:] == engine.state["meeting_code"], "Станция совещания не найдена")
            require(s["host"] or any(s["uid"] in r["admins"] for r in engine.state["rooms"].values()), "Печать станции доступна администраторам")
        elif payload.startswith('x_'):
            _, (_, use, _) = engine.interaction(payload[2:])
            require(use == 'repair', 'Для запуска саботажей используйте кнопки в игре; печатаются только QR ремонта')
            require(s['host'] or any(s['uid'] in r['admins'] for r in engine.state['rooms'].values()), 'Печать саботажей доступна хосту')
        else:
            raise GameError("Неизвестный тип QR")
    link = deep_link(payload)
    if payload.startswith("j_") and cfg["mode"] == "telegram" and cfg["bot_username"]:
        link = f"https://t.me/{cfg['bot_username']}?start={payload}"
    image = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    stream = io.BytesIO()
    image.save(stream)
    return {"url": link, "image": "data:image/svg+xml;base64," + base64.b64encode(stream.getvalue()).decode(), "demo": cfg["mode"] == "demo"}


@app.post("/api/demo/populate")
async def demo_populate(request: Request):
    s = host_auth(request)
    require(cfg["mode"] == "demo" and local(request), "Только локальная демонстрация")
    data = await request.json()
    code = data.get("code", "")
    # Build four explicit test participants, never silently fabricate real players.
    for n, name in enumerate(("Алекс", "Саша", "Дана", "Миша"), 1):
        uid = f"demo_{n}"
        engine.register(uid, name)
        engine.join(code, uid)
    selected = []
    for name, kind in (("Электрика — проводка", "wires"), ("Админ — карта доступа", "card_swipe"), ("Распределитель", "calibration"), ("Реактор", "sequence")):
        existing = next((st for st in engine.state["stations"].values() if st["kind"] == kind), None)
        selected.append((existing or engine.station(name, kind))["code"])
    engine.configure(code, s["uid"], {"stations": selected}, True)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/guide")
def guide():
    return FileResponse(ROOT / "README.html")


@app.post("/api/demo/chat")
async def demo_chat(request: Request):
    s = auth(request)
    require(cfg["mode"] == "demo" and local(request), "Демочат отключён в Telegram")
    data = await request.json()
    text = data.get("text", "")
    require(isinstance(text, str) and 0 < len(text) <= 200, "Введите сообщение")
    user = engine.state["users"][s["uid"]]
    return {"messages": chat_bot.process(s["uid"], user["name"], text)}
