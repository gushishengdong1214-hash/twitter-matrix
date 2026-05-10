"""视频质量评分算法。

给 crawled_videos 打分(0-100),维度:
- 时长推断(标题含数字+"分"/"min")
- 标题高点击关键词
- 缩略图质量
- 标题长度
- 站点信誉

使用方式:
    score = score_video(item)  # item 是 crawled_videos 的一条记录
"""

import re

# 高点击关键词(日文成人内容)
_HIGH_CLICK_KEYWORDS = [
    "最新", "新作", "高清", "hd", "full", "完整",
    "無修正", "无码", "uncensored",
    "中出", "中出し",
    "人妻", "熟女", "jk", "女子高生",
    "痴女", "巨乳", "美乳", "美少女",
    "デビュー", "出道",
    "初撮", "初拍",
    "juq", "jul", "midv", "ssis", "ipx", "mide", "star",
]

# 低质量关键词(自动扣分)
_LOW_QUALITY_KEYWORDS = [
    "预告", "trailer", "pv", "sample", "サンプル",
    "cm", "广告", "ad",
]


def _extract_duration(title: str) -> int | None:
    """从标题推断视频时长(分钟)。"""
    if not title:
        return None
    # 匹配 "123分" "123 min" "123min" "123:45"
    for pat in [r"(\d{1,3})\s*分", r"(\d{1,3})\s*min", r"(\d{1,3}):\d{2}"]:
        m = re.search(pat, title.lower())
        if m:
            return int(m.group(1))
    return None


def _score_duration(title: str) -> int:
    """时长评分(0-30)。"""
    minutes = _extract_duration(title)
    if minutes is None:
        return 15  # 未知时长,给中等分
    if 3 <= minutes <= 15:
        return 30  # 最佳时长
    if 15 < minutes <= 30:
        return 25
    if 30 < minutes <= 60:
        return 20
    if 1 <= minutes < 3:
        return 10  # 太短
    return 5  # 太长或异常


def _score_keywords(title: str) -> int:
    """标题关键词评分(0-30)。"""
    if not title:
        return 0
    t = title.lower()
    score = 0
    for kw in _HIGH_CLICK_KEYWORDS:
        if kw.lower() in t:
            score += 10
    for kw in _LOW_QUALITY_KEYWORDS:
        if kw.lower() in t:
            score -= 15
    return max(0, min(30, score))


def _score_thumbnail(thumbnail_url: str) -> int:
    """缩略图评分(0-15)。"""
    if not thumbnail_url:
        return 0
    score = 10  # 有缩略图基础分
    url_lower = thumbnail_url.lower()
    if any(k in url_lower for k in ["hd", "high", "1080", "large"]):
        score += 5
    return score


def _score_title_length(title: str) -> int:
    """标题长度评分(0-10)。"""
    if not title:
        return 0
    length = len(title)
    if 15 <= length <= 60:
        return 10
    if 10 <= length < 15 or 60 < length <= 100:
        return 7
    return 5


def _score_site(site: str) -> int:
    """站点信誉评分(0-15)。"""
    trusted = {"jable.tv", "hanime1.me"}
    if site and site.lower() in trusted:
        return 15
    return 5


def score_video(item: dict) -> int:
    """给一条 crawled_videos 记录打分(0-100)。

    Args:
        item: dict(url, title, thumbnail_url, site, ...)
    Returns:
        0-100 的整数分数
    """
    title = item.get("title") or ""
    thumbnail = item.get("thumbnail_url") or ""
    site = item.get("site") or ""

    total = (
        _score_duration(title)
        + _score_keywords(title)
        + _score_thumbnail(thumbnail)
        + _score_title_length(title)
        + _score_site(site)
    )
    return min(100, max(0, total))
