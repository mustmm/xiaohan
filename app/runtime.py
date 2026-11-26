"""运行时：收消息 → 判断 → 让她说话。所有限额都在这里把关。"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time

from . import browse, kougei, life, policy
from .adapters.base import Adapter, Incoming
from .brain import Brain
from .memory import Memory


class Runtime:
    def __init__(self, cfg: dict, memory: Memory, brain: Brain, adapter: Adapter, log=print):
        self.cfg = cfg
        self.memory = memory
        self.brain = brain
        self.adapter = adapter
        self.log = log
        self._send_lock = threading.Lock()
        self._last_say: dict[str, float] = {}
        self._stop = threading.Event()
        self._moment_busy = False
        self.typing = cfg["limits"]["typing"]
        self.group_limits = cfg["limits"]["group_insert"]
        self.features = cfg.get("features", {})

    # ---------- 发送 ----------

    def say(self, chat_id: str, parts: list[str], is_group: bool,
            max_bubbles: int | None = None) -> list[str]:
        """发出去的才返回。适配器明确返回 False，或发送抛错，就停，不把没送出的话记成已回。"""
        if max_bubbles:
            parts = parts[:max_bubbles]
        sent: list[str] = []
        for i, part in enumerate(parts):
            if not part or not part.strip():
                continue
            delay = policy.typing_delay(
                part,
                self.typing.get("chars_per_sec", 6.0),
                self.typing.get("min_delay", 0.8),
                self.typing.get("max_delay", 4.5),
            )
            if i == 0:
                delay = min(delay, self.typing.get("first_delay_max", 1.2))
            if delay > 0:
                time.sleep(delay)
            try:
                with self._send_lock:
                    result = self.adapter.send_text(chat_id, part, is_group)
            except Exception as exc:  # noqa: BLE001
                self.log(f"发送失败 [{chat_id}]: {exc}")
                break
            if result is False:
                self.log(f"发送未成功，停在 [{chat_id}]")
                break
            sent.append(part)
            self.log(f"→ [{chat_id}] {part}")
        return sent

    def _record_out(self, chat_id: str, chat_name: str, is_group: bool,
                    parts: list[str], ts: float | None = None) -> None:
        for part in parts:
            self.memory.add_message(
                chat_id=chat_id, chat_name=chat_name, is_group=is_group,
                sender_id="self", sender_name="具小涵", role="assistant",
                content=part, ts=ts or time.time(),
            )

    # ---------- 表情包 ----------

    def send_sticker(self, chat_id: str, label: str, is_group: bool) -> bool:
        book = getattr(self.brain, "stickers", None)
        sender = getattr(self.adapter, "send_sticker", None)
        if not book or not sender:
            return False
        item = book.pick(label)
        if not item:
            return False
        try:
            time.sleep(random.uniform(0.6, 1.6))
            with self._send_lock:
                result = sender(chat_id, item["path"])
            if result is False:
                return False
            self.log(f"→ [{chat_id}] [表情:{item.get('caption')}]")
            self.memory.add_message(
                chat_id=chat_id, chat_name=chat_id, is_group=is_group,
                sender_id="self", sender_name="具小涵", role="assistant",
                content=f"[表情:{item.get('caption')}]", ts=time.time(),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"表情包发送失败: {exc}")
            return False

    # ---------- 收消息 ----------

    def _drop_if_overtaken(self, msg: Incoming) -> bool:
        """一句一答的要点：等他一句说完再答。

        收到消息先等一小会儿，如果同一个人马上又补了一句，就让后一条去回，
        这一条直接放弃——不然她会对半句话各回一句，看着像复读机。
        """
        # 用户要求：有人发消息一定秒回，所以这里只做一点点等待（合并连发），
        # 绝不丢消息——哪怕对方又补了一句，这一条也照回。
        wait = float(self.cfg.get("limits", {}).get("debounce_sec", 0.6) or 0)
        if wait > 0:
            time.sleep(min(wait, 1.0))
        return False

    def handle(self, msg: Incoming) -> None:
        if not msg.content.strip():
            return
        # 没见过的人：建档，但只是「被动回复」，绝不主动找他
        if not msg.is_group:
            self.memory.ensure_contact(msg.sender_id or msg.chat_id,
                                       msg.sender_name or msg.chat_name)
            self._apply_proactive_default(msg.sender_id or msg.chat_id,
                                          msg.sender_name or msg.chat_name)
        # 只跟好友聊；陌生人一律不回
        if not msg.is_group and not self.adapter.is_friend(msg.sender_id):
            self.log(f"忽略非好友: {msg.sender_id}")
            return

        self.memory.add_message(
            chat_id=msg.chat_id, chat_name=msg.chat_name, is_group=msg.is_group,
            sender_id=msg.sender_id, sender_name=msg.sender_name,
            role="user", content=msg.content, ts=msg.ts,
        )

        if self._drop_if_overtaken(msg):
            return

        now = time.time()
        day = policy.today(now)
        hour_key = f"g_h:{msg.chat_id}:{policy.hour_of(now)}"

        if msg.is_group:
            since = self.memory.last_assistant_ts(msg.chat_id) or (now - 3600)
            fresh = self.memory.count_since(msg.chat_id, since)
            if not policy.group_should_insert(
                is_mentioned=msg.is_mentioned,
                new_messages=max(fresh, 1),
                last_self_ts=self.memory.last_assistant_ts(msg.chat_id),
                inserted_this_hour=self.memory.get_counter(hour_key, day),
                inserted_today=self.memory.get_counter(f"g_d:{msg.chat_id}", day),
                now=now,
                limits=self.group_limits,
                last_sender=msg.sender_name or "",
                last_self_sender=self.memory.sender_of_last_reply(msg.chat_id),
                question=policy.looks_like_question(msg.content),
                text=msg.content or "",
            ):
                return
        else:
            # 私聊：如果她刚发完、用户又追了一句，照常接上，不抢话是靠队列保证的
            pass

        # 雷霆抽象话：碰到那两个字的组合，百分百套公式，不过大脑
        if not msg.is_group or msg.is_mentioned:
            hit = kougei.find_word(msg.content)
            if hit and not self._is_elder(msg):
                reply = kougei.make_reply(hit)
                self.log(f"雷霆接梗：「{hit}」→ {reply}")
                sent = self.say(msg.chat_id, [reply], msg.is_group)
                if sent:
                    self._record_out(msg.chat_id, msg.chat_name, msg.is_group, sent)
                return

        correction = self._learn_correction(msg)
        try:
            replies = self.brain.respond(
                chat_id=msg.chat_id, chat_name=msg.chat_name, is_group=msg.is_group,
                group_name=msg.chat_name if msg.is_group else "",
                peers=self._peers(msg), contact_wxid=None if msg.is_group else msg.sender_id,
                user_name=msg.sender_name, text=msg.content,
                is_mentioned=msg.is_mentioned, now=now,
                correction=correction,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"大脑报错: {exc}")
            return

        if not replies:
            return
        texts, labels = policy.extract_stickers(replies)
        texts, media = policy.extract_media(texts)
        texts, callbacks = life.split_callbacks(texts)
        for what in callbacks:
            life.add_callback(
                self.memory, name=msg.sender_name or msg.chat_name,
                wxid=msg.sender_id, chat_id=msg.chat_id, what=what,
            )
        if media:
            threading.Thread(target=self._make_and_send_image,
                             args=(msg.chat_id, msg.is_group, media[0]), daemon=True).start()
        if not texts and not labels:
            return
        # 对方只说几个字，就别砸一堆气泡过去（不然看着像自己在跟自己聊）
        brief = len(msg.content.strip())
        max_bubbles = 1 if brief <= 8 else (2 if brief <= 22 else 3)
        sent: list[str] = []
        if texts:
            # 同一个会话至少隔 5 秒，避免机关枪式连发
            gap = 5.0 - (time.time() - self._last_say.get(msg.chat_id, 0.0))
            if gap > 0:
                time.sleep(min(gap, 5.0))
            sent = self.say(msg.chat_id, texts, msg.is_group, max_bubbles=max_bubbles)
            if sent:
                self._last_say[msg.chat_id] = time.time()
        for label in labels[:1]:
            self.send_sticker(msg.chat_id, label, msg.is_group)
        if sent:
            self._record_out(msg.chat_id, msg.chat_name, msg.is_group, sent)
        if msg.is_group and sent:
            self.memory.bump(hour_key, day)
            self.memory.bump(f"g_d:{msg.chat_id}", day)

        # 被拍一拍：一半概率拍回去，像真人在闹
        if "拍了拍" in msg.content and not msg.is_group:
            if hasattr(self.adapter, "send_poke") and random.random() < 0.5:
                try:
                    with self._send_lock:
                        self.adapter.send_poke(msg.chat_id)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"拍回去失败: {exc}")

        # 聊到有情绪的地方，自己上网找张图甩过来（她不用等别人点她）
        if not media:
            self._maybe_drop_web_image(msg.chat_id, msg.is_group, msg.content)

    def _is_elder(self, msg) -> bool:
        """长辈（爸妈、老师）面前不玩梗、不说圈内话。"""
        names = {"爸爸", "妈妈", "爸", "妈"}
        if (msg.sender_name or "") in names or (msg.chat_name or "") in names:
            return True
        wxid = msg.sender_id or msg.chat_id
        try:
            row = self.memory.contact(wxid) or {}
        except Exception:  # noqa: BLE001
            row = {}
        text = f"{row.get('name','')}{row.get('relation','')}{row.get('note','')}"
        return ("爸爸" in text) or ("妈妈" in text) or ("长辈" in text)

    def _apply_proactive_default(self, wxid: str, name: str) -> None:
        """按 config.proactive_defaults 决定这个人能不能被她主动找。

        默认规则是「新朋友一律被动」；爸爸妈妈这种例外写在 config 里，
        这样他们哪天被加上好友，第一次说话就自动按设定建档。
        """
        table = (self.cfg.get("proactive_defaults") or {})
        if not name or name not in table:
            return
        want = int(table[name])
        cur = self.memory.contact(wxid) or {}
        if int(cur.get("proactive_enabled") or 0) == want:
            return
        self.memory.upsert_contact(
            wxid, name=name,
            relation=cur.get("relation") or "",
            note=cur.get("note") or "",
            proactive_limit=policy.proactive_cap(cur, self.cfg["limits"]["proactive"]),
            proactive_enabled=want,
        )

    def _peers(self, msg: Incoming) -> list[str]:
        rows = self.memory.recent(msg.chat_id, 30)
        names = {r["sender_name"] for r in rows if r["role"] == "user" and r["sender_name"]}
        names.discard(msg.sender_name)
        return list(names)[:6]

    # ---------- 主动 ----------

    def proactive_once(self, contact: dict, kind: str) -> bool:
        now = time.time()
        day = policy.today(now)
        sent = self.memory.get_counter(f"p:{contact['wxid']}", day)
        if not policy.can_proactive(contact=contact, sent_today=sent, now=now,
                                    limits=self.cfg["limits"]["proactive"]):
            return False
        try:
            parts = self.brain.proactive(contact_wxid=contact["wxid"],
                                         chat_name=contact.get("name") or contact["wxid"],
                                         kind=kind, now=now)
        except Exception as exc:  # noqa: BLE001
            self.log(f"主动消息失败: {exc}")
            return False
        if not parts:
            return False
        texts, labels = policy.extract_stickers(parts)
        if not texts and not labels:
            return False
        sent = self.say(contact["wxid"], texts, False) if texts else []
        sticker_ok = False
        for label in labels[:1]:
            if self.send_sticker(contact["wxid"], label, False):
                sticker_ok = True
        if sent:
            self._record_out(contact["wxid"], contact.get("name") or contact["wxid"], False, sent)
        if not sent and not sticker_ok:
            return False
        self.memory.bump(f"p:{contact['wxid']}", day)
        return True

    # ---------- 后台：主动开口 ----------

    def start_scheduler(self) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    self.log(f"调度异常: {exc}")
                    self.log(traceback.format_exc()[-1200:])
                self._stop.wait(600)

        t = threading.Thread(target=loop, name="scheduler", daemon=True)
        t.start()
        return t

    # ---------- 画图 / 找图 ----------
    def _make_and_send_image(self, chat_id: str, is_group: bool, item) -> None:
        """她说了 [自拍:…] / [画:…] / [找图:…]，就去弄一张再发出去。"""
        kind, arg = item
        if kind == "找图":
            self._find_and_send_image(chat_id, is_group, arg)
            return
        try:
            from . import imagegen
            key = self._img_key()
            if not key:
                self.log("没有生图 key，跳过画图")
                return
            self.log(f"开始画：{kind}「{arg}」")
            cfg = self.cfg.get("imagegen") or {}
            path = (imagegen.selfie(arg, key, cfg) if kind == "自拍"
                    else imagegen.artwork(arg, key, cfg,
                                          colored=bool(cfg.get("colored"))))
            if not path:
                self.log("画失败了")
                return
            send = getattr(self.adapter, "send_image", None)
            if send is None:
                self.log("这版适配器发不了图")
                return
            with self._send_lock:
                ok = send(chat_id, path, is_group)
            self.log(f"画好了并发出：{os.path.basename(path)} → {ok}")
            if ok is not True:
                return
            self._record_out(
                chat_id, chat_id, is_group,
                [f"[{'自拍' if kind == '自拍' else '画'}]{arg}"],
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"画图出错：{exc}")

    # ---------- 日常聊着聊着自己甩张图 ----------
    MOOD_WORDS = (
        ("笑死", ("笑死", "哈哈", "hhh", "233", "太好笑", "绷不住", "笑不活了")),
        ("无语", ("无语", "服了", "离谱", "恶心", "烦", "骂", "气死", "忍不了")),
        ("困了", ("困", "好累", "累死", "想睡", "熬夜", "撑不住", "打哈欠")),
        ("抱抱", ("难受", "难受死", "委屈", "哭", "emo", "心累", "撑不下去", "想摆")),
        ("加油", ("加油", "冲", "稳了", "搞定", "赢了", "上岸", "过了")),
    )

    def _mood_keyword(self, text: str) -> str | None:
        low = (text or "").lower()
        for mood, words in self.MOOD_WORDS:
            if any(w in low for w in words):
                # 一半概率挂上她偏好的圈层，找出来的图才像她存的
                prefix = random.choice(("邦多利 ", "pjsk ", ""))
                return f"{prefix}{mood} 表情包"
        return None

    def _maybe_drop_web_image(self, chat_id: str, is_group: bool, text: str) -> None:
        """日常聊天里主动甩图：有情绪、且没发太勤的时候才发。"""
        kw = self._mood_keyword(text)
        if not kw:
            return
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        last = float(self.memory.kv_get(f"webimg_ts:{chat_id}") or 0)
        if now - last < 180:  # 同一个人三分钟内只甩一张
            return
        if self.memory.get_counter(f"webimg:{chat_id}", day) >= 10:
            return
        if random.random() > 0.5:
            return
        self.memory.kv_set(f"webimg_ts:{chat_id}", str(now))
        self.memory.bump(f"webimg:{chat_id}", day)
        threading.Thread(target=self._find_and_send_image,
                         args=(chat_id, is_group, kw), daemon=True).start()

    def _find_and_send_image(self, chat_id: str, is_group: bool, keyword: str) -> None:
        """上网找一张应景的图/表情包发出去。"""
        try:
            from . import webimg

            self.log(f"上网找图：「{keyword}」")
            path = webimg.find_image(keyword)
            if not path:
                self.log("没找到合适的图，算了")
                return
            send = getattr(self.adapter, "send_image", None)
            if send is None:
                self.log("这版适配器发不了图")
                return
            with self._send_lock:
                ok = send(chat_id, path, is_group)
            self.log(f"找到图并发出：{os.path.basename(path)} → {ok}")
            if ok is not True:
                return
            self._record_out(chat_id, chat_id, is_group, [f"[找图]{keyword}"])
        except Exception as exc:  # noqa: BLE001
            self.log(f"找图出错：{exc}")

    def _img_key(self) -> str:
        if self.cfg.get("imagegen", {}).get("api_key"):
            return self.cfg["imagegen"]["api_key"]
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "keys.json"),
                      encoding="utf-8") as fh:
                return (json.load(fh).get("siliconflow_api_key") or "").strip()
        except Exception:
            return ""

    # ---------- 朋友圈 ----------
    def _maybe_post_moment(self, now: float) -> None:
        """到点了就自己发条朋友圈：一天最多 per_day 条，只在 window 里，按概率来。"""
        if not self.features.get("allow_moments_post", True):
            return
        cfg = (self.cfg.get("moments") or {})
        if not cfg.get("enabled"):
            return
        lo, hi = (cfg.get("window") or [10, 22])[:2]
        h = policy.hour_of(now)
        if not (lo <= h < hi):
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        key = f"moment:{day}"
        used = int(self.memory.kv_get(key) or 0)
        if used >= int(cfg.get("per_day", 1)):
            return
        if random.random() > float(cfg.get("chance", 0.15)):
            return
        if self._moment_busy:
            return
        self._moment_busy = True
        threading.Thread(target=self._post_moment_worker, args=(key,), daemon=True).start()

    def _post_moment_worker(self, key: str) -> None:
        try:
            try:                      # 这条线程要碰 UIA/COM，先初始化
                import ctypes
                ctypes.windll.ole32.CoInitializeEx(None, 2)
            except Exception:
                pass
            from . import moments
            folder = os.path.join(os.path.dirname(os.path.dirname(__file__)), "stickers", "png")
            picks = []
            if os.path.isdir(folder):
                picks = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".png")]
            if not picks:
                self.log("朋友圈：没有图可发，跳过")
                return
            image = random.choice(picks)
            hint = ""
            try:
                hint = self.brain.brain_moment_hint() if hasattr(self.brain, "brain_moment_hint") else ""
            except Exception:
                hint = ""
            text = ""
            try:
                text = self.brain.moment_caption(hint=hint) or ""
            except Exception as exc:  # noqa: BLE001
                self.log(f"朋友圈配文失败：{exc}")
            self.log(f"朋友圈：准备发 {os.path.basename(image)} 配文「{text}」")
            res = moments.post_moment(image, text, main_hwnd=self.adapter.hwnd if hasattr(self.adapter, "hwnd") else None)
            self.log(f"朋友圈：{res}")
            if res.get("ok"):
                used = int(self.memory.kv_get(key) or 0)
                self.memory.kv_set(key, str(used + 1))
        except Exception as exc:  # noqa: BLE001
            self.log(f"朋友圈异常：{exc}")
        finally:
            self._moment_busy = False

    # ---------- 自己上网逛 ----------

    _BROWSE_BAN = ("爸爸", "妈妈")

    def _maybe_browse(self, now: float) -> None:
        cfg = self.cfg.get("browse") or {}
        if not cfg.get("enabled", True):
            return
        if not (9 <= time.localtime(now).tm_hour < 23):
            return
        last = int(self.memory.kv_get(browse.TS_KEY) or 0)
        if now - last < cfg.get("interval_min", 150) * 60:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if (self.memory.kv_get("browse_day") or "") != day:
            self.memory.kv_set("browse_day", day)
            self.memory.kv_set("browse_count", "0")
        if int(self.memory.kv_get("browse_count") or 0) >= cfg.get("max_per_day", 4):
            return
        if random.random() > cfg.get("chance", 0.7):
            self.memory.kv_set(browse.TS_KEY, int(now))
            return

        limits = self.cfg["limits"]["proactive"]
        cands = []
        for c in self.memory.contacts():
            wxid = str(c.get("wxid") or "")
            if not c.get("proactive_enabled", 1):
                continue
            if c.get("name") in self._BROWSE_BAN:
                continue
            if not (c.get("relation") or c.get("note")):
                continue
            if wxid.endswith("@chatroom"):
                continue
            sent_today = self.memory.get_counter(f"p:{wxid}", day)
            if not policy.can_proactive(contact=c, sent_today=sent_today, now=now, limits=limits):
                continue
            cands.append(c)
        if not cands:
            return
        item = browse.pick(self.memory, now)
        if not item:
            return
        target = random.choice(cands)
        hint = browse.note(item)
        try:
            texts = self.brain.proactive(contact_wxid=target["wxid"],
                                         chat_name=target.get("name") or "",
                                         kind="browse", hint=hint)
        except Exception as exc:  # noqa: BLE001
            self.log(f"逛到东西但没说成：{exc}")
            return
        if not texts:
            return
        sent = self.say(target["wxid"], texts, False)
        if not sent:
            return
        self._record_out(target["wxid"], target.get("name") or "", False, sent, ts=now)
        self.memory.bump(f"p:{target['wxid']}", day)
        self.memory.kv_set("browse_count",
                           str(int(self.memory.kv_get("browse_count") or 0) + 1))
        self.log(f"刷到东西分享给 {target.get('name')}：{hint[:40]}")

    def _tick(self) -> None:
        now = time.time()
        self._deliver_promises(now)
        self._digest_memories(now)
        h = policy.hour_of(now)
        limits = self.cfg["limits"]["proactive"]
        if policy.in_quiet_hours(now, limits.get("quiet_start", 0), limits.get("quiet_end", 7)):
            return
        self._deliver_callbacks(now)
        self._maybe_browse(now)
        self._maybe_post_moment(now)
        if life.roll_chance(now, self.cfg.get("limits", {}).get("free_roam", {}).get("chance", 0.12)):
            self._free_roam(now)
        for contact in self.memory.contacts():
            if not contact.get("proactive_enabled", 1):
                continue
            last = self.memory.last_assistant_ts(contact["wxid"])
            if not last:
                # 还没跟她说过话的人：只给一次开场的机会，19–21 点之间
                if h in (19, 20, 21) and not self.memory.kv_get(
                        f"first_ping:{contact['wxid']}"):
                    if self.proactive_once(contact, "miss"):
                        self.memory.kv_set(f"first_ping:{contact['wxid']}", "1")
                        self.log(f"主动开场 → {contact.get('name')}")
                continue
            idle_days = (now - last) / 86400
            kind = None
            if 1.2 <= idle_days <= 6 and h in (20, 21):
                kind = "miss"
            elif h == 9 and idle_days < 1.2:
                kind = "morning"
            elif h == 22 and idle_days < 1.2:
                kind = "night"
            if kind and self.proactive_once(contact, kind):
                self.log(f"主动消息 → {contact.get('name')} ({kind})")

    def _deliver_callbacks(self, now: float) -> None:
        """隔天补刀：她说过的「[补:xxx]」，到期了补一句。"""
        limits = self.cfg["limits"]["proactive"]
        day = policy.today(now)
        for item in life.due_callbacks(self.memory, now, limit=2):
            wxid = item.get("wxid") or ""
            contact = self.memory.contact(wxid) if wxid else None
            if not contact or not int(contact.get("proactive_enabled") or 0):
                life.pop_callback(self.memory, item)
                continue
            sent_today = self.memory.get_counter(f"p:{wxid}", day)
            if not policy.can_proactive(contact=contact, sent_today=sent_today, now=now,
                                        limits=limits):
                continue
            name = contact.get("name") or item.get("name") or ""
            try:
                parts = self.brain.proactive(
                    contact_wxid=wxid, chat_name=name, kind="callback",
                    hint=item.get("what", ""), now=now,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"补刀失败: {exc}")
                continue
            parts = [p for p in (parts or []) if p.strip()]
            if parts:
                texts, labels = policy.extract_stickers(parts)
                sent = self.say(wxid, texts, False) if texts else []
                if texts and not sent:
                    continue
                if sent:
                    self._record_out(wxid, name, False, sent)
                    self.memory.bump(f"p:{wxid}", day)
                    self.log(f"补刀 → {name}：{item.get('what')}")
                for label in labels[:1]:
                    self.send_sticker(wxid, label, False)
            life.pop_callback(self.memory, item)

    def _free_roam(self, now: float) -> None:
        """自由活动：自己挑个人，主动说点什么。"""
        import random as _r
        cands = []
        for c in self.memory.contacts():
            if not c.get("proactive_enabled", 1):
                continue
            last = self.memory.last_assistant_ts(c["wxid"])
            if last and now - last < 2 * 3600:
                continue
            cands.append(c)
        if not cands:
            return
        c = _r.choice(cands)
        if self.proactive_once(c, "share"):
            self.log(f"自由活动 → {c.get('name')}")

    def stop(self) -> None:
        self._stop.set()

    # ---------- 记忆沉淀 ----------

    def _digest_memories(self, now: float, min_new: int = 6,
                         gap: float = 900) -> None:
        """每隔一阵，把跟某个人最近聊的东西提炼成印象存起来。"""
        for contact in self.memory.contacts():
            wxid = contact["wxid"]
            is_group = wxid.endswith("@chatroom")
            key = f"digest:{wxid}"
            try:
                last = float(self.memory.kv_get(key) or 0)
            except (TypeError, ValueError):
                last = 0.0
            if now - last < gap:
                continue
            if self.memory.count_since(wxid, last) < min_new:
                continue
            self.memory.kv_set(key, str(now))
            name = contact.get("name") or wxid
            threading.Thread(target=self._digest_one,
                             args=(wxid, name, is_group),
                             daemon=True).start()

    # ---------- 学：被纠正就记下来 ----------

    _NEG = ("别", "不要", "不许", "不准", "不准", "甭")
    _TRIM = "了了吧啊呀嘛哦的呗诶 "

    @staticmethod
    def _trim_word(word: str) -> str:
        w = (word or "").strip()
        w = w.strip("「」『』\"'\u201c\u201d\u2018\u2019")
        while w and w[-1] in Runtime._TRIM:
            w = w[:-1]
        return w

    def _learn_correction(self, msg) -> str:
        """用户当场纠正她（叫法 / 喜好 / 别做的事）→ 立刻写进记忆，这一轮就带上。"""
        text = (msg.content or "").strip()
        if not text or len(text) > 120:
            return ""
        # 纯问句就当聊天，不当成「纠正」来学（否则「你是谁」会被记成身份）
        if ("?" in text or "？" in text) and not any(
                k in text for k in ("我叫", "别叫", "不要叫", "不许叫", "叫我")):
            return ""
        scope = f"chat:{msg.chat_id}" if msg.is_group else f"contact:{msg.sender_id}"
        found: list[tuple[str, str]] = []

        _JUNK = ("我", "你", "他", "她", "这个", "那个", "谁", "啥", "什么", "哪",
                 "怎么", "为什么", "吗", "呢", "吧", "啊", "呀", "哪个", "几点")

        def add(key: str, value: str) -> None:
            value = Runtime._trim_word(value)
            if not value or len(value) > 14:
                return
            if value in _JUNK:
                return
            # 「你是谁」「什么名字」「哪来的」这种是问句，不是在自我介绍
            if any(w in value for w in ("谁", "啥", "什么", "哪", "怎么", "为什么")):
                return
            item = (key, value[:120])
            if item not in found:
                found.append(item)

        # 1) 叫我 / 别叫我 —— 一个正则同时抓，前面带否定词就算「别叫」
        for m in re.finditer(r"(别|不要|不许|不准)?\s*叫我?([^\s，。,.!！?？「」\"'\u201c\u201d]{1,14})", text):
            neg, word = m.group(1), m.group(2)
            if word.startswith("的"):  # 「叫我的名字」这种
                continue
            if neg:
                add("雷区", f"别叫TA：{word}")
            else:
                add("称呼", f"叫TA：{word}")
        # 2) 我叫X
        for m in re.finditer(r"我叫([^\s，。,.!！?？「」\"'\u201c\u201d]{1,14})", text):
            add("称呼", f"TA叫：{m.group(1)}")
        # 3) 不喜欢 / 喜欢
        for m in re.finditer(r"我(?:不太|不|不咋|不怎么)?喜欢([^\s，。,.!！?？「」\"'\u201c\u201d]{1,14})", text):
            head = text[max(0, m.start()):m.start(1)]
            if "不" in head:
                add("雷区", f"TA不喜欢：{m.group(1)}")
            else:
                add("喜好", f"TA喜欢：{m.group(1)}")
        # 4) 别XX（别发、别问、别说…）
        for m in re.finditer(r"别(?:再|老是|总)?([^\s，。,.!！?？「」\"'\u201c\u201d]{1,14})", text):
            word = m.group(1)
            if word.startswith(("叫", "的")) or word in ("人",):
                continue
            add("雷区", f"别做的事：{word}")
        # 5) 称呼/身份：我是X的Y
        for m in re.finditer(r"我是([^\s，。,.!！?？「」\"'\u201c\u201d]{1,16})", text):
            add("身份", f"TA自称：{m.group(1)}")

        if not found:
            return ""
        for key, value in found:
            self.memory.set_fact(scope, key, value)
        brief = "；".join(f"{k}:{v}" for k, v in found)
        self.log(f"记住纠正：{brief}")
        return brief

    # ---------- 约定到期：自己提出来 ----------

    def _deliver_promises(self, now: float) -> None:
        limits = self.cfg["limits"]["proactive"]
        day = policy.today(now)
        for item in life.due_promises(self.memory, now):
            wxid = item.get("wxid") or ""
            chat_id = item.get("chat_id") or wxid
            name = item.get("name") or chat_id
            is_group = str(chat_id).endswith("@chatroom")
            if wxid:
                contact = self.memory.contact(wxid)
                if not contact or not int(contact.get("proactive_enabled") or 0):
                    self.log(f"约定不发（未允许主动）：{name}")
                    life.pop_promise(self.memory, item)
                    continue
                sent_today = self.memory.get_counter(f"p:{wxid}", day)
                if not policy.can_proactive(contact=contact, sent_today=sent_today, now=now,
                                            limits=limits):
                    continue
            elif policy.in_quiet_hours(now, limits.get("quiet_start", 23), limits.get("quiet_end", 9)):
                continue
            try:
                texts = self.brain.proactive(
                    contact_wxid=wxid or chat_id, chat_name=name,
                    kind="promise", hint=item.get("what") or "", now=now,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"提约定失败（{name}）：{exc}")
                continue
            if not texts:
                life.pop_promise(self.memory, item)
                continue
            sent = self.say(chat_id, texts, is_group)
            if not sent:
                continue
            self._record_out(chat_id, name, is_group, sent)
            if wxid and not is_group:
                self.memory.bump(f"p:{wxid}", day)
            self.log(f"提约定：{name} ← {item.get('what')}")
            life.pop_promise(self.memory, item)

    def _digest_one(self, wxid: str, name: str, is_group: bool = False) -> None:
        try:
            n = self.brain.remember(chat_id=wxid, chat_name=name,
                                    is_group=is_group,
                                    contact_wxid=None if is_group else wxid)
            if n:
                self.log(f"更新印象：{name}（{n} 条）")
                made = life.register_promises_from_facts(
                    self.memory, wxid=wxid, name=name, chat_id=wxid)
                if made:
                    self.log(f"记下约定：{name}（{made} 条）")
        except Exception as exc:  # noqa: BLE001
            self.log(f"记忆整理失败（{name}）：{exc}")
