"""任务类型目录与 profile 配置状态：P1 拥有 profiles/ 下的运行时目录。

- profiles/task_types.json：类型描述（来源为共享契约，P1 维护运行时副本）。
- profiles/<profile_id>.lock.json 存在 => CONFIGURED，digest 为锁文件原字节 SHA256。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

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
        self._load()

    def _load(self) -> None:
        source = self._dir / "task_types.json"
        if source.exists():
            data = json.loads(source.read_text(encoding="utf-8"))
            self._types = data.get("task_types", DEFAULT_TASK_TYPES)
        else:
            self._types = json.loads(json.dumps(DEFAULT_TASK_TYPES))
        for t in self._types:
            lock = self._dir / f"{t['profile_id']}.lock.json"
            if lock.exists():
                digest = hashlib.sha256(lock.read_bytes()).hexdigest()
                t["profile_digest"] = digest
                t["configuration_state"] = "CONFIGURED"
                t["reason"] = t.get("reason")
            else:
                t["profile_digest"] = None
                t["configuration_state"] = "UNCONFIGURED"
                if not t.get("reason"):
                    t["reason"] = "profile 锁文件未发布。"

    def reload(self) -> None:
        self._load()

    def all(self) -> list[dict]:
        return [dict(t) for t in self._types]

    def get(self, task_type: str) -> Optional[dict]:
        for t in self._types:
            if t["task_type"] == task_type:
                return dict(t)
        return None

    def find_profile(self, profile_id: str, profile_digest: str) -> Optional[dict]:
        for t in self._types:
            if t["profile_id"] == profile_id and t["profile_digest"] == profile_digest:
                return dict(t)
        return None
