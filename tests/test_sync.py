"""同步逻辑测试。"""

import json

import pytest
import database as db
import sync as syn


class TestBuildConfigPayload:
    def test_proxy_config(self, temp_db):
        """config.json 应包含代理配置。"""
        pid = db.add_proxy("p1", "192.168.1.1", 1080, "user", "pass")
        wid = db.add_worker(
            nickname="test", vps_host="1.2.3.4", proxy_id=pid,
            user_agent="Test UA", cookie_json="[{\"name\":\"auth\"}]",
        )
        worker = db.get_worker(wid)
        payload = syn._build_config_payload(worker)

        assert payload["worker_id"] == wid
        assert payload["user_agent"] == "Test UA"
        assert payload["cookie_json"] == '[{"name":"auth"}]'
        assert payload["proxy"]["host"] == "192.168.1.1"
        assert payload["proxy"]["port"] == 1080
        assert payload["proxy"]["username"] == "user"
        assert payload["proxy"]["password"] == "pass"

    def test_no_proxy(self, temp_db):
        """没有代理时 proxy 为 None。"""
        wid = db.add_worker(nickname="test", vps_host="1.2.3.4")
        worker = db.get_worker(wid)
        payload = syn._build_config_payload(worker)
        assert payload["proxy"] is None


class TestPushTasksMerge:
    def test_final_status_preserved(self, temp_db):
        """Worker 端标记为 done/failed/human_required 的任务不应被覆盖。"""
        tasks = [
            {"id": 1, "video_url": "https://a.com/1", "caption": "c1", "status": "scheduled"},
            {"id": 2, "video_url": "https://a.com/2", "caption": "c2", "status": "scheduled"},
        ]
        remote = [
            {"id": 1, "video_url": "https://a.com/1", "caption": "c1", "status": "done", "finished_at": "2024-01-01"},
        ]

        result = syn._merge_tasks(tasks, remote)

        t1 = next(t for t in result if t["id"] == 1)
        assert t1["status"] == "done"
        assert t1.get("finished_at") == "2024-01-01"

        t2 = next(t for t in result if t["id"] == 2)
        assert t2["status"] == "scheduled"

    def test_new_tasks_override(self, temp_db):
        """非终态任务以中控版本为准。"""
        tasks = [
            {"id": 1, "video_url": "https://a.com/1", "caption": "c1", "status": "scheduled"},
        ]
        remote = [
            {"id": 1, "video_url": "https://a.com/1", "caption": "c1", "status": "running"},
        ]

        result = syn._merge_tasks(tasks, remote)
        assert result[0]["status"] == "scheduled"

    def test_final_only_remote_preserved(self, temp_db):
        """worker 独有的终态历史任务也应保留。"""
        tasks = [
            {"id": 1, "video_url": "https://a.com/1", "caption": "c1", "status": "scheduled"},
        ]
        remote = [
            {"id": 2, "video_url": "https://a.com/2", "caption": "c2", "status": "done"},
        ]

        result = syn._merge_tasks(tasks, remote)
        assert len(result) == 2
        t2 = next(t for t in result if t["id"] == 2)
        assert t2["status"] == "done"
