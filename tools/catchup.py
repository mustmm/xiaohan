# -*- coding: utf-8 -*-
"""把一个会话里“没回过的最后一条消息”补答一次。

用法：python -X utf8 tools/catchup.py 王洪涛 [--minutes 360]
只补最近 N 分钟内的，太久以前的（昨晚、凌晨）不去硬接，免得像诈尸。
"""
import argparse
import json
import sqlite3
import sys
import time

sys.path.insert(0, ".")

from app.adapters.base import Incoming
from app.brain import Brain
from app.memory import Memory
from app.model import LLM
from app.persona import Persona
from app.runtime import Runtime


def model_name(cfg: dict) -> str:
    """config.json 里的字段是 model，不是 name。"""
    block = cfg.get("model") or {}
    return block.get("model") or block.get("name") or "deepseek-chat"


def find_latest_user(db, name: str):
    """按会话名找最后一条对方消息。先精确匹配，避免「哥」补答到「哥哥」。"""
    name = (name or "").strip()
    if not name:
        return None
    sql = (
        "select chat_id, chat_name, is_group, sender_id, sender_name, content, ts "
        "from messages where chat_name = ? and role = 'user' order by id desc limit 1"
    )
    row = db.execute(sql, (name,)).fetchone()
    if row:
        return row
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_sql = (
        "select chat_id, chat_name, is_group, sender_id, sender_name, content, ts "
        "from messages where chat_name like ? escape '\\' and role = 'user' "
        "order by id desc limit 1"
    )
    return db.execute(like_sql, (f"%{escaped}%",)).fetchone()


def build():
    from app.adapters.wx4 import Wx4Adapter
    from app.stickers import StickerBook

    cfg = json.load(open("config.json", encoding="utf-8"))
    keys = json.load(open("keys.json", encoding="utf-8"))
    mem = Memory(cfg.get("db_path", "data/xiaohan.db"))
    llm = LLM(keys["deepseek_api_key"],
              base_url=cfg["model"]["base_url"],
              model=model_name(cfg))
    brain = Brain(cfg, mem, Persona("persona.md"), llm, StickerBook())
    adapter = Wx4Adapter(poll_interval=cfg.get("wx4", {}).get("poll_interval", 1.0))
    rt = Runtime(cfg, mem, brain, adapter, log=lambda m: print("[catchup]", m, flush=True))
    return cfg, mem, adapter, rt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--minutes", type=float, default=360)
    args = ap.parse_args()

    cfg, mem, adapter, rt = build()
    db = sqlite3.connect(cfg.get("db_path", "data/xiaohan.db"))
    row = find_latest_user(db, args.name)
    if not row:
        print("数据库里没找到这个人的未回消息", flush=True)
        return 1
    chat_id, chat_name, is_group, sender_id, sender_name, content, ts = row
    age_min = (time.time() - ts) / 60.0
    print(f"最后一条未回：{chat_name}｜{time.strftime('%m-%d %H:%M', time.localtime(ts))}"
          f"｜{age_min:.0f} 分钟前｜{content[:40]}", flush=True)
    if age_min > args.minutes:
        print(f"超过 {args.minutes:.0f} 分钟，不硬接（要接就加 --minutes）", flush=True)
        return 2
    msg = Incoming(chat_id=chat_id, chat_name=chat_name, is_group=bool(is_group),
                   sender_id=sender_id or "", sender_name=sender_name or chat_name,
                   content=content or "", ts=ts)
    rt.handle(msg)
    print("补答完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
