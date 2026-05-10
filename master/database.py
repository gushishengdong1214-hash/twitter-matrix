import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Iterable

DB_PATH = Path(__file__).parent / "data" / "matrix.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# SQLite 不是线程安全的写操作；daemon 的 ThreadPoolExecutor 会并发同步多个 Worker
# 用锁确保同一时刻只有一个线程执行 DB 写操作（读可以并发，WAL 模式支持）
_db_write_lock = threading.Lock()

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT,
    password TEXT,
    type TEXT DEFAULT 'static_residential',
    note TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT NOT NULL UNIQUE,
    vps_host TEXT NOT NULL,
    ssh_port INTEGER DEFAULT 22,
    ssh_user TEXT DEFAULT 'root',
    ssh_password TEXT,
    ssh_key_path TEXT,
    proxy_id INTEGER,
    twitter_handle TEXT,
    cookie_json TEXT,
    source_site TEXT,
    work_start TEXT DEFAULT '08:00',
    work_end TEXT DEFAULT '23:30',
    rest_min_minutes INTEGER DEFAULT 30,
    rest_max_minutes INTEGER DEFAULT 90,
    daily_target INTEGER DEFAULT 8,
    traffic_quota_gb INTEGER DEFAULT 1000,
    traffic_used_gb REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    last_heartbeat TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (proxy_id) REFERENCES proxies(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL,
    video_url TEXT NOT NULL,
    caption TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    scheduled_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    attempt INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tasks_worker_status ON tasks(worker_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON tasks(scheduled_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER,
    task_id INTEGER,
    type TEXT,
    message TEXT,
    screenshot_path TEXT,
    html_snapshot_path TEXT,
    resolved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_unresolved ON alerts(resolved, created_at);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER,
    task_id INTEGER,
    level TEXT,
    message TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_logs_worker_time ON logs(worker_id, created_at DESC);

CREATE TABLE IF NOT EXISTS crawled_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    url_hash TEXT NOT NULL UNIQUE,
    site TEXT NOT NULL,
    title TEXT,
    original_description TEXT,
    translated_caption TEXT,
    thumbnail_url TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    worker_id INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_crawled_status ON crawled_videos(status);
CREATE INDEX IF NOT EXISTS idx_crawled_site ON crawled_videos(site);
"""


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """幂等加列。已有列不会重复加。"""
    # workers 表迁移
    existing_workers = {r[1] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
    worker_migrations = [
        ("user_agent", "TEXT"),
        ("viewport_width", "INTEGER DEFAULT 1920"),
        ("viewport_height", "INTEGER DEFAULT 1080"),
        ("timezone", "TEXT DEFAULT 'America/New_York'"),
        ("locale", "TEXT DEFAULT 'en-US'"),
        ("account_password", "TEXT"),
        ("twofa_secret", "TEXT"),
        ("last_log_offset", "INTEGER DEFAULT 0"),
    ]
    for col, typ in worker_migrations:
        if col not in existing_workers:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {col} {typ}")

    # crawled_videos 表迁移
    existing_crawled = {r[1] for r in conn.execute("PRAGMA table_info(crawled_videos)").fetchall()}
    if "score" not in existing_crawled:
        conn.execute("ALTER TABLE crawled_videos ADD COLUMN score INTEGER DEFAULT 0")


@contextmanager
def get_conn():
    # 串行化所有 DB 访问，防止 daemon 的 ThreadPoolExecutor 并发同步时触发
    # "database is locked"（SQLite 写锁竞争）。WAL 模式允许并发读，但写仍串行。
    with _db_write_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ---------- proxies ----------

def list_proxies() -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM proxies ORDER BY id DESC")]


def add_proxy(nickname: str, host: str, port: int, username: str, password: str, note: str = "") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO proxies (nickname, host, port, username, password, note) VALUES (?,?,?,?,?,?)",
            (nickname, host, port, username, password, note),
        )
        return cur.lastrowid


def update_proxy(pid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE proxies SET {cols} WHERE id = ?", (*fields.values(), pid))


def delete_proxy(pid: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM proxies WHERE id = ?", (pid,))


# ---------- workers ----------

def list_workers() -> list[dict]:
    with get_conn() as c:
        rows = c.execute("""
            SELECT w.*, p.host AS proxy_host, p.port AS proxy_port, p.nickname AS proxy_nickname
            FROM workers w LEFT JOIN proxies p ON w.proxy_id = p.id
            ORDER BY w.id ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_worker(wid: int) -> Optional[dict]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM workers WHERE id = ?", (wid,)).fetchone()
        return dict(r) if r else None


def add_worker(**fields) -> int:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_conn() as c:
        cur = c.execute(f"INSERT INTO workers ({cols}) VALUES ({placeholders})", tuple(fields.values()))
        return cur.lastrowid


def update_worker(wid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE workers SET {cols} WHERE id = ?", (*fields.values(), wid))


def delete_worker(wid: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM workers WHERE id = ?", (wid,))


def set_worker_status(wid: int, status: str) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE workers SET status = ?, last_heartbeat = datetime('now', 'localtime') WHERE id = ?",
            (status, wid),
        )


# ---------- tasks ----------

def list_tasks(worker_id: Optional[int] = None, status: Optional[str] = None, limit: int = 500) -> list[dict]:
    sql = "SELECT t.*, w.nickname AS worker_nickname FROM tasks t JOIN workers w ON t.worker_id = w.id WHERE 1=1"
    args: list = []
    if worker_id is not None:
        sql += " AND t.worker_id = ?"
        args.append(worker_id)
    if status is not None:
        sql += " AND t.status = ?"
        args.append(status)
    sql += " ORDER BY t.id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def add_tasks_bulk(worker_id: int, items: Iterable[tuple[str, str]]) -> int:
    rows = [(worker_id, url, caption) for url, caption in items]
    with get_conn() as c:
        c.executemany("INSERT INTO tasks (worker_id, video_url, caption) VALUES (?,?,?)", rows)
        return len(rows)


def update_task(tid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), tid))


def task_counts_by_worker() -> dict[int, dict[str, int]]:
    with get_conn() as c:
        rows = c.execute("""
            SELECT worker_id, status, COUNT(*) AS n FROM tasks GROUP BY worker_id, status
        """).fetchall()
    result: dict[int, dict[str, int]] = {}
    for r in rows:
        result.setdefault(r["worker_id"], {})[r["status"]] = r["n"]
    return result


# ---------- alerts ----------

def add_alert(worker_id: int, task_id: Optional[int], type_: str, message: str,
              screenshot_path: str = "", html_snapshot_path: str = "") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO alerts (worker_id, task_id, type, message, screenshot_path, html_snapshot_path) VALUES (?,?,?,?,?,?)",
            (worker_id, task_id, type_, message, screenshot_path, html_snapshot_path),
        )
        return cur.lastrowid


def list_alerts(only_unresolved: bool = True, limit: int = 200) -> list[dict]:
    sql = """
        SELECT a.*, w.nickname AS worker_nickname FROM alerts a
        LEFT JOIN workers w ON a.worker_id = w.id
    """
    if only_unresolved:
        sql += " WHERE a.resolved = 0"
    sql += " ORDER BY a.id DESC LIMIT ?"
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, (limit,))]


