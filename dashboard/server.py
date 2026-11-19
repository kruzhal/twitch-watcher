#!/usr/bin/env python3
"""Small, owner-only HTTPS dashboard for the Twitch Watcher server."""

from __future__ import annotations

import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


ROOT = Path(__file__).resolve().parent
IP = os.environ.get("DASHBOARD_IP", "localhost")
DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", str(ROOT.parent / "data" / "watcher.sqlite3")))
BOT_ENV = Path(os.environ.get("DASHBOARD_BOT_ENV", str(ROOT.parent / ".env")))
LOCAL_APP = ROOT.parent
BOT_ROOT = Path(os.environ.get("DASHBOARD_BOT_ROOT", str(LOCAL_APP if (LOCAL_APP / "app" / "version.py").exists() else BOT_ENV.parent)))
sys.path.insert(0, str(BOT_ROOT))
from app.admin_store import database, catalog_page, change_catalog, import_rows, filter_settings, change_filter
from app.catalog import parse_catalog_csv
from app.schema import migrate_sync
from app.health import summarize_health
from app.version import VERSION
AUTH_FILE = Path(os.environ.get("DASHBOARD_AUTH_FILE", str(ROOT.parent / "data" / "dashboard-auth.json")))
ACME_ROOT = Path(os.environ.get("DASHBOARD_ACME_ROOT", "/var/www/letsencrypt")).resolve()
CERT_FILE = os.environ.get("DASHBOARD_TLS_CERT", f"/etc/letsencrypt/live/{IP}/fullchain.pem")
KEY_FILE = os.environ.get("DASHBOARD_TLS_KEY", f"/etc/letsencrypt/live/{IP}/privkey.pem")
BOOTSTRAP_TOKEN = os.environ.get("DASHBOARD_BOOTSTRAP_TOKEN", "")
SESSION_SECRET = os.environ.get("DASHBOARD_SESSION_SECRET", "").encode()
BOT_SERVICE = os.environ.get("DASHBOARD_BOT_SERVICE", "twitch-watcher.service")
SESSION_SECONDS = 8 * 60 * 60
COOKIE_NAME = "watcher_session"
LOGIN_ATTEMPTS: dict[str, tuple[int, float]] = {}
APP_TOKEN: tuple[str, float] | None = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s dashboard: %(message)s")
log = logging.getLogger("dashboard")


def _run(*args: str, timeout: int = 5) -> tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip()
    except Exception:
        log.exception("Command failed: %s", args[0])
        return 1, ""


def _env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in BOT_ENV.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _db():
    if not DB_PATH.exists():
        raise RuntimeError("База данных бота пока не найдена")
    return database(DB_PATH)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()


def _auth_record() -> dict | None:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_auth(password: str) -> None:
    salt = secrets.token_bytes(32)
    record = {"salt": salt.hex(), "password_hash": _hash_password(password, salt), "created_at": datetime.now(timezone.utc).isoformat()}
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(AUTH_FILE)


def _make_cookie() -> str:
    expires = int(time.time()) + SESSION_SECONDS
    payload = f"{expires}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _cookie_valid(value: str) -> bool:
    try:
        expires, nonce, signature = value.split(".", 2)
        payload = f"{expires}.{nonce}"
        expected = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        return int(expires) > int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def _csrf_for(cookie: str) -> str:
    return hmac.new(SESSION_SECRET, b"csrf:" + cookie.encode(), hashlib.sha256).hexdigest()


