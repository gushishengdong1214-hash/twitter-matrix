"""中控 ↔ Worker 业务同步层。

- push_*  把数据从中控推到 Worker
- pull_*  从 Worker 拉数据回中控
- provision_worker / update_worker_code  初次部署 / 更新代码
"""

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import database as db
from ssh_client import SSHClient


WORKER_REMOTE_DIR = "/opt/twitter-worker"
WORKER_LOCAL_DIR = Path(__file__).parent.parent / "worker"
SCREENSHOT_LOCAL_DIR = Path(__file__).parent / "data" / "screenshots"


# ---------- 工具 ----------

def _creds(w: dict) -> dict:
    return dict(
        host=w["vps_host"],
        port=w.get("ssh_port") or 22,
        user=w.get("ssh_user") or "root",
        password=w.get("ssh_password") or None,
        key_path=w.get("ssh_key_path") or None,
    )


def _build_config_payload(worker: dict) -> dict:
    """生成给 worker 的 config.json:绑定信息 + 调度参数 + 浏览器指纹。"""
    proxy = None
    if worker.get("proxy_id"):
        with db.get_conn() as c:
            row = c.execute(
                "SELECT host, port, username, password FROM proxies WHERE id = ?",
                (worker["proxy_id"],),
            ).fetchone()
            if row:
                proxy = dict(row)
    return {
        "worker_id": worker["id"],
        "nickname": worker["nickname"],
        "twitter_handle": worker.get("twitter_handle"),
        "cookie_json": worker.get("cookie_json") or "",
        "account_password": worker.get("account_password") or "",
        "twofa_secret": worker.get("twofa_secret") or "",
        "proxy": proxy,
        "user_agent": worker.get("user_agent") or "",
        "viewport": {
            "width": int(worker.get("viewport_width") or 1920),
            "height": int(worker.get("viewport_height") or 1080),
        },
        "timezone_id": worker.get("timezone") or "America/New_York",
        "locale": worker.get("locale") or "en-US",
        "work_start": worker.get("work_start", "08:00"),
        "work_end": worker.get("work_end", "23:30"),
        "rest_min_minutes": worker.get("rest_min_minutes", 30),
        "rest_max_minutes": worker.get("rest_max_minutes", 90),
        "daily_target": worker.get("daily_target", 8),
        "traffic_quota_gb": worker.get("traffic_quota_gb", 1000),
        "source_site": worker.get("source_site") or "",
    }


# ---------- push ----------

def push_config(worker: dict) -> None:
    payload = _build_config_payload(worker)
    with SSHClient(**_creds(worker)) as ssh:
        ssh.put_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     f"{WORKER_REMOTE_DIR}/config.json")


FINAL_STATUSES = {"done", "failed", "human_required"}


def _merge_tasks(local_tasks: list[dict], remote_tasks: list[dict]) -> list[dict]:
    """合并本地（中控）和远程（worker）任务列表。

    合并策略：
    - 终态任务（done/failed/human_required）保留 worker 版本
    - 其余以中控版本为准
    - worker 独有的终态历史任务也保留

    纯函数，无 I/O，便于测试。
    """
    remote_by_id = {t["id"]: t for t in remote_tasks if "id" in t}
    local_ids = {t["id"] for t in local_tasks}

    payload = []
    for t in local_tasks:
        tid = t["id"]
        rt = remote_by_id.get(tid)
        if rt and rt.get("status") in FINAL_STATUSES:
            payload.append(rt)
        else:
            payload.append({
                "id": tid,
                "video_url": t["video_url"],
                "caption": t["caption"],
                "status": t.get("status", "scheduled"),
                "scheduled_at": t.get("scheduled_at"),
                "attempt": t.get("attempt", 0),
            })

    for rt in remote_tasks:
        if rt.get("id") not in local_ids and rt.get("status") in FINAL_STATUSES:
            payload.append(rt)

    return payload


def push_tasks(worker: dict, tasks: list[dict]) -> None:
    """推送任务列表到 worker，与 worker 本地 tasks.json 合并而非覆盖。"""
    remote_tasks = []
    try:
        with SSHClient(**_creds(worker)) as ssh:
            remote_text = ssh.get_text(f"{WORKER_REMOTE_DIR}/tasks.json")
            if remote_text:
                remote_tasks = json.loads(remote_text)
    except Exception:
        remote_tasks = []

    payload = _merge_tasks(tasks, remote_tasks)

    with SSHClient(**_creds(worker)) as ssh:
        ssh.put_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     f"{WORKER_REMOTE_DIR}/tasks.json")

    # 推送任务后发送 reload 命令,强制 worker 中断 sleep 并立即拉取任务
    push_command(worker, "reload")


