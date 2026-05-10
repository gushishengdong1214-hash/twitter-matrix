"""内容过滤层。

自动审批前检查标题/文案是否含敏感词。
命中则拒绝自动审批，标记为 rejected 并记录原因。

使用方式:
    result = check_content(title, caption)
    if result.blocked:
        # 拒绝审批
"""

import re

# 敏感词列表（按严重程度分组）
_BLOCKED_KEYWORDS = [
    # 儿童相关（绝对红线）
    "儿童", "幼女", "萝莉", "loli", "未成年", "teen", "underage",
    "小学生", "中学生", "初中生", "jc", "js",
    # 极端暴力
    "强奸", "轮奸", "凌辱", "虐待", "暴力", "血腥",
    "強制", "強姦", "レイプ", "陵辱",
    # 动物
    "兽交", "人兽", "animal",
    # 毒品
    "毒品", "吸毒", "冰毒",
]

# 警告词（命中后降低分数但不直接拒绝）
_WARNING_KEYWORDS = [
    "偷拍", "盗摄", "隠し撮り",
    "流出", "泄露", "ハメ撮り",
]

_RE_BLOCKED = re.compile(
    "|".join(re.escape(kw) for kw in _BLOCKED_KEYWORDS),
    re.IGNORECASE,
)
_RE_WARNING = re.compile(
    "|".join(re.escape(kw) for kw in _WARNING_KEYWORDS),
    re.IGNORECASE,
)


class FilterResult:
    def __init__(self, blocked: bool, reason: str = "", warning: bool = False):
        self.blocked = blocked
        self.reason = reason
        self.warning = warning

    def __bool__(self):
        return self.blocked


def check_content(title: str = "", caption: str = "") -> FilterResult:
    """检查标题和文案是否含敏感词。

    Returns:
        FilterResult: blocked=True 表示应拒绝审批
    """
    text = f"{title} {caption}"

    # 检查阻断词
    m = _RE_BLOCKED.search(text)
    if m:
        return FilterResult(
            blocked=True,
            reason=f"命中敏感词: {m.group(0)}",
        )

    # 检查警告词
    m = _RE_WARNING.search(text)
    if m:
        return FilterResult(
            blocked=False,
            warning=True,
            reason=f"命中警告词: {m.group(0)}",
        )

    return FilterResult(blocked=False)
