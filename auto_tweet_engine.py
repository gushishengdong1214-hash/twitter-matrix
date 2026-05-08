"""推特矩阵搬运系统 V2.0 - 底层引擎

核心能力：
1. download_video() — 双模式抓取
   - regular: yt-dlp 直拉，支持 best / mp4 / 720p / 1080p 等格式偏好
   - sniff:   Playwright 无头浏览器拦截 .m3u8 网络请求，再交给 yt-dlp
2. post_to_twitter() — 发推流程
   - cookie 注入登录、写入文案、上传视频、动态等待转码、发送、结果校验
   - upload_wait_time_minutes 控制等待转码超时
两个函数都接收 log_callback(msg) 实时把每一步进度推回上层（worker -> DB），
返回 (success: bool, message: str) 双元组，便于队列调度器统一处理结果。
"""
import os
import time
import traceback

from playwright.sync_api import sync_playwright
import yt_dlp

# 兼容 playwright_stealth 旧/新两套 API
try:
    from playwright_stealth import stealth_sync as _stealth_sync_fn

    def _apply_stealth(page):
        try:
            _stealth_sync_fn(page)
        except Exception:
            pass
except Exception:  # 新版本 API
    try:
        from playwright_stealth import Stealth as _Stealth

        def _apply_stealth(page):
            try:
                _Stealth().apply_stealth_sync(page)
            except Exception:
                pass
    except Exception:
        def _apply_stealth(page):
            return  # stealth 不可用时静默跳过


VIDEO_TEMP = "temp_video.mp4"
SUPPORTED_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ts")


# ============================================================
# 工具
# ============================================================
def _build_proxy_url(proxy_config):
    user = (proxy_config.get("user") or "").strip()
    pwd = (proxy_config.get("pass") or "").strip()
    host = proxy_config["host"]
    port = proxy_config["port"]
    if user and pwd:
        return f"socks5://{user}:{pwd}@{host}:{port}"
    return f"socks5://{host}:{port}"


def _build_pw_proxy(proxy_config):
    """Playwright 代理配置。注意：Playwright 对 SOCKS5 认证支持有限，
    若代理启用了用户名/密码鉴权，建议改用 HTTP 代理。"""
    proxy = {
        "server": f"socks5://{proxy_config['host']}:{proxy_config['port']}",
    }
    user = (proxy_config.get("user") or "").strip()
    pwd = (proxy_config.get("pass") or "").strip()
    if user:
        proxy["username"] = user
        proxy["password"] = pwd
    return proxy


def _format_string(video_format):
    fmt_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "mp4": "best[ext=mp4]/best",
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
    }
    return fmt_map.get(video_format, fmt_map["best"])


def _log(callback, msg):
    if callback:
        try:
            callback(msg)
        except Exception:
            pass
    print(msg, flush=True)


def _purge_temp():
    """清理上一轮残留产物（含 .part / 不同扩展名）。"""
    candidates = [VIDEO_TEMP, VIDEO_TEMP + ".part"]
    for ext in SUPPORTED_VIDEO_EXTS:
        candidates.append("temp_video" + ext)
        candidates.append("temp_video" + ext + ".part")
    for path in candidates:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def _normalize_output_filename():
    """yt-dlp 偶尔会按源格式落盘（temp_video.webm 等），统一改名为 temp_video.mp4。"""
    if os.path.exists(VIDEO_TEMP) and os.path.getsize(VIDEO_TEMP) > 0:
        return True
    for ext in SUPPORTED_VIDEO_EXTS:
        candidate = "temp_video" + ext
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            try:
                if os.path.exists(VIDEO_TEMP):
                    os.remove(VIDEO_TEMP)
                os.rename(candidate, VIDEO_TEMP)
                return True
            except Exception:
                pass
    return False


