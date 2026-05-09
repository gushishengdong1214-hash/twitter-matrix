"""下载 + 发推核心。基于单账号脚本改造,加 proxy / popup_handler / 异常驱动 / 浏览器指纹 / 2FA。"""

import json
from pathlib import Path
from typing import Optional

import yt_dlp
from playwright.sync_api import sync_playwright

import popup_handler as pop


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _try_handle_twofa(page, twofa_secret: str, log) -> bool:
    """检测到 2FA 验证页则用 TOTP 填上。返回 True 表示处理过(无论成功)。"""
    selectors = [
        "input[autocomplete='one-time-code']",
        "input[name='challenge_response']",
        "input[data-testid='ocfEnterTextTextInput']",
    ]
    box = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=1500):
                box = loc
                break
        except Exception:
            continue
    if not box:
        return False
    if not twofa_secret:
        log("检测到 2FA 验证页,但未填 twofa_secret")
        return True
    try:
        import pyotp
    except ImportError:
        log("worker 没装 pyotp, 跳过 2FA")
        return True
    try:
        code = pyotp.TOTP(twofa_secret).now()
        box.fill(code)
        for sel in [
            "[data-testid='ocfEnterTextNextButton']",
            "[data-testid='LoginForm_Login_Button']",
            "button[type='submit']",
            "div[role='button']:has-text('Next')",
            "div[role='button']:has-text('Verify')",
        ]:
            btn = page.locator(sel).first
            try:
                if btn.is_visible(timeout=800):
                    btn.click()
                    break
            except Exception:
                continue
        log(f"已自动输入 2FA 验证码 {code}")
        page.wait_for_timeout(4000)
    except Exception as e:
        log(f"2FA 处理失败:{e}")
    return True


def _sniff_m3u8(url: str, pw_proxy: Optional[dict], user_agent: str, log) -> Optional[str]:
    """对 jable.tv 这类站点,用浏览器嗅探真实视频流。"""
    log("打开浏览器嗅探 m3u8...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=pw_proxy)
            context = browser.new_context(user_agent=user_agent)
            page = context.new_page()
            captured: list[str] = []
            page.on(
                "request",
                lambda r: captured.append(r.url) if ".m3u8" in r.url else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(8000)
            browser.close()
            for link in captured:
                if ".m3u8" in link:
                    return link
    except Exception as e:
        log(f"m3u8 嗅探失败:{e}")
    return None


def download_video(
    url: str,
    out_path: Path,
    yt_proxy_url: Optional[str],
    pw_proxy: Optional[dict],
    user_agent: str,
    log,
) -> bool:
    if out_path.exists() and out_path.stat().st_size > 10 * 1024 * 1024:
        log(f"已有完整视频 {out_path}, 跳过下载")
        return True

    real_url = url
    if "jable.tv" in url:
        sniffed = _sniff_m3u8(url, pw_proxy, user_agent, log)
        if sniffed:
            real_url = sniffed
            log(f"嗅探到 m3u8: {sniffed[:60]}...")
        else:
            log("嗅探失败 — yt-dlp 会拒绝 jable.tv 原 URL,直接报错")
            return False

    if out_path.exists():
        out_path.unlink()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(out_path),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": True,
        "concurrent_fragment_downloads": 15,
        "http_headers": {
            "User-Agent": user_agent,
            "Referer": url,
        },
    }
    if yt_proxy_url:
        ydl_opts["proxy"] = yt_proxy_url

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([real_url])
        log(f"视频下载完成 {out_path}")
        return True
    except Exception as e:
        log(f"下载失败:{e}")
        return False


def post_to_twitter(
    config: dict,
    caption: str,
    video_path: Path,
    pw_proxy: Optional[dict],
    screenshot_dir: Path,
    log,
):
    """成功返回 None;碰到未知卡住状态抛 UnknownPopupError。"""
    cookie_raw = config.get("cookie_json", "")
    user_agent = config.get("user_agent") or DEFAULT_UA
    viewport = config.get("viewport") or {"width": 1920, "height": 1080}
    timezone_id = config.get("timezone_id") or "America/New_York"
    locale = config.get("locale") or "en-US"
    twofa_secret = config.get("twofa_secret") or ""

    cookies = []
    if cookie_raw:
        try:
            cookies = _normalize_cookies(json.loads(cookie_raw))
        except Exception as e:
            raise pop.UnknownPopupError(f"cookie 解析失败:{e}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            proxy=pw_proxy,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            user_agent=user_agent,
            viewport=viewport,
            timezone_id=timezone_id,
            locale=locale,
        )
        # 屏蔽 navigator.webdriver = true(playwright 自动化默认会暴露)
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        try:
            page.goto("https://x.com/compose/tweet", timeout=60_000)
            log("已打开发推页面")
            page.wait_for_timeout(2000)

            # 可能跳转到 2FA / 登录页
            if _try_handle_twofa(page, twofa_secret, log):
                page.wait_for_timeout(2000)
                page.goto("https://x.com/compose/tweet", timeout=60_000)
                page.wait_for_timeout(2000)

            pop.dismiss_all_overlays(page, log)

            try:
                page.wait_for_selector(
                    "[data-testid='tweetTextarea_0']", timeout=30_000
                )
            except Exception:
                shot, html = pop.snapshot_unknown(page, screenshot_dir, "no_textarea")
                raise pop.UnknownPopupError(
                    "找不到 tweetTextarea_0(可能未登录、2FA、或弹窗挡住)", shot, html
                )

            try:
                pop.type_caption_with_mentions(
                    page, "[data-testid='tweetTextarea_0']", caption, log
                )
            except Exception as e:
                shot, html = pop.snapshot_unknown(page, screenshot_dir, "type_caption_fail")
                raise pop.UnknownPopupError(
                    f"输入文案失败,可能有未识别的浮层挡住:{e}", shot, html,
                )
            log("文案输入完成")

            pop.dismiss_all_overlays(page, log)
            page.locator("input[data-testid='fileInput']").first.set_input_files(str(video_path))
            log(f"已选择视频文件 {video_path}, 等待 X 处理...")

            pop.dismiss_all_overlays(page, log)

            try:
                page.wait_for_selector(
                    "[data-testid='tweetButton'][aria-disabled='false']",
                    timeout=900_000,
                )
                log("发送按钮已激活")
            except Exception:
                pop.dismiss_all_overlays(page, log)
                btn = page.locator("[data-testid='tweetButton']").first
                disabled = True
                try:
                    disabled = btn.get_attribute("aria-disabled") != "false"
                except Exception:
                    pass
                if disabled:
                    shot, html = pop.snapshot_unknown(
                        page, screenshot_dir, "button_not_active"
                    )
                    raise pop.UnknownPopupError(
                        "发送按钮 15 分钟内未激活,可能卡在弹窗或视频处理失败",
                        shot, html,
                    )

            page.locator("[data-testid='tweetButton']").first.click()
            log("已点击发送")
            page.wait_for_timeout(8_000)
            pop.dismiss_all_overlays(page, log)
            page.wait_for_timeout(5_000)
            log("发推完成")
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _normalize_cookies(raw: list[dict]) -> list[dict]:
    allowed = ["name", "value", "url", "domain", "path",
               "expires", "httpOnly", "secure", "sameSite"]
    out = []
    for c in raw:
        clean = {}
        for k in allowed:
            if k in c:
                if k == "sameSite" and c[k] not in ("Strict", "Lax", "None"):
                    continue
                clean[k] = c[k]
        out.append(clean)
    return out
