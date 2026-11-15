"""离线基线：python -m node_agent.benchmark --manifest inputs.jsonl --profile <lock.json>
    --backend cpu --threads 2 --output-dir <目录> --run-id <本轮ID>

manifest 每行一个 BaselineInput：input_index / input_artifact_id / local_path / input_sha256。
stdout 只输出 UTF-8 JSONL：每输入一条 BaselineRecord，最后一条 BaselineSummary。
退出码：0=全部成功；1=至少一输入失败；2=配置/manifest/启动失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from resource_service.resultschema import ResultInvalid, validate_ocr_result
from runners.profile import load_locked_runner


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ms() -> float:
    return time.monotonic() * 1000


def _error(code: str, message: str, retryable: bool = False) -> dict:
    return {
        "code": code,
        "message": message[:500],
        "retryable": retryable,
        "request_id": str(uuid.uuid4()),
        "details": {},
    }


def load_worker_id() -> str:
    data_dir = Path(os.environ.get("NODE_AGENT_DATA_DIR", "./data/agent")).resolve()
    if data_dir.exists():
        for f in sorted(data_dir.glob("install-*.json")):
            try:
                worker_id = json.loads(f.read_text()).get("worker_id")
                if worker_id:
                    return str(worker_id)
            except (OSError, json.JSONDecodeError):
                continue
    return "baseline"


def main() -> int:
    parser = argparse.ArgumentParser(description="P1 离线基线（供 P4 单机吞吐对照）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="BaselineInput JSONL 文件")
    source.add_argument("--inputs", type=Path, help="独立测量用的 PNG/JPEG 目录")
    parser.add_argument("--profile", required=True, help="RuntimeProfile 锁文件路径")
    parser.add_argument("--backend", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--threads", type=int, default=0, help="推理线程数；0=不设置")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True, help="本轮运行 ID（[A-Za-z0-9_-]）")
    args = parser.parse_args()

    run_id = args.run_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) or args.threads < 0:
        print("invalid --run-id", file=sys.stderr)
        return 2

    # 线程数必须在 onnxruntime 加载前设置
    if args.threads > 0:
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ.setdefault(var, str(args.threads))

    lock_path = Path(args.profile)
    if not lock_path.exists():
        print(f"profile 锁文件不存在: {lock_path}", file=sys.stderr)
        return 2
    lock_bytes = lock_path.read_bytes()
    profile_digest = hashlib.sha256(lock_bytes).hexdigest()
    try:
        profile = json.loads(lock_bytes)
    except json.JSONDecodeError as e:
        print(f"profile 锁文件不是合法 JSON: {e}", file=sys.stderr)
        return 2
    task_type = profile.get("task_type", "")
    profile_id = profile.get("profile_id", "")
    if args.backend not in profile.get("allowed_backends", ["cpu"]):
        print(f"backend {args.backend} 不在锁文件 allowed_backends 内", file=sys.stderr)
        return 2

    inputs: list[dict] = []
    try:
        if args.inputs:
            paths = sorted(
                p for p in args.inputs.iterdir()
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
            )
            inputs = [
                {
                    "input_index": index,
                    "input_artifact_id": f"input-{index}",
                    "local_path": str(path.resolve()),
                    "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for index, path in enumerate(paths)
            ]
        else:
            inputs = [
                json.loads(line)
                for line in args.manifest.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        indices = set()
        for item in inputs:
            if (
                not isinstance(item, dict)
                or item.keys() != {"input_index", "input_artifact_id", "local_path", "input_sha256"}
                or type(item["input_index"]) is not int or item["input_index"] < 0
                or not isinstance(item["local_path"], str) or not item["local_path"]
                or not isinstance(item["input_artifact_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item["input_artifact_id"])
                or not isinstance(item["input_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["input_sha256"])
            ):
                raise ValueError("manifest 行不符合 BaselineInput")
            if item["input_index"] in indices:
                raise ValueError("manifest 重复 input_index")
            indices.add(item["input_index"])
    except (OSError, ValueError) as e:
        print(f"manifest 解析失败: {e}", file=sys.stderr)
        return 2
    if not inputs:
        print("manifest 为空", file=sys.stderr)
        return 2

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_dir = out_dir / "results"
    results_dir.mkdir(exist_ok=True)

    try:
        runner, model_sha, provider = load_locked_runner(
            lock_bytes, task_type, profile_id, profile_digest, args.backend, args.threads
        )
        print(f"backend={provider} model_sha256={model_sha}", file=sys.stderr)
    except Exception as e:
        print(f"执行器加载失败: {e}", file=sys.stderr)
        return 2

    worker_id = load_worker_id()

    def base_record(item: dict) -> dict:
        return {
            "record_type": "file",
            "run_id": run_id,
            "worker_id": worker_id,
            "task_type": task_type,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "input_index": item["input_index"],
            "input_artifact_id": item["input_artifact_id"],
            "input_sha256": item["input_sha256"],
            "backend": args.backend,
        }

    # 预热：第一个输入跑一次，不计入正式窗口
    warm_start = _now_ms()
    try:
        first = inputs[0]
        runner.execute(Path(first["local_path"]), {}, {}, results_dir)
        warmup_ms = _now_ms() - warm_start
    except Exception as e:
        print(f"预热失败（继续执行正式窗口）: {e}", file=sys.stderr)
        warmup_ms = _now_ms() - warm_start

    records: list[dict] = []
    succeeded = 0
    failed = 0
    window_started = _now_ms()
    summary_started = _iso_now()
    for item in inputs:
        record = base_record(item)
        record["started_at"] = _iso_now()
        t0 = _now_ms()
        result_path: Optional[str] = None
        result_sha: Optional[str] = None
        error: Optional[dict] = None
        status = "SUCCEEDED"
        try:
            local = Path(item["local_path"])
            if not local.exists():
                raise FileNotFoundError(f"输入文件不存在: {local}")
            actual_sha = hashlib.sha256(local.read_bytes()).hexdigest()
            if actual_sha != item["input_sha256"]:
                raise ValueError(f"输入 SHA 不匹配：期望 {item['input_sha256']}，实际 {actual_sha}")
            result = runner.execute(local, {}, {}, results_dir)
            result["input_artifact_id"] = item["input_artifact_id"]
            validate_ocr_result(result, item["input_artifact_id"])
            out_file = results_dir / f"{int(item['input_index']):04d}-{item['input_artifact_id']}.json"
            out_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            result_path = str(out_file)
            result_sha = hashlib.sha256(out_file.read_bytes()).hexdigest()
        except Exception as e:
            status = "FAILED"
            error = _error("RESULT_INVALID" if isinstance(e, ResultInvalid) else "EXECUTION_ERROR", str(e))
        record["finished_at"] = _iso_now()
        record["elapsed_ms"] = round(_now_ms() - t0, 3)
        record["status"] = status
        record["result_path"] = result_path
        record["result_sha256"] = result_sha
        record["error"] = error
        if status == "SUCCEEDED":
            succeeded += 1
        else:
            failed += 1
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    wall_ms = _now_ms() - window_started
    summary = {
        "record_type": "summary",
        "run_id": run_id,
        "started_at": summary_started,
        "finished_at": _iso_now(),
        "wall_ms": round(wall_ms, 3),
        "warmup_ms": round(warmup_ms, 3),
        "total": len(inputs),
        "succeeded": succeeded,
        "failed": failed,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)

    # 持久副本（stdout 之外）
    (out_dir / f"baseline-{run_id}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records + [summary]) + "\n",
        encoding="utf-8",
    )
    (out_dir / f"baseline-{run_id}.runtime.json").write_text(
        json.dumps({"runtime_provider": provider, "threads": args.threads}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
