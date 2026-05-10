"""hanime1.me 视频采集器 — BeautifulSoup 版。"""
import re
import unicodedata
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

            # 标题提取策略：
            # 1) <a title="...">
            # 2) <img alt="...">
            # 3) 祖父级 div title
            # 4) a.text 去掉时长/thumb_up/百分比/观看数前缀
            title = ""
            if a.get("title"):
                title = a.get("title").strip()
            if not title and img and img.get("alt"):
                title = img.get("alt").strip()
            if not title:
                parent = a.find_parent()
                if parent and parent.get("title"):
                    title = parent.get("title").strip()
            if not title:
                # hanime1 的 a.text 包含 "时长thumb_up百分比观看数标题"
                # 例: "06:01thumb_up100%141👁TRIGGER GRITY CINEMA 4K"
                txt = a.get_text(strip=True)
                # 先尝试直接匹配 thumb_up + 百分比 + 数字 + 标题
                m = re.search(r'thumb_up\s*\d+%\s*\d+\s*(.+)', txt, re.IGNORECASE)
                if m:
                    title = m.group(1).strip()
                else:
                    # fallback: 逐步去掉已知前缀
                    title = re.sub(r'^\d{1,2}:\d{2}\s*', '', txt)
                    title = re.sub(r'thumb_up\s*\d+%', '', title, flags=re.IGNORECASE)
                    title = re.sub(r'^\d+\s*', '', title)
                    # 去掉开头的 emoji/特殊符号
                    title = re.sub(r'^[☀-➿　-〿\s]+', '', title)
                    # 去掉 hanime1 特有的观看次数前缀：.9萬次 / 萬次 / 次
                    title = re.sub(r'^\.?\d+萬次', '', title)
                    title = re.sub(r'^萬次', '', title)
                    title = re.sub(r'^次', '', title)
                    title = title.strip()
            if not title:
                title = "(无标题)"

            # 统一清理：去掉开头 emoji/符号/空格
            while title and unicodedata.category(title[0]) in ('So', 'Sc', 'Sk', 'Sm', 'Zs', 'Cc', 'Cf', 'Co'):
                title = title[1:]
            # 去掉观看次数前缀
            title = re.sub(r'^\.?\d+萬次', '', title)
            title = re.sub(r'^萬次', '', title)
            title = re.sub(r'^次', '', title)
            title = title.strip()

            results.append({
                "url": href,
                "title": title,
                "thumbnail_url": thumb,
                "site": "hanime1.me",
            })

    return results
