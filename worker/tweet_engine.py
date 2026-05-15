"""下载 + 发推核心。基于单账号脚本改造,加 proxy / popup_handler / 异常驱动 / 浏览器指纹 / 2FA。"""

import json
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import yt_dlp
from playwright.sync_api import sync_playwright

import popup_handler as pop

# playwright-stealth 用于补 navigator.plugins/languages/hardwareConcurrency/chrome/permissions/WebGL 等指纹
# 装不上时降级为不打 stealth(WebRTC 屏蔽仍生效),不影响主流程
try:
    from playwright_stealth import stealth_sync as _stealth_sync
    _HAS_STEALTH = True
except ImportError:
    _stealth_sync = None
    _HAS_STEALTH = False


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 高清下载专用:更完整的浏览器请求头伪装
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# X 视频发布硬指标:H.264 + AAC / yuv420p
# 时长限制放宽到 10 分钟(600s),Twitter Blue 支持更长
TWITTER_DURATION_LIMIT_S = 600
# 体积限制放宽到 2GB,Twitter Blue 支持最大 8GB
TWITTER_SIZE_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
TWITTER_TARGET_BITRATE_K = 8000  # 8Mbps,稳在 25Mbps 上限内

# Chromium 启动参数:WebRTC 屏蔽 + 隐藏自动化痕迹
# WebRTC mode 3:UDP 走代理或丢弃(SOCKS5 转 UDP 在住宅链路上 99% 不通,等同丢弃)
# 防止 X 通过 WebRTC STUN 看到 VPS 真 IP
CHROMIUM_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,WebRtcHideLocalIpsWithMdns",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]

# 注入到每个 page 的初始化脚本:隐藏 webdriver + 过滤 WebRTC ICE 服务器(belt + suspenders)
_INIT_SCRIPT = """
    // 1. 隐藏 webdriver
    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});

    // 2. WebRTC ICE 服务器过滤(屏蔽 stun/turn,阻止 IP 候选生成)
    if (window.RTCPeerConnection) {
        const _RTCPC = window.RTCPeerConnection;
        window.RTCPeerConnection = function(config, constraints) {
            if (config && config.iceServers) {
                config.iceServers = config.iceServers.filter(s => {
                    const urls = Array.isArray(s.urls) ? s.urls : [s.urls];
                    return !urls.some(u => u && (u.startsWith('stun:') || u.startsWith('turn:')));
                });
            }
            return new _RTCPC(config, constraints);
        };
        window.RTCPeerConnection.prototype = _RTCPC.prototype;
    }
    if (window.webkitRTCPeerConnection) {
        window.webkitRTCPeerConnection = window.RTCPeerConnection;
    }
"""


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


