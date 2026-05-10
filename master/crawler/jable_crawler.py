"""jable.tv 视频采集器。

策略:请求列表页,用正则暴力提取视频卡片信息。
不依赖特定 class 名,容错性最强。
"""
import re
from urllib.parse import urljoin

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

BASE_URL = "https://jable.tv"
LIST_URLS = [
    "https://jable.tv/latest-updates/",
    "https://jable.tv/categories/censored/",
    "https://jable.tv/categories/uncensored/",
]

# 匹配视频卡片: <a href=".../videos/..."> 内部通常有 img
_CARD_RE = re.compile(
    r'<a\s+[^>]*href="([^"]*videos/[^"]*)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
# 从卡片片段里找 img
_IMG_SRC_RE = re.compile(r'<img\s+[^>]*(?:data-src|data-original|src)="([^"]+)"[^>]*>', re.S | re.I)
_IMG_ALT_RE = re.compile(r'<img\s+[^>]*alt="([^"]*)"[^>]*>', re.S | re.I)


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

        page = 1
        while len(results) < limit and page <= 5:
            url = f"{list_url}{page}/" if page > 1 else list_url
            try:
                resp = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
                resp.raise_for_status()
            except Exception:
                break

            html = resp.text
            found_on_page = 0

            for m in _CARD_RE.finditer(html):
                if len(results) >= limit:
                    break
                href = m.group(1)
                full_url = urljoin(BASE_URL, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                inner = m.group(2)
                # 缩略图
                thumb = ""
                img_m = _IMG_SRC_RE.search(inner)
                if img_m:
                    thumb = img_m.group(1)
                if thumb and not thumb.startswith("http"):
                    thumb = urljoin(BASE_URL, thumb)

                # 标题
                title = ""
                alt_m = _IMG_ALT_RE.search(inner)
                if alt_m:
                    title = alt_m.group(1)
                if not title:
                    # 尝试从链接附近更大的范围找 alt
                    start = max(0, m.start() - 300)
                    end = min(len(html), m.end() + 300)
                    alt_m2 = _IMG_ALT_RE.search(html[start:end])
                    if alt_m2:
                        title = alt_m2.group(1)
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
