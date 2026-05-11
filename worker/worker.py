"""
Worker 主进程。每台 VPS 跑这一个,由 systemd 拉起。

工作目录约定:/opt/twitter-worker

中控会通过 SSH 推送/拉取这些文件:
  config.json   ← 中控推    绑定信息(cookie/proxy/调度参数/流量配额)
  tasks.json    ← 中控推    今日任务清单
  cmd.json      ← 中控推    命令(pause/resume),处理后由 worker 删除
  state.json    → 中控拉    当前状态 + 心跳
  log.txt       → 中控拉    运行日志(追加)
  screenshots/  → 中控拉    弹窗截图
"""

import json
import os
import random
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import popup_handler as pop
import tweet_engine as eng
import traffic
import socks_relay


# 日志统一用北京时间输出,不改系统时区(风控需要系统时区跟 IP 所在地一致)
BEIJING_TZ = timezone(timedelta(hours=8))

# 中控推送 reload 后,下轮任务拉取忽略 scheduled_at 时间限制,立即执行
_reload_triggered = False

# 看门狗:主循环超过 5 分钟无心跳则强制退出,由 systemd 重启
_WATCHDOG_LAST_BEAT = time.time()
_WATCHDOG_RUNNING = True


WORK_DIR = Path("/opt/twitter-worker")
SCREENSHOT_DIR = WORK_DIR / "screenshots"
VIDEO_TEMP_DIR = Path("/tmp")
VIDEO_TEMP_PREFIX = "twitter-worker-video-"


def video_temp_for(task_id: int) -> Path:
    return VIDEO_TEMP_DIR / f"{VIDEO_TEMP_PREFIX}{task_id}.mp4"


def video_compliant_for(task_id: int) -> Path:
    """合规化后的视频路径(供 post_to_twitter 使用,与原始下载文件分离)。"""
    return VIDEO_TEMP_DIR / f"{VIDEO_TEMP_PREFIX}{task_id}-compliant.mp4"


def cleanup_all_video_temps():
    """清理 /tmp 下所有 worker 视频残留(防止 Bug A:残留文件被下一任务复用)。"""
    try:
        for p in VIDEO_TEMP_DIR.glob(f"{VIDEO_TEMP_PREFIX}*.mp4"):
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass

CONFIG_FILE = WORK_DIR / "config.json"
TASKS_FILE = WORK_DIR / "tasks.json"
CMD_FILE = WORK_DIR / "cmd.json"
STATE_FILE = WORK_DIR / "state.json"
LOG_FILE = WORK_DIR / "log.txt"


_running = True


class PauseRequired(Exception):
    pass


def _sigterm(*_):
    global _running
    _running = False
    log("收到 SIGTERM, 准备退出")


def log(msg: str):
    ts = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


def _heartbeat():
    """看门狗心跳:每轮主循环调用一次。"""
    global _WATCHDOG_LAST_BEAT
    _WATCHDOG_LAST_BEAT = time.time()


def _watchdog_loop():
    """看门狗线程:每 60 秒检查一次主循环是否停滞。超过 5 分钟无心跳则 os._exit。"""
    global _WATCHDOG_RUNNING
    while _WATCHDOG_RUNNING:
        time.sleep(60)
        idle = time.time() - _WATCHDOG_LAST_BEAT
        if idle > 300:
            log(f"看门狗:主循环 {idle:.0f} 秒无心跳,强制退出由 systemd 重启")
            time.sleep(1)
            os._exit(1)


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"读取 {p.name} 失败:{e}")
        return default


def save_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def consume_command():
    if not CMD_FILE.exists():
        return None
    try:
        cmd = json.loads(CMD_FILE.read_text(encoding="utf-8"))
        CMD_FILE.unlink()
        return cmd
    except Exception:
        return None


def update_state(**fields):
    state = load_json(STATE_FILE, {})
    state.update(fields)
    state["heartbeat"] = _now_str()
    save_json(STATE_FILE, state)


def update_task_in_file(task_id: int, **fields):
    tasks = load_json(TASKS_FILE, [])
    for t in tasks:
        if t.get("id") == task_id:
            t.update(fields)
            t["updated_at"] = _now_str()
    save_json(TASKS_FILE, tasks)


