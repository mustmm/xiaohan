"""她的"日常"：每天的状态、隔天补刀、自由活动。

设计原则：
- 状态一天只生成一次，存进 kv（daily:<日期>），改天自动换。
- 补刀（[补:xxx]）由她自己在回复里提出来，存进 kv(callbacks)，第二天找机会补。
- 全部走 memory.kv_get / kv_set，不加表、不引第三方库。
"""

from __future__ import annotations

import json
import random
import re
import time

PROMISE_KEY = "promises"
CB_KEY = "callbacks"
CB_TAG = re.compile(r"[\[【]\s*补\s*[:：]\s*([^\]】]+?)\s*[\]】]")

_STATE_PROMPT = """你是{name}，一个高一女生。今天是 {date}（{weekday}）。

请用第一人称写你"今天的状态"：
- 今天大概在干什么（学校/画画/练琴/在家躺着，随便，但要像真的生活）
- 今天心情什么样
- 有没有什么小事想吐槽或开心

要求：3 行以内，每行一句话，用你自己说话的口吻（短句、口语、可以带一点语气词），
不要写"状态："这种标签，直接写内容。"""


def _today_key(now: float) -> str:
    return "daily:" + time.strftime("%Y-%m-%d", time.localtime(now))


def weekday_cn(now: float) -> str:
    """本地星期。time.strftime('%w') 里 0 是星期日。"""
    names = "日一二三四五六"
    idx = int(time.strftime("%w", time.localtime(now)))
    return "星期" + names[idx]


def daily(memory, llm, name: str = "具小涵", now: float | None = None) -> dict:
    """今天的状态；没有就现场生成一次并落库。"""
    now = now or time.time()
    key = _today_key(now)
    raw = memory.kv_get(key)
    if raw:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            pass
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    try:
        text = llm.chat(
            [{"role": "user",
              "content": _STATE_PROMPT.format(name=name, date=date, weekday=weekday_cn(now))}],
            temperature=1.15, max_tokens=220,
        ) or ""
    except Exception:
        text = ""
    lines = [ln.strip(" -·\t") for ln in text.splitlines() if ln.strip()]
    data = {"date": date, "lines": lines[:4]}
    memory.kv_set(key, json.dumps(data, ensure_ascii=False))
    return data


def state_note(data: dict) -> str:
    if not data or not data.get("lines"):
        return ""
    return ("你今天的状态（这是你自己昨天定的，顺着它来，别演成另一个人）：\n"
            + "\n".join("- " + ln for ln in data["lines"]))


# ---------------- 隔天补刀 ----------------

def split_callbacks(parts: list[str]) -> tuple[list[str], list[str]]:
    """把回复里的 [补:xxx] 摘出来，剩下的照常发。"""
    clean: list[str] = []
    tags: list[str] = []
    for p in parts:
        for m in CB_TAG.finditer(p or ""):
            tag = m.group(1).strip()
            if tag:
                tags.append(tag)
        cleaned = CB_TAG.sub("", p or "").strip()
        if cleaned:
            clean.append(cleaned)
    return clean, tags


