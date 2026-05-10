"""hanime1.me 视频采集器。

策略:请求首页,用正则提取所有 href="https://hanime1.me/watch?v=xxx" 链接,
然后在链接附近找缩略图(img src)和标题(div title 或 a title)。
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

        # 步骤1: 暴力提取所有 href="https://hanime1.me/watch?v=数字" 链接
        hrefs = re.findall(r'href="(https://hanime1\.me/watch\?v=\d+)"', html)
        unique_hrefs = []
        for h in hrefs:
            if h not in unique_hrefs:
                unique_hrefs.append(h)

        for href in unique_hrefs:
            if len(results) >= limit:
                break
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # 步骤2: 在链接出现的位置附近找缩略图和标题
            pos = html.find(href)
            nearby = html[max(0, pos - 1200):min(len(html), pos + 1200)]

            # 找缩略图: 附近 img 的 src / data-src
            thumb = ""
            for img_pat in [
                r'<img[^>]*src="(https://vdownload\.hembed\.com[^"]*)"[^>]*>',
                r'<img[^>]*src="([^"]*thumbnail[^"]*)"[^>]*>',
                r'<img[^>]*src="([^"]+)"[^>]*>',
            ]:
                m = re.search(img_pat, nearby, re.S | re.I)
                if m:
                    thumb = m.group(1)
                    break

            if thumb and not thumb.startswith("http"):
                thumb = urljoin(BASE_URL, thumb)

            # 找标题: 优先附近 div title / a title, 其次附近文本
            title = ""
            for title_pat in [
                r'<div[^>]*title="([^"]*)"[^>]*>',
                r'<a[^>]*title="([^"]*)"[^>]*>',
            ]:
                m = re.search(title_pat, nearby, re.S | re.I)
                if m:
                    title = m.group(1).strip()
                    if title:
                        break

            if not title:
                # 兜底: 从附近纯文本中提取一段看起来像标题的文字
                txt_matches = re.findall(r'>([^<]{5,60})<', nearby)
                for t in txt_matches:
                    t = t.strip()
                    if t and not t.startswith(('http', '<', 'div', 'span', 'img')):
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
