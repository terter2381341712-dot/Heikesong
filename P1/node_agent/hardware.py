"""硬件探测：尽力采集，未知写 null，不让人工字段伪造加速能力。"""
from __future__ import annotations

import platform
import os
import subprocess
import sys
from typing import Any, Optional


def _run(cmd: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _cpu_name() -> Optional[str]:
    if sys.platform == "darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if sys.platform == "win32":
        return platform.processor() or None
    return _run(["sh", "-c", "lscpu | grep 'Model name' | sed 's/.*: //'"]) or platform.processor() or None


def _ram_total() -> Optional[int]:
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            return int(out)
    if sys.platform == "win32":
        out = _run(["wmic", "computersystem", "get", "TotalPhysicalMemory"])
        if out:
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
        return None
    out = _run(["sh", "-c", "awk '/MemTotal/ {print $2*1024}' /proc/meminfo"])
    if out and out.isdigit():
        return int(out)
    return None


def _gpu(device_id: int = 0) -> tuple[Optional[str], Optional[int]]:
    """只认 nvidia-smi 的真实输出；其余平台返回 null（GPU 能力以自测为准）。"""
    out = _run([
        "nvidia-smi",
        "-i", str(device_id),
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if out and "," in out:
        name, mem = out.split(",", 1)
        try:
            return name.strip(), int(float(mem.strip())) * 1024 * 1024
        except ValueError:
            return name.strip(), None
    return None, None


def detect_hardware(device_id: int = 0) -> dict:
    import psutil

    gpu_name, gpu_vram = _gpu(device_id)
    memory = psutil.virtual_memory()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_name": _cpu_name(),
        "logical_cores": os.cpu_count(),
        "ram_total_bytes": memory.total,
        "ram_available_bytes": memory.available,
        "gpu_name": gpu_name,
        "gpu_vram_bytes": gpu_vram,
        "gpu_info_source": "detected" if gpu_name else "unavailable",
        "manual_labels": {},
        "detection_errors": [],
    }
