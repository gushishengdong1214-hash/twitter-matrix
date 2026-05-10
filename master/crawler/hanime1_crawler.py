"""hanime1.me 视频采集器。

从首页抓取视频卡片,提取 URL + 标题 + 缩略图。
"""
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
LIST_URL = "https://hanime1.me/"


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
    while len(results) < limit and page <= 10:
        url = f"{LIST_URL}?page={page}" if page > 1 else LIST_URL
        try:
            resp = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"请求 hanime1.me 失败: {e}")

        soup = BeautifulSoup(resp.text, "html.parser")

        # hanime1 常见卡片选择器
        cards = (
            soup.select(".home-rows-videos-wrapper > a")
            or soup.select(".video-item")
            or soup.select(".card")
            or soup.select("a[href^='/watch']")
        )

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
    # card 可能是 <a> 标签本身,也可能是包含 <a> 的容器
    a = card if card.name == "a" else card.find("a", href=True)
    if not a:
        return None

    href = a.get("href", "")
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)
    if "/watch" not in href:
        return None

    # 标题:可能直接是 <a> 的 title,或内部 img 的 alt,或 nearby text
    title = a.get("title", "")
    if not title:
        img = a.find("img")
        if img:
            title = img.get("alt", "")
    if not title:
        # 尝试附近文本
        for sel in [".video-title", "h3", "h4", ".title"]:
            el = a.select_one(sel) if hasattr(a, "select_one") else None
            if el:
                title = el.get_text(strip=True)
                break

    # 缩略图
    thumb = ""
    img = a.find("img") if hasattr(a, "find") else None
    if img:
        thumb = img.get("data-src") or img.get("data-original") or img.get("src", "")
        if thumb and not thumb.startswith("http"):
            thumb = urljoin(BASE_URL, thumb)

    return {
        "url": href,
        "title": title,
        "thumbnail_url": thumb,
        "site": "hanime1.me",
    }
