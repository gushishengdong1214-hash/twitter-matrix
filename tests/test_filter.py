"""内容过滤层测试。"""

import pytest
from filter import check_content


class TestCheckContent:
    def test_blocked_child_related(self):
        """儿童相关关键词应阻断。"""
        result = check_content(title="萝莉福利")
        assert result.blocked
        assert "萝莉" in result.reason

    def test_blocked_violence(self):
        """暴力关键词应阻断。"""
        result = check_content(title="强奸视频")
        assert result.blocked
        assert "强奸" in result.reason

    def test_blocked_japanese(self):
        """日文敏感词应阻断。"""
        result = check_content(title="強姦レイプ")
        assert result.blocked

    def test_warning_spy(self):
        """偷拍类应标记警告但不阻断。"""
        result = check_content(title="偷拍系列")
        assert not result.blocked
        assert result.warning
        assert "偷拍" in result.reason

    def test_safe_content(self):
        """正常内容应通过。"""
        result = check_content(title="正常成人视频", caption="这是正常内容")
        assert not result.blocked
        assert not result.warning

    def test_case_insensitive(self):
        """大小写不敏感。"""
        result = check_content(title="LOLI Video")
        assert result.blocked

    def test_caption_also_checked(self):
        """文案也应检查。"""
        result = check_content(title="正常标题", caption="幼女视频")
        assert result.blocked

    def test_empty_content(self):
        """空内容应通过。"""
        result = check_content()
        assert not result.blocked

    def test_jable_title_with_id(self):
        """带番号的正常标题应通过。"""
        result = check_content(title="IPZZ-827 正常标题")
        assert not result.blocked
