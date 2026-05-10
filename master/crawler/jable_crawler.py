"""jable.tv 视频采集器 — BeautifulSoup 版。"""
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

BASE_URL = "https://jable.tv"
LIST_URLS = [
    "https://jable.tv/latest-updates/",
    "https://jable.tv/categories/censored/",
    "https://jable.tv/categories/uncensored/",
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

        page = 1
        while len(results) < limit and page <= 5:
            url = f"{list_url}{page}/" if page > 1 else list_url
            try:
                resp = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
                resp.raise_for_status()
            except Exception:
                break

            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")
            found_on_page = 0

            for a in soup.find_all("a", href=re.compile(r"/videos/[^/]+")):
                if len(results) >= limit:
                    break
                href = a.get("href", "")
                if not href:
                    continue
                full_url = urljoin(BASE_URL, href)

                # 过滤非视频链接
                if any(k in full_url.lower() for k in ["categories", "tags", "actors", "series"]):
                    continue
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # 缩略图
                thumb = ""
                img = a.find("img")
                if img:
                    thumb = img.get("src") or img.get("data-src") or ""

                # 标题: 优先 img alt, 其次 a title, 再次 a 的文本, 最后附近 h6/div
                title = ""
                if img and img.get("alt"):
                    title = img.get("alt").strip()
                if not title and a.get("title"):
                    title = a.get("title").strip()
                if not title:
                    txt = a.get_text(strip=True)
                    if txt and len(txt) > 1:
                        title = txt
                # 兜底: 在父元素里找标题
                if not title:
                    for parent in [a.find_parent(), a.find_parent().find_parent()]:
                        if not parent:
                            continue
                        for sel in [".title", ".video-title", "h6", "h5", "h3"]:
                            el = parent.select_one(sel)
                            if el:
                                t = el.get_text(strip=True)
                                if t and len(t) > 1:
                                    title = t
                                    break
                        if title:
                            break

                if not title:
                    title = "(无标题)"

                results.append({
                    "url": full_url,
                    "title": title,
                    "thumbnail_url": thumb,
                    "site": "jable.tv",
                })
                found_on_page += 1

            if found_on_page == 0:
                break
            page += 1

    return results
