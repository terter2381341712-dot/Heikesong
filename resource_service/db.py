"""SQLite 持久化：节点、artifact、preparation、execution、事件与幂等记录。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  upload_id TEXT,
  kind TEXT NOT NULL,
  original_name TEXT NOT NULL,
  content_type TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  media_info TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_upload ON artifacts(upload_id) WHERE upload_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS nodes (
  worker_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  name TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'ACTIVE',
  connection TEXT NOT NULL DEFAULT 'ONLINE',
  hardware TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '[]',
  cleanup_pending INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  registered_at TEXT NOT NULL,
  telemetry TEXT
);
CREATE TABLE IF NOT EXISTS preparations (
  preparation_id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  worker_session_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  profile_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  progress REAL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS executions (
  execution_id TEXT PRIMARY KEY,
  request_fingerprint TEXT NOT NULL,
  request TEXT NOT NULL,
  job_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  worker_session_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  profile_digest TEXT NOT NULL,
  input_artifact_id TEXT NOT NULL,
  parameters TEXT NOT NULL,
  timeout_seconds INTEGER NOT NULL,
  status TEXT NOT NULL,
  input_sha256 TEXT,
  created_at TEXT NOT NULL,
  accepted_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  lease_expires_at TEXT,
  hard_deadline_at TEXT,
  result_artifact_id TEXT,
  timings TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL,
  type TEXT NOT NULL,
  worker_id TEXT,
  job_id TEXT,
  task_id TEXT,
  execution_id TEXT,
  preparation_id TEXT,
  message TEXT NOT NULL
);
"""


def parse_json(text: Optional[str], default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)


class Database:
    """单进程 + 线程锁的 SQLite 封装；演示规模足够，WAL 提高并发读。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def write(self) -> "_WriteTx":
        return _WriteTx(self)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def insert_event(
        self,
        occurred_at: str,
        type_: str,
        message: str,
        worker_id: Optional[str] = None,
        job_id: Optional[str] = None,
        task_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        preparation_id: Optional[str] = None,
    ) -> int:
        """在写事务外单独记录事件也可（事件为追加日志，不参与业务原子性）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (occurred_at, type, worker_id, job_id, task_id, execution_id, preparation_id, message)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (occurred_at, type_, worker_id, job_id, task_id, execution_id, preparation_id, message),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _WriteTx:
    def __init__(self, db: Database) -> None:
        self._db = db

    def __enter__(self) -> sqlite3.Connection:
        self._db._lock.acquire()
        return self._db._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._db._conn.commit()
            else:
                self._db._conn.rollback()
        finally:
            self._db._lock.release()