def resolve_alert(aid: int) -> None:
    with get_conn() as c:
        c.execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (aid,))


# ---------- logs ----------

def add_log(worker_id: Optional[int], task_id: Optional[int], level: str, message: str) -> None:
    with get_conn() as c:
        c.execute("INSERT INTO logs (worker_id, task_id, level, message) VALUES (?,?,?,?)",
                  (worker_id, task_id, level, message))


def list_logs(worker_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    sql = "SELECT l.*, w.nickname AS worker_nickname FROM logs l LEFT JOIN workers w ON l.worker_id = w.id"
    args: list = []
    if worker_id is not None:
        sql += " WHERE l.worker_id = ?"
        args.append(worker_id)
    sql += " ORDER BY l.id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def prune_logs(keep_last: int = 20000) -> None:
    with get_conn() as c:
        c.execute("""
            DELETE FROM logs WHERE id NOT IN (
                SELECT id FROM logs ORDER BY id DESC LIMIT ?
            )
        """, (keep_last,))


# ---------- crawled_videos ----------

def add_crawled_video(url: str, url_hash: str, site: str, title: str = "",
                      original_description: str = "", translated_caption: str = "",
                      thumbnail_url: str = "", score: int = 0) -> int | None:
    """插入一条采集记录。如果 url_hash 已存在则返回 None（去重）。"""
    with get_conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO crawled_videos
                    (url, url_hash, site, title, original_description, translated_caption, thumbnail_url, score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, url_hash, site, title, original_description, translated_caption, thumbnail_url, score),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def is_url_crawled(url_hash: str) -> bool:
    with get_conn() as c:
        r = c.execute("SELECT 1 FROM crawled_videos WHERE url_hash = ? LIMIT 1", (url_hash,)).fetchone()
        return bool(r)


def list_crawled_videos(status: Optional[str] = None, site: Optional[str] = None,
                        limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM crawled_videos WHERE 1=1"
    args: list = []
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    if site is not None:
        sql += " AND site = ?"
        args.append(site)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def update_crawled_video(vid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE crawled_videos SET {cols} WHERE id = ?", (*fields.values(), vid))


def delete_crawled_video(vid: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM crawled_videos WHERE id = ?", (vid,))


def approve_crawled_video(vid: int, worker_id: int) -> int | None:
    """把一条已采集的视频批准并转为任务。返回任务 ID。"""
    with get_conn() as c:
        row = c.execute("SELECT * FROM crawled_videos WHERE id = ?", (vid,)).fetchone()
        if not row:
            return None
        row = dict(row)
        caption = row.get("translated_caption") or row.get("title") or ""
        cur = c.execute(
            "INSERT INTO tasks (worker_id, video_url, caption, status) VALUES (?, ?, ?, 'pending')",
            (worker_id, row["url"], caption),
        )
        c.execute("UPDATE crawled_videos SET status = 'approved', worker_id = ? WHERE id = ?",
                  (worker_id, vid))
        return cur.lastrowid


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