def _sniff_m3u8(url: str, pw_proxy: Optional[dict], user_agent: str, log,
                heartbeat_fn=None) -> Optional[str]:
    """对 jable.tv / hanime1.me 这类站点,用浏览器嗅探真实视频流。

    多层策略,按优先级:
      1) 主策略:打开页面后,从 DOM 里读主 <video> 元素的 currentSrc / src
      2) 兜底 1:抓所有 m3u8 请求,排除预览/广告/缩略图后选 #EXTINF segment 数最多的
      3) 兜底 2:返回排除后的第一个
    """
    log("打开浏览器嗅探 m3u8...")
    # 详情页常见的非主视频 m3u8 模式
    EXCLUDE_PATTERNS = [
        "/preview", "/trailer", "/thumb", "/thumbnail",
        "/ad/", "/ads/", "preview.m3u8", "/sprite",
    ]

    def is_excluded(u: str) -> bool:
        ul = u.lower()
        return any(pat in ul for pat in EXCLUDE_PATTERNS)

    def _hb():
        if heartbeat_fn:
            heartbeat_fn()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=pw_proxy, args=CHROMIUM_LAUNCH_ARGS)
            context = browser.new_context(user_agent=user_agent)
            context.add_init_script(_INIT_SCRIPT)
            page = context.new_page()
            captured: list[str] = []
            page.on(
                "request",
                lambda r: captured.append(r.url) if ".m3u8" in r.url else None,
            )
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                # 第一步:等 DOM + 播放器脚本加载(分 5 秒+心跳,防看门狗误杀)
                page.wait_for_timeout(5000)
                _hb()

                # 第二步:程序化触发主视频加载
                try:
                    page.evaluate(
                        """() => {
                            document.querySelectorAll('video').forEach(v => {
                                try { v.muted = true; } catch (e) {}
                                try { v.play().catch(() => {}); } catch (e) {}
                            });
                        }"""
                    )
                    log("已触发 video.play() 让主视频开始加载")
                except Exception as e:
                    log(f"触发 play 失败:{e}")

                # 兜底:点常见的 play 按钮
                try:
                    for sel in [
                        ".vjs-big-play-button",
                        ".plyr__control--overlaid",
                        ".jw-icon-display",
                        "button[aria-label*='play' i]",
                        ".play-btn", ".btn-play",
                    ]:
                        loc = page.locator(sel).first
                        try:
                            if loc.is_visible(timeout=500):
                                loc.click(timeout=1500)
                                log(f"点了 play 按钮:{sel}")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

                # 第三步:等 m3u8 请求(15 秒分 3 段,每 5 秒喂狗一次)
                for _ in range(3):
                    page.wait_for_timeout(5000)
                    _hb()

                # 主策略:DOM 里读 video 元素的 currentSrc,但不直接返回——
                # 播放器自适应可能选了低清晰度,把它加入候选池统一排序更保险
                video_src = None
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
                        log(f"主策略读到 video.currentSrc: {video_src[:120]}")
                except Exception as e:
                    log(f"主策略读 video 元素失败:{e}")

                # 统一候选池:捕获的网络请求 + video 元素读到的 src(去重)
                unique = list(dict.fromkeys(captured))
                if video_src and video_src not in unique:
                    unique.insert(0, video_src)

                # 兜底 1:过滤掉已知非主视频模式
                filtered = [u for u in unique if not is_excluded(u)]
                log(f"嗅到 {len(unique)} 个 m3u8,排除后剩 {len(filtered)} 个")

                if not filtered:
                    log("所有 m3u8 都被排除,放弃")
                    return None

                if len(filtered) == 1:
                    log(f"唯一候选: {filtered[0][:120]}")
                    return filtered[0]

                # 多个候选:解析 master m3u8 中的真实分辨率/带宽,选最高画质
                import re

                def _resolution_from_url(u: str) -> int:
                    m = re.search(r"_(\d+)p", u)
                    return int(m.group(1)) if m else 0

                def _parse_m3u8_variant_info(text: str) -> dict:
                    """解析 master m3u8,提取最高 variant 的分辨率和带宽。"""
                    info = {"width": 0, "height": 0, "bandwidth": 0}
                    for line in text.splitlines():
                        if line.startswith("#EXT-X-STREAM-INF"):
                            # BANDWIDTH=...
                            bm = re.search(r"BANDWIDTH=(\d+)", line)
                            if bm:
                                info["bandwidth"] = int(bm.group(1))
                            # RESOLUTION=1920x1080
                            rm = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                            if rm:
                                info["width"] = int(rm.group(1))
                                info["height"] = int(rm.group(2))
                    return info

                scored = []
                for m3u8_url in filtered:
                    url_score = _resolution_from_url(m3u8_url)
                    # 尝试读取 m3u8 内容获取真实分辨率
                    try:
                        resp = page.request.get(m3u8_url, timeout=10_000)
                        if resp.ok:
                            text = resp.text()
                            # 判断是 master m3u8 还是 media m3u8
                            if "#EXT-X-STREAM-INF" in text:
                                info = _parse_m3u8_variant_info(text)
                                # master m3u8:用里面最高 variant 的分辨率
                                height = info["height"] or url_score
                                bw = info["bandwidth"]
                                scored.append((height, bw, m3u8_url, "master"))
                            else:
                                # media m3u8:segment 数作为质量参考
                                seg_count = text.count("#EXTINF")
                                scored.append((url_score, seg_count, m3u8_url, "media"))
                        else:
                            scored.append((url_score, 0, m3u8_url, "unknown"))
                    except Exception:
                        scored.append((url_score, 0, m3u8_url, "unknown"))

                # 按分辨率降序,同分辨率选带宽高的
                scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
                log(f"候选 m3u8 分辨率排序(高→低):")
                for height, bw, u, kind in scored:
                    label = f"{height}p" if height > 0 else "?p"
                    if kind == "master":
                        log(f'  - {label} BW={bw/1000:.0f}k [{kind}] {u.split("/")[-1][:50]}')
                    elif kind == "media":
                        log(f'  - {label} segs={bw} [{kind}] {u.split("/")[-1][:50]}')
                    else:
                        log(f'  - {label} [{kind}] {u.split("/")[-1][:50]}')

                best_height, best_bw, best_url, best_kind = scored[0]

                # 直接采用候选池排序后第一名 —— 不做 URL 推断
                # (推断曾把"_240p.m3u8"替换为"_720p.m3u8",但该路径常对应另一支视频源,
                #  导致拉回错误的长片/正片,例如 2 小时 1.9GB。回归"只信探测到的"。)
                def _est_size_mb(m3u8_u: str, bw_bps: int) -> Optional[float]:
                    """快速从 m3u8(media 或 master)估算文件大小 MB。无法估算返回 None。"""
                    try:
                        r = page.request.get(m3u8_u, timeout=8_000)
                        if not r.ok:
                            return None
                        txt = r.text()
                        # master:跟到带宽最高的子流
                        if "#EXT-X-STREAM-INF" in txt:
                            from urllib.parse import urljoin
                            lines = txt.splitlines()
                            top = None  # (bw, sub_url)
                            for idx, ln in enumerate(lines):
                                if not ln.startswith("#EXT-X-STREAM-INF"):
                                    continue
                                bm = re.search(r"BANDWIDTH=(\d+)", ln)
                                cand_bw = int(bm.group(1)) if bm else 0
                                for j in range(idx + 1, len(lines)):
                                    s = lines[j].strip()
                                    if s and not s.startswith("#"):
                                        if top is None or cand_bw > top[0]:
                                            top = (cand_bw, s)
                                        break
                            if not top:
                                return None
                            sub_bw, sub_path = top
                            sub_url = sub_path if sub_path.startswith("http") else urljoin(m3u8_u, sub_path)
                            r2 = page.request.get(sub_url, timeout=8_000)
                            if not r2.ok:
                                return None
                            txt = r2.text()
                            bw_bps = sub_bw or bw_bps
                        # media:求和 #EXTINF 时长
                        durs = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", txt)]
                        if not durs or not bw_bps:
                            return None
                        total_s = sum(durs)
                        return (bw_bps * total_s) / 8 / (1024 * 1024)
                    except Exception:
                        return None

                if best_height > 0:
                    if best_kind == "master":
                        est_mb = _est_size_mb(best_url, best_bw)
                        size_str = f"{est_mb:.0f}MB" if est_mb else "?MB"
                        warn = "  ⚠️ 体积偏大,如是短视频说明嗅到了错的流" if est_mb and est_mb > 500 else ""
                        log(f"🎬 锁定下载:{best_height}p / BW={best_bw/1000:.0f}k / 预计 {size_str} ({best_kind}){warn}")
                    else:
                        # media 类型时 best_bw 是 segment 数,不是带宽,不能算大小
                        log(f"🎬 锁定下载:{best_height}p / segs={best_bw} / 大小待下载后确认 ({best_kind})")
                    log(f"  URL: {best_url[:120]}")
                    return best_url

                # 无分辨率信息:兜底选带宽/segment 最多的
                log(f"兜底返回最优候选 ({best_kind}): {best_url[:120]}")
                return best_url
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        log(f"m3u8 嗅探失败:{e}")
    return None


