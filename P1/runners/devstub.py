"""开发占位执行器：只产出结构合法的空结果，用于联调流水线。

明确标注：DevStub 不执行真实计算，不得用于正式性能或质量数据。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Runner


class DevStubRunner(Runner):
    task_type = ""  # 兼容两种类型；按 media_info 判断

    def execute(self, input_path: Path, media_info: dict, parameters: dict, workdir: Path) -> dict:
        task_type = self._detect(media_info)
        if task_type == "ocr.image.v1":
            return {
                "schema_version": "1.0",
                "task_type": "ocr.image.v1",
                "input_artifact_id": "",  # 服务端提交时以执行记录为准
                "image_width": int(media_info["width"]),
                "image_height": int(media_info["height"]),
                "text": "",
                "blocks": [],
            }
        return {
            "schema_version": "1.0",
            "task_type": "asr.file.v1",
            "input_artifact_id": "",
            "duration_ms": int(media_info["duration_ms"]),
            "language": None,
            "text": "",
            "segments": [],
        }

    def _detect(self, media_info: dict) -> str:
        if media_info.get("width") is not None:
            return "ocr.image.v1"
        if media_info.get("duration_ms") is not None:
            return "asr.file.v1"
        raise ValueError("无法从 media_info 判断任务类型")
