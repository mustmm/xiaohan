"""发言决策：什么时候回、什么时候不说话、怎么拆成多条、多久再发。"""

from __future__ import annotations

import random
import re
import time

SKIP_TOKEN = "SKIP"


def hour_of(ts: float) -> int:
    return time.localtime(ts).tm_hour


def today(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def in_quiet_hours(ts: float, quiet_start: int = 0, quiet_end: int = 7) -> bool:
    h = hour_of(ts)
    if quiet_start <= quiet_end:
        return quiet_start <= h < quiet_end
    return h >= quiet_start or h < quiet_end


def in_proactive_window(ts: float, start: int = 9, end: int = 23) -> bool:
    return start <= hour_of(ts) < end


def split_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """把模型回复拆成 1~3 条短消息。"""
    text = (text or "").strip()
    if not text:
        return []
    # 模型可以用 ||| 明确分条
    if "|||" in text:
        parts = [p.strip() for p in text.split("|||") if p.strip()]
    else:
        # 否则按空行 / 换行拆，过长的再按句号拆
        parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
        if len(parts) == 1 and len(parts[0]) > 60:
            sentences = re.findall(r"[^。！？!?\n]+[。！？!?]?", parts[0])
            parts = [s.strip() for s in sentences if s.strip()]
    parts = [p for p in parts if p and p != SKIP_TOKEN]
    if len(parts) > max_bubbles:
        # 合并尾部，避免刷屏
        head = parts[: max_bubbles - 1]
        head.append("".join(parts[max_bubbles - 1:]))
        parts = head
    return [p for p in parts if p]


STICKER_RE = re.compile(r"[\[【]\s*表情\s*[:：]\s*([^\]】]+?)\s*[\]】]")
SEARCH_RE = re.compile(r"[\[【]\s*搜\s*[:：]\s*([^\]】]+?)\s*[\]】]")
RESOURCE_RE = re.compile(r"[\[【]\s*资源\s*[:：]\s*([^\]】]+?)\s*[\]】]")
# 明显在要资源/教程/链接的问法（她没写 [资源:x] 时用来兜底触发）
RESOURCE_ASK_RE = re.compile(
    r"(有没有(?:.*?)(?:资源|软件|教程|源码|工具|项目|链接|下载)"
    r"|在哪(?:下|找|下载)|哪里(?:下|找)|求(?:个|一个)?(?:链接|资源|教程|源码)"
    r"|给(?:我)?(?:个|一个)?(?:链接|资源)|发(?:我)?(?:个|一个)?链接"
    r"|(?:资源|源码)(?:发|给)我|找一下(?:这个|那个)?项目)")


def extract_stickers(parts: list[str]) -> tuple[list[str], list[str]]:
    """把回复里的 [表情:叹气] 抠出来：返回 (剩下的文字, 想发的表情标签)。"""
    labels: list[str] = []
    cleaned: list[str] = []
    for part in parts:
        labels.extend(STICKER_RE.findall(part))
        rest = STICKER_RE.sub("", part).strip()
        if rest:
            cleaned.append(rest)
    return cleaned, labels


def typing_delay(text: str, chars_per_sec: float = 6.0,
                 min_delay: float = 0.8, max_delay: float = 4.5) -> float:
    base = len(text) / max(chars_per_sec, 1.0)
    jitter = random.uniform(0.75, 1.35)
    return max(min_delay, min(max_delay, base * jitter))


QUESTION_WORDS = ("？", "?", "吗", "呢", "啥", "怎么", "为什么", "是不是",
                 "能不能", "要不要", "有没有", "行不行", "哪个", "几点", "谁")


def looks_like_question(text: str) -> bool:
    """群聊里判断这句是不是在问她/等她接。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    return any(w in t for w in QUESTION_WORDS)


def _as_enabled(raw, default: bool = True) -> bool:
    if raw is None:
        return default
    try:
        return int(raw) != 0
    except (TypeError, ValueError):
        return bool(raw)


def proactive_cap(contact: dict | None, limits: dict | None) -> int:
    """这个人今天最多主动几条。0 就是 0，不能被 ``or`` 吃成默认值。"""
    limits = limits or {}
    default = limits.get("per_friend_per_day", 1)
    raw = default if contact is None else contact.get("proactive_limit", default)
    if raw is None or raw == "":
        raw = default
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        try:
            cap = int(default)
        except (TypeError, ValueError):
            cap = 1
    return max(0, cap)


def group_should_insert(*, is_mentioned: bool, new_messages: int, last_self_ts: float,
                        inserted_this_hour: int, inserted_today: int, now: float,
                        limits: dict, last_sender: str = "", last_self_sender: str = "",
                        question: bool = False, text: str = "") -> bool:
    """群里要不要接话。

    原则（用户要求）：尽量一句一答，别让话掉地上。
    只有这些情况才不出声：撞上每小时/每天上限、或者纯表情/单字刷屏。
    被 @ 也要守每小时/每天的上限，不然刷 @ 就能把群刷屏。
    """
    if inserted_this_hour >= limits.get("per_hour_per_group", 12):
        return False
    if inserted_today >= limits.get("per_day_per_group", 60):
        return False
    if is_mentioned:
        return True

    body = (text or "").strip()
    tiny = len(body) <= 2 and not question   # 「草」「哈哈哈」这种
    # 直接喊她的名字 → 等于点她名
    if any(k in body for k in ("小涵", "涵涵", "妹妹", "老妹", "你妹")):
        return True
    # 她刚说过话，别人顺着接一句 → 别把话掉地上
    if (len(body) >= 4 and now - last_self_ts <= limits.get("after_reply_window_sec", 90)
            and now - last_self_ts >= limits.get("question_cooldown_sec", 5)):
        return True
    # ① 跟她正聊着的人接着说：一句一答，别掉地上
    same_talker = bool(last_sender) and last_sender == last_self_sender
    if same_talker and not tiny and now - last_self_ts >= limits.get("dialog_cooldown_sec", 8):
        return True
    # ② 有人问了一句（哪怕没 @她）
    if question and now - last_self_ts >= limits.get("question_cooldown_sec", 5):
        return True
    # ③ 有实质内容的新消息：多半会接
    if new_messages >= limits.get("min_new_messages", 1) and not tiny:
        if now - last_self_ts >= limits.get("cooldown_sec", 8):
            p = min(0.75, 0.25 + new_messages * 0.05)
            return random.random() < p
    return False


def can_proactive(*, contact: dict | None, sent_today: int, now: float,
                  limits: dict) -> bool:
    if not in_proactive_window(now, limits.get("window_start", 9), limits.get("window_end", 23)):
        return False
    if in_quiet_hours(now, limits.get("quiet_start", 0), limits.get("quiet_end", 7)):
        return False
    if contact is not None and not _as_enabled(contact.get("proactive_enabled", 1)):
        return False
    return sent_today < proactive_cap(contact, limits)

MEDIA_RE = re.compile(r"[\[【]\s*(自拍|画|找图|找表情)\s*[:：]\s*([^\]】]+?)\s*[\]】]")


def extract_media(parts: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """把 [自拍:便利店门口] / [画:趴桌上睡着] / [找图:无语 表情包] 摘出来。"""
    cleaned: list[str] = []
    items: list[tuple[str, str]] = []
    for part in parts:
        for m in MEDIA_RE.finditer(part or ""):
            kind = m.group(1)
            if kind in ("找图", "找表情"):
                kind = "找图"
            items.append((kind, m.group(2).strip()))
        text = MEDIA_RE.sub("", part or "").strip()
        if text:
            cleaned.append(text)
    return cleaned, items

