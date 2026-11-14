"""Measure repeated real OCR jobs and compare matched CPU/GPU/pool evidence."""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import compute_config
import quality
from check_integration import source_digest

ROOT = compute_config.ROOT


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def execution_configuration(resources):
    result = []
    for node in sorted(resources, key=lambda n: n["worker_id"]):
        result.append({**{k: node.get(k) for k in ("worker_id", "max_slots", "threads_per_slot", "cpu_thread_budget", "prefetch_capacity")},
                       "hardware": {k:v for k,v in node["hardware"].items() if k != "ram_available_bytes"},
                       "capabilities": [{k:c.get(k) for k in ("task_type", "profile_id", "profile_digest", "backend", "runtime_provider", "model_sha256")} for c in node["capabilities"]]})
    return result


def sampled_peaks(directory):
    """Observed whole-device peaks; three-second sampling can miss short spikes."""
    peaks = {}
    path = directory / "telemetry.jsonl"
    for line in path.read_text().splitlines() if path.exists() else []:
        for node in json.loads(line)["snapshot"]["resources"]:
            telemetry = node.get("telemetry") or {}
            target = peaks.setdefault(node["worker_id"], {"samples": 0})
            target["samples"] += 1
            for key in ("cpu_percent", "ram_used_bytes", "gpu_percent", "gpu_memory_used_bytes"):
                value = telemetry.get(key)
                if isinstance(value, (int, float)):
                    target[key] = max(target.get(key, value), value)
    return peaks


def metrics(directory, references=None):
    job = read(directory / "job.json")
    result = read(directory / "results.json")
    report = read(directory / "report.json")
    tasks = job["tasks"]
    ids = [t["task_id"] for t in tasks]
    if len(ids) != len(set(ids)) or len(result["items"]) != len(ids):
        raise ValueError("逻辑任务/结果数量不一致或重复")
    if {i["task_id"] for i in result["items"]} != set(ids):
        raise ValueError("结果与任务不匹配")
    seconds = (stamp(job["finished_at"]) - stamp(job["created_at"])).total_seconds() if job.get("finished_at") else None
    observed = report.get("observed_job_seconds")
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("服务端任务计时窗口无效")
    succeeded = sum(i["status"] == "SUCCEEDED" for i in result["items"])
    counts = {state: sum(i["status"] == state for i in result["items"])
              for state in ("SUCCEEDED", "FAILED", "CANCELLED")}
    m = {"job_id": job["job_id"], "job_status": job["status"], "inputs": len(ids),
         "counts": counts, "server_job_seconds": seconds, "observed_job_seconds": observed,
         "observed_with_upload_seconds": report.get("observed_with_upload_seconds"),
         "successful_images_per_second": succeeded / seconds if seconds else None,
         "observed_successful_images_per_second": succeeded / observed if observed and observed > 0 else None,
         "correct_images_per_second": None, "quality_status": "NOT_RUN",
         "workers": report.get("successful_workers", []),
         "physical_collaboration": report.get("physical_collaboration", "NOT_RUN"),
         "profile_digest": job["profile_digest"], "parameters": job["parameters"]}
    m["sampled_device_peaks"] = sampled_peaks(directory)
    m["peak_notice"] = "约 3 秒采样的整机观测峰值，可能漏掉尖峰；未采到的字段不填 0。"
    if references is not None:
        score = quality.score(directory, references)
        m["correct_images_per_second"] = score["correct_images"] / seconds if seconds else None
        m["quality_status"] = "PASS" if score["quality_set_minimum_met"] and all(i["quality_passed"] for i in score["items"]) else "FAIL"
        m["correct_images"] = score["correct_images"]
        (directory / "quality.json").write_text(json.dumps(score, indent=2) + "\n")
    return m


