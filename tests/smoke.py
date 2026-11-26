"""离线冒烟测试：不连微信、不调真实模型，验证链路能跑通。

  python tests/smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from app import policy  # noqa: E402
from app.adapters.base import Incoming  # noqa: E402
from app.adapters.mock import MockAdapter  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.persona import Persona  # noqa: E402
from app.runtime import Runtime  # noqa: E402


class FakeLLM:
    """假装是 DeepSeek：按最后一条消息回不同内容，用来验证分条。"""

    def chat(self, messages, temperature=1.1, max_tokens=800):
        last = messages[-1]["content"]
        if "SKIP" in last:
            return "SKIP"
        if "画" in last:
            return "诶你看到了 ||| 我今天画完一张，手还是抖的"
        return "哥你回来啦"


def main() -> int:
    memory = Memory(os.path.join(BASE, "data", "smoke.db"))
    brain = Brain(
        cfg={
            "model": {"temperature": 1.0, "max_tokens": 300},
            "limits": {"bubbles": {"max": 3}, "typing": {}, "group_insert": {},
                       "proactive": {"per_friend_per_day": 1}},
            "features": {},
        },
        memory=memory,
        persona=Persona(os.path.join(BASE, "persona.md")),
        llm=FakeLLM(),
    )
    memory.kv_set("hot_words", json.dumps(["离线"], ensure_ascii=False))
    memory.kv_set("hot_ts", str(time.time()))
    runtime = Runtime(
        cfg=brain.cfg,
        memory=memory,
        brain=brain,
        adapter=MockAdapter(),
        log=print,
    )

    print("--- 私聊 ---")
    runtime.handle(Incoming(chat_id="smoke:p", chat_name="哥", is_group=False,
                            sender_id="wxid_ge", sender_name="哥", content="我回来了"))

    print("--- 私聊（带画）---")
    runtime.handle(Incoming(chat_id="smoke:p", chat_name="哥", is_group=False,
                            sender_id="wxid_ge", sender_name="哥", content="你画完了吗"))

    print("--- 分条逻辑 ---")
    parts = policy.split_bubbles("诶你看到了 ||| 我今天画完一张，手还是抖的")
    assert len(parts) == 2, parts
    print(parts)

    print("--- 群聊静默判断（消息太少应沉默）---")
    ok = policy.group_should_insert(
        is_mentioned=False, new_messages=2, last_self_ts=0,
        inserted_this_hour=0, inserted_today=0, now=1e12 + 12 * 3600,
        limits={"per_hour_per_group": 3, "per_day_per_group": 15,
                "min_new_messages": 6, "cooldown_sec": 420},
    )
    assert ok is False
    print("OK：消息不够时不插话")

    print("\n--- 安全与正确性 ---")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import safety
    safety.run()

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
