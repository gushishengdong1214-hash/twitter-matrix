"""hanime1.me 视频采集器。

策略:请求首页,用正则暴力提取所有 /watch?v=xxx 链接及其附近的标题/缩略图。
不依赖特定 class 名,容错性最强。
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

# 匹配 watch?v=数字 的链接(hanime1用完整URL: https://hanime1.me/watch?v=xxx)
_WATCH_RE = re.compile(
    r'<a\s+[^>]*href="(https://hanime1\.me/watch\?v=\d+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
# 从一段 HTML 片段里找 img 的 src / alt
_IMG_RE = re.compile(r'<img\s+[^>]*src="([^"]+)"[^>]*>', re.S | re.I)
_IMG_ALT_RE = re.compile(r'<img\s+[^>]*alt="([^"]*)"[^>]*>', re.S | re.I)
# 从一段 HTML 片段里找文本标题（简单去掉标签后的文字）
_TITLE_RE = re.compile(r'>([^<]{3,80})<', re.S)


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

        # 策略1: 正则暴力提取 <a href="/watch?v=xxx">...</a>
        for m in _WATCH_RE.finditer(html):
            if len(results) >= limit:
                break
            href = m.group(1)
            full_url = urljoin(BASE_URL, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            inner = m.group(2)
            # 从 inner 找缩略图
            thumb = ""
            img_m = _IMG_RE.search(inner)
            if img_m:
                thumb = img_m.group(1)
            else:
                # 扩大范围:在匹配点前后 500 字符内找 img
                start = max(0, m.start() - 500)
                end = min(len(html), m.end() + 500)
                nearby = html[start:end]
                img_m2 = _IMG_RE.search(nearby)
                if img_m2:
                    thumb = img_m2.group(1)

            if thumb and not thumb.startswith("http"):
                thumb = urljoin(BASE_URL, thumb)

            # 标题:优先 img 的 alt,其次 inner 里的文本
            title = ""
            alt_m = _IMG_ALT_RE.search(inner)
            if alt_m:
                title = alt_m.group(1)
            else:
                alt_m2 = _IMG_ALT_RE.search(nearby) if 'nearby' in dir() else None
                if alt_m2:
                    title = alt_m2.group(1)
            if not title:
                txt_m = _TITLE_RE.search(inner)
                if txt_m:
                    title = txt_m.group(1).strip()
            if not title:
                title = "(无标题)"

            results.append({
                "url": full_url,
                "title": title,
                "thumbnail_url": thumb,
                "site": "hanime1.me",
            })

    return results
