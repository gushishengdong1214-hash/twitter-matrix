"""视频采集模块。

统一入口: crawl_site(site_name, limit=10) -> list[dict]
"""
from .jable_crawler import crawl as _crawl_jable
from .hanime1_crawler import crawl as _crawl_hanime1

_CRAWLERS = {
    "jable.tv": _crawl_jable,
    "hanime1.me": _crawl_hanime1,
}

SUPPORTED_SITES = list(_CRAWLERS.keys())


def crawl_site(site_name: str, limit: int = 10):
    """从指定站点采集视频。
    返回 list[dict(url, title, thumbnail_url, site)]
    """
    fn = _CRAWLERS.get(site_name)
    if not fn:
        raise ValueError(f"不支持的站点: {site_name}")
    return fn(limit=limit)
