"""只访问公网 http(s)。

消息 XML、搜索页和生图接口都会把 URL 交到这里。标准库的 urlopen
会跟着跳转到 file:、内网地址和云元数据，所以先校验再读，并且每一跳都再查一次。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
import urllib.request


class UnsafeURL(ValueError):
    """目标不是可公开访问的 http(s)。"""


_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
}


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        (not ip.is_global)
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_http_url(url: str) -> str:
    """返回规范化后的 URL。不是公网 http(s) 就抛 UnsafeURL。"""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURL("empty")
    raw = url.strip()
    if any(ch in raw for ch in ("\r", "\n", "\t", " ")):
        raise UnsafeURL("whitespace")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("scheme")
    if parts.username or parts.password:
        raise UnsafeURL("userinfo")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURL("host")
    if (
        host in _BLOCKED_HOSTS
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        raise UnsafeURL("host")
    if re.fullmatch(r"\d+", host) or host.startswith("0x"):
        raise UnsafeURL("numeric-host")
    if re.fullmatch(r"(?:\d+\.){3}\d+", host):
        if any(len(part) > 1 and part.startswith("0") for part in host.split(".")):
            raise UnsafeURL("numeric-host")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise UnsafeURL("numeric-host") from exc
        if _bad_ip(ip):
            raise UnsafeURL("ip")
    else:
        port = parts.port
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURL("dns") from exc
        if not infos:
            raise UnsafeURL("dns")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if _bad_ip(ip):
                raise UnsafeURL("ip")
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment,
    ))


class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        validate_public_http_url(absolute)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def read_bytes(url: str, *, headers: dict | None = None, timeout: float = 20,
               max_bytes: int = 4 * 1024 * 1024) -> bytes:
    """下载一份有上限的响应。拒绝内网、file: 和跳进内网的重定向。"""
    safe = validate_public_http_url(url)
    req = urllib.request.Request(safe, headers=dict(headers or {}))
    opener = urllib.request.build_opener(_GuardRedirect)
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafeURL("too-large")
    return data
