"""
中控 Daemon。独立进程跑(不依赖 streamlit)。systemd 拉起。

负责:
  - 每天凌晨 3 点重排所有 worker 的今日任务,并推送 config + tasks 到 worker
  - 每分钟同步一次 worker 状态(state/log/任务进度)
  - 每 5 分钟把 DB 里 scheduled 状态的任务重新推一遍 tasks.json(防丢)

可选环境变量:
  TWMATRIX_DAEMON_PLAN_HOUR   (默认 3)
  TWMATRIX_DAEMON_PLAN_MINUTE (默认 0)
  TWMATRIX_DAEMON_SYNC_INTERVAL_S (默认 60)
  TWMATRIX_DAEMON_PUSH_INTERVAL_S (默认 300)
"""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apscheduler.schedulers.blocking import BlockingScheduler

import database as db
import scheduler as sched
import sync as syn


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("daemon")


PLAN_HOUR = int(os.getenv("TWMATRIX_DAEMON_PLAN_HOUR", "3"))
PLAN_MINUTE = int(os.getenv("TWMATRIX_DAEMON_PLAN_MINUTE", "0"))
SYNC_INTERVAL = int(os.getenv("TWMATRIX_DAEMON_SYNC_INTERVAL_S", "60"))
PUSH_INTERVAL = int(os.getenv("TWMATRIX_DAEMON_PUSH_INTERVAL_S", "300"))


def daily_plan_and_push():
    log.info("== 开始今日调度 ==")
    result = sched.plan_today_for_all()
    for wid, n in result.items():
        log.info(f"Worker {wid}: 排 {n} 条")
    push_to_all()


def push_to_all():
    workers = db.list_workers()
    for w in workers:
        try:
            syn.push_config(w)
            tasks = sched.get_today_active_tasks(w["id"])
            syn.push_tasks(w, tasks)
            log.info(f"Worker {w['id']} ({w['nickname']}): 推送 {len(tasks)} 条今日任务")
        except Exception as e:
            log.error(f"推送 worker {w['id']} 失败:{e}")


def sync_all():
    workers = db.list_workers()
    if not workers:
        return
    with ThreadPoolExecutor(max_workers=min(len(workers), 10)) as ex:
        for w in workers:
            ex.submit(_safe_sync, w)


def _safe_sync(w: dict):
    try:
        syn.sync_worker(w)
    except Exception as e:
        log.error(f"sync_worker {w['id']} 失败:{e}")


def main():
    db.init_db()
    s = BlockingScheduler()

    s.add_job(daily_plan_and_push, "cron", hour=PLAN_HOUR, minute=PLAN_MINUTE,
              id="plan_daily", max_instances=1)
    s.add_job(sync_all, "interval", seconds=SYNC_INTERVAL,
              id="sync", max_instances=1, coalesce=True)
    s.add_job(push_to_all, "interval", seconds=PUSH_INTERVAL,
              id="push", max_instances=1, coalesce=True)

    log.info(f"Daemon 启动 plan={PLAN_HOUR:02d}:{PLAN_MINUTE:02d} "
             f"sync_interval={SYNC_INTERVAL}s push_interval={PUSH_INTERVAL}s")

    # 启动时先做一次同步
    try:
        sync_all()
    except Exception as e:
        log.error(f"启动同步异常:{e}")

    try:
        s.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Daemon 退出")


if __name__ == "__main__":
    main()
