"""线上准备与离线基线使用相同的模型锁和环境校验。"""
from __future__ import annotations

import hashlib
import json
import platform
from importlib.metadata import version

import psutil

from resource_service.catalog import BACKEND_PROVIDERS


class ProfileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_locked_runner(raw: bytes, task_type: str, profile_id: str, digest: str,
                       backend: str = "cpu", threads: int = 0):
    profile = json.loads(raw)
    if (
        hashlib.sha256(raw).hexdigest() != digest
        or profile["task_type"] != task_type
        or profile["profile_id"] != profile_id
    ):
        raise ProfileError("PROFILE_MISMATCH", "节点锁文件与任务指定的 profile 不一致")
    if (
        task_type != "ocr.image.v1"
        or backend not in BACKEND_PROVIDERS
        or backend not in profile["allowed_backends"]
    ):
        raise ProfileError("MODEL_LOAD_FAILED", "本节点缺少该 profile 的执行后端")
    dependencies = profile["dependencies"]
    if platform.python_version() != dependencies["python"]:
        raise ProfileError("MODEL_LOAD_FAILED", "Python 版本与 profile 锁文件不一致")
    for package in ("rapidocr", "pillow"):
        if version(package) != dependencies[package]:
            raise ProfileError("MODEL_LOAD_FAILED", f"{package} 版本与 profile 锁文件不一致")
    import onnxruntime

    if onnxruntime.__version__ != dependencies["onnxruntime"]:
        raise ProfileError("MODEL_LOAD_FAILED", "ONNX Runtime 版本与 profile 锁文件不一致")
    if BACKEND_PROVIDERS[backend] not in onnxruntime.get_available_providers():
        raise ProfileError("MODEL_LOAD_FAILED", f"ONNX Runtime 未提供 {BACKEND_PROVIDERS[backend]}")
    if psutil.virtual_memory().available < profile["minimum_ram_bytes"]:
        raise ProfileError("MODEL_LOAD_FAILED", "可用内存低于 profile 要求")

    from .rapidocr_runner import RapidOCRRunner, combined_model_sha256

    model_sha = combined_model_sha256()
    if model_sha != profile["model_sha256"]:
        raise ProfileError("MODEL_HASH_MISMATCH", "模型 SHA 与 profile 锁文件不符")
    runner = RapidOCRRunner(threads=threads, backend=backend)
    try:
        provider = runner.runtime_provider()
    except Exception as exc:
        raise ProfileError("MODEL_LOAD_FAILED", f"模型无法启用 {BACKEND_PROVIDERS[backend]}") from exc
    return runner, model_sha, provider
