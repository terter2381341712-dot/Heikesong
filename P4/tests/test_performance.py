import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from performance import PROVIDERS, publish


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_lines(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


@pytest.fixture(params=["cpu", "cuda"])
def evidence(tmp_path, request):
    backend = request.param
    digest = "a" * 64
    start, finish = "2026-11-15T03:00:00Z", "2026-11-15T03:00:05Z"
    pool = tmp_path / "pool"
    pool.mkdir()
    inputs = [
        {
            "input_index": i, "input_artifact_id": f"img-{i}",
            "input_sha256": f"{i:064x}",
        } for i in range(20)
    ]
    references = dump(tmp_path / "refs.json", {
        "frozen_before_run": True,
        "license": "synthetic test data",
        "max_cer": 0,
        "texts_by_sha256": {item["input_sha256"]: "expected" for item in inputs},
    })
    baselines = []
    for worker, wall_ms in (("worker-a", 4000), ("worker-b", 10000)):
        directory = tmp_path / worker
        results = directory / "results"
        results.mkdir(parents=True)
        rows = []
        for entry in inputs:
            result = {
                "task_type": "ocr.image.v1", "input_artifact_id": entry["input_artifact_id"],
                "text": "wrong" if entry["input_index"] == 0 else "expected",
            }
            path = dump(results / f"{entry['input_index']:04d}-{entry['input_artifact_id']}.json", result)
            rows.append({
                **entry, "record_type": "file", "run_id": worker, "worker_id": worker,
                "backend": backend, "status": "SUCCEEDED", "task_type": "ocr.image.v1",
                "profile_digest": digest, "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        rows.append({
            "record_type": "summary", "total": len(inputs), "run_id": worker,
            "wall_ms": wall_ms, "started_at": start, "finished_at": finish,
        })
        baselines.append(write_lines(directory / f"baseline-{worker}.jsonl", rows))
        dump(baselines[-1].with_suffix(".runtime.json"), {"runtime_provider": PROVIDERS[backend], "threads": 0})

    tasks, executions, items = [], [], []
    for i, entry in enumerate(inputs):
        worker = "worker-a" if i < 10 else "worker-b"
        status = "FAILED" if i == 1 else "SUCCEEDED"
        tasks.append({
            "status": status, "input": {"sha256": entry["input_sha256"]},
            "current_execution_id": str(i),
        })
        executions.append({
            "accepted_at": start, "job_id": "job", "execution_id": str(i),
            "worker_id": worker, "worker_session_id": f"{worker}-session", "status": status,
            "input_sha256": entry["input_sha256"], "profile_digest": digest, "task_type": "ocr.image.v1",
        })
        items.append({
            "input_artifact_id": entry["input_artifact_id"], "status": status,
            "result": None if i == 1 else {"text": "wrong" if i == 0 else "expected"},
        })
    dump(pool / "job.json", {
        "job_id": "job", "task_type": "ocr.image.v1", "profile_digest": digest, "status": "PARTIAL_FAILED",
        "created_at": start, "finished_at": finish, "tasks": tasks, "parameters": {"language": "auto"},
    })
    dump(pool / "results.json", {"task_type": "ocr.image.v1", "items": items})
    dump(pool / "executions.json", executions)
    write_lines(pool / "inputs.jsonl", inputs)
    snapshot = {"resources": [{
        "worker_id": worker, "session_id": f"{worker}-session", "connection": "ONLINE", "mode": "ACTIVE",
        "cleanup_pending": False, "max_slots": 1,
        "hardware": dict(os="Windows", arch="x86_64", cpu_name="CPU", gpu_name="GPU", ram_total_bytes=None),
        "telemetry": dict(cpu_percent=37.0, gpu_percent=None, sampled_at=start),
        "capabilities": [{
            "task_type": "ocr.image.v1", "profile_digest": digest, "state": "READY",
            "verified": True, "backend": backend, "runtime_provider": PROVIDERS[backend],
        }],
    } for worker in ("worker-a", "worker-b")]}
    dump(pool / "report.json", {
        "mode": "REAL_HTTP_OCR", "job_id": "job", "initial_resources": snapshot,
        "final_resources": snapshot, "observed_job_seconds": 7,
    })
    write_lines(pool / "telemetry.jsonl", [
        {"snapshot": snapshot, "phase": "during"}, {"snapshot": snapshot, "phase": "during"},
    ])
    devices = dump(tmp_path / "devices.json", [
        {"worker_id": worker, "physical_device_id": worker, "operator_attested": True}
        for worker in ("worker-a", "worker-b")
    ])
    return SimpleNamespace(backend=backend, baseline=baselines, pool=pool, devices=devices,
                           references=references, resource_dir=tmp_path / "publish")


def test_aggregation_uses_verified_results_and_service_clock(evidence):
    result = json.loads(publish(evidence).read_text(encoding="utf-8"))
    assert result["devices"]["worker-a"]["baseline_images_per_second"] == 19 / 4
    assert result["devices"]["worker-a"]["pool_correct_images"] == 8
    assert result["devices"]["worker-b"]["pool_correct_images"] == 10
    assert result["devices"]["worker-a"]["utilization"]["cpu_percent"] == {"mean": 37.0, "samples": 1}
    assert result["devices"]["worker-a"]["utilization"]["gpu_percent"] == {"mean": None, "samples": 0}
    assert result["pool"]["images_per_second"] == 18 / 5
    assert result["pool"]["efficiency_percent"] == pytest.approx(100 * (18 / 5) / (19 / 4 + 19 / 10))
    assert result["pool"]["speedup"] == pytest.approx((18 / 5) / (19 / 4))


def test_single_machine_report_and_mixed_backend_rejection(evidence):
    pool, evidence.pool = evidence.pool, None
    output = publish(evidence)
    assert json.loads(output.read_text(encoding="utf-8"))["pool"] is None
    snapshot_file = pool / "report.json"
    report = json.loads(snapshot_file.read_text(encoding="utf-8"))
    cap = report["final_resources"]["resources"][1]["capabilities"][0]
    cap["backend"] = "cuda" if evidence.backend == "cpu" else "cpu"
    dump(snapshot_file, report)
    evidence.pool = pool
    with pytest.raises(ValueError, match="后端混用"):
        publish(evidence)
    assert json.loads(output.read_text(encoding="utf-8"))["pool"] is None