def _state() -> dict:
    code, state = _run("systemctl", "is-active", BOT_SERVICE)
    _, raw = _run("systemctl", "show", BOT_SERVICE, "-p", "ActiveEnterTimestamp", "-p", "NRestarts", "-p", "MainPID")
    properties = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    total_mem = available_mem = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            number = int(value.strip().split()[0]) * 1024
            if key == "MemTotal":
                total_mem = number
            elif key == "MemAvailable":
                available_mem = number
    except (OSError, ValueError, IndexError):
        pass
    disk = shutil.disk_usage("/")
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        load = []
    with _db() as db:
        channel_count = db.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        chosen_count = db.execute("SELECT COUNT(*) FROM channels WHERE source='manual'").fetchone()[0]
        word_count = db.execute("SELECT COUNT(*) FROM watched_words").fetchone()[0]
        catalog_count = db.execute("SELECT COUNT(*) FROM channel_catalog").fetchone()[0]
        match_count = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        enabled_count = db.execute("SELECT COUNT(*) FROM channel_catalog WHERE enabled=1").fetchone()[0]
        queue_count = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
        rows = db.execute("SELECT component,data,updated_at,instance_id FROM runtime_status").fetchall()
        health = summarize_health(rows, pid=properties.get("MainPID", "0"), service_active=code == 0 and state == "active")
    return {
        "version": VERSION,
        "health": health,
        "queued_notifications": queue_count,
        "enabled_catalog_channels": enabled_count,
        "service": "active" if code == 0 and state == "active" else state or "unknown",
        "active_since": properties.get("ActiveEnterTimestamp", "—"),
        "restarts": properties.get("NRestarts", "—"),
        "ram_used": max(0, total_mem - available_mem),
        "ram_total": total_mem,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "load": load,
        "channels": channel_count,
        "chosen_channels": chosen_count,
        "phrases": word_count,
        "catalog_channels": catalog_count,
        "matches": match_count,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _channel_rows() -> list[dict]:
    with _db() as db:
        rows = db.execute(
            """SELECT c.login, c.broadcaster_id, c.source, a.is_live, a.title, a.category
               FROM channels c LEFT JOIN channel_activity a ON a.broadcaster_id=c.broadcaster_id
               ORDER BY CASE c.source WHEN 'manual' THEN 0 ELSE 1 END, c.login"""
        ).fetchall()
    return [dict(row) for row in rows]


def _word_rows() -> list[str]:
    with _db() as db:
        return [row[0] for row in db.execute("SELECT phrase FROM watched_words ORDER BY phrase").fetchall()]


def _recent_matches() -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT created_at, channel_login, author_login, message, matched_rule FROM matches ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return [dict(row) for row in rows]


def _catalog_response(query):
    search = query.get("search", [""])[0]
    enabled = query.get("enabled", ["all"])[0]
    page = int(query.get("page", ["0"])[0])
    with _db() as db:
        result = catalog_page(db, search, page, enabled)
    return result


def _filters_response():
    with _db() as db:
        return filter_settings(db)


def _app_access_token() -> str:
    global APP_TOKEN
    if APP_TOKEN and APP_TOKEN[1] > time.time() + 30:
        return APP_TOKEN[0]
    values = _env_file()
    data = urllib.parse.urlencode({
        "client_id": values.get("TWITCH_CLIENT_ID", ""),
        "client_secret": values.get("TWITCH_CLIENT_SECRET", ""),
        "grant_type": "client_credentials",
    }).encode()
    request = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=data)
    with urllib.request.urlopen(request, timeout=8) as response:
        body = json.load(response)
    APP_TOKEN = (body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60)
    return APP_TOKEN[0]


