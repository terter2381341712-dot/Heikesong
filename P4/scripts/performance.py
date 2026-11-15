"""Publish the correct OCR throughput of one CPU or CUDA benchmark round."""

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from tempfile import NamedTemporaryFile

from quality import load_references, score, score_text

PROVIDERS = {"cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider"}
OCR = "ocr.image.v1"
DEVICE_ID = r"[A-Za-z0-9_-]{1,80}"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "测量时间须带时区")
    return result


def unique_inputs(hashes):
    unique = set(hashes)
    require(len(unique) == len(hashes), "同一轮含重复输入 SHA-256")
    require(len(unique) >= 20, "每轮至少需要 20 张不同图片")
    return unique


def read_baseline(path, backend, references):
    *records, summary = read_lines(path)
    require(summary["record_type"] == "summary" and summary["total"] == len(records), "单机摘要缺失或数量不符")
    inputs = unique_inputs([row["input_sha256"] for row in records])
    worker_id = records[0]["worker_id"]
    digest = records[0]["profile_digest"]
    require(worker_id != "baseline" and re.fullmatch(DEVICE_ID, worker_id), "单机记录缺少已注册的 worker_id")
    require(re.fullmatch(r"[a-f0-9]{64}", digest), "单机 profile 摘要无效")
    runtime = read_json(path.with_suffix(".runtime.json"))
    require(runtime["runtime_provider"] == PROVIDERS[backend], "单机实测后端不符")
    duration = summary["wall_ms"] / 1000
    require(math.isfinite(duration) and duration > 0, "单机计时窗口无效")
    require(instant(summary["started_at"]) <= instant(summary["finished_at"]), "单机时间窗倒序")
    correct = 0
    for row in records:
        require(
            row["record_type"] == "file" and row["run_id"] == summary["run_id"]
            and row["task_type"] == OCR and row["worker_id"] == worker_id
            and row["backend"] == backend and row["profile_digest"] == digest,
            "单机逐图记录的设备、轮次、任务、后端或 profile 不一致",
        )
        truth = references["texts_by_sha256"][row["input_sha256"]]
        if row["status"] != "SUCCEEDED":
            continue
        index, artifact = row["input_index"], row["input_artifact_id"]
        require(type(index) is int and index >= 0 and re.fullmatch(DEVICE_ID, artifact), "单机结果标识无效")
        raw = (path.parent / "results" / f"{index:04d}-{artifact}.json").read_bytes()
        result = json.loads(raw)
        require(
            hashlib.sha256(raw).hexdigest() == row["result_sha256"]
            and result["input_artifact_id"] == artifact and result["task_type"] == OCR,
            "单机结果校验失败",
        )
        correct += score_text(truth, result["text"], True, references["max_cer"])["quality_passed"]
    return worker_id, digest, inputs, {
        "baseline_images_per_second": correct / duration,
        "baseline_correct_images": correct,
        "baseline_window_seconds": duration,
        "baseline_started_at": summary["started_at"],
        "baseline_finished_at": summary["finished_at"],
        "baseline_run_id": summary["run_id"],
        "threads": runtime["threads"],
    }


def check_pool_snapshot(snapshot, sessions, backend, digest):
    active = {
        node["worker_id"]: node for node in snapshot["resources"]
        if node["connection"] == "ONLINE" and node["mode"] == "ACTIVE"
    }
    require(active.keys() == sessions.keys(), "在线接单设备集合与单机基线不一致")
    for worker_id, node in active.items():
        require(
            node["session_id"] == sessions[worker_id] and not node["cleanup_pending"]
            and any(
                cap["task_type"] == OCR and cap["profile_digest"] == digest
                and cap["state"] == "READY" and cap["verified"] is True
                and cap["backend"] == backend and cap["runtime_provider"] == PROVIDERS[backend]
                for cap in node["capabilities"]
            ),
            "多机窗口存在后端混用、会话变化或尚未预热的设备",
        )
    return active