# ============================================================
# yt-dlp 直接抓取
# ============================================================
def _ytdlp_download(url, proxy_config, user_agent, video_format, log, referer=None):
    proxy_url = _build_proxy_url(proxy_config)
    ydl_opts = {
        "outtmpl": VIDEO_TEMP,
        "format": _format_string(video_format),
        "merge_output_format": "mp4",
        "proxy": proxy_url,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "http_headers": {
            "User-Agent": user_agent,
            "Referer": referer or url,
        },
    }
    log(f"[yt-dlp] format={video_format} 开始抓取...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    if not _normalize_output_filename():
        raise FileNotFoundError("yt-dlp 已结束但未生成视频文件")
    return True


# ============================================================
# Playwright 深度嗅探 m3u8
# ============================================================
def _sniff_m3u8(page_url, proxy_config, user_agent, log, wait_seconds=25):
    log("[嗅探] 启动 Playwright 无头浏览器（带代理 + UA）...")
    captured = {"url": None}

    def is_video_stream(url):
        u = url.split("?", 1)[0].lower()
        return u.endswith(".m3u8") or u.endswith(".mpd") or "/manifest" in u or ".m3u8?" in url.lower()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=_build_pw_proxy(proxy_config),
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=user_agent)
        page = context.new_page()
        _apply_stealth(page)

        def on_request(req):
            if captured["url"] is not None:
                return
            try:
                if is_video_stream(req.url):
                    captured["url"] = req.url
                    log(f"[嗅探] 命中视频流: {req.url[:140]}{'...' if len(req.url) > 140 else ''}")
            except Exception:
                pass

        page.on("request", on_request)

        try:
            log(f"[嗅探] 打开页面: {page_url[:120]}")
            page.goto(page_url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log(f"[嗅探] 页面加载警告（不致命）: {e}")

        # 触发可能的延迟加载（点击播放、播放第一个 video 标签）
        try:
            page.wait_for_timeout(1500)
            page.evaluate(
                """() => {
                    document.querySelectorAll('video').forEach(v => {
                        try { v.muted = true; v.play(); } catch(e){}
                    });
                }"""
            )
        except Exception:
            pass

        deadline = time.time() + wait_seconds
        while time.time() < deadline and captured["url"] is None:
            page.wait_for_timeout(500)

        try:
            browser.close()
        except Exception:
            pass

    if captured["url"] is None:
        raise RuntimeError(f"在 {wait_seconds}s 内未拦截到 m3u8/mpd 流")

    return captured["url"]


# ============================================================
# 对外接口 1：下载视频
# ============================================================
def download_video(url, proxy_config, user_agent,
                   parse_mode="regular", video_format="best",
                   log_callback=None):
    """下载视频。
    Args:
        url: 视频源 URL
        proxy_config: {"host", "port", "user", "pass"}
        user_agent: 浏览器 UA
        parse_mode: "regular" yt-dlp 直接 / "sniff" 深度嗅探 m3u8
        video_format: "best" / "mp4" / "720p" / "1080p"
        log_callback: callable(msg) 实时进度推送
    Returns:
        (success: bool, message: str)
    """
    def log(m):
        _log(log_callback, m)

    log(f"=== 视频抓取开始 (mode={parse_mode}, fmt={video_format}) ===")
    log(f"目标 URL: {url[:140]}{'...' if len(url) > 140 else ''}")

    _purge_temp()

    try:
        if parse_mode == "sniff":
            log("步骤 1/2: 深度嗅探页面网络请求...")
            try:
                stream_url = _sniff_m3u8(url, proxy_config, user_agent, log)
            except Exception as sniff_err:
                log(f"⚠ 深度嗅探失败：{sniff_err}")
                log("→ 回退到常规 yt-dlp 模式继续尝试...")
                _ytdlp_download(url, proxy_config, user_agent, video_format, log)
            else:
                log("步骤 2/2: yt-dlp 拉取嗅探到的流地址...")
                _ytdlp_download(stream_url, proxy_config, user_agent, video_format, log, referer=url)
        else:
            log("步骤 1/1: yt-dlp 直接解析下载...")
            _ytdlp_download(url, proxy_config, user_agent, video_format, log)

        if not (os.path.exists(VIDEO_TEMP) and os.path.getsize(VIDEO_TEMP) > 0):
            raise FileNotFoundError(f"流程结束但 {VIDEO_TEMP} 不存在或为空文件")

        size_mb = os.path.getsize(VIDEO_TEMP) / 1024 / 1024
        log(f"✓ 视频下载完成 — {VIDEO_TEMP} ({size_mb:.2f} MB)")
        return True, f"下载成功 ({size_mb:.2f} MB)"

    except yt_dlp.utils.DownloadError as e:
        msg = f"yt-dlp 解析/下载失败: {e}"
        log(f"✗ {msg}")
        return False, msg
    except FileNotFoundError as e:
        msg = f"输出文件未生成: {e}"
        log(f"✗ {msg}")
        return False, msg
    except Exception as e:
        msg = f"下载阶段未预期异常: {type(e).__name__}: {e}"
        log(f"✗ {msg}")
        log(traceback.format_exc(limit=3))
        return False, msg


# ============================================================
# 对外接口 2：发送推文
# ============================================================
def post_to_twitter(caption, proxy_config, user_agent, cookie_data,
                    upload_wait_time_minutes=15, log_callback=None):
    """发布带视频的推文。
    Args:
        caption: 推文文案（支持 \\n 换行）
        proxy_config: {"host", "port", "user", "pass"}
        user_agent: 浏览器 UA
        cookie_data: Playwright cookies 数组（list[dict]）
        upload_wait_time_minutes: 等待视频转码超时（分钟）
        log_callback: callable(msg) 实时进度推送
    Returns:
        (success: bool, message: str)
    """
    def log(m):
        _log(log_callback, m)

    if not os.path.exists(VIDEO_TEMP) or os.path.getsize(VIDEO_TEMP) == 0:
        msg = f"待发送视频 {VIDEO_TEMP} 不存在或为空，无法发推"
        log(f"✗ {msg}")
        return False, msg

    upload_wait_ms = max(60_000, int(upload_wait_time_minutes) * 60 * 1000)
    log(f"=== 开始发布推文 (转码等待上限: {upload_wait_time_minutes} 分钟) ===")

    browser = None
    try:
        with sync_playwright() as p:
            log("步骤 1/9: 启动 Playwright 浏览器（代理已配置）...")
            browser = p.chromium.launch(
                headless=True,
                proxy=_build_pw_proxy(proxy_config),
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(user_agent=user_agent)

            log("步骤 2/9: 注入登录 Cookie...")
            try:
                context.add_cookies(cookie_data)
            except Exception as e:
                raise RuntimeError(f"Cookie 注入失败（格式不兼容？）: {e}")

            page = context.new_page()
            _apply_stealth(page)

            log("步骤 3/9: 打开 X 推文撰写页面...")
            page.goto("https://x.com/compose/tweet", timeout=60000)

            log("步骤 4/9: 等待文案输入框（验证登录态）...")
            try:
                page.wait_for_selector("[data-testid='tweetTextarea_0']", timeout=30000)
            except Exception:
                cur = page.url
                if "login" in cur or "i/flow" in cur or "logout" in cur:
                    raise RuntimeError("登录态失败 — Cookie 已失效或被风控，请重新导出 Cookie")
                raise RuntimeError(f"未找到推文输入框，当前 URL={cur}")
            log("✓ 登录验证通过")

            log("步骤 5/9: 写入文案...")
            page.fill("[data-testid='tweetTextarea_0']", caption.replace("\\n", "\n"))
            page.wait_for_timeout(800)
            page.keyboard.press("Space")

            log("步骤 6/9: 上传本地视频文件...")
            try:
                page.set_input_files("input[data-testid='fileInput']", VIDEO_TEMP)
            except Exception as e:
                raise RuntimeError(f"视频上传组件未找到或上传失败: {e}")

            log(f"步骤 7/9: 等待平台转码（最长 {upload_wait_time_minutes} 分钟）...")
            try:
                page.wait_for_selector(
                    "[data-testid='tweetButton'][aria-disabled='false']",
                    timeout=upload_wait_ms,
                )
            except Exception:
                raise RuntimeError(f"等待转码超时（{upload_wait_time_minutes} 分钟）— 视频太大或网络抖动")
            log("✓ 视频转码完成，发送按钮已激活")

            log("步骤 8/9: 点击发送按钮...")
            page.click("[data-testid='tweetButton']", force=True)
            page.wait_for_timeout(20000)

            log("步骤 9/9: 校验发布结果...")
            ok = "compose" not in page.url
            try:
                browser.close()
            except Exception:
                pass
            browser = None

            if ok:
                log("✓ 推文发送成功")
                return True, "推文发送成功"
            msg = "发送后页面仍停留在 compose 页 — 可能未发送成功（被风控或网络异常）"
            log(f"✗ {msg}")
            return False, msg

    except Exception as e:
        msg = f"发推阶段异常: {type(e).__name__}: {e}"
        log(f"✗ {msg}")
        log(traceback.format_exc(limit=3))
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        return False, msg