def push_command(worker: dict, action: str, **extra) -> None:
    payload = {"action": action, "ts": datetime.now().isoformat(), **extra}
    with SSHClient(**_creds(worker)) as ssh:
        ssh.put_text(json.dumps(payload), f"{WORKER_REMOTE_DIR}/cmd.json")


# ---------- pull ----------

def pull_state(worker: dict) -> Optional[dict]:
    with SSHClient(**_creds(worker)) as ssh:
        s = ssh.get_text(f"{WORKER_REMOTE_DIR}/state.json")
        return json.loads(s) if s else None


def pull_tasks_status(worker: dict) -> list[dict]:
    with SSHClient(**_creds(worker)) as ssh:
        s = ssh.get_text(f"{WORKER_REMOTE_DIR}/tasks.json")
        return json.loads(s) if s else []


def pull_log_tail(worker: dict, lines: int = 200) -> str:
    with SSHClient(**_creds(worker)) as ssh:
        rc, out, err = ssh.exec(
            f"tail -n {lines} {WORKER_REMOTE_DIR}/log.txt 2>/dev/null || true",
            timeout=20,
        )
        return out


def pull_log_incremental(worker: dict) -> tuple[str, int]:
    """读 worker 日志的新增部分。返回 (新内容, 新 offset)。"""
    last_offset = int(worker.get("last_log_offset") or 0)
    with SSHClient(**_creds(worker)) as ssh:
        rc, size_out, _ = ssh.exec(
            f"wc -c < {WORKER_REMOTE_DIR}/log.txt 2>/dev/null || echo 0",
            timeout=10,
        )
        try:
            total = int(size_out.strip())
        except ValueError:
            total = 0

        if total <= last_offset:
            return "", last_offset

        # 文件被截断 / 重置(总大小变小):从头开始读
        if total < last_offset:
            last_offset = 0

        rc, content, _ = ssh.exec(
            f"tail -c +{last_offset + 1} {WORKER_REMOTE_DIR}/log.txt",
            timeout=30,
        )
        return content, total


def pull_screenshots(worker: dict) -> list[Path]:
    """同步未下载的截图,返回新增的本地路径。"""
    SCREENSHOT_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    new_files = []
    with SSHClient(**_creds(worker)) as ssh:
        names = ssh.list_dir(f"{WORKER_REMOTE_DIR}/screenshots")
        for fn in names:
            if not fn.endswith((".png", ".jpg", ".html")):
                continue
            local = SCREENSHOT_LOCAL_DIR / f"w{worker['id']}_{fn}"
            if local.exists():
                continue
            if ssh.get_file(f"{WORKER_REMOTE_DIR}/screenshots/{fn}", local):
                new_files.append(local)
    return new_files


# ---------- 同步整台 worker 状态到 DB ----------

