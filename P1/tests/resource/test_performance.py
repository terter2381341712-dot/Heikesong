import copy
import json
from datetime import timedelta

import pytest

from resource_service import performance
from resource_service.db import Database
from resource_service.timeutil import iso, utcnow


def report(group, devices, seconds):
    nodes = [{"worker_id": worker, "max_slots": 1, "threads_per_slot": 2, "cpu_thread_budget": 2,
              "prefetch_capacity": 0, "hardware": {"cpu_name": worker},
              "capabilities": [{"profile_digest": "profile", "backend": backend,
                                "model_sha256": "model", "runtime_provider": backend}]} for worker, backend in devices]
    start = utcnow().replace(microsecond=0)
    return {"mode": "REAL_HTTP_OCR_THROUGHPUT", "status": "PASS", "group": group,
            "source_digest": "a" * 64, "references_sha256": "b" * 64, "reference_status": "FROZEN",
            "input_manifest": [{"sha256": f"{i:064x}"} for i in range(20)],
            "stable_configuration": True, "sufficient_load_confirmed": True,
            "rounds": [{"job_id": f"job-{i}", "profile_digest": "profile", "parameters": {"language": "auto"},
                        "quality_status": "PASS", "inputs": 20, "correct_images": 20,
                        "counts": {"SUCCEEDED": 20, "FAILED": 0, "CANCELLED": 0},
                        "server_job_seconds": seconds, "measurement_started_at": iso(start),
                        "measurement_finished_at": iso(start + timedelta(seconds=seconds)),
                        "resources": copy.deepcopy(nodes), "workers": [d[0] for d in devices]} for i in range(3)]}


def measurements():
    return [report("cpu", [("cpu-device", "cpu")], 20), report("gpu", [("gpu-device", "cuda")], 10),
            report("pool", [("cpu-device", "cpu"), ("gpu-device", "cuda")], 8)]


def test_pdf_formulas_recomputed_for_cpu_and_gpu():
    inputs = measurements()
    inputs[-1]["aggregation_efficiency_percent"] = 999  # Imported claims are ignored.
    result = performance.evaluate(inputs)["devices"]
    cpu, gpu = result["cpu-device"]["cpu"], result["gpu-device"]["cuda"]
    assert cpu["standalone"]["images_per_second"] == 1
    assert gpu["standalone"]["images_per_second"] == 2
    pool = cpu["pools"][0]
    assert pool == gpu["pools"][0]
    assert pool["images_per_second"] == 2.5
    assert pool["aggregation_efficiency_percent"] == pytest.approx(2.5 / 3 * 100)
    assert pool["speedup"] == 1.25
    assert not pool["review_required"]


def test_efficiency_over_100_is_reported_for_review_not_clamped():
    inputs = measurements()
    inputs[-1] = report("pool", [("cpu-device", "cpu"), ("gpu-device", "cuda")], 4)
    pool = performance.evaluate(inputs)["devices"]["cpu-device"]["cpu"]["pools"][0]
    assert pool["aggregation_efficiency_percent"] > 100 and pool["review_required"]


@pytest.mark.parametrize("problem", ["quality", "window", "references", "profile", "missing", "duplicate", "load", "budget", "rounds"])
def test_incomplete_or_incomparable_measurements_rejected(problem):
    inputs = measurements()
    if problem == "quality": inputs[-1]["rounds"][0]["quality_status"] = "NOT_RUN"
    if problem == "window": inputs[-1]["rounds"][0]["server_job_seconds"] = 0
    if problem == "references": inputs[-1]["references_sha256"] = "c" * 64
    if problem == "profile": inputs[-1]["rounds"][0]["parameters"]["language"] = "en"
    if problem == "missing": inputs.pop(1)
    if problem == "duplicate": inputs[0]["input_manifest"][1] = inputs[0]["input_manifest"][0]
    if problem == "load": inputs[-1]["sufficient_load_confirmed"] = False
    if problem == "budget": inputs[-1]["rounds"][0]["resources"][0]["threads_per_slot"] = 4
    if problem == "rounds": inputs[-1]["rounds"].pop()
    with pytest.raises(ValueError): performance.evaluate(inputs)


def test_same_device_cpu_gpu_are_not_counted_twice_in_one_pool():
    with pytest.raises(ValueError, match="同一设备"):
        performance.evaluate([report("pool", [("same", "cpu"), ("same", "cuda")], 5)])


def test_import_requires_user_permission_and_persists(client):
    path = "/api/resource/v1/performance"
    assert client.get(path).status_code == 401
    assert client.post(path, json={"reports": measurements()}, headers={"Authorization": "Bearer task-t"}).status_code == 403
    user = {"Authorization": "Bearer demo-t"}
    assert client.post(path, json={"reports": measurements()}, headers=user).status_code == 200
    saved = client.get(path, headers=user).json()
    assert saved["formal"]["devices"]["gpu-device"]["cuda"]["standalone"]["images_per_second"] == 2
    assert saved["devices"] == {}
    assert client.post(path, json={"reports": [None]}, headers=user).status_code == 422
    assert client.get(path, headers=user).json()["formal"] == saved["formal"]


