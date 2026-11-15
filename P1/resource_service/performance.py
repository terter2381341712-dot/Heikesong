"""Observed rates and imported, quality-checked standalone/pool comparisons."""

import math
import re
import statistics
from datetime import datetime, timedelta

from .db import parse_json
from .timeutil import iso, utcnow

BACKENDS = ("cpu", "cuda", "directml", "coreml")


def precise(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("测量时长、并发和批量必须为正数")
    return value


def configuration(node, profile):
    if type(node.get("max_slots")) is not int or not 1 <= node["max_slots"] <= 1024:
        raise ValueError("必须披露有效的设备并发槽位数")
    caps = [c for c in node["capabilities"] if c.get("profile_digest") == profile and c.get("backend") in BACKENDS]
    if len(caps) != 1:
        raise ValueError("设备的 CPU/GPU 后端或 profile 不唯一")
    cap = caps[0]
    return (node["worker_id"], cap["backend"]), {
        **{k: node.get(k) for k in ("max_slots", "threads_per_slot", "cpu_thread_budget", "prefetch_capacity")},
        "hardware": {k: v for k, v in node["hardware"].items() if k != "ram_available_bytes"},
        "profile_digest": profile, "model_sha256": cap.get("model_sha256"),
        "runtime_provider": cap.get("runtime_provider"),
    }


def evaluate(reports):
    """Recompute every rate from counts/time; never trust an imported efficiency."""
    if not isinstance(reports, list) or not 1 <= len(reports) <= 40:
        raise ValueError("请选择1–40份 throughput.json 测量报告")
    common, settings, baselines, pools = None, None, {}, []
    for report in reports:
        if report.get("mode") != "REAL_HTTP_OCR_THROUGHPUT" or report.get("status") != "PASS" or report.get("stable_configuration") is not True:
            raise ValueError("仅接受配置稳定、正确性核验通过的真实OCR测量报告")
        manifest = report["input_manifest"]
        hashes = [i["sha256"] for i in manifest]
        if len(hashes) < 20 or not all(isinstance(h, str) and re.fullmatch(r"[a-f0-9]{64}", h) for h in hashes) or len(set(hashes)) != len(hashes):
            raise ValueError("需要至少20张内容不同的图片，不计重复工作")
        identity = (report["source_digest"], report["references_sha256"], sorted(hashes))
        if not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in identity[:2]) or report.get("reference_status") != "FROZEN":
            raise ValueError("缺少冻结的源码、输入或人工正确性参考摘要")
        if common is not None and identity != common:
            raise ValueError("各报告的源码、输入集和正确性标准必须一致")
        common = identity
        rounds = report["rounds"]
        if len(rounds) < 3 or len({r["job_id"] for r in rounds}) != len(rounds):
            raise ValueError("每组至少需要3轮有效测量")
        rates, windows, configs = [], [], None
        for row in rounds:
            current_settings = (row["profile_digest"], row["parameters"])
            if settings is not None and settings != current_settings:
                raise ValueError("各报告的任务profile和识别参数必须一致")
            settings = current_settings
            if row.get("quality_status") != "PASS" or row["inputs"] != len(hashes):
                raise ValueError("每轮需要一致的输入集和正确性核验")
            count = row["correct_images"]
            if type(count) is not int or not 0 <= count <= row["counts"]["SUCCEEDED"] <= len(hashes):
                raise ValueError("有效完成数不能含失败或超过输入数")
            duration = positive(row["server_job_seconds"])
            started = datetime.fromisoformat(row["measurement_started_at"].replace("Z", "+00:00"))
            finished = datetime.fromisoformat(row["measurement_finished_at"].replace("Z", "+00:00"))
            if started.tzinfo is None or finished.tzinfo is None or abs((finished - started).total_seconds() - duration) > 0.01:
                raise ValueError("测量窗口与报告时长不一致，请用新版吞吐工具生成报告")
            current = dict(configuration(n, row["profile_digest"]) for n in row["resources"])
            physical = [c["hardware"].get("manual_labels", {}).get("physical_device_id") or worker
                        for (worker, _), c in current.items()]
            if len(set(physical)) != len(physical):
                raise ValueError("同一设备（物理电脑）的CPU/GPU逻辑资源不能冒充多台设备基线；请按物理设备汇总测量")
            if len(current) != len(row["resources"]) or len({k[0] for k in current}) != len(current):
                raise ValueError("同一轮不能把同一设备的CPU和GPU重复计为两台设备")
            if not current or set(row["workers"]) != {k[0] for k in current}:
                raise ValueError("必须披露全部实际参与设备")
            if configs is not None and configs != current:
                raise ValueError("各轮的设备和执行配置不一致")
            configs = current
            rates.append(count / duration)
            windows.append({"job_id": row["job_id"], "seconds": duration, "correct_images": count,
                            "started_at": row["measurement_started_at"], "finished_at": row["measurement_finished_at"],
                            "inputs": row["inputs"], "concurrency": sum(c["max_slots"] for c in current.values())})
        record = {"images_per_second": statistics.median(rates), "windows": windows,
                  "profile_digest": next(iter(configs.values()))["profile_digest"], "rounds": len(rounds)}
        group = report["group"]
        if group in ("cpu", "gpu"):
            if len(configs) != 1 or (next(iter(configs))[1] == "cpu") != (group == "cpu"):
                raise ValueError("单机报告须恰好包含对应CPU或GPU的一台设备")
            key = next(iter(configs))
            if key in baselines:
                raise ValueError("同一设备同一后端的独立基线重复，请只选择一份")
            baselines[key] = {**record, "configuration": configs[key]}
        elif group == "pool":
            if len(configs) < 2 or report.get("sufficient_load_confirmed") is not True:
                raise ValueError("池测量须至少两台设备，并通过 --sufficient-load-confirmed 确认负载充足")
            pools.append((configs, record))
        else:
            raise ValueError("未知测量组")
    devices = {}
    for (worker, backend), record in baselines.items():
        devices.setdefault(worker, {})[backend] = {"standalone": record, "pools": []}
    for configs, pool in pools:
        singles = []
        for key, config in configs.items():
            if key not in baselines or baselines[key]["configuration"] != config:
                raise ValueError("池中每台设备须有相同后端、模型和执行配置的独立基线")
            singles.append(positive(baselines[key]["images_per_second"]))
        result = {**pool, "aggregation_efficiency_percent": pool["images_per_second"] / sum(singles) * 100,
                  "speedup": pool["images_per_second"] / max(singles),
                  "baseline_sum": sum(singles), "participants": [{"worker_id": k[0], "backend": k[1]} for k in configs]}
        result["review_required"] = result["aggregation_efficiency_percent"] > 100
        for worker, backend in configs:
            devices[worker][backend]["pools"].append(result)
    return {"devices": devices, "imported_at": iso(utcnow()), "unit": "images/s",
            "method": "median_of_quality_checked_single_device_and_pool_service_runs",
            "source_digest": common[0], "references_sha256": common[1]}