def _load_cbs(memory) -> list[dict]:
    raw = memory.kv_get(CB_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _save_cbs(memory, items: list[dict]) -> None:
    memory.kv_set(CB_KEY, json.dumps(items[-30:], ensure_ascii=False))


def add_callbacks(memory, items: list[dict], what_list: list[str], delay_days: float = 1.0) -> None:
    """登记补刀：明天这个点之后找机会说出来。"""
    now = time.time()
    for what in what_list[:3]:
        if not what:
            continue
        items.append({
            "what": what[:160],
            "name": items and "" or "",
            "due": now + delay_days * 86400,
            "made": now,
        })
    _save_cbs(memory, items)


def add_callback(memory, *, name: str, wxid: str | None, chat_id: str,
                 what: str, delay_days: float = 1.0) -> None:
    items = _load_cbs(memory)
    items.append({
        "what": what[:160], "name": name, "wxid": wxid, "chat_id": chat_id,
        "due": time.time() + delay_days * 86400, "made": time.time(),
    })
    _save_cbs(memory, items)


def due_callbacks(memory, now: float | None = None, limit: int = 3) -> list[dict]:
    now = now or time.time()
    items = _load_cbs(memory)
    ready = [it for it in items if float(it.get("due", 0)) <= now]
    return ready[:limit]


def pop_callback(memory, item: dict) -> None:
    items = _load_cbs(memory)
    items = [it for it in items if it is not item and not (
        it.get("what") == item.get("what") and abs(float(it.get("made", 0)) - float(item.get("made", 0))) < 1)]
    _save_cbs(memory, items)


# ---------------- 自由活动 ----------------

def quiet(now: float, start: int = 23, end: int = 9) -> bool:
    h = time.localtime(now).tm_hour
    return h >= start or h < end


def roll_chance(now: float, per_tick: float = 0.12, rng: random.Random | None = None) -> bool:
    """每 tick 掷一次骰子，看她这会儿想不想主动说点什么。"""
    if quiet(now):
        return False
    r = rng or random
    return r.random() < per_tick


# ---------- 约定 / 欠账（说了"明天请你奶茶"这种，到点要自己提） ----------

def _load_promises(memory) -> list[dict]:
    try:
        raw = memory.kv_get(PROMISE_KEY) or "[]"
        items = json.loads(raw)
        return items if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_promises(memory, items: list[dict]) -> None:
    memory.kv_set(PROMISE_KEY, json.dumps(items, ensure_ascii=False))


def infer_due(now: float, text: str) -> float | None:
    """从「明天请你喝奶茶」「今晚八点」这种话里推个提醒时间。"""
    text = text or ""
    base = time.localtime(now)
    hour = 20
    m = re.search(r"(\d{1,2})\s*[点时]", text)
    if m:
        try:
            hour = max(0, min(23, int(m.group(1))))
        except ValueError:
            hour = 20
    # 「晚上8点」里的 8 是 20 点，不是第二天早上 8 点。
    if any(w in text for w in ("下午", "晚上", "傍晚", "今晚")) and 1 <= hour <= 11:
        hour += 12
    elif "中午" in text and 1 <= hour <= 10:
        hour += 12
    days = None
    if "今晚" in text or "今天晚上" in text:
        days = 0
    elif "明天" in text or "明儿" in text:
        days = 1
    elif "后天" in text:
        days = 2
    elif "周末" in text or "周六" in text or "周日" in text:
        wd = base.tm_wday  # 0=Mon
        days = (5 - wd) % 7 or 7
    if days is None:
        if not any(w in text for w in ("请", "约", "答应", "还", "兑现", "给你", "带你")):
            return None
        days = 1
    stamp = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, hour, 0, 0, 0, 0, -1))
    due = stamp + days * 86400
    if due <= now:
        due += 86400
    return due


def add_promise(memory, *, name: str, wxid: str | None, chat_id: str,
                what: str, due_at: float) -> None:
    items = _load_promises(memory)
    what_short = (what or "").strip()[:120]
    if not what_short:
        return
    if any(it.get("what") == what_short and it.get("chat_id") == chat_id for it in items):
        return
    items.append({"what": what_short, "name": name, "wxid": wxid, "chat_id": chat_id,
                  "due": float(due_at), "made": time.time()})
    _save_promises(memory, items)


def due_promises(memory, now: float | None = None, limit: int = 3) -> list[dict]:
    now = now or time.time()
    items = _load_promises(memory)
    ready = [it for it in items if float(it.get("due", 0)) <= now]
    ready.sort(key=lambda it: float(it.get("due", 0)))
    return ready[:limit]


def pop_promise(memory, item: dict) -> None:
    items = _load_promises(memory)
    items = [it for it in items if not (
        it.get("what") == item.get("what") and it.get("chat_id") == item.get("chat_id"))]
    _save_promises(memory, items)


def register_promises_from_facts(memory, *, wxid: str, name: str, chat_id: str,
                                 now: float | None = None) -> int:
    """把她整理出来的「约定」变成会到点提醒自己的账。"""
    now = now or time.time()
    made = 0
    try:
        facts = memory.facts([f"contact:{wxid}", f"chat:{chat_id}"])
    except Exception:  # noqa: BLE001
        return 0
    for f in facts:
        if f.get("k") not in ("约定", "欠账", "promise"):
            continue
        due = infer_due(now, f.get("v") or "")
        if not due:
            continue
        add_promise(memory, name=name, wxid=wxid if not wxid.endswith("@chatroom") else None,
                    chat_id=chat_id, what=f.get("v") or "", due_at=due)
        made += 1
    return made


def promise_note(item: dict) -> str:
    return f"你之前跟「{item.get('name') or '他'}」提过「{item.get('what')}」"
