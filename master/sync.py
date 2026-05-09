"""中控 ↔ Worker 业务同步层。

- push_*  把数据从中控推到 Worker
- pull_*  从 Worker 拉数据回中控
- provision_worker / update_worker_code  初次部署 / 更新代码
"""

import json
import shutil
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
    }


# ---------- push ----------

def push_config(worker: dict) -> None:
    payload = _build_config_payload(worker)
    with SSHClient(**_creds(worker)) as ssh:
        ssh.put_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     f"{WORKER_REMOTE_DIR}/config.json")


def push_tasks(worker: dict, tasks: list[dict]) -> None:
    payload = []
    for t in tasks:
        payload.append({
            "id": t["id"],
            "video_url": t["video_url"],
            "caption": t["caption"],
            "status": t.get("status", "scheduled"),
            "scheduled_at": t.get("scheduled_at"),
            "attempt": t.get("attempt", 0),
        })
    with SSHClient(**_creds(worker)) as ssh:
        ssh.put_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     f"{WORKER_REMOTE_DIR}/tasks.json")


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

    # 同步任务状态变化
    try:
        remote_tasks = pull_tasks_status(worker)
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
    except Exception as e:
        db.add_log(worker["id"], None, "WARN", f"同步任务状态失败:{e}")

    # 拉日志最后 200 行
    try:
        tail = pull_log_tail(worker, lines=200)
        if tail:
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                db.add_log(worker["id"], None, "INFO", line)
    except Exception:
        pass

    return {"ok": True, "message": "synced"}


# ---------- 部署 ----------

PROVISION_SH = r"""#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3 python3-pip python3-venv vnstat ffmpeg curl wget

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
/opt/twitter-worker/venv/bin/pip install playwright yt-dlp PySocks pyotp

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
