"""
弹窗处理库。中控统一维护这一个文件,推到所有 Worker。

每条规则:
- name:弹窗名称(报告用)
- test:用什么 selector / 文本判断它出现了
- action:出现后干什么(点哪个按钮、按 Esc、或自定义 callable)
- timeout:探测超时(ms)

发现未知弹窗(关键步骤超时):screenshot + dump html + 抛 UnknownPopupError。
中控收到告警后,你看截图 → 在这文件加一条规则 → 推回 Worker → 继续跑。
"""

from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Optional


class UnknownPopupError(Exception):
    def __init__(self, message: str, screenshot_path: str = "", html_path: str = ""):
        super().__init__(message)
        self.screenshot_path = screenshot_path
        self.html_path = html_path


@dataclass
class PopupRule:
    name: str
    test_selector: str
    click_selector: str = ""
    press_key: str = ""
    description: str = ""
    timeout_ms: int = 1500


KNOWN_POPUPS: list[PopupRule] = [
    PopupRule(
        name="confirmation_sheet_confirm",
        test_selector="[data-testid='confirmationSheetConfirm']",
        click_selector="[data-testid='confirmationSheetConfirm']",
        description="通用确认弹窗(敏感内容、首次发推等)",
    ),
    PopupRule(
        name="enable_notifications",
        test_selector="text=Enable notifications",
        press_key="Escape",
        description="开启浏览器通知提示",
    ),
]


def sweep_known_popups(page, log) -> int:
    """扫一遍所有已知弹窗,有则关掉。返回处理了几个。"""
    handled = 0
    for rule in KNOWN_POPUPS:
        try:
            el = page.locator(rule.test_selector).first
            if el.is_visible(timeout=rule.timeout_ms):
                if rule.click_selector:
                    page.locator(rule.click_selector).first.click(timeout=3000)
                elif rule.press_key:
                    page.keyboard.press(rule.press_key)
                log(f"关闭已知弹窗:{rule.name}")
                page.wait_for_timeout(800)
                handled += 1
        except Exception:
            continue
    return handled


def dismiss_all_overlays(page, log, max_rounds: int = 3) -> int:
    """
    主动清场:循环 sweep 已知规则,每轮如果没关掉新的就停。
    不按 Escape — 因为 X 的 compose 编辑器自己也会响应 Escape 而关掉。
    """
    cleared = 0
    for _ in range(max_rounds):
        n = sweep_known_popups(page, log)
        if n == 0:
            break
        cleared += n
        page.wait_for_timeout(400)
    return cleared


def snapshot_unknown(page, screenshot_dir: Path, prefix: str = "popup") -> tuple[str, str]:
    """碰到未知卡住状态:截图 + 存 HTML。返回 (screenshot_path, html_path)。"""
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = screenshot_dir / f"{prefix}_{ts}.png"
    html = screenshot_dir / f"{prefix}_{ts}.html"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:
        pass
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return str(shot), str(html)


def parse_caption_for_mentions(caption: str) -> list[tuple[str, str]]:
    """
    把 caption 拆成 [(kind, value), ...]。
    kind: "text" | "mention"
    例:"看 @alice 的视频 #hot" -> [("text","看 "), ("mention","alice"), ("text"," 的视频 #hot")]
    """
    segments: list[tuple[str, str]] = []
    i, n = 0, len(caption)
    buf = ""
    while i < n:
        ch = caption[i]
        if ch == "@" and (i == 0 or not caption[i - 1].isalnum()):
            j = i + 1
            while j < n and (caption[j].isalnum() or caption[j] == "_"):
                j += 1
            handle = caption[i + 1:j]
            if handle:
                if buf:
                    segments.append(("text", buf))
                    buf = ""
                segments.append(("mention", handle))
                i = j
                continue
        buf += ch
        i += 1
    if buf:
        segments.append(("text", buf))
    return segments


def type_caption_with_mentions(page, textarea_selector: str, caption: str, log):
    """
    分段输入 caption。@xxx 用 keyboard.type 触发 listbox,选匹配项;
    其余文字用 keyboard.type(快但不至于一次 fill 跳过事件)。
    """
    target = page.locator(textarea_selector).first

    def _focus():
        try:
            target.click(timeout=8_000)
            return True
        except Exception:
            try:
                target.click(force=True, timeout=4_000)
                return True
            except Exception:
                # 最后兜底:JS focus(绕过浮层拦截)
                try:
                    page.evaluate(
                        "(sel) => { const el = document.querySelector(sel); if (el) el.focus(); }",
                        textarea_selector,
                    )
                    return True
                except Exception as e:
                    log(f"JS focus 也失败:{e}")
                    return False

    if not _focus():
        raise RuntimeError("无法 focus tweetTextarea_0")
    page.wait_for_timeout(300)

    segments = parse_caption_for_mentions(caption)
    if not any(s[0] == "mention" for s in segments):
        try:
            target.fill(caption)
        except Exception:
            page.keyboard.type(caption, delay=15)
        return

    _focus()
    for kind, value in segments:
        if kind == "text":
            page.keyboard.type(value, delay=20)
        else:
            page.keyboard.type(f"@{value}", delay=80)
            try:
                page.wait_for_selector("[role='listbox']", timeout=4000)
                option = page.locator(
                    f"[role='listbox'] [role='option'] >> text=/^{value}$/i"
                ).first
                if option.is_visible(timeout=1500):
                    option.click()
                else:
                    page.keyboard.press("ArrowDown")
                    page.keyboard.press("Enter")
                log(f"已 @{value}")
            except Exception:
                log(f"@{value} 没等到 listbox,作为纯文本提交")
                page.keyboard.type(" ")
