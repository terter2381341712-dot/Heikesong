"""Per-process capacity ledger. All mutations are called under Database.write()."""

import uuid

ACTIVE = ("ACCEPTED", "RUNNING", "CANCEL_REQUESTED")


def provision(conn, worker_id, count):
    conn.execute("DELETE FROM execution_slots WHERE worker_id=?", (worker_id,))
    conn.executemany(
        "INSERT INTO execution_slots(worker_id,slot_id,generation) VALUES (?,?,?)",
        [(worker_id, i, str(uuid.uuid4())) for i in range(count)],
    )


def snapshot(db, worker_id):
    return [
        dict(r)
        for r in db.query(
            """SELECT s.*, (SELECT e.execution_id FROM executions e
        WHERE e.worker_id=s.worker_id AND e.slot_id=s.slot_id
        AND e.status IN ('ACCEPTED','RUNNING','CANCEL_REQUESTED') LIMIT 1) AS execution_id
        FROM execution_slots s WHERE s.worker_id=? ORDER BY s.slot_id""",
            (worker_id,),
        )
    ]


def free(db, worker_id):
    return db.query(
        """SELECT s.slot_id FROM execution_slots s
        WHERE s.worker_id=? AND s.ready=1 AND s.blocked=0 AND NOT EXISTS (
          SELECT 1 FROM executions e WHERE e.worker_id=s.worker_id AND e.slot_id=s.slot_id
          AND e.status IN ('ACCEPTED','RUNNING','CANCEL_REQUESTED')) ORDER BY s.slot_id""",
        (worker_id,),
    )
