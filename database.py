"""推特矩阵搬运系统 V2.0 - 数据库层

支持：
1. accounts 账号库（含 account_type 分类字段）
2. global_settings 全局设置 KV 表（upload_wait_time / task_rest_time / parse_mode / video_format）
3. task_queue 任务队列（状态机 + 实时日志追加）
"""
import sqlite3
from datetime import datetime

DB_PATH = "tweet_matrix.db"

# === 默认值 ===
DEFAULT_UPLOAD_WAIT_TIME = 15      # 视频上传后等待转码（分钟）
DEFAULT_TASK_REST_TIME = 30        # 单任务执行后休眠（分钟）
DEFAULT_PARSE_MODE = "regular"     # regular | sniff
DEFAULT_VIDEO_FORMAT = "best"      # best | mp4 | 1080p | 720p

# === 任务状态常量 ===
TASK_PENDING = "等待中"
TASK_RUNNING = "执行中"
TASK_SUCCESS = "成功"
TASK_FAILED = "失败"


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            remark       TEXT    NOT NULL,
            account_type TEXT    NOT NULL DEFAULT '通用',
            proxy_host   TEXT    NOT NULL,
            proxy_port   TEXT    NOT NULL,
            proxy_user   TEXT,
            proxy_pass   TEXT,
            user_agent   TEXT    NOT NULL,
            cookie       TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
        )
        """
    )

    # 字段迁移：旧库可能没有 account_type
    cur.execute("PRAGMA table_info(accounts)")
    existing_cols = {r["name"] for r in cur.fetchall()}
    if "account_type" not in existing_cols:
        cur.execute("ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT '通用'")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS global_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS task_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER NOT NULL,
            video_url     TEXT    NOT NULL,
            tweet_caption TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT '等待中',
            log_messages  TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL,
            started_at    TEXT,
            finished_at   TEXT,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON task_queue(status)")

    conn.commit()

    # 初始化默认设置
    defaults = {
        "upload_wait_time": str(DEFAULT_UPLOAD_WAIT_TIME),
        "task_rest_time": str(DEFAULT_TASK_REST_TIME),
        "parse_mode": DEFAULT_PARSE_MODE,
        "video_format": DEFAULT_VIDEO_FORMAT,
    }
    for k, v in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO global_settings (key, value) VALUES (?, ?)",
            (k, v),
        )
    conn.commit()
    conn.close()


# ============================================================
# 账号 (accounts)
# ============================================================
def add_account(remark, account_type, proxy_host, proxy_port,
                proxy_user, proxy_pass, user_agent, cookie):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO accounts
            (remark, account_type, proxy_host, proxy_port,
             proxy_user, proxy_pass, user_agent, cookie, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            remark, account_type, proxy_host, proxy_port,
            proxy_user, proxy_pass, user_agent, cookie,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_all_accounts():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_account_by_id(account_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_account(account_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [account_id]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE accounts SET {cols} WHERE id = ?", values)
    conn.commit()
    conn.close()


def delete_account(account_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()


# ============================================================
# 全局设置 (global_settings)
# ============================================================
def get_setting(key, default=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM global_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO global_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_all_settings():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM global_settings")
    rows = {r["key"]: r["value"] for r in cur.fetchall()}
    conn.close()
    return rows


# ============================================================
# 任务队列 (task_queue)
# ============================================================
def add_task(account_id, video_url, tweet_caption):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO task_queue
            (account_id, video_url, tweet_caption, status, log_messages, created_at)
        VALUES (?, ?, ?, ?, '', ?)
        """,
        (
            account_id, video_url, tweet_caption, TASK_PENDING,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_tasks(status=None, limit=200):
    """获取任务列表（含账号 remark / type 关联）。"""
    conn = get_conn()
    cur = conn.cursor()
    if status:
        cur.execute(
            """
            SELECT t.*, a.remark AS account_remark, a.account_type AS account_type
            FROM task_queue t
            LEFT JOIN accounts a ON t.account_id = a.id
            WHERE t.status = ?
            ORDER BY t.id ASC
            LIMIT ?
            """,
            (status, limit),
        )
    else:
        cur.execute(
            """
            SELECT t.*, a.remark AS account_remark, a.account_type AS account_type
            FROM task_queue t
            LEFT JOIN accounts a ON t.account_id = a.id
            ORDER BY t.id DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.*, a.remark AS account_remark, a.account_type AS account_type
        FROM task_queue t
        LEFT JOIN accounts a ON t.account_id = a.id
        WHERE t.id = ?
        """,
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_next_pending_task():
    """取队列里最早入队的一个等待任务。worker 用。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.*, a.remark AS account_remark, a.account_type AS account_type
        FROM task_queue t
        LEFT JOIN accounts a ON t.account_id = a.id
        WHERE t.status = ?
        ORDER BY t.id ASC
        LIMIT 1
        """,
        (TASK_PENDING,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_task_status(task_id, status, mark_started=False, mark_finished=False):
    conn = get_conn()
    cur = conn.cursor()
    sets = ["status = ?"]
    vals = [status]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if mark_started:
        sets.append("started_at = ?")
        vals.append(now)
    if mark_finished:
        sets.append("finished_at = ?")
        vals.append(now)
    vals.append(task_id)
    cur.execute(
        f"UPDATE task_queue SET {', '.join(sets)} WHERE id = ?",
        vals,
    )
    conn.commit()
    conn.close()


def append_task_log(task_id, message):
    """向任务的 log_messages 字段追加一行带时间戳的日志。线程安全。"""
    conn = get_conn()
    cur = conn.cursor()
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    cur.execute(
        """
        UPDATE task_queue
        SET log_messages = COALESCE(log_messages, '') || ?
        WHERE id = ?
        """,
        (line, task_id),
    )
    conn.commit()
    conn.close()


def delete_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM task_queue WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


def clear_finished_tasks():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM task_queue WHERE status IN (?, ?)",
        (TASK_SUCCESS, TASK_FAILED),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


def reset_running_tasks():
    """启动时调用，回收上次未结束的执行中任务。防止崩溃后任务卡死。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE task_queue SET status = ? WHERE status = ?",
        (TASK_PENDING, TASK_RUNNING),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected
