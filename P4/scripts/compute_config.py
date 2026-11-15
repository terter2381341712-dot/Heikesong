"""Optional, credential-free compute settings shared by both P4 launchers."""

import copy
import json
import os
import subprocess
from pathlib import Path

import managed_runtime

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_VERSION = "0.4.0"
DEFAULT_PATH = ROOT / "P4/var/compute.json"
DEFAULTS = {
    "auto_devices": True,
    "profile_set": "default",
    "node": {"backend": "cpu", "slots": 1, "threads": 0, "cpu_budget": None,
             "device_id": 0, "prefetch": 0, "gpu_lane_vram_bytes": None, "python": None},
    "scheduler": {"policy": "resource_aware"},
}


def load(path=None):
    source = Path(path) if path is not None else DEFAULT_PATH
    source = Path(os.path.abspath(source))
    result = copy.deepcopy(DEFAULTS)
    if source.exists():
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("计算配置无法读取或不是合法 JSON") from exc
        if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
            raise ValueError("计算配置含未知字段；不要在此文件保存 Token 或连接配置")
        for key, value in raw.items():
            if key in {"node", "scheduler"}:
                if not isinstance(value, dict) or set(value) - set(DEFAULTS[key]):
                    raise ValueError(f"计算配置 {key} 含未知字段")
                result[key].update(value)
            else:
                result[key] = value
    n = result["node"]
    if result["profile_set"] not in ("default", "heterogeneous"):
        raise ValueError("profile_set 必须为 default 或 heterogeneous")
    if type(result["auto_devices"]) is not bool:
        raise ValueError("auto_devices must be a boolean")
    if n["backend"] not in ("cpu", "cuda", "directml", "coreml"):
        raise ValueError("node.backend 必须为 cpu/cuda/directml/coreml")
    if result["scheduler"]["policy"] not in ("round_robin", "resource_aware"):
        raise ValueError("scheduler.policy 必须为 round_robin 或 resource_aware")
    for key, minimum, maximum in (("slots", 1, 64), ("threads", 0, None),
                                  ("device_id", 0, None), ("prefetch", 0, 1),
                                  ("cpu_budget", 1, None), ("gpu_lane_vram_bytes", 1, None)):
        value = n[key]
        if value is None and key in {"cpu_budget", "gpu_lane_vram_bytes"}:
            continue
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"node.{key} 超出允许的整数范围")
    budget = n["cpu_budget"]
    if budget is not None and budget > (os.cpu_count() or 1):
        raise ValueError("CPU 预算超过本机逻辑核心数")
    if n["slots"] > 1 and n["prefetch"]:
        raise ValueError("预取只支持单槽")
    if n["backend"] == "cpu" and n["slots"] > 1 and (not n["threads"] or budget is None):
        raise ValueError("CPU 多槽必须显式配置 threads 和 cpu_budget")
    if n["prefetch"] and (not n["threads"] or budget is None):
        raise ValueError("输入预取必须显式配置 threads 和 cpu_budget")
    if not (n["backend"] == "cuda" and n["slots"] > 1):
        if n["slots"] * n["threads"] + n["prefetch"] > (budget or os.cpu_count() or 1):
            raise ValueError("槽位线程与预取线程总量超过 CPU 预算")
    if n["backend"] == "cuda":
        if result["profile_set"] != "heterogeneous":
            raise ValueError("CUDA 节点须选择 heterogeneous profile")
        if n["slots"] > 1 and not n["gpu_lane_vram_bytes"]:
            raise ValueError("GPU 多路必须配置实测 gpu_lane_vram_bytes")
    if n["backend"] in {"directml", "coreml"} and n["slots"] != 1:
        raise ValueError("DirectML/CoreML 每张卡使用一个执行槽")
    if n["python"] is not None:
        if not isinstance(n["python"], str) or not n["python"].strip():
            raise ValueError("node.python 必须为解释器路径")
        # Do not resolve the interpreter symlink: that would escape its venv.
        n["python"] = os.path.abspath(source.parent / n["python"])
    return result


def profiles(config):
    base = ROOT / "P1/profiles"
    return base if config["profile_set"] == "default" else base / "heterogeneous"


def agent_options(config):
    return {k: v for k, v in config["node"].items() if k != "python" and v is not None}


def node_arguments(config):
    return [part for key, value in agent_options(config).items()
            for part in ("--" + key.replace("_", "-"), str(value))]


def node_interpreter(config, fallback):
    return Path(os.path.abspath(config["node"]["python"] or fallback))


def validate_node_runtime(config, executable):
    """Check the selected node environment, never install into or mutate it."""
    backend = config["node"]["backend"]
    profile = json.loads(next(profiles(config).glob("*.lock.json")).read_bytes())
    deps = profile.get("backend_dependencies", {}).get(backend, profile["dependencies"])
    expected = {k: v for k, v in deps.items() if k in {"rapidocr", "onnxruntime", "onnxruntime-gpu", "onnxruntime-directml", "pillow"}}
    opposite = "onnxruntime" if backend in {"cuda", "directml"} else "onnxruntime-gpu"
    code = """import importlib.metadata as m, json, platform, sys
expected=json.loads(sys.argv[1]); opposite=sys.argv[2]
installed={k:m.version(k) for k in expected}
try: m.version(opposite); mixed=True
except m.PackageNotFoundError: mixed=False
print(json.dumps({'python':platform.python_version(),'dependencies':installed,'mixed':mixed}))
"""
    try:
        p = subprocess.run([str(executable), "-c", code, json.dumps(expected), opposite],
                           capture_output=True, text=True, timeout=20, check=True,
                           env=managed_runtime.process_environment(executable))
        actual = json.loads(p.stdout)
        valid = actual["python"] == deps["python"] and actual["dependencies"] == expected and not actual["mixed"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        valid = False
    if not valid:
        raise ValueError(f"节点环境不符合 {backend} profile（Python {deps['python']}、冻结依赖、独立 ONNX Runtime）；请检查 node.python")


def environment(config, profile_directory=None):
    directory = profile_directory or profiles(config)
    return {"RESOURCE_PROFILES_DIR": str(directory), "NODE_PROFILES_DIR": str(directory),
            "TASK_SCHEDULER_POLICY": config["scheduler"]["policy"]}
