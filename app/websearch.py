"""联网搜索：不依赖任何 API key，直接抓搜索结果页。

她想要查东西的时候，会在回答里写一行 [搜:关键词]，
运行时替她搜完，把结果塞回上下文，让她带着结果再回答一次。
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TAG_RE = re.compile(r"<[^>]+>")
LI_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
H2_RE = re.compile(r'<h2>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIP_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.S)


def _clean(s: str) -> str:
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _fetch(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _search_once(query: str, n: int, timeout: float) -> list[dict]:
    q = urllib.parse.quote(query)
    # 注意：别加 setlang/ensearch/mkt 这些参数，中文查询会被替换成无关的兜底结果
    urls = [
        f"https://www.bing.com/search?q={q}",
        f"https://cn.bing.com/search?q={q}",
    ]
    page = ""
    for url in urls:
        try:
            page = _fetch(url, timeout=timeout)
            if page and "b_algo" in page:
                break
        except Exception:  # noqa: BLE001
            continue
    if not page:
        return []

    out: list[dict] = []
    for block in LI_RE.findall(page)[: n * 2]:
        m = H2_RE.search(block)
        if not m:
            continue
        link = html.unescape(m.group(1))
        if link.startswith("/") or "bing.com" in link:
            continue
        title = _clean(m.group(2))
        sm = SNIP_RE.search(block)
        snippet = _clean(sm.group(1)) if sm else ""
        if not title:
            continue
        out.append({"title": title, "url": link, "snippet": snippet[:200]})
        if len(out) >= n:
            break
    return out


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"[\s\W]+", "", text or "")
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _score(item: dict, query: str) -> int:
    """结果和问题有多贴：标题里的词重合权重更高。"""
    q = _bigrams(query)
    if not q:
        return 0
    title = _bigrams(item.get("title", ""))
    snip = _bigrams(item.get("snippet", ""))
    return 3 * len(q & title) + len(q & snip)


def search(query: str, n: int = 5, timeout: float = 8.0) -> list[dict]:
    """返回 [{title, url, snippet}]，失败返回空列表（不抛异常）。

    中文检索用一条查询常常抓瞎（「卡尔曼滤波」会被拆成 Dota 的卡尔），
    所以再拼两条更像人问法的变体，按词重合度重排，别把不相关的排在前面。
    """
    variants = [f'"{query}"', query, f"{query} 原理 讲解"]
    seen: dict[str, dict] = {}
    for v in variants:
        for item in _search_once(v, n, timeout):
            seen.setdefault(item["url"], item)
        if len(seen) >= n * 2:
            break
    if not seen:
        return []
    ranked = sorted(seen.values(), key=lambda it: _score(it, query), reverse=True)
    return ranked[:n]


def weather(city: str, timeout: float = 8.0) -> str:
    """天气走专用接口，比搜网页准（wttr.in 中文模式返回的是网页，从里面抠一行）。"""
    city = city.strip() or "长春"
    url = "https://wttr.in/" + urllib.parse.quote(city, safe="") + "?format=%l:+%c+%t+%w+%h+%p&lang=zh-cn&m"
    try:
        page = _fetch(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r'<div class="term-container">(.*?)</div>', page, re.S)
    text = _clean(m.group(1)) if m else _clean(page)
    text = text.replace("&#47;", "/").strip()
    if not text or "<" in text:
        return ""
    return text[:120]


def baike(term: str, timeout: float = 10.0) -> dict | None:
    """百度百科摘要——学术名词用它比搜网页准得多（Bing 会把「卡尔曼滤波」拆成 Dota 的卡尔）。"""
    term = (term or "").strip()
    if not term:
        return None
    # 「卡尔曼滤波 论文」这种带空格的，先拆出里面像名词的那几段挨个试
    tokens = [term] + [t for t in re.split(r"[\s,，。;；、/]+", term) if 2 <= len(t) <= 20]
    tried: set[str] = set()
    for token in tokens:
        token = token.strip()
        if not token or len(token) > 20 or token in tried:
            continue
        tried.add(token)
        card = _baike_one(token, timeout)
        if card:
            return card
    return None


def _baike_one(term: str, timeout: float) -> dict | None:
    url = ("https://baike.baidu.com/api/openapi/BaikeLemmaCardApi?scope=103&format=json"
           "&appid=379020&bk_key=" + urllib.parse.quote(term))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return None
    abstract = _clean(data.get("abstract") or "")
    if not abstract:
        return None
    key = data.get("key") or term
    return {"title": f"百度百科：{key}", "abstract": abstract,
            "url": "https://baike.baidu.com/item/" + urllib.parse.quote(key)}


def query(q: str, n: int = 5, with_url: bool = False) -> str:
    """给模型看的最终结果文本：天气走天气接口，名词先看百科。"""
    q = (q or "").strip()
    if not q:
        return ""
    if any(w in q for w in ("天气", "气温", "下雨", "下雪", "多少度")):
        m = re.search(r"([\u4e00-\u9fa5]{2,8}?)(?:今天|明天|后天|现在|的)?(?:天气|气温|下雨|下雪|多少度)", q)
        city = m.group(1) if m else q
        w = weather(city)
        if w:
            return f"（你查了下天气）{w}"
    parts: list[str] = []
    card = baike(q)
    if card:
        line = f"（百度百科「{q}」）{card['abstract'][:400]}"
        if with_url:
            line += f"\n   链接：{card['url']}"
        parts.append(line)
    body = format_results(q, search(q, n), with_url=with_url)
    parts.append(body)
    return "\n".join(p for p in parts if p)


def format_results(query: str, items: list[dict], with_url: bool = False) -> str:
    if not items:
        return f'（搜「{query}」没搜到有用的东西）'
    lines = [f"（你刚搜了「{query}」，网上是这么说的：）"]
    for i, it in enumerate(items, 1):
        line = f"{i}. {it['title']}"
        if it["snippet"]:
            line += f" —— {it['snippet']}"
        if with_url and it.get("url"):
            line += f"\n   链接：{it['url']}"
        lines.append(line)
    return "\n".join(lines)


def _json(url: str, timeout: float = 10.0):
    """取 JSON（GitHub API 之类）。"""
    import json
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _ascii_terms(query: str) -> str:
    """GitHub 搜索只吃英文/数字，中文词会搜出乱七八糟的东西。"""
    NOISE = {"github", "git", "http", "https", "www", "com", "cn", "app", "org", "net"}
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9 .+#_-]*", query)
    keep = [t.strip() for t in toks if len(t.strip()) >= 2 and t.strip().lower() not in NOISE]
    return " ".join(keep).strip()


def _github_repos(query: str, limit: int = 3, timeout: float = 10.0) -> list[str]:
    """在 GitHub 上按 star 找仓库，返回「名（★数）— 简介 — 链接」。"""
    import urllib.parse

    query = _ascii_terms(query) or query
    if not re.search(r"[A-Za-z]{2,}", query):
        return []
    out: list[str] = []
    q = urllib.parse.quote(query)
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={limit}"
    try:
        data = _json(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return out
    for it in (data.get("items") or [])[:limit]:
        name = it.get("full_name") or ""
        desc = _clean(it.get("description") or "")[:60]
        stars = it.get("stargazers_count") or 0
        link = it.get("html_url") or ""
        if name and link:
            out.append(f"GitHub 项目 {name}（★{stars}）{('— ' + desc) if desc else ''} — {link}")
    return out


# ---------- 境外正规站点 ----------
_CN_HINT = (".cn", "mihoyo.com", "bilibili.com", "zhihu.com", "qq.com", "163.com",
            "baidu.com", "taobao.com", "jd.com", "csdn.net", "toutiao.com", "sohu.com",
            "weibo.", "douyin.com", "xiaohongshu.com", "gitee.com", "aliyun.com")


def is_offshore(url: str) -> bool:
    """粗判境外站：不在常见国内域名表里就算。"""
    low = (url or "").lower()
    return bool(low) and not any(h in low for h in _CN_HINT)


def wikipedia(term: str, timeout: float = 7.0) -> dict | None:
    """维基百科条目（能通就给，不通就算了）。"""
    import urllib.parse

    for host in ("zh.wikipedia.org", "en.wikipedia.org"):
        try:
            q = urllib.parse.quote(term)
            api = (f"https://{host}/w/api.php?action=query&list=search&srsearch={q}"
                   f"&format=json&srlimit=1&utf8=1")
            data = _json(api, timeout=timeout)
            hits = ((data.get("query") or {}).get("search") or [])
            if not hits:
                continue
            title = hits[0].get("title") or term
            snip = re.sub(r"<[^>]+>", "", hits[0].get("snippet") or "")
            link = f"https://{host}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            return {"title": title, "snippet": _clean(snip), "url": link}
        except Exception:  # noqa: BLE001
            continue
    return None


def _s360(query: str, n: int = 5, timeout: float = 12.0) -> list[dict]:
    """360 搜索（中文结果比 bing 稳），返回 [{title, url}]。"""
    url = "https://www.so.com/s?q=" + urllib.parse.quote(query)
    try:
        page = _fetch(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for block in re.split(r'class="res-list', page)[1:]:
        m = re.search(r'data-mdurl="(https?://[^"]+)"', block)
        if not m:
            continue
        link = html.unescape(m.group(1))
        if any(d in link for d in ("so.com", "360.cn", "360kan.com", "360tpcdn.com")):
            continue
        if any(d in link for d in ("dictionary.", "iciba.com", "dict.", "translate.")):
            continue
        t = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        title = _clean(t.group(1))[:60] if t else ""
        if not title or "其他人还搜" in title:
            continue
        out.append({"title": title, "url": link})
        if len(out) >= n:
            break
    return out


def _relevant(title: str, query: str) -> bool:
    """标题里至少沾上一个查询词，否则当串味结果丢掉（360 偶尔给无关结果）。"""
    toks = [t for t in re.split(r"[\s,，、]+", query)
            if len(t) >= 3 or len(re.findall(r"[\u4e00-\u9fff]", t)) >= 2]
    if not toks:
        return True
    low = (title or "").lower()
    return any(t.lower() in low for t in toks)


def _s360_retry(query: str, n: int) -> list[dict]:
    """360 有时会限流返回很短的页，等一下就再来一次。"""
    import time

    got = _s360(query, n)
    if got:
        return got
    time.sleep(1.2)
    return _s360(query, n)


def resources(query: str, n: int = 4) -> str:
    """有人要资源：GitHub 项目 + 官网/官方渠道，给她现成的链接，别自己编。"""
    lines: list[str] = []
    lines.extend(_github_repos(query, limit=2))

    for q in (f"{query} 官网", query):
        for it in _s360_retry(q, 4):
            if not _relevant(it["title"], query):
                continue
            lines.append(f"网页 {it['title']} — {it['url']}")

    if len(lines) < 2:
        try:
            for it in _search_once(f"{query} 官网", 3, 8.0):
                t = _clean(it.get("title") or "")[:60]
                u = it.get("url") or ""
                if t and u.startswith("http") and _relevant(t, query):
                    lines.append(f"网页 {t} — {u}")
        except Exception:  # noqa: BLE001
            pass

    if len(lines) < 2:
        card = baike(query)
        if card and (card.get("abstract") or ""):
            lines.append(f"百科 {card.get('name') or query}：{(card.get('abstract') or '')[:120]} — {card.get('url') or ''}".strip())

    if len(lines) < 3:
        card = wikipedia(query)
        if card and card.get("url"):
            lines.append(f"维基 {card['title']}：{card['snippet'][:80]} — {card['url']}")

    seen: set[str] = set()
    uniq: list[str] = []
    for line in lines:
        url = line.rsplit(" ", 1)[-1]
        if url in seen:
            continue
        seen.add(url)
        if is_offshore(url):
            line += "（境外站点，可能要梯子）"
        uniq.append(line)
    return "\n".join(uniq[: max(3, n + 2)]) if uniq else ""
