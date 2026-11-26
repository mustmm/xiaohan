"""SQLite 记忆层：聊天记录、联系人档案、长期事实、计数。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  chat_name TEXT,
  is_group INTEGER DEFAULT 0,
  sender_id TEXT,
  sender_name TEXT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, ts);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  k TEXT NOT NULL,
  v TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(scope, k)
);

CREATE TABLE IF NOT EXISTS contacts (
  wxid TEXT PRIMARY KEY,
  name TEXT,
  relation TEXT,
  note TEXT,
  proactive_limit INTEGER DEFAULT 1,
  proactive_enabled INTEGER DEFAULT 1,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS counters (
  k TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


class Memory:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---------- 消息 ----------

    def add_message(self, chat_id: str, chat_name: str, is_group: bool,
                    sender_id: str, sender_name: str, role: str, content: str,
                    ts: float | None = None) -> None:
        ts = int(ts or time.time())
        with self._lock:
            self._db.execute(
                "INSERT INTO messages(chat_id,chat_name,is_group,sender_id,sender_name,role,content,ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (chat_id, chat_name, int(is_group), sender_id, sender_name, role, content, ts),
            )
            self._db.commit()

    def recent(self, chat_id: str, limit: int = 24) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)"
                " ORDER BY id ASC",
                (chat_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_assistant_ts(self, chat_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT ts FROM messages WHERE chat_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return int(row["ts"]) if row else 0

    def last_self_ts(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT ts FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return int(row["ts"]) if row else 0

    def count_since(self, chat_id: str, since_ts: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND ts>=? AND role='user'",
                (chat_id, int(since_ts)),
            ).fetchone()
        return int(row["n"])

    def last_user_ts(self, chat_id: str) -> float:
        """这个会话里对方最后一条消息的时间（用来判断是不是还有下文）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(ts) AS m FROM messages WHERE chat_id=? AND role='user'",
                (chat_id,)).fetchone()
        return float(row["m"] or 0)

    def sender_of_last_reply(self, chat_id: str) -> str:
        """她最近一次回复的是谁（用于判断是不是同一个人接着聊）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT ts FROM messages WHERE chat_id=? AND role='assistant' "
                "ORDER BY ts DESC, id DESC LIMIT 1", (chat_id,)).fetchone()
            if not row:
                return ""
            row2 = self._db.execute(
                "SELECT sender_name FROM messages WHERE chat_id=? AND role='user' "
                "AND ts<=? ORDER BY ts DESC, id DESC LIMIT 1",
                (chat_id, row["ts"])).fetchone()
        return (row2["sender_name"] or "") if row2 else ""

    def chat_stats(self, chat_id: str) -> tuple[int, int | None]:
        """返回（这个会话里对方说了多少句，第一次说话的时间戳）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS first_ts FROM messages "
                "WHERE chat_id=? AND role='user'",
                (chat_id,),
            ).fetchone()
        return int(row["n"] or 0), (int(row["first_ts"]) if row["first_ts"] else None)

    # ---------- 事实 ----------

    def set_fact(self, scope: str, k: str, v: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO facts(scope,k,v,updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(scope,k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (scope, k, v, int(time.time())),
            )
            self._db.commit()

    def facts(self, scopes: list[str]) -> list[dict]:
        if not scopes:
            return []
        marks = ",".join("?" * len(scopes))
        with self._lock:
            rows = self._db.execute(
                f"SELECT scope,k,v FROM facts WHERE scope IN ({marks}) ORDER BY updated_at DESC LIMIT 40",
                scopes,
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 联系人 ----------

    def upsert_contact(self, wxid: str, name: str = "", relation: str = "",
                       note: str = "", proactive_limit: int | None = None,
                       proactive_enabled: int | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO contacts(wxid,name,relation,note,proactive_limit,proactive_enabled,updated_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(wxid) DO UPDATE SET"
                " name=COALESCE(NULLIF(excluded.name,''),name),"
                " relation=COALESCE(NULLIF(excluded.relation,''),relation),"
                " note=COALESCE(NULLIF(excluded.note,''),note),"
                " proactive_limit=COALESCE(?,proactive_limit),"
                " proactive_enabled=COALESCE(?,proactive_enabled),"
                " updated_at=excluded.updated_at",
                (wxid, name, relation, note,
                 proactive_limit if proactive_limit is not None else 1,
                 proactive_enabled if proactive_enabled is not None else 1,
                 int(time.time()),
                 proactive_limit, proactive_enabled),
            )
            self._db.commit()

    def ensure_contact(self, wxid: str, name: str = "") -> dict | None:
        """见过就返回，没见过就建档——新朋友一律「被动回复」，不主动发。

        如果这个人以前是用「名字」当 wxid 建过占位档（早期版本干过这事），
        现在拿到真 wxid 了，就把占位档和它名下的记忆一起搬过来，
        免得她「明明记得郑博，见到真郑博却不认识」。
        """
        existing = self.contact(wxid)
        if existing:
            # 微信里改了备注：真档还在，但名字对不上了，顺手把名字更新掉；
            # 如果旧的「占位档」正好叫这个新名字，也一并收编。
            display = (name or "").strip()
            if display and display != existing.get("name") and display != wxid:
                self._adopt_placeholder(wxid, display)
                self.upsert_contact(wxid, name=display,
                                    relation=existing.get("relation") or "",
                                    note=existing.get("note") or "")
                return self.contact(wxid)
            return existing
        display = (name or "").strip() or wxid
        self._adopt_placeholder(wxid, display)
        self.upsert_contact(wxid, name=display, proactive_enabled=0)
        return self.contact(wxid)

    def _adopt_placeholder(self, wxid: str, name: str) -> None:
        if not name or name == wxid:
            return
        placeholder = self.contact(name)
        if not placeholder:
            return
        fields = {k: placeholder.get(k) for k in ("relation", "note",
                                                  "proactive_limit", "proactive_enabled")}
        with self._lock:
            self._db.execute("DELETE FROM contacts WHERE wxid = ?", (name,))
            self._db.execute("UPDATE facts SET scope = ? WHERE scope = ?",
                             (f"contact:{wxid}", f"contact:{name}"))
            self._db.commit()
        if fields.get("relation") or fields.get("note"):
            self.upsert_contact(
                wxid, name=name, relation=fields.get("relation") or "",
                note=fields.get("note") or "",
                proactive_limit=fields.get("proactive_limit"),
                proactive_enabled=fields.get("proactive_enabled"),
            )

    def contact(self, wxid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM contacts WHERE wxid=?", (wxid,)).fetchone()
        return dict(row) if row else None

    def contacts(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM contacts ORDER BY proactive_enabled DESC, updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 计数（按天） ----------

    def bump(self, k: str, day: str, n: int = 1) -> int:
        with self._lock:
            row = self._db.execute("SELECT day, n FROM counters WHERE k=?", (k,)).fetchone()
            if row is None:
                self._db.execute("INSERT INTO counters(k,day,n) VALUES(?,?,?)", (k, day, n))
                total = n
            elif row["day"] != day:
                self._db.execute("UPDATE counters SET day=?, n=? WHERE k=?", (day, n, k))
                total = n
            else:
                total = int(row["n"]) + n
                self._db.execute("UPDATE counters SET n=? WHERE k=?", (total, k))
            self._db.commit()
        return total

    def get_counter(self, k: str, day: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT day,n FROM counters WHERE k=?", (k,)).fetchone()
        if not row or row["day"] != day:
            return 0
        return int(row["n"])

    # ---------- 杂项 ----------

    def kv_get(self, k: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self._db.commit()
