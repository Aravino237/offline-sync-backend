import sqlite3
import os
import threading

DB_PATH = os.environ.get("SYNC_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "sync.db"))

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    fields TEXT NOT NULL DEFAULT '{}',
    version TEXT NOT NULL DEFAULT '{}',
    delete_ts TEXT,
    pending_conflict TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_changes (
    device_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, change_id)
);

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL,
    applied_fields TEXT,
    rejected_fields TEXT,
    server_version_after TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_log_record ON change_log(record_id);
"""


def get_connection() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()


def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    wal = DB_PATH + "-wal"
    shm = DB_PATH + "-shm"
    for f in (wal, shm):
        if os.path.exists(f):
            os.remove(f)
    _local.conn = None
    init_db()
