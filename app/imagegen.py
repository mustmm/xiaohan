"""生图：用 SiliconFlow 的 Kolors / Qwen-Image 画画，存到 data/art/。

她有两类图：
- 自拍 = 日系赛璐璐/厚涂二次元的插画，**永远同一个她**：
  粉色双马尾、蓝黑色瞳孔；上学穿中国式蓝白校服，不上学穿地雷系或清楚系。
  场景随时间段变（早晨/中午/傍晚/深夜…），换别人来也能一眼认出是同一个人。
- 自己的画（OC）= **黑底白线的简约线稿，不上色**，双马尾小恶魔形象。

只依赖标准库 + urllib，不引第三方 SDK。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

BASE = "https://api.siliconflow.cn/v1/images/generations"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART_DIR = os.path.join(ROOT, "data", "art")

# 她的脸和头发——每张自拍都带这段，保证是「同一个人」
SELFIE_FACE = (
    "粉发双马尾少女（两根高马尾、齐刘海），16 岁高中女生，"
    "深蓝黑色的眼睛（接近黑，微微偏蓝），皮肤白，脸小巧，微微笑"
)

SCHOOL_LOOK = (
    "穿中国式蓝白运动校服（蓝色拉链外套、白色条纹、校徽）"
)

JIRAI_LOOK = (
    "穿地雷系衣服（黑色荷叶边衬衫、丝带、黑色百褶短裙、黑色过膝袜、"
    "厚底鞋、蕾丝颈环、十字架耳坠）"
)

CLEAR_LOOK = (
    "穿清楚系衣服（米白针织开衫、白衬衫、灰格百褶裙、白袜、乐福鞋）"
)

STYLE = (
    "日本动漫截图风格，纯二次元插画，平涂赛璐璐上色，清晰描线，"
    "非 3D 非真人，色彩干净，细节丰富，无水印无文字"
)

# OC：白底、只有线、人物内部完全留白，像纯铅笔速写；画风锐利；一颗虎牙
OC_STYLE = (
    "single pencil sketch on pure white paper, black graphite lines only, "
    "outline only with empty white interior, no fill at all, no black areas, "
    "clean sharp crisp lines, even constant line weight, "
    "angular sharp drawing style, sharp eyes and sharp chin, "
    "no shading, no hatching, no smudge, no color, no grey, "
    "minimal and simple, lots of white space"
)
OC_SUBJECT = (
    "1girl, jirai kei girl (地雷系), long hair in twin tails drawn as thin lines only, "
    "blunt bangs, sharp narrow eyes, wicked smug smirk (邪魅的笑), "
    "one single little fang (只有一颗虎牙), "
    "frilly blouse with ribbon and choker, upper body, three-quarter view"
)

DEFAULTS = {
    "selfie_model": "Qwen/Qwen-Image",
    "draw_model": "Qwen/Qwen-Image",
    "image_size": "1024x1024",
    "steps": 30,
    "guidance": 4.5,
    "negative": "lowres, bad anatomy, bad hands, extra fingers, watermark, text, logo, nsfw",
}


def _post(url: str, payload: dict, key: str, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str, dest: str, crop: bool = True) -> None:
    """下载并转成 PNG；顺手裁掉 SiliconFlow 加在右下角的「AI 生成」水印条。"""
    from . import safehttp

    blob = safehttp.read_bytes(
        url, headers={"User-Agent": "xiaohan/1.0"}, timeout=120, max_bytes=20 * 1024 * 1024,
    )
    tmp = dest + ".raw"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    try:
        from PIL import Image

        with Image.open(tmp) as im:
            im = im.convert("RGB")
            w, h = im.size
            if crop:
                im = im.crop((0, 0, w, int(h * 0.94)))   # 水印在最底下那一截
            im.save(dest, "PNG")
        os.remove(tmp)
    except Exception as exc:  # noqa: BLE001
        print(f"[imagegen] 转 PNG 失败，保留原图: {exc}", flush=True)
        if os.path.exists(tmp):
            os.replace(tmp, dest)


def _invert_to_black(dest: str) -> None:
    """白纸黑线 → 反过来 = 黑底白线（她说的「黑底白线」就是这么来的）。"""
    try:
        from PIL import Image, ImageOps

        with Image.open(dest) as im:
            out = ImageOps.invert(im.convert("RGB"))
            out.save(dest, "PNG")
    except Exception as exc:  # noqa: BLE001
        print(f"[imagegen] 反色失败: {exc}", flush=True)


def _line_only(dest: str, min_thick: int = 9, grow: int = 11) -> None:
    """把「填充块」剥掉，只留细线：白底 + 黑线，人物内部保持留白。

    做法：取出深色像素 → 腐蚀掉细线、剩下大块 → 再把大块长回来 →
    从原图里减掉这些大块，剩下的就只有线条了；最后做一次闭运算把断口接上。
    """
    try:
        from PIL import Image, ImageFilter

        with Image.open(dest) as im:
            gray = im.convert("L")
            mask = gray.point(lambda v: 255 if v < 140 else 0)
            eroded = mask.filter(ImageFilter.MinFilter(min_thick))
            thick = eroded.filter(ImageFilter.MaxFilter(grow))
            px = mask.load()
            tp = thick.load()
            w, h = mask.size
            out = Image.new("L", (w, h), 255)
            op = out.load()
            for y in range(h):
                for x in range(w):
                    if px[x, y] and not tp[x, y]:
                        op[x, y] = 0
            out = out.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
            out.convert("RGB").save(dest, "PNG")
    except Exception as exc:  # noqa: BLE001
        print(f"[imagegen] 去填充失败: {exc}", flush=True)


def _slug(text: str, limit: int = 24) -> str:
    out = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text or "").strip("_")
    return (out or "art")[:limit]


def generate(prompt: str, key: str, *, cfg: dict | None = None,
             kind: str = "draw", timeout: int = 120,
             negative: str | None = None, size: str | None = None) -> str | None:
    """画一张，返回本地 png 路径。失败返回 None。"""
    cfg = cfg or {}
    model = cfg.get(f"{kind}_model", DEFAULTS[f"{kind}_model"])
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": size or cfg.get("image_size", DEFAULTS["image_size"]),
        "batch_size": 1,
        "num_inference_steps": int(cfg.get("steps", DEFAULTS["steps"])),
        "guidance_scale": float(cfg.get("guidance", DEFAULTS["guidance"])),
        "negative_prompt": negative or cfg.get("negative", DEFAULTS["negative"]),
    }
    try:
        res = _post(BASE, payload, key, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        print(f"[imagegen] HTTP {exc.code}: {body}", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[imagegen] 请求失败: {exc}", flush=True)
        return None

    images = res.get("images") or []
    if not images:
        print(f"[imagegen] 没返回图: {str(res)[:200]}", flush=True)
        return None
    url = images[0].get("url") if isinstance(images[0], dict) else images[0]
    if not url:
        return None
    os.makedirs(ART_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(ART_DIR, f"{kind}_{stamp}_{_slug(prompt)}.png")
    try:
        _download(url, dest)
    except Exception as exc:  # noqa: BLE001
        print(f"[imagegen] 下载失败: {exc}", flush=True)
        return None
    print(f"[imagegen] 画好了 {dest}", flush=True)
    return dest


# ---------- 自拍 ----------

def _time_phrase(now: float | None = None) -> str:
    hour = time.localtime(now or time.time()).tm_hour
    if 5 <= hour < 8:
        return "清晨，天刚亮的冷蓝调，街上很安静"
    if 8 <= hour < 11:
        return "上午，明亮的日光"
    if 11 <= hour < 14:
        return "正午，强烈的阳光和高对比"
    if 14 <= hour < 17:
        return "下午，暖黄的斜照光"
    if 17 <= hour < 19:
        return "日落时分，橙色天光"
    if 19 <= hour < 22:
        return "傍晚，室内暖灯，窗外是城市灯光"
    if 22 <= hour < 24:
        return "夜里，房间只开一盏小灯，屏幕微光"
    return "深夜，光线很暗，只有一处小光源"


def _is_school_time(now: float | None = None) -> bool:
    """工作日早八到晚五算在学校。"""
    t = time.localtime(now or time.time())
    return t.tm_wday < 5 and 8 <= t.tm_hour < 17


def _outfit(at_school: bool, seed: float | None = None) -> str:
    if at_school:
        return SCHOOL_LOOK
    rng = random.Random(int((seed or time.time()) // 3600))
    return JIRAI_LOOK if rng.random() < 0.6 else CLEAR_LOOK


def selfie(scene: str, key: str, cfg: dict | None = None,
           at_school: bool | None = None, now: float | None = None) -> str | None:
    """自拍：固定的脸和双马尾 + 按时间/地点换衣服和光。"""
    school = _is_school_time(now) if at_school is None else at_school
    place = "中国高中教室里（课桌、黑板、窗户）" if school else ""
    prompt = (
        f"{STYLE}，{SELFIE_FACE}，{_outfit(school, now)}，"
        f"{place + '，' if place else ''}{_time_phrase(now)}，"
        f"{scene}，半身像，看着镜头"
    )
    return generate(
        prompt, key, cfg=cfg, kind="selfie",
        negative=(cfg.get("selfie_negative") if cfg else None)
        or "3D，CG，写实，真人，塑料皮肤，厚嘴唇，"
           "单马尾，披发，两个以上人物，"
           "亮蓝色眼睛，浅蓝眼睛，青色眼睛，红色眼睛，琥珀色眼睛，"
           "水手服，超短裙，暴露，"
           "低清，畸形的手，多手指，水印，文字，logo，nsfw",
    )


# ---------- 她自己画的 OC ----------

def artwork(idea: str, key: str, cfg: dict | None = None,
            colored: bool = False) -> str | None:
    """她自己画的 OC：默认黑底白线、简约、不上色。

    做法是先让模型画「白纸黑线」，再把整张反色——比直接要求它画白线可靠得多。
    """
    if colored:
        style = "digital painting, soft colors, painterly, illustration"
        prompt = f"{style}, {OC_SUBJECT}, {idea}, hand drawn"
        return generate(prompt, key, cfg=cfg, kind="draw")
    prompt = (
        f"{OC_STYLE}, {OC_SUBJECT}, {idea}, "
        "outline only, every area inside the character stays pure white, "
        "hair drawn as separate thin lines with no solid fill"
    )
    path = generate(
        prompt, key, cfg=cfg, kind="draw",
        negative=(cfg.get("draw_negative") if cfg else None)
        or "solid black fill, filled hair, filled clothes, black areas, "
           "dark shading, hatching, cross hatching, smudge, gradient, "
           "color, colored, grey, two fangs, vampire fangs, open grin, "
           "horns, devil tail, text, watermark, photorealistic, complex detail",
    )
    return path


def latest(kind: str | None = None) -> str | None:
    """最近一张画，方便「再发一张刚才那样的」。"""
    if not os.path.isdir(ART_DIR):
        return None
    files = [os.path.join(ART_DIR, f) for f in os.listdir(ART_DIR)
             if f.lower().endswith(".png") and (not kind or f.startswith(kind))]
    if not files:
        return None
    return max(files, key=os.path.getmtime)