def test_observation_deduplicates_and_does_not_infer_historical_backend(tmp_path):
    db = Database(tmp_path / "data.sqlite")
    now = utcnow()
    try:
        columns = [r["name"] for r in db.query("PRAGMA table_info(executions)")]
        for eid, backend, status, task in [("e1", "cpu", "SUCCEEDED", "one"), ("e2", "cuda", "SUCCEEDED", "one"),
                                           ("e3", "cuda", "FAILED", "two"), ("e4", "cuda", "SUCCEEDED", "three"),
                                           ("old", None, "SUCCEEDED", "four")]:
            values = dict.fromkeys(columns, "")
            values.update(execution_id=eid, status=status, worker_id="device", job_id="job", task_id=task,
                          task_type="ocr.image.v1", finished_at=iso(now), performance_backend=backend)
            with db.write() as connection:
                connection.execute(f"INSERT INTO executions({','.join(columns)}) VALUES({','.join('?' for c in columns)})", list(values.values()))
        with db.write() as connection:
            for i, cpu in enumerate((10, 30)):
                connection.execute("INSERT INTO performance_telemetry VALUES(?,?,?)", ("device", iso(now - timedelta(seconds=i * 3)), json.dumps({"cpu_percent": cpu, "gpu_percent": None})))
        snapshot = performance.snapshot(db)
        assert snapshot["devices"]["device"]["cpu"]["completed"] == 1
        assert snapshot["devices"]["device"]["cuda"]["completed"] == 1
        assert snapshot["devices"]["device"]["cuda"]["successful_images_per_second"] == 1 / 300
        assert snapshot["utilization"]["device"]["cpu_percent"]["mean"] == 20
        assert snapshot["utilization"]["device"]["gpu_percent"]["mean"] is None
        # A retried task whose first success was outside the window is not new useful work.
        with db.write() as connection:
            connection.execute("UPDATE executions SET finished_at=? WHERE execution_id='e1'", (iso(now - timedelta(seconds=600)),))
        snapshot = performance.snapshot(db)
        assert "cpu" not in snapshot["devices"]["device"]
        assert snapshot["devices"]["device"]["cuda"]["completed"] == 1
    finally:
        db.close()


def insert_execution(db, eid, start, finish, *, job="job", task=None, backend="cpu", status="SUCCEEDED"):
    columns = [r["name"] for r in db.query("PRAGMA table_info(executions)")]
    values = dict.fromkeys(columns, "")
    values.update(execution_id=eid, worker_id="device", job_id=job, task_id=task or eid,
                  task_type="ocr.image.v1", status=status, performance_backend=backend,
                  started_at=iso(start), finished_at=iso(finish),
                  performance_started_at=performance.precise(start),
                  performance_finished_at=performance.precise(finish) if finish else None,
                  timings=json.dumps({"total_worker_ms": 200, "inference_ms": 150}))
    with db.write() as connection:
        connection.execute(f"INSERT INTO executions({','.join(columns)}) VALUES({','.join('?' for c in columns)})", list(values.values()))


def test_recent_speed_uses_actual_wall_time_and_keeps_subsecond_precision(tmp_path):
    db = Database(tmp_path / "data.sqlite")
    now = utcnow().replace(microsecond=500000)
    try:
        # Two concurrent slots finish two images within 0.2s, not 300s or 0.4s.
        for eid in ("a", "b"):
            insert_execution(db, eid, now - timedelta(seconds=0.3), now - timedelta(seconds=0.1))
        insert_execution(db, "dup", now - timedelta(seconds=0.2), now, task="a")
        data = performance.recent_processing(db, now)["devices"]["device"]["cpu"]
        assert data["processing_images_per_second"] == pytest.approx(10)
        assert data["measurement_seconds"] == pytest.approx(0.2)
        assert data["completed"] == 2
        assert data["average_worker_seconds"] == 0.2
        assert performance.recent_processing(db, now + timedelta(seconds=31))["devices"] == {}
        assert performance.recent_processing(db, now + timedelta(seconds=31), "job")["devices"]["device"]["cpu"]["completed"] == 2
    finally:
        db.close()


def test_running_and_failed_time_included_and_job_filter_isolated(tmp_path):
    db = Database(tmp_path / "data.sqlite")
    now = utcnow()
    try:
        insert_execution(db, "ok", now - timedelta(seconds=10), now - timedelta(seconds=9))
        insert_execution(db, "fail", now - timedelta(seconds=8), now - timedelta(seconds=5), status="FAILED")
        insert_execution(db, "running", now - timedelta(seconds=3), None, status="RUNNING")
        insert_execution(db, "other", now - timedelta(seconds=20), now - timedelta(seconds=15), job="other-job")
        cpu = performance.recent_processing(db, now, "job")["devices"]["device"]["cpu"]
        assert cpu["processing_images_per_second"] == pytest.approx(0.1)
        assert cpu["active_executions"] == 1 and cpu["completed"] == 1
        assert performance.recent_processing(db, now, "missing")["devices"] == {}
    finally:
        db.close()
