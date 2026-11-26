"""入口。

  python -m app.main --mock              终端里调人设，不连微信
  python -m app.main --wcf --admin       接 WeChatFerry（微信 3.9.12.17）
  python -m app.main --wxhelper --probe  探测 wxhelper 接口
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# wechatauto 自带「拟人节流」（默认 120 秒内最多写 6 次，超过要冷却 30–75 秒）。
# 这会让回复排长队，按用户要求关掉冷却：切 fast 档 + 不限突发次数。
os.environ.setdefault("WECHATAUTO_RHYTHM", "fast")
os.environ.setdefault("WECHATAUTO_WRITE_BURST", "0")

from app import logbus  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.model import LLM, ModelError  # noqa: E402
from app.persona import Persona  # noqa: E402
from app.runtime import Runtime  # noqa: E402
from app.stickers import StickerBook  # noqa: E402

CONFIG = os.path.join(BASE, "config.json")
PERSONA = os.path.join(BASE, "persona.md")
KEYS = os.path.join(BASE, "keys.json")
DB = os.path.join(BASE, "data", "xiaohan.db")
LOCK = os.path.join(BASE, "data", "xiaohan.lock")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_pid(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            return int((fh.read() or "0").strip())
    except (OSError, ValueError):
        return 0


def acquire_lock(path: str | None = None) -> bool:
    """单实例：已经在跑了就别再起一个，两个进程抢键鼠会打架。

    用 O_EXCL 创建锁文件，避免两个进程同时看到「没有锁」然后一起启动。
    """
    path = path or LOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_lock_pid(path)
            if pid == os.getpid():
                return True
            if pid and _pid_alive(pid):
                return False
            try:
                os.remove(path)
            except OSError:
                return False
            continue
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    return False


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def load_key(cfg: dict) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key and os.path.exists(KEYS):
        with open(KEYS, encoding="utf-8") as fh:
            key = (json.load(fh) or {}).get("deepseek_api_key", "")
    if not key:
        raise ModelError(
            "没有找到 API key。二选一：\n"
            f"  1) 在 {KEYS} 里写 {{\"deepseek_api_key\": \"sk-...\"}}\n"
            "  2) 设环境变量 DEEPSEEK_API_KEY"
        )
    return key.strip()


def build(cfg: dict, memory: Memory, stickers=None):
    persona = Persona(PERSONA)
    llm = LLM(load_key(cfg), base_url=cfg["model"]["base_url"],
              model=cfg["model"]["model"])
    return Brain(cfg, memory, persona, llm, stickers=stickers)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="终端调试模式，不连微信")
    ap.add_argument("--wcf", action="store_true", help="WeChatFerry 接入")
    ap.add_argument("--uia", action="store_true", help="UI 自动化接入（微信 3.9 用）")
    ap.add_argument("--wx4", action="store_true", help="微信 4.x 接入（读库收消息 + GUI 发消息）")
    ap.add_argument("--wxhelper", action="store_true", help="wxhelper 接入")
    ap.add_argument("--probe", action="store_true", help="探测 wxhelper 接口")
    ap.add_argument("--no-admin", action="store_true", help="不启动管理页")
    args = ap.parse_args(argv)

    cfg = load_config()
    memory = Memory(DB)
    stickers = StickerBook()
    logbus.log(f"表情包已加载：{len(stickers.items)} 张")

    if args.probe:
        from app.adapters.wxhelper import WxHelperAdapter

        adapter = WxHelperAdapter()
        print(json.dumps(adapter.probe(), ensure_ascii=False, indent=2))
        return 0

    try:
        brain = build(cfg, memory, stickers)
    except ModelError as exc:
        print(f"[x] {exc}")
        return 2

    if args.mock:
        from app.adapters.mock import run_mock

        run_mock(brain, memory)
        return 0

    # ---------- 真实接入 ----------

    if not (args.wcf or args.wxhelper or args.uia or args.wx4):
        print("选一个模式：--mock / --wx4 / --uia / --wcf / --wxhelper")
        return 1

    if not acquire_lock():
        print("[x] 已经有一个小涵在跑了，先把它关掉再启动。")
        return 3

    if args.wx4:
        from app.adapters.wx4 import Wx4Adapter

        wx4_cfg = cfg.get("wx4", {})
        logbus.log("连接微信 4.x（收消息读库，发消息走界面）…")
        adapter = Wx4Adapter(poll_interval=wx4_cfg.get("poll_interval", 1.0))
        logbus.log(f"我是：{adapter.self_name}（{adapter.self_wxid}）")
    elif args.uia:
        from app.adapters.uia import UiaAdapter

        uia_cfg = cfg.get("uia", {})
        logbus.log("连接微信窗口（UI 自动化，不注入）…")
        adapter = UiaAdapter(
            self_name=uia_cfg.get("self_name", "小涵"),
            poll_interval=uia_cfg.get("poll_interval", 2.0),
            chat_whitelist=uia_cfg.get("chat_whitelist", []),
        )
    elif args.wcf:
        from app.adapters.wcf import WcfAdapter

        logbus.log("连接 WeChatFerry…（需要微信 3.9.12.17 已登录）")
        adapter = WcfAdapter(self_name="具小涵")
    else:
        from app.adapters.wxhelper import WxHelperAdapter

        adapter = WxHelperAdapter()
        adapter.reload_contacts()
        logbus.log("连接 wxhelper…")

    runtime = Runtime(cfg, memory, brain, adapter, log=logbus.log)
    logbus.log(f"已上线：{adapter.name}")

    if not args.no_admin:
        from app.admin import AdminServer

        admin = AdminServer(
            cfg=cfg, memory=memory, persona_path=PERSONA,
            brain_factory=lambda: build(cfg, memory, stickers),
            host=cfg["admin"]["host"], port=cfg["admin"]["port"],
        )
        url = admin.start()
        logbus.log(f"管理页：{url}")

    runtime.start_scheduler()
    logbus.log("调度器已启动（主动消息）")

    try:
        adapter.recv_loop(runtime.handle, should_stop=lambda: False)
    except KeyboardInterrupt:
        logbus.log("收到中断，退出")
    finally:
        runtime.stop()
        adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
