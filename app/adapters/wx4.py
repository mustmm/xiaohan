"""微信 4.x 接入：收消息读本地数据库（只读，不碰界面），发消息走 GUI 自动化。

为什么这么分工：
- 4.x 的 UIA 树默认是空的，只能靠 wechatauto 那套「热激活 + 数据库」；
- 收消息走 db.Listener 轮询，纯读文件，不移动鼠标、不抢焦点、不动剪贴板；
- 发消息没法绕开界面（微信没有开放发送接口），才用 SendMsg。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import time
from ctypes import wintypes as w
from pathlib import Path

from app.adapters.base import Adapter, Incoming
from app.wxtext import chat_matches, parse_poke, redact_special, safe_token, strip_group_prefix

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_u32 = ctypes.WinDLL("user32", use_last_error=True)

_EnumProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

# 微信 4.x 主窗口的类名前缀（Qt 升级后可能变成 Qt6xxx，所以只用前缀判断）
WX_MAIN_CLASS_PREFIX = "Qt51514QWindowIcon"


def _weixin_pids() -> set:
    """Weixin.exe 的所有进程号。用进程名认窗口，避免按标题误认别的程序。"""
    try:
        import psutil
    except Exception:
        return set()
    pids = set()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in ("weixin.exe", "weixin"):
                pids.add(p.pid)
        except Exception:
            continue
    return pids


def _find_wechat_hwnd() -> int:
    """只认 Weixin.exe 进程里标题是「微信」的主窗口。

    4.x 可能同时有很多 Qt 窗口（小窗、托盘、登录框），按标题+面积挑最大的那个，
    最小化的也要算进来（后面会把它 restore 出来）。
    """
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    pids = _weixin_pids()
    if not pids:
        return 0

    best = (0, 0)  # (area, hwnd)
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _):
        nonlocal best
        try:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            cls = ctypes.create_unicode_buffer(128)
            u.GetClassNameW(hwnd, cls, 128)
            if not cls.value.startswith("Qt5151"):
                return True
            title = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(hwnd, title, 64)
            if title.value not in ("微信", "Weixin"):
                return True
            r = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            w = r.right - r.left
            h = r.bottom - r.top
            if w <= 0 or h <= 0:
                # 最小化的窗口 rect 会是负坐标，用 placement 里的还原尺寸
                pl = wintypes.WINDOWPLACEMENT()
                pl.length = ctypes.sizeof(pl)
                if u.GetWindowPlacement(hwnd, ctypes.byref(pl)):
                    rc = pl.rcNormalPosition
                    w, h = rc.right - rc.left, rc.bottom - rc.top
            area = w * h
            if title.value == "微信":
                area += 10 ** 9  # 主窗口优先
            if area > best[0]:
                best = (area, hwnd)
        except Exception:
            pass
        return True

    u.EnumWindows(CB(_cb), 0)
    return best[1]


def _show_window(hwnd: int) -> None:
    """把微信主窗口弄到可见、可点的状态。隐藏/最小化时点击会落到别的窗口上。"""
    if not hwnd:
        return
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, 9)      # SW_RESTORE
    if not _u32.IsWindowVisible(hwnd):
        _u32.ShowWindow(hwnd, 5)      # SW_SHOW
    _u32.SetForegroundWindow(hwnd)
    time.sleep(0.35)

# 微信 4.x 的库把消息类型给成中文，这里统一翻译一遍
TYPE_ALIAS = {
    "文本": "text",
    "图片": "image",
    "动画表情": "emoji",
    "表情": "emoji",
    "语音": "voice",
    "视频": "video",
    "文件/链接/卡片": "link",
    "音视频通话": "voip",
    "引用": "quote",
    "系统消息": "system",
    "位置": "location",
    "红包": "hongbao",
}

# 微信里这几类消息留给上层决定怎么处理，先统一成占位文字
TYPE_PLACEHOLDER = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "emoji": "[表情]",
    "file": "[文件]",
    "link": "[链接]",
    "card": "[名片]",
    "quote": "[引用]",
    "voip": "[通话]",
}


class Wx4Adapter(Adapter):
    name = "wx4"

    def __init__(self, poll_interval: float = 1.0):
        from wechatauto.db import WeChatDB

        self.poll_interval = poll_interval
        self.db = WeChatDB()
        info = self.db.get_self_info() or {}
        self.self_wxid = info.get("username") or ""
        self.self_name = info.get("nick_name") or ""
        self._wx = None
        self._names: dict[str, str] = {}
        self._last_poke: dict[str, float] = {}
        self._media = None          # MediaDownloader（懒加载）
        self._vision_key = None
        self._vision_off = False
        self._seen_chat: dict[str, int] = {}   # 每个会话见过的最新 local_id（去重用）
        self._seen_recent: set[str] = set()   # 兜底：不按 local_id 顺序时也不重复
        self._seen_file = Path("data/wx4_seen.json")
        try:
            if self._seen_file.exists():
                self._seen_chat = {k: int(v) for k, v in json.loads(
                    self._seen_file.read_text(encoding="utf-8")).items()}
        except Exception:
            self._seen_chat = {}

    # ---------- 发送 ----------

    def _client(self):
        if self._wx is None:
            from wechatauto import WeChat
            from wechatauto.guia import WeChatGUI

            hwnd = _find_wechat_hwnd()
            if not hwnd:
                raise RuntimeError("找不到微信主窗口（Weixin.exe 没在跑？）")
            _show_window(hwnd)
            gui = WeChatGUI(hwnd=hwnd)   # 显式指定窗口，不走库的标题打分
            self._wx = WeChat(gui=gui)
        else:
            self._restore_window()
        return self._wx

    def _restore_window(self) -> None:
        """发消息前确保主窗口可见可点，否则点击落点会落到别的窗口上。"""
        try:
            hwnd = getattr(self._wx._gui, "main_hwnd", 0) or 0
            _show_window(hwnd)
        except Exception:
            pass

    def display_name(self, username: str) -> str:
        """会话 username（wxid / xxx@chatroom / filehelper）→ 界面显示名。"""
        if not username:
            return ""
        cached = self._names.get(username)
        if cached:
            return cached
        name = username
        try:
            if username == "filehelper":
                name = "文件传输助手"
            elif username.endswith("@chatroom"):
                name = self.db.group_id_to_name(username) or username
            else:
                name = self.db.get_nickname(username) or username
        except Exception:
            name = username
        if name:
            self._names[username] = name
        return name

    def _confirm_chat(self, wx, name: str, attempts: int = 2):
        """切到 name，并且当前会话名一致才把客户端交回去。

        SendMsg / SendFiles 不带 who 时会发进当前打开的会话。对不上就返回 None。
        """
        name = (name or "").strip()
        if not name:
            return None
        for _ in range(attempts):
            try:
                current = (wx.ChatInfo() or {}).get("chat_name") or ""
            except Exception:
                current = ""
            if chat_matches(current, name):
                return wx
            try:
                wx.ChatWith(name)
                current = (wx.ChatInfo() or {}).get("chat_name") or ""
            except Exception:
                current = ""
            if chat_matches(current, name):
                return wx
            time.sleep(0.6)
            self._restore_window()
            try:
                wx = self._client()
            except Exception:
                return None
        return None

    def send_text(self, chat_id: str, text: str, is_group: bool) -> bool:
        if not text or not text.strip():
            return False
        name = self.display_name(chat_id)
        # 抢焦点之前先把用户的前台窗口记下来，_client() 内部会强制把微信拉前
        prev_fg = _focus_guard()
        try:
            wx = self._client()
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 拿不到微信客户端：{exc}", flush=True)
            self._wx = None
            return False
        wx_hwnd = getattr(wx._gui, "main_hwnd", 0) or 0
        if prev_fg in (0, wx_hwnd):
            prev_fg = _focus_guard() if prev_fg == 0 else 0
        _wait_user_pause()
        try:
            if not self._chat_exists(chat_id, name):
                print(f"[wx4] 跳过：微信里没有这个人/群（{name}）", flush=True)
                return False
            # 先切会话，再核对当前会话确实是目标，最后不带 who 发送
            # ——SendMsg 带 who 失败时会退回"当前打开的会话"，那样会发错人
            wx = self._confirm_chat(wx, name)
            if wx is None:
                print(f"[wx4] 会话没切过去（要 {name}），跳过", flush=True)
                return False
            res = wx.SendMsg(text)
            if not _ok(res):
                print(f"[wx4] 发送失败：{res!r}", flush=True)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 发送异常：{exc}", flush=True)
            return False
        finally:
            self._restore_blockers()
            _restore_focus(prev_fg, wx_hwnd)

    def _restore_blockers(self) -> None:
        """把刚才为了点得准而最小化的窗口还回去，别留在最小化状态。"""
        try:
            gui = getattr(self._wx, "_gui", None)
            if gui is not None:
                gui.restore_blockers()
        except Exception:
            pass

    def _chat_exists(self, chat_id: str, name: str) -> bool:
        if chat_id == "filehelper":
            return True
        try:
            for s in self.db.get_sessions(limit=500):
                if s.get("username") == chat_id:
                    return True
        except Exception:
            pass
        try:
            return any(h.get("username") == chat_id for h in (self.db.search_contact(name) or []))
        except Exception:
            return False

    def send_image(self, chat_id: str, path: str, is_group: bool = False) -> bool:
        """发一张图（自拍/她画的画）。"""
        name = self.display_name(chat_id)
        prev_fg = _focus_guard()
        try:
            wx = self._client()
            _wait_user_pause()
            if not self._chat_exists(chat_id, name):
                print(f"[wx4] 跳过（没有会话）：{name}", flush=True)
                return False
            # 和文字一样：没核对到目标会话就不要发，否则图会进当前打开的聊天。
            wx = self._confirm_chat(wx, name)
            if wx is None:
                print(f"[wx4] 会话没切过去（要 {name}），图片不发", flush=True)
                return False
            res = wx.SendFiles(path, who=None)
            ok = bool(getattr(res, "is_success", False)) if res is not None else False
            print(f"[wx4] 发图 → {name}: {getattr(res, 'message', res)}", flush=True)
            return ok
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 发图失败：{exc}", flush=True)
            return False
        finally:
            _restore_focus(prev_fg, _find_wechat_hwnd())

    def send_poke(self, chat_id: str) -> bool:
        """拍一拍对方（被拍回来的时候用）。"""
        name = self.display_name(chat_id)
        try:
            wx = self._client()
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 拍一拍失败：{exc}", flush=True)
            return False
        prev_fg = _focus_guard()
        try:
            if not self._chat_exists(chat_id, name):
                return False
            wx = self._confirm_chat(wx, name, attempts=1)
            if wx is None:
                return False
            res = wx.Poke()
            ok = bool(getattr(res, "is_success", False)) if res is not None else False
            print(f"[wx4] 拍回去 → {name}: {getattr(res, 'message', res)}", flush=True)
            return ok
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 拍一拍失败：{exc}", flush=True)
            return False
        finally:
            _restore_focus(prev_fg, _find_wechat_hwnd())

    def send_sticker(self, chat_id: str, bmp_path: str) -> bool:
        """表情包：微信 4.x 可以按图片直接发。"""
        name = self.display_name(chat_id)
        try:
            wx = self._client()
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 拿不到微信客户端：{exc}", flush=True)
            self._wx = None
            return False
        try:
            if not self._chat_exists(chat_id, name):
                print(f"[wx4] 跳过表情：微信里没有（{name}）", flush=True)
                return False
            wx = self._confirm_chat(wx, name, attempts=1)
            if wx is None:
                print(f"[wx4] 表情会话没切过去（要 {name}），跳过", flush=True)
                return False
            res = wx.SendFiles(bmp_path)
            if not _ok(res):
                print(f"[wx4] 发图失败：{res!r}", flush=True)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 发图异常：{exc}", flush=True)
            return False
        finally:
            self._restore_blockers()

    def is_friend(self, wxid: str) -> bool:
        return True

    # ---------- 识图 ----------

    def _vision_key_of(self) -> str:
        if self._vision_key is not None:
            return self._vision_key
        key = ""
        try:
            import json as _json
            key_path = os.path.join(_ROOT, "keys.json")
            with open(key_path, encoding="utf-8") as fh:
                key = (_json.load(fh) or {}).get("siliconflow_api_key") or ""
        except Exception:
            key = ""
        self._vision_key = key
        return key

    def see_emoji(self, content: str) -> str:
        """表情包：4.x 不落本地，但消息 XML 里有 cdnurl/thumburl，直接下下来看。"""
        if self._vision_off or not self._vision_key_of():
            return ""
        import html
        import re as _re

        url = ""
        for attr in ("cdnurl", "thumburl"):
            m = _re.search(rf'{attr}\s*=\s*"([^"]+)"', content or "")
            if m:
                url = html.unescape(m.group(1))
                break
        if not url:
            return ""
        md5 = ""
        m = _re.search(r'md5\s*=\s*"([^"]+)"', content or "")
        if m:
            md5 = m.group(1)
        dest_dir = os.path.join(_ROOT, "data", "incoming")
        os.makedirs(dest_dir, exist_ok=True)
        token = safe_token(md5) or "x"
        raw = os.path.join(dest_dir, f"emoji_{token}.gif")
        root = os.path.realpath(dest_dir)
        full = os.path.realpath(raw)
        if os.path.commonpath([root, full]) != root:
            return ""
        try:
            from app import safehttp

            data = safehttp.read_bytes(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://wx.qq.com/"},
                timeout=20,
                max_bytes=8 * 1024 * 1024,
            )
            if len(data) < 200:
                return ""
            with open(full, "wb") as f:
                f.write(data)
            raw = full
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 表情下载失败: {exc}", flush=True)
            return ""
        png = raw.rsplit(".", 1)[0] + ".png"
        try:
            from PIL import Image

            with Image.open(raw) as im:
                im.convert("RGBA").save(png, "PNG")
        except Exception:
            png = raw
        try:
            from app import recognize

            desc = recognize.understand(png, self._vision_key_of(), kind="emoji")
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 表情识别失败: {exc}", flush=True)
            return ""
        if desc:
            print(f"[wx4] 看见表情包了：{desc[:60]}", flush=True)
        return desc

    def see_image(self, chat_username: str, local_id, shard: str | None = None) -> str:
        """把聊天里的图解密出来交给视觉模型，返回一句描述；不行就空串。"""
        if self._vision_off:
            return ""
        key = self._vision_key_of()
        if not key:
            return ""
        try:
            if self._media is None:
                from wechatauto.media import MediaDownloader

                self._media = MediaDownloader(self.db, save_dir="data/incoming")
            # 原图要 AES 密钥（得先在微信里点开一张大图才拿得到），
            # 识图用压缩版就够；没有压缩版再退缩略图
            path = None
            for tier in ("mid", "thumb"):
                for sd in (shard, None) if shard else (None,):
                    try:
                        path = self._media.download_image(
                            chat_username, int(local_id), tier=tier, shard=sd)
                    except Exception:
                        path = None
                    if path:
                        break
                if path:
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 取图失败: {exc}", flush=True)
            return ""
        if not path:
            return ""
        try:
            from app import recognize

            desc = recognize.understand(path, key, kind="image")
        except Exception as exc:  # noqa: BLE001
            print(f"[wx4] 识图失败: {exc}", flush=True)
            return ""
        if desc:
            print(f"[wx4] 看见图了：{desc[:60]}", flush=True)
        return desc

    # ---------- 收消息 ----------

    def _to_incoming(self, msg: dict) -> Incoming | None:
        username = msg.get("username") or msg.get("chat") or ""
        sender_username = msg.get("sender_username") or ""
        if not username:
            return None
        if sender_username and sender_username == self.self_wxid:
            return None  # 自己发的
        if _is_system_account(username):
            return None  # 微信团队/公众号/系统号，不搭话
        is_group = username.endswith("@chatroom")
        raw_type = msg.get("type") or "text"
        mtype = TYPE_ALIAS.get(raw_type, raw_type)
        content = msg.get("content") or ""
        # 拍一拍在 4.x 里是 appmsg(type=62) 走「文件/链接/卡片」，不是「系统消息」，
        # 所以必须在安全改写之前先认出来，否则会变成「[链接]（她没点开）」。
        poke = parse_poke(content)
        if poke:
            self._last_poke[username] = time.time()
            content = f"（{poke}）"
            mtype = "_poke"
        # 链接 / 文件 / 红包 / 位置 / 通话：不打开，引用则保留对方新说的那句。
        mtype, content = redact_special(mtype, content, raw_type)
        if mtype == "image":
            desc = self.see_image(username, msg.get("local_id"), msg.get("shard"))
        elif mtype == "emoji":
            desc = self.see_emoji(content)
        else:
            desc = ""
        if mtype in ("image", "emoji"):
            if desc:
                tag = "图片" if mtype == "image" else "表情包"
                content = f"[{tag}：{desc}]"
            else:
                content = TYPE_PLACEHOLDER.get(mtype, f"[{raw_type}]")
        elif mtype == "system":
            poke = parse_poke(content)
            if poke:
                content = f"（{poke}）"
                self._last_poke[username] = time.time()
            else:
                return None  # 其它系统提示（入群、撤回之类），不当作聊天
        elif mtype not in ("text", "_safe_skip", "_poke"):
            content = TYPE_PLACEHOLDER.get(mtype, f"[{raw_type}]") + (
                f" {content}" if content and content != f"[{raw_type}]" else ""
            )
        sender_name = ""
        if is_group and sender_username:
            try:
                sender_name = self.db.get_nickname(sender_username) or ""
            except Exception:
                sender_name = ""
        sender_name = sender_name or self.display_name(username)
        if is_group:
            content = strip_group_prefix(content)
        return Incoming(
            chat_id=username,
            chat_name=self.display_name(username),
            is_group=is_group,
            sender_id=sender_username or username,
            sender_name=sender_name,
            content=content,
            ts=float(msg.get("create_time") or time.time()),
            is_mentioned=bool(is_group and self.self_name and self.self_name in content),
            raw=msg,
        )

    def _is_duplicate(self, chat: str, local_id: int) -> bool:
        """同一条消息只处理一次。"""
        key = f"{chat}:{local_id}"
        if key in self._seen_recent:
            return True
        last = self._seen_chat.get(chat, 0)
        if local_id <= last and local_id != 0:
            # 老消息（包括重启后重新投递的历史）直接丢
            self._seen_recent.add(key)
            return True
        self._seen_chat[chat] = max(last, local_id)
        self._seen_recent.add(key)
        if len(self._seen_recent) > 4000:
            self._seen_recent = set(list(self._seen_recent)[-2000:])
        try:
            self._seen_file.parent.mkdir(parents=True, exist_ok=True)
            self._seen_file.write_text(json.dumps(self._seen_chat, ensure_ascii=False),
                                       encoding="utf-8")
        except Exception:
            pass
        return False

    def recv_loop(self, handler, should_stop=lambda: False) -> None:
        from wechatauto.db import Listener

        def on_msg(msg: dict, _lst) -> None:
            # 去重：库的监听器会把同一条消息反复投给我们（水位没推进时尤其明显），
            # 不去重她就会对着同一句话连发好几条，看着像自己在跟自己聊。
            chat = msg.get("username") or ""
            try:
                local_id = int(msg.get("local_id") or 0)
            except Exception:
                local_id = 0
            if local_id and self._is_duplicate(chat, local_id):
                if os.environ.get("XIAOHAN_DEBUG"):
                    print(f"[wx4] 重复消息跳过 {chat}:{local_id}", flush=True)
                return
            if os.environ.get("XIAOHAN_DEBUG"):
                print(f"[wx4] 收到 {chat}:{local_id} type={msg.get('type')} "
                      f"from={msg.get('sender_username')}", flush=True)
            try:
                incoming = self._to_incoming(msg)
            except Exception as exc:  # noqa: BLE001
                print(f"[wx4] 消息解析失败: {exc}", flush=True)
                return
            if incoming is None:
                return
            try:
                handler(incoming)
            except Exception as exc:  # noqa: BLE001
                print(f"[wx4] handler 异常: {exc}", flush=True)

        lst = Listener(self.db, interval=self.poll_interval)
        lst.add_all(on_msg, discover=True)
        lst.start()
        try:
            while not should_stop():
                time.sleep(1)
        finally:
            lst.stop()

    def close(self) -> None:
        try:
            if self._wx is not None:
                self._wx.Close()
        except Exception:
            pass


def _user_idle_ms() -> int:
    """距离用户最后一次动键鼠过去了多少毫秒。"""

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not _u32.GetLastInputInfo(ctypes.byref(li)):
        return 10 ** 6
    return ctypes.windll.kernel32.GetTickCount() - li.dwTime


def _wait_user_pause(max_wait: float = 2.5, quiet_ms: int = 700) -> None:
    """别在你正打字的时候抢窗口：等到你停手一下下再发。"""
    waited = 0.0
    while waited < max_wait:
        if _user_idle_ms() >= quiet_ms:
            return
        time.sleep(0.3)
        waited += 0.3


def _focus_guard() -> int:
    """记下发消息前的前台窗口，发完还回去。"""
    try:
        return int(_u32.GetForegroundWindow())
    except Exception:
        return 0


def _restore_focus(prev: int, wechat_hwnd: int) -> None:
    """把前台还给用户原来那个窗口（微信自己就不还了）。

    SetForegroundWindow 在调用方不是前台进程时会被系统忽略，
    所以要先用 AttachThreadInput 沾一下目标窗口的输入队列。
    """
    if not prev or prev == wechat_hwnd:
        return
    try:
        if not _u32.IsWindow(prev):
            return
        if _u32.GetForegroundWindow() == prev:
            return
        _u32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        _u32.SetForegroundWindow(prev)
        if _u32.GetForegroundWindow() == prev:
            return
        # SetForegroundWindow 被前台锁挡掉时的王牌（未公开但好用）
        try:
            _u32.SwitchToThisWindow(prev, True)
            if _u32.GetForegroundWindow() == prev:
                return
        except Exception:
            pass
        target_tid = _u32.GetWindowThreadProcessId(prev, None)
        cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        if target_tid and target_tid != cur_tid:
            _u32.AttachThreadInput(cur_tid, target_tid, True)
            try:
                _u32.SetForegroundWindow(prev)
                _u32.BringWindowToTop(prev)
            finally:
                _u32.AttachThreadInput(cur_tid, target_tid, False)
    except Exception:
        pass


_SYSTEM_EXACT = {"weixin", "notifymessage", "fmessage", "medianote", "floatbottle",
                 "qqmail", "qmessage", "tmessage", "brandsessionholder", "mphelper"}


def _is_system_account(username: str) -> bool:
    """微信团队、公众号、系统通知这类不该被当成「朋友」。"""
    u = (username or "").lower()
    if not u:
        return True
    if u in _SYSTEM_EXACT:
        return True
    return u.startswith("gh_")


def _ok(res) -> bool:
    """wechatauto 的 WxResponse 兼容层。"""
    if res is None:
        return False
    if res is True:
        return True
    for attr in ("is_success", "ok", "success"):
        val = getattr(res, attr, None)
        if isinstance(val, bool):
            return val
        if callable(val):
            try:
                return bool(val())
            except Exception:
                pass
    return bool(getattr(res, "success", False))
