"""上网找图 —— 她想发一张应景的图/表情包，本地库里没有就去搜。

优先搜「邦多利 / pjsk」这类的图，返回一张已经下载好、洗过尺寸的本地文件。
全部失败返回 None，调用方就当没这回事。
"""

from __future__ import annotations

import hashlib
import html
import io
import os
import re
import urllib.parse
import urllib.request

from . import websearch

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
OUT_DIR = os.path.join("data", "webimg")
MAX_SIDE = 640
MIN_BYTES = 6 * 1024
MAX_BYTES = 4 * 1024 * 1024

# 想让她更偏向哪类，可以在搜索词后面加这些
PREFER = {"邦": "邦多利", "pjsk": "pjsk", "世界计划": "世界计划"}


def _bing_images(query: str, count: int = 30) -> list[str]:
    url = (
        "https://www.bing.com/images/async?q="
        + urllib.parse.quote(query)
        + f"&first=0&count={count}&mmasync=1"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        page = resp.read().decode("utf-8", "ignore")
    urls = re.findall(r'murl&quot;:&quot;(.*?)&quot;', page)
    seen: list[str] = []
    for raw in urls:
        u = html.unescape(raw)
        if u.startswith("http") and u not in seen:
            seen.append(u)
    return seen


def _download(url: str) -> bytes | None:
    try:
        from . import safehttp

        data = safehttp.read_bytes(url, headers={
            "User-Agent": UA,
            "Referer": "https://www.bing.com/",
            "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        }, timeout=15, max_bytes=MAX_BYTES)
    except Exception:  # noqa: BLE001
        return None
    if not data or len(data) < MIN_BYTES:
        return None
    return data


def _normalize(data: bytes, dest_dir: str, stem: str) -> str | None:
    """转成微信吃得下的格式，顺手缩到不夸张的尺寸。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001
        return None
    if im.width < 80 or im.height < 80:
        return None
    if im.width > 1600 or im.height > 1600:
        return None
    keep_gif = getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1
    if keep_gif:
        im.seek(0)
        out = os.path.join(dest_dir, f"{stem}.gif")
        try:
            im.convert("P").save(out, "GIF")
            return out
        except Exception:  # noqa: BLE001
            pass
    im = im.convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    out = os.path.join(dest_dir, f"{stem}.png")
    im.save(out, "PNG")
    return out


def find_image(keyword: str, limit: int = 6) -> str | None:
    """按关键词找一张图，返回本地路径。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return None
    os.makedirs(OUT_DIR, exist_ok=True)
    queries = [keyword]
    if not any(k in keyword for k in ("邦多利", "pjsk", "世界计划", "表情")):
        queries.append(f"{keyword} 邦多利 表情包")
        queries.append(f"{keyword} pjsk 表情包")
    for query in queries[:2]:
        urls = []
        try:
            urls = _bing_images(query)
        except Exception as exc:  # noqa: BLE001
            print(f"[webimg] 搜图失败: {exc}", flush=True)
        for url in urls[:limit]:
            data = _download(url)
            if not data:
                continue
            stem = "web_" + hashlib.md5(url.encode()).hexdigest()[:12]
            path = _normalize(data, OUT_DIR, stem)
            if path:
                print(f"[webimg] 找到图：{path}（{query}）", flush=True)
                return path
    # 图搜不到就退一步：先搜文字，看看有没有"图包"之类的页面标题可用
    try:
        hits = websearch.search(f"{keyword} 表情包", n=3)
    except Exception:  # noqa: BLE001
        hits = []
    for hit in hits:
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        try:
            urls = _bing_images(title)
        except Exception:  # noqa: BLE001
            continue
        for url in urls[:2]:
            data = _download(url)
            if not data:
                continue
            stem = "web_" + hashlib.md5(url.encode()).hexdigest()[:12]
            path = _normalize(data, OUT_DIR, stem)
            if path:
                print(f"[webimg] 换关键词找到图：{path}（{title}）", flush=True)
                return path
    return None