def recent_processing(db, now, job_id=None):
    """Observed burst/job wall time, separate from quality-checked benchmark rates."""
    since = iso(now - timedelta(seconds=30))
    condition, params = ("e.job_id=?", (job_id,)) if job_id else (
        "(e.finished_at>=? OR e.status='RUNNING')", (since,))
    rows = db.query("""SELECT e.* FROM executions e WHERE e.task_type='ocr.image.v1'
        AND e.started_at IS NOT NULL AND """ + condition + """
        AND (e.status!='SUCCEEDED' OR NOT EXISTS (
            SELECT 1 FROM executions prior WHERE prior.job_id=e.job_id AND prior.task_id=e.task_id
            AND prior.status='SUCCEEDED' AND (prior.finished_at<e.finished_at OR
                (prior.finished_at=e.finished_at AND prior.execution_id<e.execution_id))))
        ORDER BY e.started_at,e.execution_id""", params)
    groups = {}
    for row in rows:
        if not row["started_at"] or row["performance_backend"] not in BACKENDS or not (row["finished_at"] or row["status"] == "RUNNING"):
            continue
        begin = datetime.fromisoformat((row["performance_started_at"] or row["started_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat((row["performance_finished_at"] or row["finished_at"]).replace("Z", "+00:00")) if row["finished_at"] else now
        if end < begin:
            continue
        item = groups.setdefault((row["worker_id"], row["performance_backend"]), {
            "completed": 0, "active_executions": 0, "start": begin, "end": end,
            "worker_seconds": [], "inference_seconds": [],
        })
        item["start"], item["end"] = min(item["start"], begin), max(item["end"], end)
        item["active_executions"] += row["status"] == "RUNNING"
        if row["status"] == "SUCCEEDED":
            item["completed"] += 1
            timings = parse_json(row["timings"], {})
            for source, target in (("total_worker_ms", "worker_seconds"), ("inference_ms", "inference_seconds")):
                value = timings.get(source)
                if type(value) in (int, float) and math.isfinite(value) and value > 0:
                    item[target].append(value / 1000)
    devices = {}
    for (worker, backend), item in groups.items():
        seconds = (item["end"] - item["start"]).total_seconds()
        devices.setdefault(worker, {})[backend] = {
            "completed": item["completed"], "active_executions": item["active_executions"],
            "processing_images_per_second": item["completed"] / seconds if seconds > 0 and (item["completed"] or not item["active_executions"]) else None,
            "measurement_seconds": seconds, "started_at": precise(item["start"]), "finished_at": precise(item["end"]),
            "average_worker_seconds": statistics.mean(item["worker_seconds"]) if item["worker_seconds"] else None,
            "average_inference_seconds": statistics.mean(item["inference_seconds"]) if item["inference_seconds"] else None,
        }
    return {"lookback_seconds": 30, "job_id": job_id, "devices": devices}


def snapshot(db, job_id=None):
    now = utcnow()
    start = iso(now - timedelta(seconds=300))
    devices, seen = {}, set()
    for row in db.query("""SELECT e.worker_id,e.job_id,e.task_id,e.performance_backend FROM executions e
        WHERE e.status='SUCCEEDED' AND e.task_type='ocr.image.v1' AND e.finished_at>=?
        AND NOT EXISTS (SELECT 1 FROM executions prior WHERE prior.job_id=e.job_id
            AND prior.task_id=e.task_id AND prior.status='SUCCEEDED'
            AND (prior.finished_at<e.finished_at OR
                (prior.finished_at=e.finished_at AND prior.execution_id<e.execution_id)))
        ORDER BY e.finished_at,e.execution_id""", (start,)):
        logical = (row["job_id"], row["task_id"])
        if logical in seen:
            continue
        seen.add(logical)
        backend = row["performance_backend"]
        if backend not in BACKENDS:
            continue  # Old rows have no trustworthy backend; never infer from today's node.
        record = devices.setdefault(row["worker_id"], {}).setdefault(backend, {"completed": 0})
        record["completed"] += 1
        record["successful_images_per_second"] = record["completed"] / 300
    samples = {}
    for row in db.query("SELECT worker_id,telemetry FROM performance_telemetry WHERE sampled_at>=?", (start,)):
        target = samples.setdefault(row["worker_id"], {"cpu_percent": [], "gpu_percent": []})
        body = parse_json(row["telemetry"], {})
        for field in target:
            value = body.get(field)
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100:
                target[field].append(value)
    averages = {worker: {field: {"mean": statistics.mean(values) if values else None, "samples": len(values)} for field, values in fields.items()} for worker, fields in samples.items()}
    stored = db.query_one("SELECT value FROM meta WHERE key='performance_report'")
    return {"window_start": start, "window_end": iso(now), "window_seconds": 300,
            "sampling_interval_seconds": 3, "devices": devices, "utilization": averages,
            "realtime": recent_processing(db, now, job_id),
            "formal": parse_json(stored["value"]) if stored else None}
