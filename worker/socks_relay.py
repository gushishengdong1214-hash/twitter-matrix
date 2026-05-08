"""本地 SOCKS5 中继。

为什么需要:chromium 不支持带认证的 SOCKS5(playwright proxy auth 只对 firefox/webkit 有效)。
解决:用 gost 起一个本地 127.0.0.1:11080 无认证 SOCKS5,它再转到带认证的住宅代理。
chromium / yt-dlp 都连这个本地端口,认证由 gost 在外层处理。

worker 主循环每次跑任务前调 ensure_relay,如果 proxy 没变就复用,变了就重启。
"""

import subprocess
import time
from typing import Optional

GOST_BIN = "/usr/local/bin/gost"
LOCAL_SOCKS_PORT = 11080

_proc: Optional[subprocess.Popen] = None
_signature: str = ""


def _signature_of(proxy: dict | None) -> str:
    if not proxy or not proxy.get("host"):
        return ""
    return f"{proxy['host']}:{proxy['port']}:{proxy.get('username','')}:{proxy.get('password','')}"


def is_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def stop():
    global _proc, _signature
    if _proc and _proc.poll() is None:
        try:
            _proc.kill()
        except Exception:
            pass
    _proc = None
    _signature = ""


def ensure_relay(proxy_config: dict | None, log) -> bool:
    """启动 / 重启 / 保持 gost。proxy_config 为 None 时停掉。
    返回 True 表示中继可用(本地 11080 能用)。
    """
    global _proc, _signature

    sig = _signature_of(proxy_config)
    if not sig:
        stop()
        return False

    if is_alive() and _signature == sig:
        return True

    if is_alive():
        try:
            _proc.kill()
        except Exception:
            pass

    auth = ""
    if proxy_config.get("username"):
        auth = f"{proxy_config['username']}:{proxy_config.get('password','')}@"

    cmd = [
        GOST_BIN,
        "-L", f"socks5://127.0.0.1:{LOCAL_SOCKS_PORT}",
        "-F", f"socks5://{auth}{proxy_config['host']}:{proxy_config['port']}",
    ]
    log(f"启动 SOCKS5 中继:本地 {LOCAL_SOCKS_PORT} → {proxy_config['host']}:{proxy_config['port']}")
    try:
        _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log(f"找不到 {GOST_BIN}, 中继无法启动")
        return False

    time.sleep(2)
    if _proc.poll() is not None:
        log("gost 启动失败")
        return False

    _signature = sig
    return True


def local_chromium_proxy() -> dict:
    """给 playwright 用的本地无认证 SOCKS5 代理 dict。"""
    return {"server": f"socks5://127.0.0.1:{LOCAL_SOCKS_PORT}"}


def local_ytdlp_proxy_url() -> str:
    """给 yt-dlp 用的本地代理 URL。"""
    return f"socks5://127.0.0.1:{LOCAL_SOCKS_PORT}"
