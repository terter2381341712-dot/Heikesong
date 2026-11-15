"""Prepare one CPU worker plus every compatible physical GPU, independently."""

import json
import os
import subprocess

import managed_runtime


def build(config, cpu_python, physical_id, progress, manager=None):
    manager = manager or managed_runtime.Manager()
    probe = managed_runtime.ROOT / "P1/node_agent/accelerators.py"
    try:
        reply = subprocess.run([str(cpu_python), "-I", str(probe)], capture_output=True,
                               text=True, encoding="utf-8", timeout=25, check=True,
                               env=managed_runtime.process_environment(cpu_python))
        adapters = json.loads(reply.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        progress(f"GPU 自动探测不可用，CPU 继续工作：{exc}")
        return []
    if not adapters:
        progress("未检测到兼容 GPU，使用 CPU。")
        return []
    cpu = {k: v for k, v in config["node"].items() if k != "python" and v is not None} if config["node"]["backend"] == "cpu" else {
        "backend": "cpu", "slots": 1, "threads": 0, "device_id": 0, "prefetch": 0}
    cores = os.cpu_count() or 1
    # Reserve host threads for GPU preprocessing rather than starting unlimited
    # CPU inference workers alongside every GPU.
    budget = max(1, min(cpu.get("cpu_budget") or cores, cores - len(adapters)))
    cpu["slots"] = min(cpu["slots"], budget)
    cpu.update(threads=max(1, min(cpu["threads"] or 4, budget // cpu["slots"])), cpu_budget=budget)
    workers = [{"key": "cpu", "python": str(cpu_python), "options": cpu}]
    prepared = {}
    for adapter in adapters:
        backend, index = adapter["backend"], adapter["device_id"]
        try:
            if backend not in prepared:
                component = {"directml": "DML", "cuda": "CUDA", "coreml": "P1"}[backend]
                try:
                    prepared[backend] = manager.ensure(component, progress)
                except (OSError, ValueError, subprocess.SubprocessError):
                    if backend != "cuda" or "directml_device_id" not in adapter:
                        raise
                    progress(f"{adapter['name']} 的 CUDA 环境不可用，尝试 DirectML；不会将 CPU 执行记作 GPU。")
                    adapter = {**adapter, "backend": "directml", "device_id": adapter["directml_device_id"]}
                    backend, index = adapter["backend"], adapter["device_id"]
                    prepared[backend] = manager.ensure("DML", progress)
            workers.append({"key": f"gpu-{backend}-{index}", "python": str(prepared[backend]),
                            "adapter": adapter, "options": {"backend": backend, "device_id": index,
                                "slots": 1, "threads": 1, "cpu_budget": cores, "prefetch": 0}})
            progress(f"纳入 GPU：{adapter['name']}，后端 {backend}，设备 {index}；执行能力将在模型准备时自测。")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            progress(f"GPU {adapter['name']} 暂不可用：{exc}；其他设备继续工作。")
    return {"physical_id": physical_id, "workers": workers} if len(workers) > 1 else []
