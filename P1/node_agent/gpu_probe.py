"""Windows CUDA preflight with real OCR and per-model operator placement evidence.

Run in the GPU environment: python -m node_agent.gpu_probe --output-dir <directory>
Does not contact P1/P2. PASS is local GPU verification, not multi-machine acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
from collections import Counter
from pathlib import Path

from resource_service.config import REPO_ROOT
from runners.profile import load_locked_runner


def provider_counts(events):
    return dict(
        Counter(
            e.get("args", {}).get("provider")
            for e in events
            if e.get("cat") == "Node" and e.get("args", {}).get("provider")
        )
    )


def nvidia_mem_used_bytes(device_id):
    """nvidia-smi 查询当前显存占用字节；不可用时返回 None（不用 0 冒充）。"""
    try:
        smi = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                str(device_id),
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if smi.returncode == 0 and smi.stdout.strip():
            return int(float(smi.stdout.strip().splitlines()[0].strip())) * 1024 * 1024
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        default=REPO_ROOT / "profiles/heterogeneous/ocr.heterogeneous.v1.lock.json",
    )
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.device_id < 0:
        parser.error("device-id must be nonnegative")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = dict(
        status="FAIL",
        scope="LOCAL_GPU_ONLY",
        python=platform.python_version(),
        platform=platform.platform(),
        device_id=args.device_id,
    )
    sessions = {}
    try:
        import onnxruntime as ort
        from node_agent.agent import Agent
        from runners.rapidocr_runner import MODEL_NAMES, model_files

        raw = args.profile.read_bytes()
        p = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        report.update(
            profile_id=p["profile_id"],
            profile_digest=digest,
            available_providers=ort.get_available_providers(),
        )
        vram_before = nvidia_mem_used_bytes(args.device_id)
        runner, model_sha, provider = load_locked_runner(
            raw,
            p["task_type"],
            p["profile_id"],
            digest,
            backend="cuda",
            device_id=args.device_id,
        )
        report.update(model_sha256=model_sha, runtime_provider=provider)
        engine = runner.ensure_engine()
        # 每路显存估算 = 生产 runner 三个 session 加载前后的显存增量；
        # 在诊断性 profiling session 之前测量，避免把额外 session 计入。
        vram_after = nvidia_mem_used_bytes(args.device_id)
        report["lane_vram_estimate_bytes"] = (
            max(0, vram_after - vram_before)
            if vram_before is not None and vram_after is not None
            else None
        )
        paths = {p.name: p for p in model_files()}
        # Diagnostic-only sessions with profiling enabled, same frozen models and
        # provider options as the production runner. They process actual OCR tensors.
        for name, attr in [
            ("Det", "text_det"),
            ("Cls", "text_cls"),
            ("Rec", "text_rec"),
        ]:
            wrapper = getattr(engine, attr).session
            original = wrapper.session
            options = original.get_provider_options()
            providers = [(ep, options.get(ep, {})) for ep in original.get_providers()]
            opts = ort.SessionOptions()
            opts.enable_profiling = True
            opts.profile_file_prefix = str(output / name)
            session = ort.InferenceSession(
                str(paths[MODEL_NAMES[name]]), sess_options=opts, providers=providers
            )
            session.disable_fallback()
            wrapper.session = session
            sessions[name] = session
        runner.runtime_provider()
        agent = Agent.__new__(Agent)
        agent.data_dir = output
        started = time.monotonic()
        report["calibration"] = agent._self_test("ocr.image.v1", runner)
        report["self_test_ms"] = (time.monotonic() - started) * 1000
        if not report["calibration"]["passed"]:
            raise RuntimeError("真实 OCR 自测未通过")
        placements = {}
        for name, session in sessions.items():
            path = Path(session.end_profiling())
            counts = provider_counts(json.loads(path.read_text()))
            placements[name] = dict(trace=str(path), operators_by_provider=counts)
        sessions.clear()
        report["placements"] = placements
        if any(
            x["operators_by_provider"].get("CUDAExecutionProvider", 0) == 0
            for x in placements.values()
        ):
            raise RuntimeError(
                "至少一组模型未观察到 CUDA 算子执行，不能认定 GPU 验证通过"
            )
        try:
            smi = subprocess.run(
                [
                    "nvidia-smi",
                    "-i",
                    str(args.device_id),
                    "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            report["nvidia_smi"] = (
                smi.stdout.strip() if smi.returncode == 0 else smi.stderr.strip()
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["nvidia_smi_error"] = str(exc)
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for name, session in sessions.items():
            try:
                report.setdefault("partial_traces", {})[name] = session.end_profiling()
            except Exception:
                pass
        (output / "gpu-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