def sync_worker(worker: dict) -> dict:
    """
    一次完整同步:推 config 是上层职责,这里只做拉取并把 state/任务状态/日志/告警 写回 DB。
    返回 {ok, message}。
    """
    try:
        state = pull_state(worker)
    except Exception as e:
        db.set_worker_status(worker["id"], "error")
        db.add_log(worker["id"], None, "ERROR", f"SSH 连接失败:{e}")
        return {"ok": False, "message": f"连接失败:{e}"}

    # 处理 worker 启动时报告的僵尸任务重置
    reset_ids = state.get("reset_running_tasks", []) if state else []
    if reset_ids:
        for tid in reset_ids:
            db.update_task(tid, status="failed", error_message="worker 重启导致任务中断")
            db.add_log(worker["id"], tid, "INFO", f"Worker 启动时自动重置 running 任务 {tid} 为 failed")
        # 清除 worker state.json 中的标志,避免重复处理
        try:
            with SSHClient(**_creds(worker)) as ssh:
                new_state = dict(state)
                new_state.pop("reset_running_tasks", None)
                new_state.pop("reset_running_at", None)
                ssh.put_text(json.dumps(new_state, ensure_ascii=False, indent=2),
                             f"{WORKER_REMOTE_DIR}/state.json")
        except Exception as e:
            db.add_log(worker["id"], None, "WARN", f"清除 worker reset_running_tasks 标志失败:{e}")

    if state:
        new_status = state.get("status", "idle")
        db.update_worker(
            worker["id"],
            status=new_status,
            last_heartbeat=state.get("heartbeat"),
            traffic_used_gb=state.get("traffic_used_gb", 0) or 0,
        )
        # 处理告警
        if new_status == "human_required":
            alert = state.get("last_alert") or {}
            shots = []
            try:
                shots = pull_screenshots(worker)
            except Exception:
                pass
            local_shot = ""
            if alert.get("screenshot"):
                local_shot = str(SCREENSHOT_LOCAL_DIR / f"w{worker['id']}_{Path(alert['screenshot']).name}")
            # 同一 worker+task 的未解决告警只保留一条,避免每分钟同步导致 18 条重复
            with db.get_conn() as c:
                existed = c.execute(
                    "SELECT id FROM alerts WHERE worker_id = ? AND task_id IS ? AND resolved = 0",
                    (worker["id"], alert.get("task_id")),
                ).fetchone()
            if not existed:
                db.add_alert(
                    worker_id=worker["id"],
                    task_id=alert.get("task_id"),
                    type_="popup_unknown",
                    message=alert.get("message", ""),
                    screenshot_path=local_shot,
                    html_snapshot_path="",
                )

    # 同步任务状态变化 + 兜底创建告警
    # 设计:alert 创建以 task 状态为准(不再依赖 state.status),
    # 因为 worker 重启会清掉 state 但 tasks.json 是持久化的 — 之前的
    # 实现把 alert 创建绑在 state.status 上,会漏掉 worker 重启后
    # state=idle 但 task=human_required 的场景。
    try:
        remote_tasks = pull_tasks_status(worker)
        new_alerts_to_create: list[dict] = []
        for rt in remote_tasks:
            tid = rt.get("id")
            if not tid:
                continue
            fields = {}
            for k in ("status", "started_at", "finished_at", "error_message", "attempt"):
                if k in rt and rt[k] is not None:
                    fields[k] = rt[k]
            if fields:
                db.update_task(tid, **fields)

            # 中控主动探测:任务 "running" 超过 30 分钟且无完成,自动标记超时
            if rt.get("status") == "running" and rt.get("started_at"):
                try:
                    from datetime import datetime
                    started = datetime.strptime(rt["started_at"], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - started).total_seconds() > 1800:
                        db.update_task(
                            tid,
                            status="failed",
                            error_message="任务运行超过30分钟无完成,中控判定超时",
                        )
                        db.add_log(
                            worker["id"], tid, "WARN",
                            f"任务 {tid} 运行超30分钟,自动标记超时失败",
                        )
                except Exception:
                    pass

            if rt.get("status") == "human_required":
                with db.get_conn() as c:
                    existed = c.execute(
                        "SELECT id FROM alerts WHERE worker_id = ? AND task_id = ? AND resolved = 0",
                        (worker["id"], tid),
                    ).fetchone()
                if not existed:
                    new_alerts_to_create.append(rt)

        if new_alerts_to_create:
            # 有新告警才拉一次截图,避免每次同步都拖
            try:
                pull_screenshots(worker)
            except Exception:
                pass
            for rt in new_alerts_to_create:
                shot_path = rt.get("screenshot_path") or ""
                local_shot = ""
                if shot_path:
                    local_shot = str(
                        SCREENSHOT_LOCAL_DIR / f"w{worker['id']}_{Path(shot_path).name}"
                    )
                db.add_alert(
                    worker_id=worker["id"],
                    task_id=rt.get("id"),
                    type_="popup_unknown",
                    message=rt.get("error_message", "") or "task 状态为 human_required",
                    screenshot_path=local_shot,
                    html_snapshot_path="",
                )
    except Exception as e:
        db.add_log(worker["id"], None, "WARN", f"同步任务状态失败:{e}")

    # 增量拉日志,避免重复
    try:
        new_content, new_offset = pull_log_incremental(worker)
        if new_content:
            for line in new_content.splitlines():
                line = line.strip()
                if not line:
                    continue
                db.add_log(worker["id"], None, "INFO", line)
            db.update_worker(worker["id"], last_log_offset=new_offset)
    except Exception as e:
        db.add_log(worker["id"], None, "WARN", f"增量同步日志失败:{e}")

    return {"ok": True, "message": "synced"}


# ---------- 部署 ----------

PROVISION_SH = r"""#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3 python3-pip python3-venv vnstat ffmpeg curl wget

# 开 4GB swap（2GB 内存机器必需，否则 Chromium OOM）
if [ ! -f /swapfile ]; then
    fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

systemctl enable vnstat || true
systemctl start vnstat || true

# 装 gost(SOCKS5 中继,因为 chromium 不支持带认证的 socks5)
if [ ! -f /usr/local/bin/gost ]; then
    cd /tmp
    GOST_URL="https://github.com/ginuerzh/gost/releases/download/v2.11.5/gost-linux-amd64-2.11.5.gz"
    wget -q "$GOST_URL" -O gost.gz
    gunzip -f gost.gz
    mv gost /usr/local/bin/gost
    chmod +x /usr/local/bin/gost
fi

mkdir -p /opt/twitter-worker /opt/twitter-worker/screenshots

if [ ! -d /opt/twitter-worker/venv ]; then
    python3 -m venv /opt/twitter-worker/venv
fi

/opt/twitter-worker/venv/bin/pip install --upgrade pip
/opt/twitter-worker/venv/bin/pip install playwright yt-dlp PySocks pyotp curl-cffi

/opt/twitter-worker/venv/bin/playwright install chromium
/opt/twitter-worker/venv/bin/playwright install-deps chromium

echo "PROVISION_DONE"
"""

