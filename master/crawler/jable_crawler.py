"""jable.tv 视频采集器。

从最新更新页/标签页抓取视频卡片,提取 URL + 标题 + 缩略图。
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
LIST_URLS = [
    "https://jable.tv/latest-updates/",
    "https://jable.tv/categories/censored/",
    "https://jable.tv/categories/uncensored/",
]


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

    results = []
    page = 1

    for base_list_url in LIST_URLS:
        if len(results) >= limit:
            break

        while len(results) < limit and page <= 10:
            url = f"{base_list_url}?page={page}" if page > 1 else base_list_url
            try:
                resp = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
                resp.raise_for_status()
            except Exception:
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # 策略1: 常见卡片容器
            cards = (
                soup.select(".video-img-box")
                or soup.select(".video-box")
                or soup.select(".card")
                or soup.select(".item")
            )

            if cards:
                for card in cards:
                    if len(results) >= limit:
                        break
                    try:
                        item = _parse_card(card)
                        if item:
                            results.append(item)
                    except Exception:
                        continue

            # 策略2: 如果没抓到,找所有 /videos/ 链接
            if not cards:
                links = soup.find_all("a", href=re.compile(r"/videos?/[^/]+/?$"))
                for a in links:
                    if len(results) >= limit:
                        break
                    try:
                        item = _parse_from_a(a)
                        if item:
                            results.append(item)
                    except Exception:
                        continue

            page += 1
            if not cards and not links:
                break

    return results


def _parse_card(card) -> dict | None:
    """解析单个视频卡片。"""
    a = card.find("a", href=True)
    if not a:
        return None
    return _parse_from_a(a)


def _parse_from_a(a) -> dict | None:
    """从 <a> 标签提取视频信息。"""
    href = a.get("href", "")
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)
    if "/videos/" not in href and "/video/" not in href:
        return None

    # 标题
    title = a.get("title", "")
    if not title:
        img = a.find("img")
        if img:
            title = img.get("alt", "")
    if not title:
        for sel in [".title", ".video-title", "h6", "h5", "h3"]:
            el = a.select_one(sel)
            if not el:
                parent = a.find_parent()
                if parent:
                    el = parent.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                break

    # 缩略图
    thumb = ""
    img = a.find("img")
    if img:
        thumb = img.get("data-src") or img.get("data-original") or img.get("src", "")
    if not thumb:
        parent = a.find_parent()
        if parent:
            img2 = parent.find("img")
            if img2:
                thumb = img2.get("data-src") or img2.get("data-original") or img2.get("src", "")
    if thumb and not thumb.startswith("http"):
        thumb = urljoin(BASE_URL, thumb)

    if not title:
        title = "(无标题)"

    return {
        "url": href,
        "title": title,
        "thumbnail_url": thumb,
        "site": "jable.tv",
    }
