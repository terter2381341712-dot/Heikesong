"""线上准备与离线基线使用相同的模型锁和环境校验。"""
from __future__ import annotations

import hashlib
import json
import platform
from importlib.metadata import PackageNotFoundError, version

import psutil
from resource_service.backends import PROVIDERS, RUNTIME_PACKAGES


class ProfileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_locked_runner(raw: bytes, task_type: str, profile_id: str, digest: str,
                       backend: str = "cpu", threads: int = 0, device_id: int = 0):
    profile = json.loads(raw)
    if (
        hashlib.sha256(raw).hexdigest() != digest
        or profile["task_type"] != task_type
        or profile["profile_id"] != profile_id
    ):
        raise ProfileError("PROFILE_MISMATCH", "节点锁文件与任务指定的 profile 不一致")
    if task_type != "ocr.image.v1" or backend not in PROVIDERS or backend not in profile["allowed_backends"]:
        raise ProfileError("MODEL_LOAD_FAILED", "本节点缺少该 profile 的执行后端")
    if threads < 0 or device_id < 0:
        raise ProfileError("MODEL_LOAD_FAILED", "线程数和设备编号不能为负数")
    dependencies = profile.get("backend_dependencies", {}).get(backend, profile["dependencies"])
    if platform.python_version() != dependencies["python"]:
        raise ProfileError("MODEL_LOAD_FAILED", "Python 版本与 profile 锁文件不一致")
    runtime_package = RUNTIME_PACKAGES[backend]
    # These distributions install the same import namespace; mixing them is ambiguous.
    for opposite in set(RUNTIME_PACKAGES.values()) - {runtime_package}:
        try:
            version(opposite)
        except PackageNotFoundError:
            pass
        else:
            raise ProfileError("MODEL_LOAD_FAILED", f"请使用独立环境，仅安装 {runtime_package}")
    for package in ("rapidocr", runtime_package, "pillow"):
        try:
            actual = version(package)
        except PackageNotFoundError:
            raise ProfileError("MODEL_LOAD_FAILED", f"缺少依赖 {package}") from None
        if actual != dependencies.get(package):
            raise ProfileError("MODEL_LOAD_FAILED", f"{package} 版本与 profile 锁文件不一致")
    if psutil.virtual_memory().available < profile["minimum_ram_bytes"]:
        raise ProfileError("MODEL_LOAD_FAILED", "可用内存低于 profile 要求")

    from .rapidocr_runner import RapidOCRRunner, combined_model_sha256

    model_sha = combined_model_sha256()
    if model_sha != profile["model_sha256"]:
        raise ProfileError("MODEL_HASH_MISMATCH", "模型 SHA 与 profile 锁文件不符")
    runner = RapidOCRRunner(threads=threads, backend=backend, device_id=device_id)
    return runner, model_sha, runner.runtime_provider()