SYSTEMD_UNIT = """[Unit]
Description=Twitter Matrix Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/twitter-worker
ExecStart=/opt/twitter-worker/venv/bin/python /opt/twitter-worker/worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def _push_all_worker_py(ssh) -> int:
    """把 worker/ 下所有 .py 文件推到 worker:/opt/twitter-worker/。返回推了几个。"""
    n = 0
    for f in sorted(WORKER_LOCAL_DIR.glob("*.py")):
        ssh.put_file(f, f"{WORKER_REMOTE_DIR}/{f.name}")
        n += 1
    return n


def provision_worker(
    worker: dict,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """初次部署:装环境 + 推代码 + 注册 systemd。"""
    creds = _creds(worker)

    def report(msg):
        if on_progress:
            on_progress(msg)

    try:
        with SSHClient(**creds, timeout=30) as ssh:
            report("上传 provision.sh ...")
            ssh.put_text(PROVISION_SH, "/root/provision_worker.sh")
            ssh.exec("chmod +x /root/provision_worker.sh")

            report("装系统依赖、Python venv、Playwright(几分钟)...")
            rc, out, err = ssh.exec("bash /root/provision_worker.sh 2>&1", timeout=1200)
            if rc != 0 or "PROVISION_DONE" not in out:
                return False, f"环境准备失败 (rc={rc}):\n{out[-2000:]}"

            report("推送 worker 代码...")
            n = _push_all_worker_py(ssh)
            report(f"已推送 {n} 个 .py 文件")

            report("写入 config.json + 空 tasks.json...")
            payload = _build_config_payload(worker)
            ssh.put_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         f"{WORKER_REMOTE_DIR}/config.json")
            ssh.put_text("[]", f"{WORKER_REMOTE_DIR}/tasks.json")

            report("注册 systemd 服务...")
            ssh.put_text(SYSTEMD_UNIT, "/etc/systemd/system/twitter-worker.service")
            ssh.exec("systemctl daemon-reload")
            ssh.exec("systemctl enable twitter-worker")
            ssh.exec("systemctl restart twitter-worker")

            rc, out, err = ssh.exec("systemctl is-active twitter-worker", timeout=10)
            if "active" not in out:
                rc2, log_out, _ = ssh.exec(
                    "journalctl -u twitter-worker --no-pager -n 50", timeout=10
                )
                return False, f"服务未启动:\n{log_out}"

        db.set_worker_status(worker["id"], "idle")
        return True, "部署成功"
    except Exception as e:
        return False, f"异常:{e}"


def update_worker_code(worker: dict) -> tuple[bool, str]:
    """只推代码 + 重启服务,不重装环境。"""
    try:
        with SSHClient(**_creds(worker)) as ssh:
            n = _push_all_worker_py(ssh)
            ssh.exec("systemctl restart twitter-worker")
        return True, f"已更新 {n} 个 .py 文件并重启"
    except Exception as e:
        return False, str(e)


def restart_worker(worker: dict) -> tuple[bool, str]:
    try:
        with SSHClient(**_creds(worker)) as ssh:
            rc, out, err = ssh.exec("systemctl restart twitter-worker", timeout=30)
            if rc == 0:
                return True, "已重启"
            return False, err or out
    except Exception as e:
        return False, str(e)


def kill_worker_processes(worker: dict) -> tuple[bool, str]:
    """物理杀掉 worker 上的任务子进程并重启服务。

    用于急停:删除正在 running 的任务时,必须终止 worker 防止它继续执行。
    先 SIGTERM 优雅终止,2 秒后 SIGKILL 确保杀干净,最后 systemctl restart 拉起新实例。
    """
    try:
        with SSHClient(**_creds(worker)) as ssh:
            # 先优雅终止 worker 主进程
            ssh.exec("pkill -TERM -f 'python.*worker\\.py' 2>/dev/null || true", timeout=10)
            time.sleep(2)
            # 强制终止所有残留(yt-dlp/ffmpeg/chromium 可能作为孤儿进程残留)
            ssh.exec("pkill -9 -f 'python.*worker\\.py' 2>/dev/null || true", timeout=10)
            ssh.exec("pkill -9 -f 'yt-dlp' 2>/dev/null || true", timeout=10)
            ssh.exec("pkill -9 -f 'ffmpeg' 2>/dev/null || true", timeout=10)
            # 重启服务(systemd 会拉起新 worker 进程)
            ssh.exec("systemctl restart twitter-worker", timeout=30)
        return True, "已物理杀掉进程并重启"
    except Exception as e:
        return False, str(e)
