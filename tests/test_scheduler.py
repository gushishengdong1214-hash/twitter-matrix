"""任务调度逻辑测试。"""

from datetime import datetime, timedelta

import pytest
import database as db
import scheduler as sched


class TestPlanTodayForWorker:
    def test_basic_scheduling(self, temp_db):
        """正常排期：pending 任务变为 scheduled。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4", daily_target=3)
        db.add_tasks_bulk(wid, [
            ("https://a.com/1", "c1"),
            ("https://a.com/2", "c2"),
            ("https://a.com/3", "c3"),
            ("https://a.com/4", "c4"),
        ])

        worker = db.get_worker(wid)
        n = sched.plan_today_for_worker(worker)

        assert n == 3  # daily_target=3
        scheduled = db.list_tasks(worker_id=wid, status="scheduled")
        assert len(scheduled) == 3

    def test_skip_unhealthy_worker(self, temp_db):
        """unhealthy Worker 应返回 -1。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        db.update_worker(wid, status="human_required")
        result = sched.plan_today_for_all()
        assert result[wid] == -1

    def test_stale_running_reset(self, temp_db):
        """超过 2 小时的 running 任务应回退为 pending，再被重新排期。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        db.add_tasks_bulk(wid, [("https://a.com/1", "c1")])
        # 手动设为 running 且 started_at 很早
        db.update_task(1, status="running", started_at="2020-01-01 00:00:00")

        worker = db.get_worker(wid)
        sched.plan_today_for_worker(worker)

        tasks = db.list_tasks(worker_id=wid)
        # stale running 先被回退为 pending，然后 scheduler 重新排期为 scheduled
        assert tasks[0]["status"] in ("scheduled", "pending")
        assert tasks[0]["status"] != "running"

    def test_respect_daily_target(self, temp_db):
        """pending 多于 daily_target 时只排目标数。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4", daily_target=2)
        db.add_tasks_bulk(wid, [
            ("https://a.com/1", "c1"),
            ("https://a.com/2", "c2"),
            ("https://a.com/3", "c3"),
        ])

        worker = db.get_worker(wid)
        n = sched.plan_today_for_worker(worker)

        assert n == 2
        pending = db.list_tasks(worker_id=wid, status="pending")
        assert len(pending) == 1  # 剩余 1 条

    def test_no_pending_tasks(self, temp_db):
        """没有 pending 任务时返回 0。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        worker = db.get_worker(wid)
        assert sched.plan_today_for_worker(worker) == 0


class TestGetTodayActiveTasks:
    def test_only_today_scheduled(self, temp_db):
        """只返回今天的 scheduled + running。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        db.add_tasks_bulk(wid, [
            ("https://a.com/1", "c1"),
            ("https://a.com/2", "c2"),
        ])

        # 今天 scheduled
        db.update_task(1, status="scheduled", scheduled_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 昨天 scheduled（不应返回）
        db.update_task(2, status="scheduled", scheduled_at="2020-01-01 12:00:00")

        active = sched.get_today_active_tasks(wid)
        assert len(active) == 1
        assert active[0]["id"] == 1
