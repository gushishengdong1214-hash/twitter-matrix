"""pytest 共享 fixtures。

每个测试函数自动获得一个临时 SQLite 数据库，避免污染生产数据。
"""

import sys
import tempfile
from pathlib import Path

import pytest

# 确保 tests/ 能导入 master/ 下的模块
sys.path.insert(0, str(Path(__file__).parent.parent / "master"))


@pytest.fixture(scope="function")
def temp_db(monkeypatch):
    """为每个测试提供一个独立的临时数据库。"""
    import database as db
    import sqlite3

    import uuid
    temp_path = Path(tempfile.gettempdir()) / f"twmatrix_test_{uuid.uuid4().hex}.db"

    # 确保全新数据库：删除旧的 + 重新建表
    try:
        temp_path.unlink(missing_ok=True)
    except PermissionError:
        pass

    monkeypatch.setattr(db, "DB_PATH", temp_path)
    db.init_db()

    yield db

    # 测试结束后清理
    try:
        temp_path.unlink(missing_ok=True)
    except PermissionError:
        pass