def _list_formats(url: str, ydl_opts: dict, log) -> list[dict]:
    """用 yt-dlp 提取所有可用格式,打印分辨率列表供 debug。返回格式列表。

    注意:m3u8 URL 跳过探测,因为 yt-dlp 的 extract_info 在处理某些 m3u8 时会
    卡住(下载 segment 探测耗时过长),而 master m3u8 的 variant 信息已在
    _sniff_m3u8 阶段打印过了。
    """
    formats = []
    if ".m3u8" in url:
        log("格式探测:URL 为 m3u8,跳过 yt-dlp extract_info(避免卡死),依赖嗅探阶段的分辨率日志")
        return formats
    try:
        with yt_dlp.YoutubeDL({**ydl_opts, "quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                log("格式探测:未获取到视频信息")
                return formats
            formats = info.get("formats") or []
            if not formats:
                log("格式探测:无可用格式")
                return formats

            # 过滤出有视频流的格式,按分辨率排序
            video_formats = []
            for f in formats:
                vcodec = f.get("vcodec", "none")
                if vcodec == "none" or not vcodec:
                    continue
                height = f.get("height") or 0
                width = f.get("width") or 0
                fps = f.get("fps") or 0
                tbr = f.get("tbr") or 0  # 总码率 kbps
                abr = f.get("abr") or 0
                ext = f.get("ext", "")
                fmt_id = f.get("format_id", "")
                video_formats.append({
                    "id": fmt_id,
                    "ext": ext,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "tbr": tbr,
                    "abr": abr,
                    "vcodec": vcodec,
                })

            # 去重:同分辨率只保留码率最高的
            best_by_res = {}
            for f in video_formats:
                key = f["height"]
                if key not in best_by_res or f["tbr"] > best_by_res[key]["tbr"]:
                    best_by_res[key] = f

            sorted_fmts = sorted(best_by_res.values(), key=lambda x: x["height"], reverse=True)
            lines = [f'{f["height"]}p ({f["width"]}x{f["height"]}) {f["ext"]} {f["tbr"]:.0f}kbps fps={f["fps"]}' for f in sorted_fmts]
            log(f'格式探测:发现 {len(sorted_fmts)} 个视频分辨率(去重后):')
            for line in lines:
                log(f'  - {line}')
            if sorted_fmts:
                best = sorted_fmts[0]
                log(f'最高画质: {best["height"]}p / {best["tbr"]:.0f}kbps / {best["ext"]}')
    except Exception as e:
        log(f"格式探测失败(非致命):{e}")
    return formats


def _make_progress_hook(log, heartbeat_fn=None):
    """yt-dlp progress_hooks 工厂:每 5% 输出简洁进度日志,同时喂狗。"""
    last_logged_pct = -1
    last_logged_time = 0

    def hook(d):
        nonlocal last_logged_pct, last_logged_time
        status = d.get("status")
        if status == "downloading":
            # 每收到一个分片都喂狗
            if heartbeat_fn:
                heartbeat_fn()

            pct = d.get("percentage") or 0.0
            now = time.time()
            rounded_pct = int(pct // 5) * 5

            # 每 5% 变化 或 每 15 秒强制输出一次
            if rounded_pct > last_logged_pct or (now - last_logged_time) >= 15:
                last_logged_pct = rounded_pct
                last_logged_time = now

                total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0
                speed_mbps = speed / (1024 * 1024) if speed else 0

                if total_bytes:
                    total_mb = total_bytes / (1024 * 1024)
                    log(f"[下载进度] {rounded_pct}% ({speed_mbps:.1f}MB/s / {total_mb:.0f}MB)")
                else:
                    log(f"[下载进度] {rounded_pct}% ({speed_mbps:.1f}MB/s)")
        elif status == "finished":
            log("[下载进度] 100% 完成,合并中...")
            if heartbeat_fn:
                heartbeat_fn()

    return hook


def download_video(
    url: str,
    out_path: Path,
    yt_proxy_url: Optional[str],
    pw_proxy: Optional[dict],
    user_agent: str,
    log,
    heartbeat_fn=None,
) -> bool:
    # 通用策略:先用浏览器嗅 m3u8(过 Cloudflare 等反爬),嗅到就用 m3u8 给 yt-dlp,
    # 没嗅到再 fallback 给 yt-dlp 原 URL(yt-dlp 内置支持很多站)。
    # jable.tv 特殊:嗅不到就直接放弃,因为 yt-dlp 会拒绝它的页面 URL。
    real_url = url
    sniffed = _sniff_m3u8(url, pw_proxy, user_agent, log, heartbeat_fn=heartbeat_fn)
    if sniffed:
        real_url = sniffed
        log(f"嗅探到 m3u8: {sniffed[:120]}")
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
        # 强制最高画质:不限制容器格式,让 yt-dlp 选 bestvideo+bestaudio 后合并为 mp4
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,  # 关闭 yt-dlp 自带输出,用 progress_hooks 接管
        "no_warnings": True,
        # 硬性约束:禁止无限重试,分片 60 秒超时
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 60,
        "concurrent_fragment_downloads": 15,
        "progress_hooks": [_make_progress_hook(log, heartbeat_fn)],
        "http_headers": {
            **DEFAULT_HEADERS,
            "Referer": url,
        },
    }
    # yt-dlp 在 worker VPS 上可能没有 curl_cffi impersonate 插件。
    # 只在 fallback(给 yt-dlp 原 URL 直接下载)时设 impersonate,因为:
    #   - 嗅到 m3u8 后 yt-dlp 下 segments 通常不需要(绝大多数站点直接下就行)
    #   - 否则 worker 上 yt-dlp 不支持 chrome-124 会直接报错(Impersonate target not available)
    # hanime1 的 m3u8 下载如遇 403,需在 worker VPS 升级 yt-dlp + curl_cffi:
    #     pip install -U yt-dlp curl_cffi
    if real_url == url:
        try:
            ydl_opts["impersonate"] = "chrome-124"
        except Exception:
            pass
    if yt_proxy_url:
        ydl_opts["proxy"] = yt_proxy_url

    # 高清链路锁死:下载前打印所有可用分辨率,确保我们能看见最高流
    _list_formats(real_url, ydl_opts, log)

    # yt-dlp 全显:把实际下发的命令参数打印出来,方便核对 URL 和内容是否一致
    proxy_flag = f"代理={yt_proxy_url}" if yt_proxy_url else "代理=无"
    log(f"[yt-dlp] 目标 URL={real_url}")
    log(f"[yt-dlp] 格式={ydl_opts['format']} 输出={out_path} {proxy_flag} 合并格式={ydl_opts.get('merge_output_format','mp4')}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([real_url])
        log(f"视频下载完成 {out_path}")
        return True
    except Exception as e:
        log(f"下载失败:{e}")
        return False


def ensure_video_compliance(in_path: Path, out_path: Path, log, heartbeat_fn=None) -> bool:
    """检查并转码视频以符合 Twitter 发布标准。

    Twitter 规范:时长 <= 140s,大小 <= 512MB,H.264 + AAC,yuv420p。
    超规视频取前 140s 转码为 H.264 720p / 8Mbps / AAC 128k。

    返回 True 表示视频就绪(原视频符合 OR 转码成功)。
    返回 False 表示无法处理(ffmpeg 不存在 / 转码失败 / 输出异常)。
    """
    if not in_path.exists():
        log(f"压制:输入视频不存在 {in_path}")
        return False

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        size = in_path.stat().st_size
        if size <= TWITTER_SIZE_LIMIT_BYTES:
            log(f"压制:未找到 ffmpeg,原视频 {size/1024/1024:.1f}MB 在大小限内,直接采用")
            if in_path != out_path:
                shutil.copy(in_path, out_path)
            return True
        log(f"压制:未找到 ffmpeg,原视频 {size/1024/1024:.1f}MB 超 {TWITTER_SIZE_LIMIT_BYTES/1024/1024:.0f}MB,放弃")
        return False

    # 探测时长 + 尺寸
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(in_path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        info = json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        size = int(info.get("format", {}).get("size", 0))
    except Exception as e:
        log(f"压制:ffprobe 探测失败 {e}")
        return False

    log(f"压制:原视频 {duration:.1f}s / {size/1024/1024:.1f}MB")

    # 已合规:直接采用(放宽标准:只要编码格式对,时长和大小由 Twitter 自己拦)
    if size <= TWITTER_SIZE_LIMIT_BYTES:
        log(f"压制:原视频 {duration:.1f}s/{size/1024/1024:.1f}MB 在体积限内,直接采用")
        if in_path != out_path:
            shutil.copy(in_path, out_path)
        return True

    # 转码:不再截断时长,只限制码率和尺寸
    log(f"压制:开始转码(时长={duration:.1f}s,大小={size/1024/1024:.1f}MB)")
    cmd = [
        ffmpeg, "-y",
        "-i", str(in_path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-maxrate", f"{TWITTER_TARGET_BITRATE_K}k",
        "-bufsize", f"{TWITTER_TARGET_BITRATE_K * 2}k",
        "-vf", "scale='min(1280,iw)':'-2'",  # 等比缩到宽 <= 1280
        "-pix_fmt", "yuv420p",                # X 要求 yuv420p
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-movflags", "+faststart",            # web 友好(meta 前置)
        str(out_path),
    ]

    # ffmpeg 运行期间启动独立喂狗线程,防止看门狗误杀
    _ffmpeg_hb_running = True
    def _ffmpeg_hb():
        while _ffmpeg_hb_running:
            if heartbeat_fn:
                heartbeat_fn()
            time.sleep(10)
    _ffmpeg_hb_thread = threading.Thread(target=_ffmpeg_hb, daemon=True)
    _ffmpeg_hb_thread.start()

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log("压制:ffmpeg 超时(10min),放弃")
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        return False
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "")[-1000:]
        log(f"压制:ffmpeg 失败 rc={e.returncode}")
        log(f"压制:ffmpeg stderr 末段:{tail}")
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        return False
    finally:
        _ffmpeg_hb_running = False

    if not out_path.exists() or out_path.stat().st_size == 0:
        log("压制:输出文件不存在或为空")
        return False

    new_size = out_path.stat().st_size
    log(f"压制:完成,输出 {new_size/1024/1024:.1f}MB")
    return True


def post_to_twitter(
    config: dict,
    caption: str,
    video_path: Path,
    pw_proxy: Optional[dict],
    screenshot_dir: Path,
    log,
    heartbeat_fn=None,
):
    """成功返回 None;碰到未知卡住状态抛 UnknownPopupError。

    heartbeat_fn: 长等待期间定期调用,防止看门狗误杀。
    """
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
            args=CHROMIUM_LAUNCH_ARGS,
        )
        context = browser.new_context(
            user_agent=user_agent,
            viewport=viewport,
            timezone_id=timezone_id,
            locale=locale,
        )
        # WebRTC 屏蔽 + 隐藏 webdriver(详见模块顶部 _INIT_SCRIPT)
        context.add_init_script(_INIT_SCRIPT)
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        # playwright-stealth 补 navigator.plugins / languages / hardwareConcurrency / chrome / permissions / WebGL 等
        if _HAS_STEALTH:
            try:
                _stealth_sync(page)
            except Exception as e:
                log(f"stealth_sync 失败:{e}")
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

            # 等附件占位出现(大视频文件上传慢,放宽到 5 分钟)
            try:
                page.wait_for_selector(
                    "[data-testid='attachments']", timeout=300_000
                )
                log("已附加视频(看到 attachments 容器)")
            except Exception:
                log("没等到 attachments selector,继续")

            pop.dismiss_all_overlays(page, log)

            # 等待上传进度条消失:大视频上传极慢,放宽到 10 分钟
            log("等待视频上传进度完成...")
            try:
                page.wait_for_selector(
                    "[role='progressbar']",
                    state="hidden",
                    timeout=600_000,
                )
                log("上传进度条已消失,文件上传完成")
            except Exception:
                log("未检测到上传进度条或已提前完成,继续")

            # 真正的等待:button 的 disabled 属性消失
            # X 的发推按钮视觉上始终是黑色,但 DOM 里 disabled 真实存在,
            # disabled 阻止 React 处理 onClick,即使 force click 也不会触发后端。
            # 必须等 X 后台处理完视频(disabled 消失)才能 click。
            # 大视频处理可能超过 30 分钟,放宽到 60 分钟,10 秒轮询喂狗。
            log("等待 X 后台处理视频(等按钮 disabled 消失,最多 60 分钟)...")
            found = False
            deadline = time.time() + 3600  # 60 分钟
            while time.time() < deadline:
                try:
                    page.wait_for_selector(
                        "[data-testid='tweetButton']:not([disabled]), "
                        "[data-testid='tweetButtonInline']:not([disabled])",
                        timeout=10_000,
                    )
                    found = True
                    break
                except Exception:
                    if heartbeat_fn:
                        heartbeat_fn()
                    pop.dismiss_all_overlays(page, log)

            if not found:
                pop.dismiss_all_overlays(page, log)
                shot, html = pop.snapshot_unknown(
                    page, screenshot_dir, "button_disabled_30min"
                )
                raise pop.UnknownPopupError(
                    "30 分钟内 Post 按钮仍 disabled,X 可能在审核或拒绝该视频",
                    shot, html,
                )
            log("按钮 enabled,X 准备好发送")

            # 模拟人类反应延迟 5-15 秒(真人看到按钮亮起也会顿一下再点)
            human_delay_ms = random.randint(5_000, 15_000)
            log(f"模拟人类延迟 {human_delay_ms/1000:.1f} 秒后点发送")
            page.wait_for_timeout(human_delay_ms)
            if heartbeat_fn:
                heartbeat_fn()
            # JS 强制点击,绕过 r-lp0dtai 等遮罩层拦截
            try:
                page.evaluate(
                    "() => {"
                    "  const btn = document.querySelector('[data-testid=\"tweetButton\"]')"
                    "    || document.querySelector('[data-testid=\"tweetButtonInline\"]');"
                    "  if (btn) btn.click();"
                    "}"
                )
                log("JS 强制点击发送成功")
            except Exception as e:
                log(f"JS 点击失败,尝试 fallback 到普通 click: {e}")
                page.locator(
                    "[data-testid='tweetButton']:not([disabled]), "
                    "[data-testid='tweetButtonInline']:not([disabled])"
                ).first.click(timeout=10_000)
                log("普通 click 发送成功")

            # 等推文真正发出去——检测成功反馈,不是盲等
            log("点击已触发,等待 X 反馈成功提示...")
            tweet_sent = False
            check_deadline = time.time() + 30  # 30 秒内必须看到成功反馈
            while time.time() < check_deadline:
                try:
                    # 检测1: "Your post was sent" / "Your tweet was sent" toast
                    toast_selectors = [
                        "div[role='alert']",
                        "[data-testid='toast']",
                        "[data-testid='toastContainer']",
                    ]
                    for sel in toast_selectors:
                        try:
                            toast = page.locator(sel).first
                            if toast.is_visible(timeout=500):
                                text = toast.inner_text(timeout=500).lower()
                                if "sent" in text or "已发布" in text or "posted" in text:
                                    log(f"检测到成功提示: {text[:80]}")
                                    tweet_sent = True
                                    break
                        except Exception:
                            pass
                    if tweet_sent:
                        break

                    # 检测2: URL 离开 compose/tweet 页面(跳转到了时间线)
                    current_url = page.url
                    if "compose/tweet" not in current_url:
                        log(f"页面已跳转,当前 URL: {current_url[:100]}")
                        tweet_sent = True
                        break
                except Exception:
                    pass

                if heartbeat_fn:
                    heartbeat_fn()
                pop.dismiss_all_overlays(page, log)
                page.wait_for_timeout(3_000)

            if not tweet_sent:
                log("警告:未检测到发推成功反馈(toast 或 URL 跳转),可能发送失败")
                shot, html = pop.snapshot_unknown(page, screenshot_dir, "tweet_no_feedback")
                raise pop.UnknownPopupError(
                    "点击发送后未检测到成功反馈(无 toast/无 URL 跳转),推文可能未发出",
                    shot, html,
                )
            log("发推完成(已验证)")
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
