"""jable.tv 视频采集器。

策略:请求列表页,用正则找到所有视频链接的精确位置,取附近 HTML 提取缩略图和标题。
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

# 匹配 <a href=".../videos/..."> 的精确位置
_A_RE = re.compile(
    r'<a\s+[^>]*href="([^"]*videos/[^"]*)"[^>]*>',
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

            for m in _A_RE.finditer(html):
                if len(results) >= limit:
                    break
                href = m.group(1)
                full_url = urljoin(BASE_URL, href)

                # 过滤:确保是视频页面链接(有 slug),排除分类/标签页
                if not re.search(r'/videos/[^/]+', full_url):
                    continue
                # 排除明显的非视频路径
                if any(k in full_url.lower() for k in ['categories', 'tags', 'actors', 'series']):
                    continue
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # 取匹配位置附近 800 字符
                start = max(0, m.start() - 800)
                end = min(len(html), m.end() + 800)
                nearby = html[start:end]

                # 缩略图
                thumb = ""
                for pat in [
                    r'src="(https://[^"]*cdn[^"]*)"',
                    r'src="([^"]*\.(?:jpg|jpeg|png|webp))"',
                    r'data-src="([^"]*)"',
                ]:
                    mm = re.search(pat, nearby, re.S | re.I)
                    if mm:
                        thumb = mm.group(1)
                        break

                if thumb and not thumb.startswith("http"):
                    thumb = urljoin(BASE_URL, thumb)

                # 标题: 优先 img alt, 其次附近文本
                title = ""
                mm = re.search(r'alt="([^"]*)"', nearby, re.S | re.I)
                if mm:
                    title = mm.group(1).strip()
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
