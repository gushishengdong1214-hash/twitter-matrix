"""文案翻译封装。用 deep-translator 免费调用 Google Translate。"""

try:
    from deep_translator import GoogleTranslator
    _TRANSLATOR = GoogleTranslator(source="auto", target="zh-CN")
except Exception:
    _TRANSLATOR = None


def translate(text: str, max_len: int = 280) -> str:
    """把文本翻译成中文。失败时返回原文。
    推文有 280 字限制,这里只截断翻译结果,不处理原文。
    """
    if not text or not text.strip():
        return ""
    if _TRANSLATOR is None:
        return text
    try:
        result = _TRANSLATOR.translate(text.strip())
        if result and len(result) > max_len:
            result = result[:max_len - 3] + "..."
        return result or text
    except Exception:
        return text
