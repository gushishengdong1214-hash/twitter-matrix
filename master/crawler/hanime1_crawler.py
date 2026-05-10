"""hanime1.me 视频采集器。

策略:逐个匹配 <a href="https://hanime1.me/watch?v=xxx"> 的精确位置,
缩略图只在 <a> 开始后 500 字符内的 <img> 里找,
标题只在 <a> 开始前 500 字符内的 <div title> 里找,
避免和页面其他区域的 img/div 混淆。
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

            # --- 缩略图:只在 <a> 开始后 500 字符内的 <img> 里找 ---
            thumb = ""
            img_area = html[m.end():m.end() + 500]
            mm = re.search(r'<img[^>]*src="([^"]*)"[^>]*>', img_area, re.S | re.I)
            if mm:
                thumb = mm.group(1).strip()
            if not thumb:
                mm = re.search(r'<img[^>]*data-src="([^"]*)"[^>]*>', img_area, re.S | re.I)
                if mm:
                    thumb = mm.group(1).strip()
            if thumb and not thumb.startswith("http"):
                thumb = urljoin(BASE_URL, thumb)

            # --- 标题:只在 <a> 开始前 500 字符内的 div title 里找 ---
            title = ""
            title_area = html[max(0, m.start() - 500):m.start()]
            mm = re.search(r'<div[^>]*title="([^"]*)"', title_area, re.S | re.I)
            if mm:
                t = mm.group(1).strip()
                if len(t) > 1:
                    title = t
            if not title:
                # 兜底:在 <a> 内部找 img alt
                mm = re.search(r'alt="([^"]*)"', img_area, re.S | re.I)
                if mm:
                    t = mm.group(1).strip()
                    if len(t) > 1:
                        title = t
            if not title:
                title = "(无标题)"

            results.append({
                "url": href,
                "title": title,
                "thumbnail_url": thumb,
                "site": "hanime1.me",
            })

    return results