def read_pool(directory, backend, digest, inputs, devices, reference_file, device_file):
    report = read_json(directory / "report.json")
    job = read_json(directory / "job.json")
    require(
        report["mode"] == "REAL_HTTP_OCR" and report["job_id"] == job["job_id"]
        and job["task_type"] == OCR and job["profile_digest"] == digest
        and job["status"] in {"SUCCEEDED", "PARTIAL_FAILED", "FAILED"}
        and job["parameters"] == {"language": "auto"},
        "需要同一 profile 的、已结束的真实 OCR Job",
    )
    duration = (instant(job["finished_at"]) - instant(job["created_at"])).total_seconds()
    require(duration > 0, "服务端 Job 计时窗口无效")
    require(all(device["threads"] == 0 for device in devices.values()), "多机对照的单机基线须使用默认线程配置")

    quality = score(directory, reference_file)
    require(unique_inputs([row["input_sha256"] for row in quality["items"]]) == inputs, "单机与多机输入字节不同")
    executions = {
        execution["execution_id"]: execution
        for execution in read_json(directory / "executions.json") if execution["accepted_at"] is not None
    }
    sessions = {execution["worker_id"]: execution["worker_session_id"] for execution in executions.values()}
    require(
        devices.keys() == sessions.keys() and len(sessions) >= 2
        and all(
            execution["worker_session_id"] == sessions[execution["worker_id"]]
            and execution["job_id"] == job["job_id"]
            and execution["task_type"] == OCR and execution["profile_digest"] == digest
            for execution in executions.values()
        ),
        "实际计算者与单机基线不一致，或多机轮次发生了重连",
    )
    physical = {
        row["worker_id"]: row["physical_device_id"] for row in read_json(device_file)
        if row["operator_attested"] is True and row["worker_id"] in sessions and row["physical_device_id"]
    }
    require(physical.keys() == sessions.keys() and len(set(physical.values())) == len(sessions), "需确认每个节点对应独立物理设备")
    before = check_pool_snapshot(report["initial_resources"], sessions, backend, digest)
    check_pool_snapshot(report["final_resources"], sessions, backend, digest)
    samples = {worker_id: {} for worker_id in sessions}
    for entry in read_lines(directory / "telemetry.jsonl"):
        if entry.get("phase") != "during":
            continue
        active = check_pool_snapshot(entry["snapshot"], sessions, backend, digest)
        for worker_id, node in active.items():
            telemetry = node["telemetry"]
            if telemetry and telemetry["sampled_at"]:
                samples[worker_id][telemetry["sampled_at"]] = telemetry

    tasks = {task["input"]["sha256"]: task for task in job["tasks"]}
    require(tasks.keys() == inputs and len(tasks) == len(job["tasks"]), "Job 输入与质量核验记录不一致")
    contributions = Counter()
    for row in quality["items"]:
        if not row["quality_passed"]:
            continue
        task = tasks[row["input_sha256"]]
        execution = executions[task["current_execution_id"]]
        require(
            task["status"] == execution["status"] == "SUCCEEDED"
            and execution["input_sha256"] == row["input_sha256"]
            and execution["profile_digest"] == digest,
            "正确结果缺少对应的成功执行记录",
        )
        contributions[execution["worker_id"]] += 1
    require(contributions.keys() == sessions.keys(), "每台参评设备均须贡献至少一项正确结果")

    for worker_id, device in devices.items():
        node = before[worker_id]
        utilization = {}
        for field in ("cpu_percent", "gpu_percent"):
            values = [entry[field] for entry in samples[worker_id].values() if entry[field] is not None]
            utilization[field] = {"mean": mean(values) if values else None, "samples": len(values)}
        device.update(
            pool_images_per_second=contributions[worker_id] / duration,
            pool_correct_images=contributions[worker_id],
            hardware={key: node["hardware"][key] for key in ("os", "arch", "cpu_name", "gpu_name", "ram_total_bytes")},
            max_slots=node["max_slots"],
            utilization=utilization,
        )
    total_rate = sum(contributions.values()) / duration
    baselines = [device["baseline_images_per_second"] for device in devices.values()]
    return {
        "job_id": job["job_id"],
        "started_at": job["created_at"],
        "finished_at": job["finished_at"],
        "window_seconds": duration,
        "concurrency_slots": sum(device["max_slots"] for device in devices.values()),
        "correct_images": sum(contributions.values()),
        "images_per_second": total_rate,
        "efficiency_percent": 100 * total_rate / sum(baselines) if sum(baselines) else None,
        "speedup": total_rate / max(baselines) if max(baselines) else None,
    }


def publish(args):
    references = load_references(args.references)
    devices = {}
    for path in args.baseline:
        worker_id, baseline_digest, baseline_inputs, stats = read_baseline(path, args.backend, references)
        if not devices:
            digest, inputs = baseline_digest, baseline_inputs
        require(digest == baseline_digest and inputs == baseline_inputs, "各单机须使用同一 profile 和相同输入字节")
        require(worker_id not in devices, "同一轮不可重复登记同一设备的单机基线")
        devices[worker_id] = stats

    pool = None
    if args.pool:
        pool = read_pool(args.pool, args.backend, digest, inputs, devices, args.references, args.devices)
    measured_at = pool["finished_at"] if pool else max(
        (device["baseline_finished_at"] for device in devices.values()), key=instant
    )
    result = {
        "backend": args.backend,
        "task_type": OCR,
        "profile_digest": digest,
        "measured_at": measured_at,
        "input_count": len(inputs),
        "batch_size": 1,
        "max_cer": references["max_cer"],
        "target_sample_interval_seconds": 3,
        "devices": devices,
        "pool": pool,
    }
    target = args.resource_dir / f"ocr-performance-{args.backend}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with NamedTemporaryFile(dir=target.parent, delete=False, mode="w", encoding="utf-8") as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=PROVIDERS)
    parser.add_argument("--references", required=True, type=Path)
    parser.add_argument("--baseline", required=True, nargs="+", type=Path)
    parser.add_argument("--pool", type=Path, help="verify.py 生成的固定多机服务证据目录")
    parser.add_argument("--devices", type=Path, help="经确认的物理设备映射 JSON")
    parser.add_argument("--resource-dir", required=True, type=Path, help="中心 P1 的 RESOURCE_DATA_DIR")
    args = parser.parse_args()
    if args.pool and not args.devices:
        parser.error("--pool 需要 --devices")
    try:
        print(publish(args))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"无法发布实测记录：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
