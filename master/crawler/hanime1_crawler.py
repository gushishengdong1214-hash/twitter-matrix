"""hanime1.me 视频采集器。

从首页/分类页抓取视频卡片,提取 URL + 标题 + 缩略图。
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

BASE_URL = "https://hanime1.me"
LIST_URLS = [
    "https://hanime1.me/",
    "https://hanime1.me/search?sort=最近更新",
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

    for list_url in LIST_URLS:
        if len(results) >= limit:
            break
        try:
            resp = curl_requests.get(list_url, headers=headers, impersonate="chrome124", timeout=30)
            resp.raise_for_status()
        except Exception as e:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # 策略1: 找所有 /watch?v= 链接
        watch_links = soup.find_all("a", href=re.compile(r"^/watch\?v="))
        if watch_links:
            for a in watch_links:
                if len(results) >= limit:
                    break
                try:
                    item = _parse_from_a_tag(a)
                    if item:
                        results.append(item)
                except Exception:
                    continue

        # 策略2: 如果策略1没抓到,尝试常见卡片容器
        if not results:
            cards = (
                soup.select(".video-item")
                or soup.select(".content-item")
                or soup.select(".video-card")
                or soup.select(".card")
            )
            for card in cards:
                if len(results) >= limit:
                    break
                try:
                    item = _parse_card_container(card)
                    if item:
                        results.append(item)
                except Exception:
                    continue

    return results


def _parse_from_a_tag(a) -> dict | None:
    """从 <a href='/watch?v=xxx'> 标签及周围提取信息。"""
    href = a.get("href", "")
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)

    # 标题: 优先 <a> 的 title,其次内部 img 的 alt
    title = a.get("title", "")
    thumb = ""

    img = a.find("img")
    if img:
        if not title:
            title = img.get("alt", "")
        thumb = img.get("data-src") or img.get("data-original") or img.get("src", "")
        if thumb and not thumb.startswith("http"):
            thumb = urljoin(BASE_URL, thumb)

    # 如果 <a> 里没 img,可能是文字链接,尝试附近找图
    if not thumb:
        parent = a.find_parent()
        if parent:
            img2 = parent.find("img")
            if img2:
                thumb = img2.get("data-src") or img2.get("data-original") or img2.get("src", "")
                if thumb and not thumb.startswith("http"):
                    thumb = urljoin(BASE_URL, thumb)
            # 标题也可能在兄弟元素里
            if not title:
                for sel in [".title", ".video-title", "h3", "h4", "h5", ".name"]:
                    el = parent.select_one(sel)
                    if el:
                        title = el.get_text(strip=True)
                        break

    if not title:
        title = "(无标题)"

    return {
        "url": href,
        "title": title,
        "thumbnail_url": thumb,
        "site": "hanime1.me",
    }


def _parse_card_container(card) -> dict | None:
    """从卡片容器解析。"""
    a = card.find("a", href=re.compile(r"/watch\?v="))
    if not a:
        return None
    return _parse_from_a_tag(a)
