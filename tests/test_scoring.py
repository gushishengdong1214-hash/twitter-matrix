"""质量评分算法测试。"""

from scoring import score_video


class TestScoreVideo:
    def test_best_duration(self):
        """3-15 分钟应得最高时长分。"""
        item = {
            "url": "https://example.com",
            "title": "Title with 10 minutes",
            "thumbnail_url": "",
            "site": "jable.tv",
        }
        score = score_video(item)
        assert 25 <= score <= 100  # 站点 15 + 时长 30 = 45 起步

    def test_high_click_keywords(self):
        """高点击词加分。"""
        item = {
            "url": "https://example.com",
            "title": "最新高清完整版 JUL-123",
            "thumbnail_url": "",
            "site": "jable.tv",
        }
        score = score_video(item)
        assert score >= 55  # 站点 15 + 时长 15 + 关键词 30 = 60

    def test_low_quality_keywords(self):
        """低质量词扣分。"""
        item = {
            "url": "https://example.com",
            "title": "sample trailer PV preview",
            "thumbnail_url": "",
            "site": "jable.tv",
        }
        score = score_video(item)
        assert score <= 40  # 关键词被扣 45 分

    def test_thumbnail_quality(self):
        """HD 缩略图加分。"""
        item = {
            "url": "https://example.com",
            "title": "Title",
            "thumbnail_url": "https://example.com/thumb_hd.jpg",
            "site": "jable.tv",
        }
        score = score_video(item)
        # 缩略图 15 = 有图 10 + hd 5
        assert score >= 35  # 站点 15 + 缩略图 15 = 30, 再加时长和标题长度

    def test_title_length(self):
        """15-60 字符标题得满分。"""
        item = {
            "url": "https://example.com",
            "title": "A" * 30,
            "thumbnail_url": "",
            "site": "jable.tv",
        }
        score = score_video(item)
        assert score >= 30  # 站点 15 + 标题长度 10 = 25, 加时长

    def test_empty_title(self):
        """空标题得低分。"""
        item = {
            "url": "https://example.com",
            "title": "",
            "thumbnail_url": "",
            "site": "jable.tv",
        }
        score = score_video(item)
        assert score <= 30
