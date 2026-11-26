"""离线检查：限额、管理页、URL、发信记录。不连微信，不访问公网。"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys

sys.path.insert(0, BASE)

from app import life, policy, safehttp  # noqa: E402
from app.adapters.base import Incoming  # noqa: E402
from app.admin import AdminServer  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.main import acquire_lock  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.persona import Persona  # noqa: E402
from app.runtime import Runtime  # noqa: E402
from app.wxtext import (  # noqa: E402
    chat_matches,
    redact_special,
    safe_token,
    strip_group_prefix,
)


class FakeLLM:
    def chat(self, messages, temperature=1.1, max_tokens=800):
        return "哥你回来啦"


class Gate:
    def __init__(self):
        self.sent: list[str] = []
        self.allow = True

    def send_text(self, chat_id, text, is_group):
        if not self.allow:
            return False
        self.sent.append(text)
        return True

    def is_friend(self, wxid):
        return True


def _noon() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 12, 0, 0, 0, 0, -1))


def _cfg() -> dict:
    return {
        "model": {"temperature": 0.0, "max_tokens": 100},
        "limits": {
            "bubbles": {"max": 3},
            "typing": {"chars_per_sec": 1000, "min_delay": 0, "max_delay": 0, "first_delay_max": 0},
            "group_insert": {},
            "proactive": {
                "per_friend_per_day": 1,
                "window_start": 0,
                "window_end": 24,
                "quiet_start": 0,
                "quiet_end": 0,
            },
            "debounce_sec": 0,
        },
        "features": {"allow_moments_post": True},
        "moments": {"enabled": False},
        "browse": {"enabled": True, "interval_min": 0, "chance": 1, "max_per_day": 4},
    }


def _runtime(memory: Memory, gate: Gate | None = None) -> tuple[Runtime, Gate]:
    gate = gate or Gate()
    cfg = _cfg()
    memory.kv_set("hot_words", json.dumps(["离线"]))
    memory.kv_set("hot_ts", str(time.time()))
    brain = Brain(cfg, memory, Persona(os.path.join(BASE, "persona.md")), FakeLLM())
    runtime = Runtime(cfg, memory, brain, gate, log=lambda *_a, **_k: None)
    return runtime, gate


def _check(cond, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def test_weekday_and_evening() -> None:
    seen = {}
    start = time.time()
    for i in range(8):
        ts = start + i * 86400
        seen[int(time.strftime("%w", time.localtime(ts)))] = life.weekday_cn(ts)
    _check(seen[0] == "星期日", seen[0])
    _check(seen[1] == "星期一", seen[1])
    _check(seen[4].endswith("四"), seen[4])
    morning = time.mktime((2026, 11, 26, 10, 0, 0, 0, 0, -1))
    due = life.infer_due(morning, "晚上8点请你喝奶茶")
    _check(due is not None and time.localtime(due).tm_hour == 20, str(due))
    due = life.infer_due(morning, "下午3点请你吃饭")
    _check(due is not None and time.localtime(due).tm_hour == 15, str(due))


def test_counter_rolls_by_day() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        memory = Memory(path)
        _check(memory.bump("p:wx", "2026-11-26") == 1, "first")
        _check(memory.bump("p:wx", "2026-11-26") == 2, "same day adds")
        _check(memory.get_counter("p:wx", "2026-11-26") == 2, "read back")
        _check(memory.bump("p:wx", "2026-11-27") == 1, "new day resets")
        _check(memory.get_counter("p:wx", "2026-11-26") == 0, "yesterday is zero")
        _check(memory.get_counter("p:wx", "2026-11-27") == 1, "today is one")
    finally:
        os.remove(path)


def test_limits() -> None:
    noon = _noon()
    limits = {
        "window_start": 0, "window_end": 24, "quiet_start": 0, "quiet_end": 0,
        "per_friend_per_day": 1,
    }
    _check(not policy.can_proactive(
        contact={"proactive_enabled": 1, "proactive_limit": 0},
        sent_today=0, now=noon, limits=limits,
    ), "limit 0 should block")
    _check(policy.can_proactive(
        contact={"proactive_enabled": 1, "proactive_limit": 1},
        sent_today=0, now=noon, limits=limits,
    ), "limit 1 should allow the first")
    _check(not policy.can_proactive(
        contact={"proactive_enabled": "0", "proactive_limit": 5},
        sent_today=0, now=noon, limits=limits,
    ), "enabled 0 should block")
    group = {
        "per_hour_per_group": 2, "per_day_per_group": 10,
        "min_new_messages": 6, "cooldown_sec": 0,
    }
    _check(policy.group_should_insert(
        is_mentioned=True, new_messages=1, last_self_ts=0,
        inserted_this_hour=0, inserted_today=0, now=noon, limits=group,
    ), "mention under cap should reply")
    _check(not policy.group_should_insert(
        is_mentioned=True, new_messages=1, last_self_ts=0,
        inserted_this_hour=2, inserted_today=2, now=noon, limits=group,
    ), "mention over hourly cap should stay quiet")


def test_message_text() -> None:
    _check(strip_group_prefix("wxid_abc:\n你好") == "你好", "wxid prefix")
    _check(strip_group_prefix("Hello: 在吗") == "Hello: 在吗", "english colon kept")
    _check(strip_group_prefix("abcde: world") == "abcde: world", "short token kept")
    _check(strip_group_prefix("customid:\n正文") == "正文", "newline sender prefix")
    quoted = (
        "<msg><appmsg><title>我是说这个 https://evil.example/a</title>"
        "<refermsg><displayname>哥</displayname><content>明天考试</content></refermsg>"
        "</appmsg></msg>"
    )
    kind, text = redact_special("link", quoted, "文件/链接/卡片")
    _check(kind == "_safe_skip", kind)
    _check("我是说这个" in text and "明天考试" in text, text)
    _check("https://" not in text and "evil.example" not in text, text)
    kind, text = redact_special(
        "link",
        "<msg><appmsg><title>看这个</title><url>https://evil.example/x</url></appmsg></msg>",
        "link",
    )
    _check("没点开" in text and "evil.example" not in text, text)
    kind, text = redact_special("hongbao", "<msg><wcpayinfo>secret</wcpayinfo></msg>", "红包")
    _check("没领" in text and "wcpayinfo" not in text, text)
    kind, text = redact_special("location", '<location x="39.9" y="116.3"/>', "位置")
    _check("39.9" not in text and "没点开" in text, text)
    _check(safe_token("../../etc/passwd") == "etcpasswd", safe_token("../../etc/passwd"))
    _check(not chat_matches("文件传输助手", "爸爸"), "different chats")
    _check(not chat_matches("", ""), "empty chat is not a match")
    _check(chat_matches("爸爸", "爸爸"), "same chat")


def test_safe_url() -> None:
    blocked = [
        "file:///etc/passwd",
        "http://127.0.0.1/latest",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
        "http://192.168.0.5/a",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://user:pass@1.1.1.1/",
        "http://localhost/admin",
        "ftp://1.1.1.1/a",
    ]
    for url in blocked:
        try:
            safehttp.validate_public_http_url(url)
        except safehttp.UnsafeURL:
            continue
        raise AssertionError("should block " + url)
    safehttp.validate_public_http_url("http://1.1.1.1/ok")
    safehttp.validate_public_http_url("https://8.8.8.8/dns")

    class Req:
        full_url = "https://1.1.1.1/a"

    try:
        safehttp._GuardRedirect().redirect_request(
            Req(), None, 302, "found", {}, "http://127.0.0.1/secret",
        )
    except safehttp.UnsafeURL:
        return
    raise AssertionError("redirect to loopback should be blocked")


def test_failed_send_is_not_remembered() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        memory = Memory(path)
        runtime, gate = _runtime(memory)
        gate.allow = False
        runtime.handle(Incoming(
            chat_id="p1", chat_name="甲", is_group=False,
            sender_id="wxid_a", sender_name="甲", content="你好",
        ))
        rows = memory.recent("p1")
        _check(any(row["role"] == "user" for row in rows), "user message kept")
        _check(all(row["role"] != "assistant" for row in rows), "failed send not stored")
        _check(gate.sent == [], "adapter rejected the text")
    finally:
        os.remove(path)


def test_browse_and_promises_and_moments() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        memory = Memory(path)
        runtime, gate = _runtime(memory)
        noon = _noon()
        day = policy.today(noon)
        memory.upsert_contact("wxid_a", name="甲", note="朋友", relation="朋友",
                              proactive_enabled=1, proactive_limit=1)
        memory.bump(f"p:wxid_a", day, 1)
        picked = {"n": 0}

        def fake_pick(mem, now=None, rng=None):
            picked["n"] += 1
            return {"title": "测试", "source": "b站", "up": "", "url": ""}

        import app.browse as browse
        old_pick = browse.pick
        browse.pick = fake_pick
        runtime.brain.proactive = lambda **_k: ["分享一句"]
        try:
            runtime._maybe_browse(noon)
            _check(gate.sent == [], "browse over cap must not send")
            _check(picked["n"] == 0, "browse should not search once nobody is eligible")
        finally:
            browse.pick = old_pick
        with memory._lock:
            memory._db.execute("DELETE FROM counters")
            memory._db.commit()
        picked["n"] = 0
        browse.pick = fake_pick
        try:
            runtime._maybe_browse(noon)
        finally:
            browse.pick = old_pick
        _check(gate.sent == ["分享一句"], f"browse under cap should send, got {gate.sent}")
        rows = [row for row in memory.recent("wxid_a") if row["role"] == "assistant"]
        _check(rows and rows[-1]["sender_name"] == "具小涵", rows[-1]["sender_name"] if rows else None)
        _check("|||" not in (rows[-1]["content"] if rows else ""), "bubbles stored separately")
        _check(memory.get_counter("p:wxid_a", day) == 1, "browse counts toward the daily cap")

        gate.sent.clear()
        memory.upsert_contact("wxid_off", name="乙", note="朋友", proactive_enabled=0, proactive_limit=2)
        life.add_promise(memory, name="乙", wxid="wxid_off", chat_id="wxid_off",
                         what="明天请奶茶", due_at=noon - 10)
        calls = {"n": 0}

        def counting(**_k):
            calls["n"] += 1
            return ["到点了"]

        runtime.brain.proactive = counting
        runtime._deliver_promises(noon)
        _check(calls["n"] == 0 and gate.sent == [], "disabled contact must not get a promise")

        memory.upsert_contact("wxid_on", name="丙", note="朋友", proactive_enabled=1, proactive_limit=2)
        life.add_promise(memory, name="丙", wxid="wxid_on", chat_id="wxid_on",
                         what="晚上8点请你喝奶茶", due_at=noon - 10)
        runtime.cfg["limits"]["proactive"]["quiet_start"] = 23
        runtime.cfg["limits"]["proactive"]["quiet_end"] = 9
        night = time.mktime(time.localtime(noon)[:3] + (23, 30, 0, 0, 0, -1))
        runtime._deliver_promises(night)
        _check(gate.sent == [], "quiet hours hold the promise")
        runtime.cfg["limits"]["proactive"]["quiet_start"] = 0
        runtime.cfg["limits"]["proactive"]["quiet_end"] = 0
        runtime._deliver_promises(noon)
        _check(gate.sent == ["到点了"], f"promise should send at noon, got {gate.sent}")

        runtime.features["allow_moments_post"] = False
        runtime.cfg["moments"] = {"enabled": True, "window": [0, 24], "per_day": 3, "chance": 1}
        runtime._maybe_post_moment(noon)
        _check(runtime._moment_busy is False, "moments flag should stop the post")
    finally:
        os.remove(path)


def test_catchup_match() -> None:
    path = os.path.join(BASE, "tools", "catchup.py")
    spec = importlib.util.spec_from_file_location("xiaohan_catchup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _check(mod.model_name({"model": {"model": "deepseek-reasoner"}}) == "deepseek-reasoner", "model key")
    _check(mod.model_name({"model": {"name": "legacy"}}) == "legacy", "legacy name")
    db = sqlite3.connect(":memory:")
    db.execute(
        "create table messages (id integer primary key, chat_id, chat_name, is_group, "
        "sender_id, sender_name, content, ts, role)"
    )
    db.execute("insert into messages values (1,'a','哥哥',0,'a','哥哥','晚',1,'user')")
    db.execute("insert into messages values (2,'b','哥',0,'b','哥','早',2,'user')")
    db.execute("insert into messages values (3,'c','哥哥',0,'c','哥哥','更新',3,'user')")
    row = mod.find_latest_user(db, "哥")
    _check(row[0] == "b", f"exact name, got {row}")
    row = mod.find_latest_user(db, "%")
    _check(row is None, f"wildcard name must not match everyone, got {row}")
    row = mod.find_latest_user(db, "哥哥")
    _check(row[0] == "c", f"latest exact 哥哥, got {row}")


def test_lock() -> None:
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "xiaohan.lock")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
        _check(acquire_lock(path) is False, "live pid should keep the lock")
    finally:
        proc.kill()
        proc.wait(timeout=5)
    _check(acquire_lock(path) is True, "dead pid should release the lock")
    _check(acquire_lock(path) is True, "the same process already holds it")
    other = os.path.join(folder, "other.lock")
    with open(other, "w", encoding="utf-8") as fh:
        fh.write("not-a-pid")
    _check(acquire_lock(other) is True, "garbage lock should be replaced")


def _admin_status(port: int, method: str, target: str, body: bytes | None = None,
                  headers: dict | None = None) -> tuple[int, bytes, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        cookie = resp.getheader("Set-Cookie") or ""
        return resp.status, payload, cookie
    finally:
        conn.close()


def test_admin() -> None:
    folder = tempfile.mkdtemp()
    db_path = os.path.join(folder, "t.db")
    persona = os.path.join(folder, "persona.md")
    with open(persona, "w", encoding="utf-8") as fh:
        fh.write("原人设标记XYZ\n")
    memory = Memory(db_path)
    token = "t" * 32
    server = AdminServer(
        cfg={"limits": {"proactive": {}}, "features": {}},
        memory=memory, persona_path=persona, brain_factory=lambda: None,
        host="0.0.0.0", port=0, token=token,
    )
    _check(server.host == "127.0.0.1", server.host)
    url = server.start()
    try:
        _check("127.0.0.1" in url and "token=" in url, url)
        port = server.port
        status, body, _cookie = _admin_status(port, "GET", "/api/state")
        _check(status == 401, str(status))
        _check("原人设标记XYZ" not in body.decode("utf-8", "ignore"), "state leaked")

        status, _body, cookie = _admin_status(port, "GET", "/?token=" + token)
        _check(status == 302 and "xiaohan_admin=" in cookie and "HttpOnly" in cookie, cookie)
        cookie_pair = cookie.split(";", 1)[0]
        status, body, _cookie = _admin_status(
            port, "GET", "/api/state", headers={"Cookie": cookie_pair},
        )
        _check(status == 200 and "原人设标记XYZ" in body.decode(), body[:80])
        status, body, _cookie = _admin_status(
            port, "GET", "/api/state",
            headers={"Host": "evil.example", "Cookie": cookie_pair},
        )
        _check(status == 403, str(status))
        _check("原人设标记XYZ" not in body.decode("utf-8", "ignore"), "evil host leaked state")

        payload = json.dumps({
            "wxid": "wxid_t", "name": "甲", "proactive_limit": 0, "proactive_enabled": True,
        }).encode()
        status, _body, _cookie = _admin_status(
            port, "POST", "/api/contact", body=payload,
            headers={"Cookie": cookie_pair, "Content-Type": "application/json"},
        )
        _check(status == 200, str(status))
        _check(memory.contact("wxid_t")["proactive_limit"] == 0, memory.contact("wxid_t"))

        bad = json.dumps({"wxid": "wxid_bad", "proactive_limit": "nope"}).encode()
        status, _body, _cookie = _admin_status(
            port, "POST", "/api/contact", body=bad,
            headers={"Cookie": cookie_pair, "Content-Type": "application/json"},
        )
        _check(status == 200, str(status))
        _check(memory.contact("wxid_bad")["proactive_limit"] == 1, memory.contact("wxid_bad"))

        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        try:
            req = (
                "POST /api/persona HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Cookie: {cookie_pair}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 99999999\r\n"
                "\r\n"
            )
            sock.sendall(req.encode())
            data = sock.recv(1024)
        finally:
            sock.close()
        _check(b"413" in data, data[:120])
        with open(persona, encoding="utf-8") as fh:
            _check("原人设标记XYZ" in fh.read(), "huge body must not truncate the persona")
    finally:
        server.stop()


def run() -> None:
    test_weekday_and_evening()
    test_counter_rolls_by_day()
    test_limits()
    test_message_text()
    test_safe_url()
    test_failed_send_is_not_remembered()
    test_browse_and_promises_and_moments()
    test_catchup_match()
    test_lock()
    test_admin()
    print("安全与正确性检查通过")


if __name__ == "__main__":
    run()
