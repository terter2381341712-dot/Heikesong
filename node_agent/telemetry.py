"""资源采样：CPU/内存用 psutil；GPU 只信 nvidia-smi。未知 null，不用 0 顶替。"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import psutil
except ImportError:  # psutil 缺失时降级为 null 采样
    psutil = None  # type: ignore[assignment]


def _gpu_sample() -> tuple[Optional[float], Optional[int]]:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
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


def sample() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cpu = ram = None
    if psutil is not None:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().used
    gpu_util, gpu_mem = _gpu_sample()
    source = "nvidia-smi" if gpu_util is not None else "unavailable"
    return {
        "sampled_at": now,
        "cpu_percent": cpu,
        "ram_used_bytes": ram,
        "gpu_percent": gpu_util,
        "gpu_memory_used_bytes": gpu_mem,
        "gpu_metric_source": source,
    }