def measure(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    paths = sorted(p for p in args.inputs.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not paths:
        raise ValueError("没有 PNG/JPG 输入")
    manifest = [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    reference = args.references.resolve() if args.references else None
    if reference:
        ref = read(reference)
        if ref.get("frozen_before_run") is not True or not ref.get("license") or type(ref.get("max_cer")) not in (int, float) or not 0 <= ref["max_cer"] <= 1:
            raise ValueError("参考文本/阈值未在实验前冻结")
        if any(p["sha256"] not in ref["texts_by_sha256"] for p in manifest):
            raise ValueError("输入缺少冻结参考文本")
    config = compute_config.load(args.compute_config)
    report = {"mode": "REAL_HTTP_OCR_THROUGHPUT", "status": "RUNNING", "group": args.group,
              "source_digest": source_digest(), "input_manifest": manifest, "compute": config,
              "references_sha256": hashlib.sha256(reference.read_bytes()).hexdigest() if reference else None,
              "reference_status": "FROZEN" if reference else "NOT_RUN",
              "cold": None, "warmup": None, "rounds": [], "formal_benchmark": "NOT_RUN",
              "notice": "服务端计时来自同一 P2 Job 时间线；成功不等于识别正确。未冻结人工参考时正确吞吐量为空。"}
    try:
        # First request is cold-or-current-state, never assumed cold unless the
        # operators actually restarted the selected nodes before this command.
        for label in ["initial", "warmup"] + [f"round-{i+1}" for i in range(args.rounds)]:
            run = out / label
            cmd = [sys.executable, str(ROOT / "P4/scripts/verify.py"), "--config", str(args.config.resolve()),
                   "--inputs", str(args.inputs.resolve()), "--out", str(run), "--timeout", str(args.timeout),
                   "--profiles-dir", str(args.profiles_dir.resolve() if args.profiles_dir else compute_config.profiles(config))]
            if args.group == "pool":
                if not args.devices:
                    raise ValueError("双机测量必须提供已人工确认的 --devices")
                cmd += ["--min-workers", "2", "--devices", str(args.devices.resolve())]
            with (out / (label + ".log")).open("w") as log:
                result = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout + 180)
            if (run / "job.json").exists() and (run / "results.json").exists():
                row = metrics(run, reference)
            else:
                row = {"status": "FAIL", "error": "missing terminal job/results"}
            row.update(run=label, verification_exit_code=result.returncode)
            if label == "initial":
                row["cold_start_confirmed"] = args.cold_start_confirmed
                report["cold"] = row
            elif label == "warmup": report["warmup"] = row
            else: report["rounds"].append(row)
            if result.returncode != 0:
                raise ValueError("验收失败；保留本轮失败和耗时，不以重跑替换")
            if args.group != "pool" and len(row["workers"]) != 1:
                raise ValueError("单机测量出现多个计算者，请关闭其他设备共享后重测")
            executions = read(run / "executions.json")
            snapshot = read(run / "report.json").get("final_resources", {})
            nodes = snapshot.get("resources", []) if isinstance(snapshot, dict) else []
            providers = {c.get("backend") for n in nodes if n["worker_id"] in row["workers"]
                         for c in n["capabilities"] if c.get("profile_digest") == row["profile_digest"] and c.get("verified")}
            row["observed_backends"] = sorted(p for p in providers if p)
            if args.group in {"cpu", "gpu"} and providers != {"cpu" if args.group == "cpu" else "cuda"}:
                raise ValueError("实际计算后端与测试组不符")
            # Record actual snapshots, not just the client's declared compute file.
            row["resources"] = [{k:n.get(k) for k in ("worker_id", "hardware", "max_slots", "threads_per_slot", "cpu_thread_budget", "prefetch_capacity", "capabilities")} for n in nodes if n["worker_id"] in row["workers"]]
            row["execution_attempts"] = len(executions)
            timings = [e["timings"] for e in executions if e.get("timings") and e.get("status") == "SUCCEEDED"]
            row["median_execution_timings_ms"] = {k: statistics.median(t[k] for t in timings if k in t)
                for k in ("download_ms", "decode_ms", "inference_ms", "postprocess_ms", "total_worker_ms")
                if any(k in t for t in timings)}
            row["prefetch_hits"] = sum(t.get("prefetch_hit") is True for t in timings)
            row["model_load_seconds"] = None
            row["model_load_note"] = "模型加载与校准时间从节点日志单独提取；Job 时间包含调度/准备，不能当作纯加载时间。"
        for key in ("successful_images_per_second", "correct_images_per_second", "server_job_seconds"):
            values = [r.get(key) for r in report["rounds"]]
            report["median_" + key] = statistics.median(values) if all(v is not None for v in values) else None
        stable = len({json.dumps(execution_configuration(r["resources"]), sort_keys=True) for r in report["rounds"]}) == 1
        # Compare execution settings, not changing telemetry or calibration times.
        report["stable_configuration"] = stable
        report["status"] = "PASS" if stable and all(r["quality_status"] == "PASS" for r in report["rounds"]) else "MEASURED_NOT_ACCEPTED"
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
    finally:
        (out / "throughput.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(report["status"], out / "throughput.json")
    return 0 if report["status"] == "PASS" else 2 if report["status"] == "MEASURED_NOT_ACCEPTED" else 1


def compare(args):
    reports = {k: read(getattr(args, k)) for k in ("cpu", "gpu", "pool")}
    for key, value in reports.items():
        if value["group"] != key or value["status"] != "PASS" or len(value["rounds"]) < 3:
            raise ValueError("比较需要各组至少三轮、人工质量核验通过的有效报告")
    for field in ("source_digest", "input_manifest", "references_sha256"):
        if any(r[field] != reports["cpu"][field] for r in reports.values()):
            raise ValueError("源码、输入或人工参考不一致，不能比较")
    profile = reports["cpu"]["rounds"][0]["profile_digest"]
    if any(row["profile_digest"] != profile for r in reports.values() for row in r["rounds"]):
        raise ValueError("profile 摘要不一致，不能比较")
    solo = {}
    for key in ("cpu", "gpu"):
        for row in reports[key]["rounds"]:
            for node in row["resources"]:
                solo[node["worker_id"]] = node
    for row in reports["pool"]["rounds"]:
        if set(row["workers"]) != set(solo):
            raise ValueError("双机计算者与两个单机基线不一致")
        for node in row["resources"]:
            # Calibration timestamps change on restart; compare frozen execution
            # configuration rather than ephemeral readiness observations.
            for field in ("hardware", "max_slots", "threads_per_slot", "cpu_thread_budget", "prefetch_capacity"):
                a, b = node.get(field), solo[node["worker_id"]].get(field)
                if field == "hardware":
                    a = {k:v for k,v in a.items() if k != "ram_available_bytes"}
                    b = {k:v for k,v in b.items() if k != "ram_available_bytes"}
                if a != b:
                    raise ValueError("单机与双机的设备/槽数/线程配置不一致")
    rates = {k:r["median_correct_images_per_second"] for k,r in reports.items()}
    if not all(isinstance(v,(int,float)) and math.isfinite(v) and v > 0 for v in rates.values()):
        raise ValueError("正确吞吐量必须为正数")
    result = {"status": "PASS", "correct_images_per_second": rates,
              "speedup_over_best_single": rates["pool"] / max(rates["cpu"], rates["gpu"]),
              "service_aggregation_ratio": rates["pool"] / (rates["cpu"] + rates["gpu"]),
              "formal_local_executor_aggregation_efficiency": None,
              "note": "service_aggregation_ratio 的分母是单机 P2 服务吞吐；未替代本地执行器基准的正式聚合效率。"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as f: json.dump(result, f, ensure_ascii=False, indent=2)
    print(args.out)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--config", type=Path, required=True)
    m.add_argument("--inputs", type=Path, required=True)
    m.add_argument("--references", type=Path)
    m.add_argument("--group", choices=["cpu", "gpu", "pool"], required=True)
    m.add_argument("--compute-config")
    m.add_argument("--profiles-dir", type=Path)
    m.add_argument("--devices", type=Path)
    m.add_argument("--rounds", type=int, default=3)
    m.add_argument("--timeout", type=float, default=600)
    m.add_argument("--cold-start-confirmed", action="store_true")
    m.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare")
    for k in ("cpu", "gpu", "pool", "out"): c.add_argument("--" + k, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "measure" and (args.rounds < 3 or not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("至少三轮，timeout 必须为有限正数")
    try:
        return measure(args) if args.command == "measure" else compare(args)
    except Exception as exc:
        print("FAIL", type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
