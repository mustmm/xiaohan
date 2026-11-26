"""表情包：从 stickers/ 里挑一张，塞进 Windows 剪贴板，再粘到微信里发出去。

不依赖 PIL：图片在导入时已经用 PowerShell + System.Drawing 转成 24 位 BMP
（白底），发的时候只要把 BMP 文件头去掉，剩下的就是 CF_DIB 需要的字节。
"""

from __future__ import annotations

import ctypes
import json
import os
import random
from ctypes import wintypes

CF_DIB = 8
GMEM_MOVEABLE = 0x0002

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(BASE, "stickers")

_user32 = None
_kernel32 = None


def _win32():
    """剪贴板 API 到真要粘贴时再加载，避免在非 Windows 上 import 就失败。"""
    global _user32, _kernel32
    if _user32 is not None:
        return _user32, _kernel32
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    _user32, _kernel32 = user32, kernel32
    return user32, kernel32


def set_clipboard_dib(bmp_path: str) -> bool:
    """把 BMP 塞进剪贴板（CF_DIB，微信能直接当图片粘贴）。"""
    try:
        with open(bmp_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    if len(data) < 54 or data[:2] != b"BM":
        return False
    payload = data[14:]  # 去掉 BITMAPFILEHEADER，剩 BITMAPINFOHEADER + 像素
    size = len(payload)

    try:
        user32, kernel32 = _win32()
    except (AttributeError, OSError):
        return False
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, payload, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_DIB, handle):
            kernel32.GlobalFree(handle)
            return False
    finally:
        user32.CloseClipboard()
    return True


class StickerBook:
    """能发的表情包。caption 就是文件名里的中文标签，比如"叹气""哈？！"。"""

    def __init__(self, root: str = ROOT):
        self.root = root
        self.items: list[dict] = []
        self._by_caption: dict[str, list[dict]] = {}
        manifest = os.path.join(root, "manifest.json")
        if not os.path.exists(manifest):
            return
        try:
            with open(manifest, encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            raw = [raw]
        for it in raw:
            bmp = os.path.join(root, it.get("bmp", ""))
            if os.path.exists(bmp):
                self.items.append({**it, "path": bmp})
        for it in self.items:
            self._by_caption.setdefault(it.get("caption", ""), []).append(it)

    def __bool__(self) -> bool:
        return bool(self.items)

    def captions(self) -> list[str]:
        return sorted(c for c in self._by_caption if c)

    def pick(self, caption: str | None = None) -> dict | None:
        if not self.items:
            return None
        if caption:
            caption = caption.strip().strip("[]【】 ")
            pool = self._by_caption.get(caption)
            if not pool:
                # 模糊一点：包含关系也算，比如"生气！"→"生气"
                for key, items in self._by_caption.items():
                    if caption and (caption in key or key in caption):
                        pool = items
                        break
            if pool:
                return random.choice(pool)
        return random.choice(self.items)