def _resolve_channel(login: str) -> tuple[str, str]:
    token = _app_access_token()
    values = _env_file()
    url = "https://api.twitch.tv/helix/users?" + urllib.parse.urlencode({"login": login})
    request = urllib.request.Request(url, headers={"Client-ID": values["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=8) as response:
        body = json.load(response)
    users = body.get("data", [])
    if not users:
        raise ValueError("Twitch не нашёл такой канал")
    return users[0]["login"], str(users[0]["id"])


def _journal() -> list[str]:
    code, text = _run("journalctl", "-u", BOT_SERVICE, "--since", "24 hours ago", "-n", "120", "--no-pager", "-o", "short-iso", timeout=8)
    if code != 0:
        return ["Не удалось прочитать журнал службы."]
    scrubbed = []
    for line in text.splitlines():
        line = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[скрыто]", line)
        line = re.sub(r"(?i)((?:client_secret|access_token|refresh_token)\s*[=:]\s*)\S+", r"\1[скрыто]", line)
        scrubbed.append(line[:500])
    return scrubbed[-100:]


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
    tls_context = None

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request_thread(self, request, client_address):
        # A stalled TLS handshake must not block accepting other connections.
        if self.tls_context is not None:
            try:
                request = self.tls_context.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                request.close()
                return
        super().process_request_thread(request, client_address)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WatcherPanel"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.client_address[0], fmt % args)

    def _headers(self, content_type: str, length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        if getattr(self.server, "secure", False):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if length is not None:
            self.send_header("Content-Length", str(length))

    def _send_json(self, status: int, value: dict, extra_headers: list[tuple[str, str]] | None = None) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(data))
        for key, val in extra_headers or []:
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(data))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]")
        return host == IP or host == "localhost" or host == "127.0.0.1"

    def _secure_request(self) -> bool:
        return bool(getattr(self.server, "secure", False))

    def _cookie(self) -> str:
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _authenticated(self) -> bool:
        return bool(SESSION_SECRET) and _cookie_valid(self._cookie())

    def _authorized_api(self) -> bool:
        if not self._authenticated():
            self._send_json(401, {"error": "Нужно войти в панель заново"})
            return False
        return True

    def _csrf_ok(self) -> bool:
        expected_origin = f"https://{IP}"
        if self.headers.get("Origin") != expected_origin:
            self._send_json(403, {"error": "Запрос отклонён"})
            return False
        expected = _csrf_for(self._cookie())
        supplied = self.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(expected, supplied):
            self._send_json(403, {"error": "Обнови страницу и повтори действие"})
            return False
        return True

    def _json_body(self, limit=16384) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError("Нужен JSON-запрос")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > limit:
            raise ValueError("Некорректный размер запроса")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Некорректный запрос")
        return value

    def _redirect_http(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/.well-known/acme-challenge/"):
            relative = parsed.path.lstrip("/")
            target = (ACME_ROOT / relative).resolve()
            if not str(target).startswith(str(ACME_ROOT) + os.sep) or not target.is_file():
                self.send_error(404)
                return
            self._send_bytes(200, target.read_bytes(), "text/plain; charset=utf-8")
            return
        location = f"https://{IP}{self.path}"
        self.send_response(308)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._host_ok():
            self.send_error(421)
            return
        if not self._secure_request():
            self._redirect_http()
            return
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            self._send_json(200, {"setup_required": _auth_record() is None})
            return
        if path == "/api/session":
            if not self._authenticated():
                self._send_json(401, {"setup_required": _auth_record() is None})
                return
            self._send_json(200, {"csrf": _csrf_for(self._cookie())})
            return
        if path.startswith("/api/"):
            if not self._authorized_api():
                return
            if path == "/api/state":
                self._api_call(_state)
            elif path == "/api/channels":
                self._api_call(_channel_rows)
            elif path == "/api/words":
                self._api_call(_word_rows)
            elif path == "/api/matches":
                self._api_call(_recent_matches)
            elif path == "/api/logs":
                self._api_call(_journal)
            elif path == "/api/catalog":
                self._api_call(lambda: _catalog_response(parse_qs(urlparse(self.path).query)))
            elif path == "/api/filters":
                self._api_call(_filters_response)
            else:
                self._send_json(404, {"error": "Страница не найдена"})
            return
        asset = {"/": (ROOT / "index.html", "text/html; charset=utf-8"),
                 "/app.css": (ROOT / "app.css", "text/css; charset=utf-8"),
                 "/app.js": (ROOT / "app.js", "text/javascript; charset=utf-8")}.get(path)
        if asset is None:
            self.send_error(404)
            return
        try:
            self._send_bytes(200, asset[0].read_bytes(), asset[1])
        except OSError:
            self.send_error(404)

    def _api_call(self, function) -> None:
        try:
            self._send_json(200, {"data": function()})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)[:180]})
        except Exception as exc:
            log.exception("Dashboard read endpoint failed")
            self._send_json(503, {"error": str(exc)[:180]})

    def do_POST(self) -> None:
        if not self._host_ok() or not self._secure_request():
            self.send_error(421)
            return
        if self.headers.get("Origin") != f"https://{IP}":
            self._send_json(403, {"error": "Запрос отклонён"})
            return
        path = urlparse(self.path).path
        # Reject unauthenticated/CSRF-invalid uploads before allocating their body.
        if path not in {"/api/login", "/api/setup"}:
            if not self._authorized_api() or not self._csrf_ok():
                return
        try:
            payload = self._json_body(limit=3_000_000 if path in {"/api/catalog/preview", "/api/catalog/import"} else 16384)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)[:160]})
            return

        if path == "/api/setup":
            self._setup(payload)
            return
        if path == "/api/login":
            self._login(payload)
            return
        if path == "/api/logout":
            self._send_json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")])
        elif path == "/api/service":
            self._service_action(payload)
        elif path == "/api/words":
            self._word_action(payload)
        elif path == "/api/channels":
            self._channel_action(payload)
        elif path in {"/api/catalog", "/api/catalog/preview", "/api/catalog/import", "/api/filters"}:
            self._catalog_or_filter_action(path, payload)
        else:
            self._send_json(404, {"error": "Страница не найдена"})

    def _catalog_or_filter_action(self, path, payload):
        try:
            if path in {"/api/catalog/preview", "/api/catalog/import"}:
                rows = parse_catalog_csv(payload.get("csv"))
                mode = payload.get("mode", "merge")
                if not isinstance(mode, str) or mode not in {"merge", "replace"}:
                    raise ValueError("Неизвестный режим импорта")
                with _db() as db:
                    existing = {row[0] for row in db.execute("SELECT login FROM channel_catalog")}
                incoming = {row[0] for row in rows}
                info = {"incoming": len(incoming), "added": len(incoming - existing), "removed": len(existing - incoming) if mode == "replace" else 0}
                if path == "/api/catalog/import":
                    if mode == "replace" and payload.get("confirm") is not True:
                        raise ValueError("Для замены каталога нужно явное подтверждение")
                    with _db() as db:
                        import_rows(db, rows, mode)
                self._send_json(200, {"ok": True, "data": info})
            elif path == "/api/catalog":
                with _db() as db:
                    change_catalog(db, payload)
                self._send_json(200, {"ok": True})
            else:
                with _db() as db:
                    change_filter(db, payload)
                self._send_json(200, {"ok": True, "data": _filters_response()})
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = "Такой канал уже есть в базе" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
            self._send_json(400, {"error": message[:180]})
        except sqlite3.Error:
            log.exception("Catalog/filter database operation failed")
            self._send_json(503, {"error": "База занята. Повтори действие через несколько секунд."})

    def _setup(self, payload: dict) -> None:
        if _auth_record() is not None:
            self._send_json(409, {"error": "Первичная настройка уже завершена"})
            return
        supplied = str(payload.get("bootstrap_token", ""))
        if not BOOTSTRAP_TOKEN or not hmac.compare_digest(supplied, BOOTSTRAP_TOKEN):
            if self._failed_login("setup"):
                self._send_json(429, {"error": "Слишком много попыток. Подожди 10 минут."})
                return
            self._send_json(401, {"error": "Код настройки неверный"})
            return
        password = str(payload.get("password", ""))
        if len(password) < 16 or len(password) > 256:
            self._send_json(400, {"error": "Пароль должен содержать от 16 до 256 символов"})
            return
        _write_auth(password)
        self._send_json(200, {"ok": True})

    def _failed_login(self, _kind: str) -> bool:
        now = time.time()
        count, until = LOGIN_ATTEMPTS.get(self.client_address[0], (0, now + 600))
        if until <= now:
            count, until = 0, now + 600
        count += 1
        LOGIN_ATTEMPTS[self.client_address[0]] = (count, until)
        return count > 8

    def _login(self, payload: dict) -> None:
        if self._failed_login("login"):
            self._send_json(429, {"error": "Слишком много попыток. Подожди 10 минут."})
            return
        record = _auth_record()
        if record is None:
            self._send_json(409, {"error": "Сначала выполни первичную настройку"})
            return
        try:
            salt = bytes.fromhex(record["salt"])
            expected = record["password_hash"]
        except (KeyError, ValueError):
            self._send_json(503, {"error": "Настройки входа повреждены"})
            return
        supplied = str(payload.get("password", ""))
        if not hmac.compare_digest(_hash_password(supplied, salt), expected):
            self._send_json(401, {"error": "Пароль не подошёл"})
            return
        LOGIN_ATTEMPTS.pop(self.client_address[0], None)
        cookie = _make_cookie()
        self._send_json(200, {"csrf": _csrf_for(cookie)}, [
            ("Set-Cookie", f"{COOKIE_NAME}={cookie}; Path=/; Max-Age={SESSION_SECONDS}; Secure; HttpOnly; SameSite=Strict")
        ])

    def _service_action(self, payload: dict) -> None:
        action = str(payload.get("action", ""))
        if action not in {"start", "stop", "restart"}:
            self._send_json(400, {"error": "Неизвестное действие"})
            return
        unit = f"watcher-panel-{action}-{int(time.time())}"
        try:
            result = subprocess.run(
                ["systemd-run", "--quiet", f"--unit={unit}", "--on-active=3s", "/usr/bin/systemctl", action, BOT_SERVICE],
                capture_output=True, text=True, timeout=8, check=False,
            )
            if result.returncode != 0:
                self._send_json(503, {"error": result.stderr.strip()[:180] or "systemd не принял команду"})
                return
            self._send_json(200, {"ok": True, "action": action})
        except Exception:
            log.exception("Could not schedule service action")
            self._send_json(503, {"error": "Не удалось отправить команду службе"})

    def _word_action(self, payload: dict) -> None:
        action = str(payload.get("action", ""))
        phrase = str(payload.get("phrase", "")).strip().casefold()
        if action not in {"add", "delete"} or not phrase or len(phrase) > 200:
            self._send_json(400, {"error": "Укажи фразу длиной от 1 до 200 символов"})
            return
        with _db() as db:
            if action == "add":
                db.execute("INSERT OR IGNORE INTO watched_words(phrase) VALUES (?)", (phrase,))
            else:
                db.execute("DELETE FROM watched_words WHERE phrase=?", (phrase,))
            db.commit()
        self._send_json(200, {"ok": True, "words": _word_rows()})

    def _channel_action(self, payload: dict) -> None:
        action = str(payload.get("action", ""))
        login = str(payload.get("login", "")).strip().lstrip("@").casefold()
        if action not in {"add", "delete"}:
            self._send_json(400, {"error": "Неизвестное действие"})
            return
        if action == "delete":
            with _db() as db:
                row = db.execute("SELECT broadcaster_id FROM channels WHERE login=?", (login,)).fetchone()
                db.execute("DELETE FROM channels WHERE login=?", (login,))
                if row:
                    db.execute("DELETE FROM channel_activity WHERE broadcaster_id=?", (row[0],))
                db.commit()
            self._send_json(200, {"ok": True, "channels": _channel_rows()})
            return
        if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
            self._send_json(400, {"error": "Проверь Twitch-логин канала"})
            return
        try:
            canonical_login, broadcaster_id = _resolve_channel(login)
            with _db() as db:
                count = db.execute("SELECT COUNT(*) FROM channels WHERE source='manual'").fetchone()[0]
                existing = db.execute("SELECT source FROM channels WHERE broadcaster_id=?", (broadcaster_id,)).fetchone()
                if count >= 100 and not (existing and existing[0] == "manual"):
                    self._send_json(400, {"error": "Можно выбрать не более 100 каналов"})
                    return
                db.execute(
                    """INSERT INTO channels(login, broadcaster_id, source) VALUES (?, ?, 'manual')
                       ON CONFLICT(login) DO UPDATE SET broadcaster_id=excluded.broadcaster_id, source='manual'""",
                    (canonical_login.casefold(), broadcaster_id),
                )
                db.commit()
            self._send_json(200, {"ok": True, "channels": _channel_rows()})
        except (urllib.error.URLError, KeyError, TimeoutError) as exc:
            log.exception("Channel lookup failed")
            self._send_json(502, {"error": "Не удалось проверить канал через Twitch"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)[:180]})


def serve_http() -> None:
    server = ThreadingHTTPServer((os.environ.get("DASHBOARD_BIND", "0.0.0.0"), 80), DashboardHandler)
    server.secure = False
    server.serve_forever()


def _ensure_compatible_db() -> None:
    """Keep the dashboard compatible with the currently deployed bot schema."""
    with _db() as db:
        migrate_sync(db)


def main() -> None:
    if not BOOTSTRAP_TOKEN or len(SESSION_SECRET) < 32:
        raise RuntimeError("Dashboard auth secrets are not configured")
    _ensure_compatible_db()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    https_server = ThreadingHTTPServer((os.environ.get("DASHBOARD_BIND", "0.0.0.0"), 443), DashboardHandler)
    https_server.secure = True
    https_server.tls_context = context
    threading.Thread(target=serve_http, name="dashboard-http-redirect", daemon=True).start()
    log.info("Dashboard listening on HTTPS port 443")
    https_server.serve_forever()


if __name__ == "__main__":
    main()
