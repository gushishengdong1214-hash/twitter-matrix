"""数据库操作测试。"""

import pytest
import database as db


class TestCrawledVideos:
    def test_add_crawled_video_success(self, temp_db):
        """正常插入记录。"""
        vid = db.add_crawled_video(
            url="https://example.com/video1",
            url_hash="abc123",
            site="jable.tv",
            title="Test Video",
            score=80,
        )
        assert vid is not None
        assert vid > 0

    def test_add_crawled_video_dedup(self, temp_db):
        """同一 url_hash 重复插入应返回 None。"""
        db.add_crawled_video(url="https://a.com/1", url_hash="dup", site="jable.tv")
        result = db.add_crawled_video(url="https://a.com/1", url_hash="dup", site="jable.tv")
        assert result is None

    def test_approve_crawled_video(self, temp_db):
        """审批后状态变为 approved，并生成任务。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        vid = db.add_crawled_video(
            url="https://example.com/v", url_hash="h1", site="jable.tv",
            translated_caption="Test Caption",
        )
        tid = db.approve_crawled_video(vid, wid)
        assert tid is not None

        row = db.list_crawled_videos(status="approved")[0]
        assert row["status"] == "approved"
        assert row["worker_id"] == wid

        tasks = db.list_tasks(worker_id=wid)
        assert len(tasks) == 1
        assert tasks[0]["caption"] == "Test Caption"

    def test_list_crawled_videos_filter(self, temp_db):
        """按 status 过滤。"""
        db.add_crawled_video(url="https://a.com/1", url_hash="h1", site="jable.tv")
        vid2 = db.add_crawled_video(url="https://a.com/2", url_hash="h2", site="jable.tv")
        vid3 = db.add_crawled_video(url="https://a.com/3", url_hash="h3", site="jable.tv")
        db.update_crawled_video(vid2, status="approved")
        db.update_crawled_video(vid3, status="rejected")

        assert len(db.list_crawled_videos(status="pending")) == 1
        assert len(db.list_crawled_videos(status="approved")) == 1
        assert len(db.list_crawled_videos(status="rejected")) == 1


class TestTasks:
    def test_update_task(self, temp_db):
        """动态字段更新。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        tid = db.add_tasks_bulk(wid, [("https://a.com/v", "caption")])

        db.update_task(tid, status="running", started_at="2024-01-01 12:00:00")
        tasks = db.list_tasks(worker_id=wid)
        assert tasks[0]["status"] == "running"
        assert tasks[0]["started_at"] == "2024-01-01 12:00:00"

    def test_task_counts_by_worker(self, temp_db):
        """按 Worker 统计任务数。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        db.add_tasks_bulk(wid, [("https://a.com/1", "c1"), ("https://a.com/2", "c2")])

        db.update_task(1, status="done")
        db.update_task(2, status="failed")

        counts = db.task_counts_by_worker()
        assert counts[wid]["done"] == 1
        assert counts[wid]["failed"] == 1


class TestWorkers:
    def test_set_worker_status(self, temp_db):
        """更新 Worker 状态。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        db.set_worker_status(wid, "running")
        w = db.get_worker(wid)
        assert w["status"] == "running"

    def test_list_workers_with_proxy(self, temp_db):
        """list_workers 应返回代理信息。"""
        pid = db.add_proxy("p1", "1.2.3.4", 1080, "", "")
        db.add_worker(nickname="test", vps_host="1.2.3.4", proxy_id=pid)

        workers = db.list_workers()
        assert workers[0]["proxy_host"] == "1.2.3.4"
        assert workers[0]["proxy_port"] == 1080
