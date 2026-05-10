"""hanime1.me 视频采集器。

策略:用正则找到所有 <a href="https://hanime1.me/watch?v=xxx"> 的精确位置,
对每个匹配取附近 HTML 提取缩略图(img src)和标题(div title / a title / img alt)。
"""
import re
from urllib.parse import urljoin

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

BASE_URL = "https://hanime1.me"
LIST_URLS = [
    "https://hanime1.me/",
    "https://hanime1.me/search?sort=%E6%9C%80%E8%BF%91%E6%9B%B4%E6%96%B0",
]

# 匹配 <a href="https://hanime1.me/watch?v=数字" ...>
_A_RE = re.compile(
    r'<a\s+[^>]*href="(https://hanime1\.me/watch\?v=\d+)"[^>]*>',
    re.S | re.I,
)


def crawl(limit: int = 10) -> list[dict]:
    """返回视频列表,每项包含 url / title / thumbnail_url / site。"""
    if curl_requests is None:
        raise RuntimeError("curl_cffi 未安装")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    results = []
    seen_urls = set()

    for list_url in LIST_URLS:
        if len(results) >= limit:
            break
        try:
            resp = curl_requests.get(list_url, headers=headers, impersonate="chrome124", timeout=30)
            resp.raise_for_status()
        except Exception:
            continue

        html = resp.text

        for m in _A_RE.finditer(html):
            if len(results) >= limit:
                break
            href = m.group(1)
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # 取匹配位置前后 1000 字符
            start = max(0, m.start() - 1000)
            end = min(len(html), m.end() + 1000)
            nearby = html[start:end]

            # 缩略图: 优先 vdownload.hembed.com, 其次任何 src
            thumb = ""
            for pat in [
                r'src="(https://vdownload\.hembed\.com[^"]*)"',
                r'src="([^"]*thumbnail[^"]*)"',
                r'src="([^"]+\.(?:jpg|jpeg|png|webp))"',
                r'data-src="([^"]*)"',
            ]:
                mm = re.search(pat, nearby, re.S | re.I)
                if mm:
                    thumb = mm.group(1)
                    break

            if thumb and not thumb.startswith("http"):
                thumb = urljoin(BASE_URL, thumb)

            # 标题: 优先 div title / a title, 其次 img alt, 最后附近文本
            title = ""
            for pat in [
                r'<div[^>]*title="([^"]*)"[^>]*>',
                r'<a[^>]*title="([^"]*)"[^>]*>',
                r'alt="([^"]*)"',
            ]:
                mm = re.search(pat, nearby, re.S | re.I)
                if mm:
                    t = mm.group(1).strip()
                    if t and len(t) > 2:
                        title = t
                        break

            if not title:
                txt = re.findall(r'>([^<]{5,60})<', nearby)
                for t in txt:
                    t = t.strip()
                    if t and not t.startswith(('http', '<', 'div', 'span', 'img', 'script')):
                        title = t
                        break

            if not title:
                title = "(无标题)"

            results.append({
                "url": href,
                "title": title,
                "thumbnail_url": thumb,
                "site": "hanime1.me",
            })

    return results
