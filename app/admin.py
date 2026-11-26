"""本地管理页：改人设、加好友、看日志、试对话。

只监听 127.0.0.1。启动日志里的地址带 token，打开一次后写成 HttpOnly cookie。
config 里如果把 admin.host 写成 0.0.0.0，这里会忽略，仍然只绑本机。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import logbus

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
ROOT = os.path.dirname(WEB_DIR)
TOKEN_NAME = "xiaohan_admin"
MAX_BODY = 1_000_000
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{24,}$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _loopback_bind(host: str) -> str:
    h = (host or "").strip().lower()
    if h in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    logbus.log(f"管理页只监听 127.0.0.1，已忽略 admin.host={host!r}")
    return "127.0.0.1"


def load_token(path: str) -> str:
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
            if _TOKEN_RE.fullmatch(token):
                return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def clamp_limit(raw, default: int = 1) -> int:
    """主动条数。缺省用 default；0 保留；坏值退回 default；上限 20。"""
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, 20))


def host_is_loopback(value: str) -> bool:
    host = (value or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        end = host.find("]")
        name = host[1:end] if end != -1 else host
    elif host.count(":") == 1:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    return name in _LOOPBACK


def origin_is_loopback(value: str) -> bool:
    if not value:
        return True
    parts = urllib.parse.urlsplit(value)
    return parts.scheme in {"http", "https"} and host_is_loopback(parts.hostname or "")


class AdminServer:
    def __init__(self, *, cfg: dict, memory, persona_path: str, brain_factory,
                 host="127.0.0.1", port=8799, token: str | None = None,
                 token_path: str | None = None):
        self.cfg = cfg
        self.memory = memory
        self.persona_path = persona_path
        self.brain_factory = brain_factory
        self.host = _loopback_bind(host)
        self.port = port
        self.token = token or load_token(token_path or os.path.join(ROOT, "data", "admin.token"))
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默
                return

            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def _presented_token(self) -> str:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("token"):
                    return query["token"][0]
                header = (self.headers.get("X-Admin-Token") or "").strip()
                if header:
                    return header
                auth = self.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
                raw = self.headers.get("Cookie") or ""
                for part in raw.split(";"):
                    key, _, value = part.strip().partition("=")
                    if key == TOKEN_NAME:
                        return urllib.parse.unquote(value.strip())
                return ""

            def _authorized(self) -> bool:
                got = self._presented_token()
                if not got or len(got) != len(outer.token):
                    return False
                return hmac.compare_digest(got, outer.token)

            def _guard(self) -> bool:
                if not host_is_loopback(self.headers.get("Host") or ""):
                    self._json({"error": "forbidden"}, 403)
                    return False
                origin = self.headers.get("Origin") or ""
                if origin and not origin_is_loopback(origin):
                    self._json({"error": "forbidden"}, 403)
                    return False
                return True

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return {}
                if n <= 0:
                    return {}
                if n > MAX_BODY:
                    return {"__too_large__": True}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return {}

            def _length_ok(self) -> bool:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._json({"error": "bad length"}, 400)
                    return False
                if n < 0 or n > MAX_BODY:
                    self._json({"error": "body too large"}, 413)
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if not self._guard():
                    return
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                authed = self._authorized()
                if path in ("/", "/index.html"):
                    if not authed:
                        self._send(401, "请用启动日志里打印的管理页地址打开。".encode("utf-8"),
                                   "text/plain; charset=utf-8")
                        return
                    if urllib.parse.parse_qs(parsed.query).get("token"):
                        self.send_response(302)
                        self.send_header(
                            "Set-Cookie",
                            f"{TOKEN_NAME}={outer.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000",
                        )
                        self.send_header("Location", "/")
                        self.send_header("Content-Length", "0")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as fh:
                        self._send(200, fh.read().encode("utf-8"), "text/html; charset=utf-8")
                    return
                if not authed:
                    self._json({"error": "unauthorized"}, 401)
                    return
                if path == "/api/state":
                    with open(outer.persona_path, encoding="utf-8") as fh:
                        persona = fh.read()
                    self._json({
                        "persona": persona,
                        "contacts": outer.memory.contacts(),
                        "limits": outer.cfg["limits"],
                        "features": outer.cfg.get("features", {}),
                        "log": logbus.tail(120),
                        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    return
                if path == "/api/log":
                    self._json({"log": logbus.tail(150)})
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                if not self._guard():
                    return
                if not self._authorized():
                    self._json({"error": "unauthorized"}, 401)
                    return
                if not self._length_ok():
                    return
                path = urllib.parse.urlparse(self.path).path
                data = self._body()
                if data.get("__too_large__"):
                    self._json({"error": "body too large"}, 413)
                    return

                if path == "/api/persona":
                    text = (data.get("text") or "").strip()
                    if not text:
                        self._json({"error": "人设不能为空"}, 400)
                        return
                    with open(outer.persona_path, "w", encoding="utf-8") as fh:
                        fh.write(text + "\n")
                    logbus.log("人设已更新")
                    self._json({"ok": True})
                    return

                if path == "/api/contact":
                    wxid = (data.get("wxid") or "").strip()
                    if not wxid:
                        self._json({"error": "缺 wxid"}, 400)
                        return
                    outer.memory.upsert_contact(
                        wxid,
                        name=(data.get("name") or "").strip(),
                        relation=(data.get("relation") or "").strip(),
                        note=(data.get("note") or "").strip(),
                        proactive_limit=clamp_limit(data.get("proactive_limit"), 1),
                        proactive_enabled=1 if data.get("proactive_enabled", True) else 0,
                    )
                    logbus.log(f"好友已保存: {wxid}")
                    self._json({"ok": True, "contacts": outer.memory.contacts()})
                    return

                if path == "/api/test":
                    text = (data.get("text") or "").strip()
                    if not text:
                        self._json({"error": "空消息"}, 400)
                        return
                    try:
                        brain = outer.brain_factory()
                        replies = brain.respond(
                            chat_id="admin:test", chat_name="哥", is_group=False,
                            group_name="", peers=[], contact_wxid=None,
                            user_name="哥", text=text, is_mentioned=False,
                        )
                        self._json({"replies": replies})
                    except Exception as exc:  # noqa: BLE001
                        self._json({"error": str(exc)[:300]}, 500)
                    return

                self._json({"error": "not found"}, 404)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        threading.Thread(target=self._httpd.serve_forever, name="admin", daemon=True).start()
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
