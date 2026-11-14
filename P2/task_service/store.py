"""Single-owner SQLite state. Each edit reloads state inside one transaction.

Network operations never hold a transaction. A response therefore cannot erase a
cancel request that arrived while awaiting P1. Results and success commit together.
"""

import json
import os
import sqlite3

from task_service.contract import JOB_TERMINAL, TASK_TERMINAL, APIError, canonical, now


def refresh(state):
    job = state["job"]
    statuses = [t["status"] for t in job["tasks"]]
    job["counts"] = {
        s.lower(): statuses.count(s)
        for s in (
            "QUEUED",
            "WAITING_FOR_RESOURCES",
            "DISPATCHING",
            "RUNNING",
            "CANCELLING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        )
    }
    job["counts"]["total"] = len(statuses)
    if job["status"] in JOB_TERMINAL:
        return
    terminal = all(s in TASK_TERMINAL for s in statuses)
    if job["cancel_requested"]:
        status = "CANCELLED" if terminal else "CANCELLING"
    elif all(s == "SUCCEEDED" for s in statuses):
        status = "SUCCEEDED"
    elif terminal:
        status = "PARTIAL_FAILED" if "SUCCEEDED" in statuses else "FAILED"
    elif any(s in {"RUNNING", "DISPATCHING"} for s in statuses):
        status = "RUNNING"
    elif "WAITING_FOR_RESOURCES" in statuses:
        status = "WAITING_FOR_RESOURCES"
    else:
        status = "QUEUED"
    job["status"] = status
    if status in {"RUNNING", "CANCELLING"} and job["started_at"] is None:
        job["started_at"] = now()
    if status in JOB_TERMINAL:
        job["finished_at"] = now()
        job["download_path"] = f"/api/task/v1/jobs/{job['job_id']}/download"


class Store:
    def __init__(self, data_dir):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = (data_dir / "p2.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.lock.write(b"0")
                self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("TASK_DATA_DIR 已被其他 P2 进程占用；仅支持单 worker") from None
        self.db = sqlite3.connect(data_dir / "tasks.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preparations (key TEXT PRIMARY KEY, state TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()
        self.lock.close()

    def replay(self, payload):
        row = self.db.execute(
            "SELECT payload,state FROM jobs WHERE client_id=?", (payload["client_request_id"],)
        ).fetchone()
        if row:
            if row[0] != canonical(payload):
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "同一 client_request_id 的请求内容不同")
            return json.loads(row[1])
        return None

    def create(self, payload, state):
        with self.db:
            existing = self.replay(payload)
            if existing:
                return existing, False
            refresh(state)
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                (
                    state["job"]["job_id"],
                    payload["client_request_id"],
                    canonical(payload),
                    canonical(state),
                ),
            )
        return state, True

    def get(self, job_id):
        row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise APIError(404, "NOT_FOUND", "Job 不存在")
        return json.loads(row[0])

    def active(self):
        return [
            s
            for (raw,) in self.db.execute("SELECT state FROM jobs ORDER BY rowid")
            if (s := json.loads(raw))["job"]["status"] not in JOB_TERMINAL
        ]

    def edit(self, job_id, change):
        with self.db:
            state = self.get(job_id)
            if state["job"]["status"] not in JOB_TERMINAL:
                change(state)
                refresh(state)
                self.db.execute("UPDATE jobs SET state=? WHERE id=?", (canonical(state), job_id))
        return state

    def preparations(self):
        return {
            key: json.loads(raw)
            for key, raw in self.db.execute("SELECT key,state FROM preparations")
        }

    def save_preparation(self, key, state):
        with self.db:
            self.db.execute(
                "INSERT INTO preparations VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                (key, canonical(state)),
            )