def proxy_dicts(config) -> tuple:
    """通过本地 SOCKS5 中继间接走带认证的住宅代理。
    chromium 不支持带认证的 socks5,所以走 gost 中继。
    """
    p = config.get("proxy") or {}
    if not p.get("host"):
        return None, None

    if not socks_relay.ensure_relay(p, log):
        log("中继启动失败,直接尝试用住宅代理(chromium 可能因为不支持 socks5 auth 失败)")
        # 退回直连
        pw = {"server": f"socks5://{p['host']}:{p['port']}"}
        if p.get("username"):
            pw["username"] = p["username"]
        if p.get("password"):
            pw["password"] = p["password"]
        auth = ""
        if p.get("username"):
            auth = f"{p['username']}:{p.get('password','')}@"
        yt = f"socks5://{auth}{p['host']}:{p['port']}"
        return pw, yt

    return socks_relay.local_chromium_proxy(), socks_relay.local_ytdlp_proxy_url()


def pick_next_task(tasks, ignore_schedule=False):
    """挑下一条:status=scheduled 且 scheduled_at <= now,取最早。

    ignore_schedule=True 时,忽略 scheduled_at 时间限制(用于收到中控 reload 强制唤醒后
    立即执行,不受 work_start 排期窗口限制)。
    """
    now = datetime.now()
    candidates = []
    for t in tasks:
        if t.get("status") != "scheduled":
            continue
        sched = t.get("scheduled_at")
        if not sched:
            if ignore_schedule:
                candidates.append((now, t))
            continue
        try:
            t_time = datetime.strptime(sched, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if t_time <= now or ignore_schedule:
            candidates.append((t_time, t))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def run_one_task(task: dict, config: dict):
    tid = task["id"]
    url = task["video_url"]
    caption = task["caption"]
    video_temp = video_temp_for(tid)
    video_compliant = video_compliant_for(tid)

    # 任务开头强制清理同名残留(防 Bug A:上次任务的不完整文件被复用)
    for p in (video_temp, video_compliant):
        if p.exists():
            try:
                p.unlink()
                log(f"清理任务 {tid} 残留 {p.name}")
            except Exception:
                pass

    log(f"=== 任务 {tid} 开始 === {url[:80]}")
    update_task_in_file(tid, status="running", started_at=_now_str(), attempt=task.get("attempt", 0) + 1)
    update_state(status="running", current_task_id=tid)

    pw_proxy, yt_proxy = proxy_dicts(config)
    user_agent = config.get("user_agent") or eng.DEFAULT_UA

    if not eng.download_video(url, video_temp, yt_proxy, pw_proxy, user_agent, log):
        log(f"任务 {tid} 下载失败")
        # 部分写入的文件也要清掉,防止下次复用
        for p in (video_temp, video_compliant):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        update_task_in_file(tid, status="failed", finished_at=_now_str(),
                            error_message="download failed")
        return

    # 视频合规检查:超过 Twitter 限制(140s/512MB)的会被转码截断
    if not eng.ensure_video_compliance(video_temp, video_compliant, log):
        log(f"任务 {tid} 视频合规检查失败")
        for p in (video_temp, video_compliant):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        update_task_in_file(tid, status="failed", finished_at=_now_str(),
                            error_message="compliance failed")
        return

    try:
        eng.post_to_twitter(
            config=config,
            caption=caption,
            video_path=video_compliant,
            pw_proxy=pw_proxy,
            screenshot_dir=SCREENSHOT_DIR,
            log=log,
        )
        update_task_in_file(tid, status="done", finished_at=_now_str())
        log(f"任务 {tid} 完成")
    except pop.UnknownPopupError as e:
        log(f"任务 {tid} 卡在未知弹窗:{e}")
        update_task_in_file(
            tid, status="human_required",
            error_message=str(e),
            screenshot_path=e.screenshot_path,
        )
        update_state(
            status="human_required",
            current_task_id=tid,
            last_alert={
                "task_id": tid,
                "type": "popup_unknown",
                "message": str(e),
                "screenshot": e.screenshot_path,
                "html": e.html_path,
            },
        )
        raise PauseRequired()
    except Exception as e:
        log(f"任务 {tid} 异常:{e}\n{traceback.format_exc()}")
        update_task_in_file(tid, status="failed", finished_at=_now_str(),
                            error_message=str(e))
    finally:
        for p in (video_temp, video_compliant):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass


def reset_stale_running_tasks():
    """启动时把 tasks.json 里 status=running 的任务回退为 scheduled。
    防止 worker 重启时丢了正在跑的任务,导致后续永远不再调度它。"""
    tasks = load_json(TASKS_FILE, [])
    n = 0
    for t in tasks:
        if t.get("status") == "running":
            t["status"] = "scheduled"
            t["started_at"] = None
            n += 1
    if n:
        save_json(TASKS_FILE, tasks)
        log(f"启动时回退 {n} 个 stale running 任务为 scheduled")


def main_loop():
    global _reload_triggered, _WATCHDOG_RUNNING
    paused = False
    update_state(status="idle", current_task_id=None)
    log("Worker 启动")
    reset_stale_running_tasks()
    cleanup_all_video_temps()

    # 启动看门狗线程(守护线程,主进程退出时自动结束)
    wd = threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog")
    wd.start()

    while _running:
        _heartbeat()  # 看门狗心跳

        cmd = consume_command()
        if cmd:
            action = cmd.get("action")
            if action == "pause":
                paused = True
                update_state(status="paused")
                log("收到 pause")
            elif action == "resume":
                paused = False
                update_state(status="idle")
                log("收到 resume")
            elif action == "reload":
                log("收到 reload,强制重载任务")
                _reload_triggered = True
            else:
                log(f"未识别命令:{cmd}")

        gb = traffic.get_monthly_traffic_gb()
        update_state(traffic_used_gb=gb)

        if paused:
            log("已暂停,等待 resume")
            time.sleep(15)
            continue

        config = load_json(CONFIG_FILE, None)
        if not config or not config.get("cookie_json"):
            log("等待 config.json 或 cookie_json")
            update_state(status="pending", message="缺 config.json 或 cookie_json")
            time.sleep(30)
            continue

        quota = config.get("traffic_quota_gb", 1000)
        if gb >= quota * 0.95:
            log(f"流量 {gb:.1f}/{quota} 接近上限,暂停")
            update_state(status="paused", message=f"流量 {gb:.1f}/{quota} 接近上限,暂停")
            time.sleep(300)
            continue

        tasks = load_json(TASKS_FILE, [])
        task = pick_next_task(tasks, ignore_schedule=_reload_triggered)
        if _reload_triggered:
            _reload_triggered = False

        if not task:
            log("无到点任务,等待 30 秒")
            update_state(status="idle", message="无到点任务")
            time.sleep(30)
            continue

        try:
            run_one_task(task, config)
        except PauseRequired:
            log("已暂停,等待中控指令")
            paused = True
            continue
        except Exception:
            log(f"主循环异常:\n{traceback.format_exc()}")

        rest_min = int(config.get("rest_min_minutes", 30))
        rest_max = int(config.get("rest_max_minutes", 90))
        if rest_max < rest_min:
            rest_max = rest_min
        rest_seconds = random.randint(rest_min * 60, rest_max * 60)
        log(f"休息 {rest_seconds // 60} 分钟")
        update_state(status="resting", message=f"休息至 {(datetime.now().timestamp() + rest_seconds)}")

        for _ in range(rest_seconds):
            if not _running:
                break
            cmd = consume_command()
            if cmd:
                if cmd.get("action") == "pause":
                    paused = True
                    update_state(status="paused")
                    log("休息中收到 pause")
                    break
                elif cmd.get("action") == "reload":
                    log("休息中收到 reload,提前结束休息")
                    _reload_triggered = True
                    break
            time.sleep(1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        main_loop()
    except Exception:
        log(f"FATAL:\n{traceback.format_exc()}")
        sys.exit(1)
    log("退出")
