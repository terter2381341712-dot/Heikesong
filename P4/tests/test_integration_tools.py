import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_integration as integration
import throughput


def dump(path, body):
    path.write_text(json.dumps(body))
    return path


def test_throughput_counts_unique_success_not_attempts(tmp_path):
    dump(tmp_path / "job.json", {"job_id": "job", "status": "PARTIAL", "created_at": "2026-11-15T00:00:00Z",
        "finished_at": "2026-11-15T00:00:04Z", "tasks": [{"task_id": str(i)} for i in range(3)],
        "profile_digest": "digest", "parameters": {}})
    items = [{"task_id": str(i), "status": s} for i,s in enumerate(["SUCCEEDED", "FAILED", "CANCELLED"])]
    dump(tmp_path / "results.json", {"items": items})
    dump(tmp_path / "report.json", {"observed_job_seconds": 5})
    m = throughput.metrics(tmp_path)
    assert m["successful_images_per_second"] == .25
    assert m["observed_successful_images_per_second"] == .2
    assert m["correct_images_per_second"] is None and m["quality_status"] == "NOT_RUN"
    items[2]["task_id"] = "0"
    dump(tmp_path / "results.json", {"items": items})
    with pytest.raises(ValueError):
        throughput.metrics(tmp_path)


def test_attestation_cannot_turn_missing_or_wrong_evidence_into_pass(tmp_path):
    artifact = tmp_path / "browser.txt"
    artifact.write_text("two operators witnessed two physical devices")
    body = {"stage": "S2", "source_digest": "abc", "operators": ["A", "B"], "physical_devices": ["PC-A", "PC-B"],
        "gates": {"G4": {"status": "PASS", "operator_attested": True,
            "artifacts": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}},
        "stage_checks": {"S2": {"status": "NOT_RUN"}}}
    path = dump(tmp_path / "attestation.json", body)
    assert integration.validate_attestation(path, "S2", "abc")["G4"]["status"] == "PASS"
    assert integration.validate_attestation(path, "S2", "abc")["S2"]["status"] == "NOT_RUN"
    artifact.write_text("changed after review")
    with pytest.raises(ValueError):
        integration.validate_attestation(path, "S2", "abc")
    with pytest.raises(ValueError):
        integration.validate_attestation(path, "S2", "other source")


def test_human_evidence_cannot_override_automated_gate(tmp_path):
    path = dump(tmp_path / "a.json", {"stage": "S0", "source_digest": "x", "operators": ["A"],
        "physical_devices": ["PC"], "gates": {"G0": {"status": "PASS"}}})
    with pytest.raises(ValueError):
        integration.validate_attestation(path, "S0", "x")


def test_sampled_peaks_preserve_missing_gpu_and_detect_actual_maximum(tmp_path):
    rows = [{"snapshot": {"resources": [{"worker_id": "a", "telemetry": {"cpu_percent": x}}]}} for x in (1, 60, 10)]
    (tmp_path / "telemetry.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    peak = throughput.sampled_peaks(tmp_path)["a"]
    assert peak["cpu_percent"] == 60 and peak["samples"] == 3
    assert "gpu_memory_used_bytes" not in peak


def test_comparison_rejects_changed_budget_and_keeps_service_metric_distinct(tmp_path):
    cpu = {"worker_id": "CPU", "hardware": {"logical_cores": 8}, "max_slots": 1,
           "threads_per_slot": 2, "cpu_thread_budget": 4, "prefetch_capacity": 0}
    gpu = {**cpu, "worker_id": "GPU"}
    args = SimpleNamespace(out=tmp_path / "comparison.json")
    for group, nodes, rate in (("cpu", [cpu], 1.), ("gpu", [gpu], 2.), ("pool", [cpu,gpu], 2.5)):
        body = {"status": "PASS", "group": group, "source_digest": "code", "input_manifest": ["same"],
                "references_sha256": "refs", "median_correct_images_per_second": rate,
                "rounds": [{"workers": [n["worker_id"] for n in nodes], "resources": nodes,
                            "profile_digest": "profile"} for _ in range(3)]}
        setattr(args, group, dump(tmp_path / (group + ".json"), body))
    assert throughput.compare(args) == 0
    output = json.loads(args.out.read_text())
    assert output["speedup_over_best_single"] == 1.25
    assert output["pool_aggregation_efficiency_percent"] == pytest.approx(2.5 / (1. + 2.) * 100)
    assert output["device_standalone_correct_images_per_second"] == {"CPU": 1., "GPU": 2.}
    assert output["judge_review_required"] is False
    assert output["formulas"]["aggregation_efficiency_percent"] == "P_pool / sum(P_i) * 100"
    body = json.loads(args.pool.read_text())
    body["median_correct_images_per_second"] = 4.
    dump(args.pool, body)
    args.out.unlink()
    assert throughput.compare(args) == 0
    assert json.loads(args.out.read_text())["judge_review_required"] is True
    assert output["formal_local_executor_aggregation_efficiency"] is None
    body = json.loads(args.pool.read_text())
    body["rounds"][0]["resources"][0]["threads_per_slot"] = 4
    dump(args.pool, body)
    with pytest.raises(ValueError, match="线程"):
        throughput.compare(args)
