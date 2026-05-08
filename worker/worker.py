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
import random
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import popup_handler as pop
import tweet_engine as eng
import traffic


WORK_DIR = Path("/opt/twitter-worker")
SCREENSHOT_DIR = WORK_DIR / "screenshots"
VIDEO_TEMP = Path("/tmp/twitter-worker-video.mp4")

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
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


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
    p = config.get("proxy") or {}
    if not p.get("host"):
        return None, None
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


def pick_next_task(tasks):
    """挑下一条:status=scheduled 且 scheduled_at <= now,取最早。"""
    now = datetime.now()
    candidates = []
    for t in tasks:
        if t.get("status") != "scheduled":
            continue
        sched = t.get("scheduled_at")
        if not sched:
            continue
        try:
            t_time = datetime.strptime(sched, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if t_time <= now:
            candidates.append((t_time, t))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def run_one_task(task: dict, config: dict):
    tid = task["id"]
    url = task["video_url"]
    caption = task["caption"]

    log(f"=== 任务 {tid} 开始 === {url[:80]}")
    update_task_in_file(tid, status="running", started_at=_now_str(), attempt=task.get("attempt", 0) + 1)
    update_state(status="running", current_task_id=tid)

    pw_proxy, yt_proxy = proxy_dicts(config)
    user_agent = config.get("user_agent") or eng.DEFAULT_UA

    if not eng.download_video(url, VIDEO_TEMP, yt_proxy, pw_proxy, user_agent, log):
        log(f"任务 {tid} 下载失败")
        update_task_in_file(tid, status="failed", finished_at=_now_str(),
                            error_message="download failed")
        return

    try:
        eng.post_to_twitter(
            config=config,
            caption=caption,
            video_path=VIDEO_TEMP,
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
        if VIDEO_TEMP.exists():
            try:
                VIDEO_TEMP.unlink()
            except Exception:
                pass


def main_loop():
    paused = False
    update_state(status="idle", current_task_id=None)
    log("Worker 启动")

    while _running:
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
                log("收到 reload")
            else:
                log(f"未识别命令:{cmd}")

        gb = traffic.get_monthly_traffic_gb()
        update_state(traffic_used_gb=gb)

        if paused:
            time.sleep(15)
            continue

        config = load_json(CONFIG_FILE, None)
        if not config or not config.get("cookie_json"):
            update_state(status="pending", message="缺 config.json 或 cookie_json")
            time.sleep(30)
            continue

        quota = config.get("traffic_quota_gb", 1000)
        if gb >= quota * 0.95:
            update_state(status="paused", message=f"流量 {gb:.1f}/{quota} 接近上限,暂停")
            time.sleep(300)
            continue

        tasks = load_json(TASKS_FILE, [])
        task = pick_next_task(tasks)
        if not task:
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
            if cmd and cmd.get("action") == "pause":
                paused = True
                update_state(status="paused")
                log("休息中收到 pause")
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
