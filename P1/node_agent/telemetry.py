"""资源采样：CPU/内存用 psutil；CUDA 用 NVSMI，DirectML 用 PDH。未知为 null。"""
from __future__ import annotations

import subprocess
import os
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import psutil
except ImportError:  # psutil 缺失时降级为 null 采样
    psutil = None  # type: ignore[assignment]


def _gpu_sample(device_id: int = 0) -> tuple[Optional[float], Optional[int]]:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "-i", str(device_id),
                "--query-gpu=utilization.gpu,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            util, mem = out.stdout.strip().splitlines()[0].split(",", 1)
            return float(util.strip()), int(float(mem.strip())) * 1024 * 1024
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None, None


def sample(device_id: int = 0) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cpu = ram = None
    if psutil is not None:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().used
    gpu_util, gpu_mem = _gpu_sample(device_id)
    source = "nvidia-smi" if gpu_util is not None else "unavailable"
    if os.environ.get("P4_EXECUTION_BACKEND") == "directml":
        gpu_util, gpu_mem, source = None, None, "unavailable"
        try:
            from .windows_gpu import utilization
            gpu_util = utilization(os.environ.get("P4_ADAPTER_LUID", ""))
            if gpu_util is not None:
                source = "windows-pdh"
        except (OSError, ValueError, AttributeError):
            pass
    elif os.environ.get("P4_EXECUTION_BACKEND") in {"cpu", "coreml"}:
        gpu_util, gpu_mem, source = None, None, "unavailable"
    return {
        "sampled_at": now,
        "cpu_percent": cpu,
        "ram_used_bytes": ram,
        "gpu_percent": gpu_util,
        "gpu_memory_used_bytes": gpu_mem,
        "gpu_metric_source": source,
    }
