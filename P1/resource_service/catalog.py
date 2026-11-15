"""任务类型目录与 profile 配置状态：P1 拥有 profiles/ 下的运行时目录。

- profiles/task_types.json：类型描述（来源为共享契约，P1 维护运行时副本）。
- profiles/<profile_id>.lock.json 存在 => CONFIGURED，digest 为锁文件原字节 SHA256。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

DEFAULT_TASK_TYPES: list[dict] = [
    {
        "task_type": "ocr.image.v1",
        "display_name": "图片文字识别",
        "profile_id": "ocr.default.v1",
        "profile_digest": None,
        "configuration_state": "UNCONFIGURED",
        "supported_languages": ["auto", "zh", "en"],
        "input_mime_types": ["image/jpeg", "image/png"],
        "max_file_bytes": 10485760,
        "max_pixels": 20000000,
        "max_duration_ms": None,
        "max_inputs_per_job": 200,
        "split_unit": "input_file",
        "result_schema": "OCRResult",
        "default_timeout_seconds": 120,
        "reason": "计划类型；模型、许可和依赖待P1验证发布。",
    },
    {
        "task_type": "asr.file.v1",
        "display_name": "短录音文字识别",
        "profile_id": "asr.default.v1",
        "profile_digest": None,
        "configuration_state": "UNCONFIGURED",
        "supported_languages": ["auto", "zh", "en"],
        "input_mime_types": ["audio/wav"],
        "max_file_bytes": 2097152,
        "max_pixels": None,
        "max_duration_ms": 60000,
        "max_inputs_per_job": 200,
        "split_unit": "input_file",
        "result_schema": "ASRResult",
        "default_timeout_seconds": 300,
        "reason": "计划类型；模型、许可和依赖待P1验证发布。",
    },
]


class Catalog:
    def __init__(self, profiles_dir: Path) -> None:
        self._dir = profiles_dir
        self._types: list[dict] = []
        self._model_hashes: dict[str, str] = {}
        self._backends: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        source = self._dir / "task_types.json"
        if source.exists():
            data = json.loads(source.read_text(encoding="utf-8"))
            self._types = data.get("task_types", DEFAULT_TASK_TYPES)
        else:
            self._types = json.loads(json.dumps(DEFAULT_TASK_TYPES))
        self._model_hashes.clear()
        self._backends.clear()
        for t in self._types:
            lock = self._dir / f"{t['profile_id']}.lock.json"
            t.update(profile_digest=None, configuration_state="UNCONFIGURED")
            if t["task_type"] != "ocr.image.v1" or not lock.is_file():
                t["reason"] = t.get("reason") or "profile 锁文件未发布。"
                continue
            try:
                raw = lock.read_bytes()
                profile = json.loads(raw)
                model_sha = profile["model_sha256"]
                if (
                    profile["task_type"] != t["task_type"] or profile["profile_id"] != t["profile_id"]
                    or not re.fullmatch(r"[a-f0-9]{64}", model_sha)
                    or profile["weights_license_verified"] is not True
                    or not profile["allowed_backends"]
                    or not set(profile["allowed_backends"]) <= {"cpu", "cuda"}
                ):
                    raise ValueError("profile 锁文件字段与目录不一致")
            except (OSError, ValueError, KeyError, TypeError):
                t["reason"] = "profile 锁文件无效。"
                continue
            self._model_hashes[t["profile_id"]] = model_sha
            self._backends[t["profile_id"]] = profile["allowed_backends"]
            t.update(
                profile_digest=hashlib.sha256(raw).hexdigest(),
                configuration_state="CONFIGURED",
                reason=None,
            )

    def reload(self) -> None:
        self._load()

    def all(self) -> list[dict]:
        return [dict(t) for t in self._types]

    def get(self, task_type: str) -> Optional[dict]:
        for t in self._types:
            if t["task_type"] == task_type:
                return dict(t)
        return None

    def model_sha256(self, profile_id: str) -> Optional[str]:
        return self._model_hashes.get(profile_id)

    def backend_allowed(self, profile_id: str, backend: str, provider: str) -> bool:
        return (backend in self._backends.get(profile_id, [])
                and provider == {"cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider"}.get(backend))
