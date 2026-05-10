"""下载 + 发推核心。基于单账号脚本改造,加 proxy / popup_handler / 异常驱动 / 浏览器指纹 / 2FA。"""

import json
import random
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
    """对 jable.tv / hanime1.me 这类站点,用浏览器嗅探真实视频流。

    多层策略,按优先级:
      1) 主策略:打开页面后,从 DOM 里读主 <video> 元素的 currentSrc / src(若是 m3u8 URL 直接命中)。
         注:jable 用 HLS.js,video.src 通常是 blob,这层会 miss → 走下面
      2) 兜底 1:抓所有 m3u8 请求,排除预览/广告/缩略图等 URL 模式后,
         只剩 1 个就用它;有多个就 GET 一遍 m3u8 内容,选 #EXTINF segment 数最多的
         (主视频通常 segment 数 >> 推荐位预览)
      3) 兜底 2:若以上都失败,fallback 到旧逻辑(返回排除后的第一个)
    """
    log("打开浏览器嗅探 m3u8...")
    # 详情页常见的非主视频 m3u8 模式 — 主要排除推荐位 hover preview / 广告 / 缩略图
    EXCLUDE_PATTERNS = [
        "/preview", "/trailer", "/thumb", "/thumbnail",
        "/ad/", "/ads/", "preview.m3u8", "/sprite",
    ]

    def is_excluded(u: str) -> bool:
        ul = u.lower()
        return any(pat in ul for pat in EXCLUDE_PATTERNS)

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
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(8000)

                # 主策略:DOM 里找主 video 元素的 m3u8 src
                try:
                    video_src = page.evaluate(
                        """() => {
                            const vs = document.querySelectorAll('video');
                            for (const v of vs) {
                                const s = v.currentSrc || v.src || '';
                                if (s && s.includes('.m3u8')) return s;
                            }
                            return null;
                        }"""
                    )
                    if video_src:
                        log(f"主策略:从 <video>.currentSrc 命中 m3u8")
                        return video_src
                except Exception as e:
                    log(f"主策略读 video 元素失败:{e}")

                # 兜底 1:过滤掉已知非主视频模式
                unique = list(dict.fromkeys(captured))  # 去重保序
                filtered = [u for u in unique if not is_excluded(u)]
                log(f"嗅到 {len(unique)} 个 m3u8,排除预览/广告/缩略图后剩 {len(filtered)} 个")

                if not filtered:
                    log("所有 m3u8 都被排除,放弃")
                    return None

                if len(filtered) == 1:
                    return filtered[0]

                # 多个候选:GET 每个 m3u8,选 #EXTINF segment 数最多的
                best = None
                best_count = -1
                for m3u8_url in filtered:
                    try:
                        resp = page.request.get(m3u8_url, timeout=10_000)
                        if not resp.ok:
                            continue
                        text = resp.text()
                        seg_count = text.count("#EXTINF")
                        if seg_count == 0:
                            # 可能是 master playlist,粗略计数 .ts / 行数
                            seg_count = max(text.count(".ts"), len(text.splitlines()))
                        if seg_count > best_count:
                            best_count = seg_count
                            best = m3u8_url
                    except Exception:
                        continue

                if best:
                    log(f"次兜底:多候选,选 segment 数最多 ({best_count}) 的 m3u8")
                    return best

                # 最后兜底:返回排除后的第一个
                log("兜底 2:返回排除后的第一个 m3u8")
                return filtered[0]
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
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
    # 通用策略:先用浏览器嗅 m3u8(过 Cloudflare 等反爬),嗅到就用 m3u8 给 yt-dlp,
    # 没嗅到再 fallback 给 yt-dlp 原 URL(yt-dlp 内置支持很多站)。
    # jable.tv 特殊:嗅不到就直接放弃,因为 yt-dlp 会拒绝它的页面 URL。
    real_url = url
    sniffed = _sniff_m3u8(url, pw_proxy, user_agent, log)
    if sniffed:
        real_url = sniffed
        log(f"嗅探到 m3u8: {sniffed[:60]}...")
    elif "jable.tv" in url:
        log("jable.tv 嗅不到 m3u8,yt-dlp 会拒绝其页面,放弃")
        return False
    else:
        log("未嗅到 m3u8,fallback 给 yt-dlp 直连原 URL")

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
    # 只在 fallback(嗅不到 m3u8,直接下原 URL)时启用 impersonate 过 Cloudflare;
    # 嗅到 m3u8 后下 segments 是无状态的,不需要 impersonate(且会因为版本名问题报错)
    if real_url == url:
        try:
            ydl_opts["impersonate"] = "chrome-124"
        except Exception:
            pass
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
            log(f"已选择视频文件 {video_path}")

            # 等附件占位出现
            try:
                page.wait_for_selector(
                    "[data-testid='attachments']", timeout=120_000
                )
                log("已附加视频(看到 attachments 容器)")
            except Exception:
                log("没等到 attachments selector,继续")

            pop.dismiss_all_overlays(page, log)

            # 真正的等待:button 的 disabled 属性消失
            # X 的发推按钮视觉上始终是黑色,但 DOM 里 disabled 真实存在,
            # disabled 阻止 React 处理 onClick,即使 force click 也不会触发后端。
            # 必须等 X 后台处理完视频(disabled 消失)才能 click。
            log("等待 X 后台处理视频(等按钮 disabled 消失,最多 30 分钟)...")
            try:
                page.wait_for_selector(
                    "[data-testid='tweetButton']:not([disabled]), "
                    "[data-testid='tweetButtonInline']:not([disabled])",
                    timeout=1800_000,  # 30 分钟
                )
                log("按钮 enabled,X 准备好发送")
            except Exception:
                pop.dismiss_all_overlays(page, log)
                shot, html = pop.snapshot_unknown(
                    page, screenshot_dir, "button_disabled_30min"
                )
                raise pop.UnknownPopupError(
                    "30 分钟内 Post 按钮仍 disabled,X 可能在审核或拒绝该视频",
                    shot, html,
                )

            # 模拟人类反应延迟 5-15 秒(真人看到按钮亮起也会顿一下再点)
            human_delay_ms = random.randint(5_000, 15_000)
            log(f"模拟人类延迟 {human_delay_ms/1000:.1f} 秒后点发送")
            page.wait_for_timeout(human_delay_ms)
            page.locator(
                "[data-testid='tweetButton']:not([disabled]), "
                "[data-testid='tweetButtonInline']:not([disabled])"
            ).first.click(timeout=10_000)
            log("已点击发送")

            # 等推文真正发出去
            page.wait_for_timeout(8_000)
            pop.dismiss_all_overlays(page, log)
            page.wait_for_timeout(8_000)
            log("发推完成")
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
