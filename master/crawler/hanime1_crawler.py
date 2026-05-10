"""hanime1.me 视频采集器 — BeautifulSoup 版。"""
import re
from urllib.parse import urljoin

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

BASE_URL = "https://hanime1.me"
LIST_URLS = [
    "https://hanime1.me/",
    "https://hanime1.me/search?sort=%E6%9C%80%E8%BF%91%E6%9B%B4%E6%96%B0",
]


def crawl(limit: int = 10) -> list[dict]:
    if curl_requests is None:
        raise RuntimeError("curl_cffi 未安装")
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 未安装")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
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

        # lxml 解析器对 HTML5 嵌套更友好,不会把 <a> 内部的 <div>/<img> 移出
        try:
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception:
            soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=re.compile(r"^https://hanime1\.me/watch\?v=\d+")):
            if len(results) >= limit:
                break
            href = a.get("href", "")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)

            # 缩略图: 在 <a> 内部找 <img>
            thumb = ""
            img = a.find("img")
            if img:
                thumb = img.get("src") or img.get("data-src") or ""

            # 标题: 优先祖父级 div title, 其次 <a> 的 title, 最后 img alt
            title = ""
            parent = a.find_parent()
            if parent and parent.get("title"):
                title = parent.get("title").strip()
            if not title and a.get("title"):
                title = a.get("title").strip()
            if not title and img and img.get("alt"):
                title = img.get("alt").strip()
            if not title:
                title = "(无标题)"

            results.append({
                "url": href,
                "title": title,
                "thumbnail_url": thumb,
                "site": "hanime1.me",
            })

    return results
