"""
任务调度。

每个 worker 每天跑 daily_target 条任务,在 work_start..work_end 时间窗口内分布,
相邻任务起始时间间隔 = 任务执行预估时长 + 随机休息(rest_min..rest_max 分钟)。

plan_today_for_worker(worker) 会把当天还未跑的 scheduled 任务先回退到 pending,
再从 pending 池里按 ID 顺序挑 daily_target 条排今日时间表。
"""

import random
from datetime import datetime, date, time as dtime, timedelta

import database as db


TASK_DURATION_ESTIMATE_MIN = 60


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def plan_today_for_worker(worker: dict) -> int:
    wid = worker["id"]
    target = int(worker.get("daily_target") or 8)
    rest_min = int(worker.get("rest_min_minutes") or 30)
    rest_max = int(worker.get("rest_max_minutes") or 90)
    if rest_max < rest_min:
        rest_max = rest_min

    # 已 done / failed / human_required 不动;scheduled 回退;pending 不动
    with db.get_conn() as c:
        c.execute(
            "UPDATE tasks SET status='pending', scheduled_at=NULL "
            "WHERE worker_id = ? AND status = 'scheduled'",
            (wid,),
        )

    pending = db.list_tasks(worker_id=wid, status="pending", limit=target)
    if not pending:
        return 0

    today = date.today()
    sh, sm = _parse_hm(worker.get("work_start") or "08:00")
    eh, em = _parse_hm(worker.get("work_end") or "23:30")

    start_base = datetime.combine(today, dtime(sh, sm))
    end_dt = datetime.combine(today, dtime(eh, em))

    cur = start_base + timedelta(minutes=random.randint(-30, 30))
    now = datetime.now()
    if cur < now:
        cur = now + timedelta(minutes=5)

    pending.sort(key=lambda t: t["id"])

    n = 0
    for t in pending:
        if n >= target or cur > end_dt:
            break
        db.update_task(
            t["id"],
            status="scheduled",
            scheduled_at=cur.strftime("%Y-%m-%d %H:%M:%S"),
        )
        n += 1
        rest = random.randint(rest_min, rest_max)
        cur = cur + timedelta(minutes=TASK_DURATION_ESTIMATE_MIN + rest)

    return n


def plan_today_for_all() -> dict[int, int]:
    return {w["id"]: plan_today_for_worker(w) for w in db.list_workers()}


def get_today_active_tasks(worker_id: int) -> list[dict]:
    """返回该 worker 当前应该让 worker 进程看到的任务(scheduled + running)。"""
    today = date.today().isoformat()
    rows = []
    for t in db.list_tasks(worker_id=worker_id, limit=500):
        if t.get("status") not in ("scheduled", "running"):
            continue
        sched = t.get("scheduled_at") or ""
        if not sched.startswith(today):
            continue
        rows.append(t)
    return rows
