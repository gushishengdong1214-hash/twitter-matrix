"""jable.tv 视频采集器。

列表页解析:从最新更新页抓取视频卡片,提取 URL + 标题 + 缩略图。
"""
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
LIST_URL = "https://jable.tv/latest-updates/"


def crawl(limit: int = 10) -> list[dict]:
    """返回视频列表,每项包含 url / title / thumbnail_url / site。"""
    if curl_requests is None:
        raise RuntimeError("curl_cffi 未安装,无法绕过 Cloudflare")
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 未安装")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    # 先试最新更新页;如果页数不够再翻页
    results = []
    page = 1
    while len(results) < limit and page <= 10:
        url = f"{LIST_URL}?page={page}" if page > 1 else LIST_URL
        try:
            resp = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"请求 jable.tv 失败: {e}")

        soup = BeautifulSoup(resp.text, "html.parser")

        # jable 视频卡片常见结构
        cards = soup.select(".video-img-box") or soup.select(".video-box") or soup.select(".card")
        if not cards:
            # 兜底:用正则找视频链接
            cards = _fallback_parse(resp.text)

        for card in cards:
            if len(results) >= limit:
                break
            try:
                item = _parse_card(card)
                if item and item.get("url"):
                    results.append(item)
            except Exception:
                continue

        page += 1
        if not cards:
            break

    return results


def _parse_card(card) -> dict | None:
    """解析单个视频卡片。"""
    # 找链接
    a = card.find("a", href=True)
    if not a:
        return None
    href = a["href"]
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)
    if "/videos/" not in href and "/video/" not in href:
        return None

    # 找标题
    title = ""
    for sel in [".title", "h6", "h5", ".video-title", "img"]:
        el = card.select_one(sel)
        if el:
            title = el.get_text(strip=True) or el.get("alt", "")
            if title:
                break

    # 找缩略图
    thumb = ""
    img = card.find("img")
    if img:
        thumb = img.get("data-src") or img.get("data-original") or img.get("src", "")
        if thumb and not thumb.startswith("http"):
            thumb = urljoin(BASE_URL, thumb)

    return {
        "url": href,
        "title": title,
        "thumbnail_url": thumb,
        "site": "jable.tv",
    }


def _fallback_parse(html: str) -> list:
    """兜底:用正则找视频卡片区域。"""
    # 简单提取所有 /videos/xxx 链接 + 附近文本
    pattern = re.compile(r'href="([^"]*\/videos\/[^"]+)"[^>]*>\s*<img[^>]*(?:data-src|src)="([^"]+)"[^>]*(?:alt|title)="([^"]*)"')
    items = []
    for m in pattern.finditer(html):
        href, thumb, title = m.groups()
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        if not thumb.startswith("http"):
            thumb = urljoin(BASE_URL, thumb)
        items.append({
            "url": href,
            "title": title,
            "thumbnail_url": thumb,
            "site": "jable.tv",
        })
    return items
