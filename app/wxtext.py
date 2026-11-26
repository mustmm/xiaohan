"""微信消息文本的纯处理：不碰 Win32，方便离线测试。"""

from __future__ import annotations

import re

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")

PLACEHOLDER = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "emoji": "[表情]",
    "file": "[文件]",
    "link": "[链接]",
    "card": "[名片]",
    "quote": "[引用]",
    "voip": "[通话]",
    "hongbao": "[红包]",
    "location": "[位置]",
}

# 这些类型的原文里常有 URL、领取参数或坐标，不能原样塞进模型。
_RISK_TYPES = {"link", "file", "card", "hongbao", "voip", "location"}


def xml_text(raw: str) -> str:
    """把一段 XML 扒成纯文字：去标签、反转义、压掉多余空白。"""
    import html

    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def strip_urls(text: str) -> str:
    return _URL_RE.sub("", text or "").strip()


def safe_token(raw: str, n: int = 64) -> str:
    """文件名片段：只留字母数字，避免 md5 一类字段带上 ../。"""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "")[:n]


def chat_matches(current: str | None, target: str | None) -> bool:
    """当前打开的会话是不是要发的那一个。空名字一律当没对上。"""
    target = (target or "").strip()
    return bool(target) and (current or "").strip() == target


def strip_group_prefix(content: str) -> str:
    """去掉群消息库里的发送者前缀。

    库存格式是 ``wxid_xxx:\\n正文``，有时是 ``发送者id:\\n正文``。
    不能把「Hello: 在吗」这种正文里的冒号也切掉。
    """
    if not content:
        return content
    matched = re.match(r"^\s*(wxid_[0-9A-Za-z_\-]+):\s*", content)
    if matched:
        return content[matched.end():].lstrip("\n")
    matched = re.match(r"^\s*([0-9A-Za-z_\-]{5,64}):\n", content)
    if matched:
        return content[matched.end():]
    return content


def parse_quote(content: str) -> tuple[str, str] | None:
    """识别引用消息：返回 (被引用的人, 被引用的话)。"""
    if not content or "<refermsg>" not in content:
        return None
    ref = re.search(r"<refermsg>(.*?)</refermsg>", content, re.S)
    if not ref:
        return None
    inner = ref.group(1)
    who = ""
    found = re.search(r"<displayname>(.*?)</displayname>", inner, re.S)
    if found:
        who = xml_text(found.group(1))
    body = ""
    found = re.search(r"<content>(.*?)</content>", inner, re.S)
    if found:
        body = xml_text(found.group(1))
    if not body:
        found = re.search(r"<title>(.*?)</title>", inner, re.S)
        if found:
            body = xml_text(found.group(1))
    return who or "对方", body[:120]


def own_words(content: str) -> str:
    """引用消息里，对方新说的那句（appmsg 的 title），不是被引用的原文。"""
    found = re.search(r"<title>(.*?)</title>", content or "", re.S)
    if found:
        return xml_text(found.group(1))
    if content and not content.strip().startswith("<"):
        return content.strip()
    return ""


def parse_poke(content: str) -> str:
    """识别拍一拍：返回一句人话，例如「郑博拍了拍小涵」。"""
    if not content or "拍了拍" not in content:
        return ""
    found = re.search(r"<title>(.*?)</title>", content, re.S)
    if found:
        plain = xml_text(found.group(1)).strip()
        if "拍了拍" in plain:
            return plain[:60]
    plain = re.sub(r"<[^>]+>", " ", content)
    plain = xml_text(plain).strip()
    plain = re.sub(r"\s+", " ", plain)
    return plain[:60] if "拍了拍" in plain else ""


def redact_special(mtype: str, content: str, raw_type: str = "") -> tuple[str, str]:
    """链接、文件、红包、位置、通话改写成一句占位，不把原文里的 URL/坐标交给模型。

    引用消息保留「对方新说的话」和被引用的文字，但去掉 URL。
    """
    if mtype not in _RISK_TYPES:
        return mtype, content
    if mtype == "hongbao":
        return "_safe_skip", "[红包]（她没领）"
    if mtype == "voip":
        return "_safe_skip", "[通话]（她没接）"
    if mtype == "location":
        return "_safe_skip", "[位置]（她没点开）"
    if mtype == "link":
        quoted = parse_quote(content)
        if quoted:
            who, said = quoted
            said = strip_urls(said)
            mine = strip_urls(own_words(content))
            if mine:
                return "_safe_skip", f"（引用「{who}」说的：{said}）\n{mine}"
            return "_safe_skip", f"（引用「{who}」说的：{said}）"
        return "_safe_skip", "[链接]（她没点开，也没下载）"
    safe = PLACEHOLDER.get(mtype, f"[{raw_type or mtype}]")
    title = strip_urls(xml_text(content)[:80] if content else "")
    if title:
        return "_safe_skip", f"{safe}：{title}（她没打开、没下载）"
    return "_safe_skip", safe + "（她没打开、没下载）"
