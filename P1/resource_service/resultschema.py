"""结果 schema 校验：OCRResult / ASRResult（OpenAPI components 定义）。"""
from __future__ import annotations

import re
from typing import Any, Optional

RESULT_SCHEMA_VERSION = "1.0"


class ResultInvalid(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ResultInvalid(message)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_common(result: dict, task_type: str, input_artifact_id: str) -> None:
    _require(isinstance(result, dict), "结果必须是对象")
    _require(result.get("schema_version") == RESULT_SCHEMA_VERSION, "schema_version 必须为 '1.0'")
    _require(result.get("task_type") == task_type, "task_type 与执行请求不符")
    _require(bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", input_artifact_id)), "input_artifact_id 非法")
    _require(result.get("input_artifact_id") == input_artifact_id, "input_artifact_id 与执行请求不符")


def _check_keys(obj: dict, allowed: set, name: str) -> None:
    extra = set(obj.keys()) - allowed
    if extra:
        raise ResultInvalid(f"{name} 存在未声明字段: {sorted(extra)}")


def validate_ocr_result(result: dict, input_artifact_id: str) -> None:
    _check_common(result, "ocr.image.v1", input_artifact_id)
    _check_keys(
        result,
        {"schema_version", "task_type", "input_artifact_id", "image_width", "image_height", "text", "blocks"},
        "OCRResult",
    )
    w, h = result.get("image_width"), result.get("image_height")
    _require(isinstance(w, int) and not isinstance(w, bool) and w >= 1, "image_width 非法")
    _require(isinstance(h, int) and not isinstance(h, bool) and h >= 1, "image_height 非法")
    _require(isinstance(result.get("text"), str), "text 必须是字符串")
    blocks = result.get("blocks")
    _require(isinstance(blocks, list), "blocks 必须是数组")
    for block in blocks:
        _require(isinstance(block, dict), "block 必须是对象")
        _check_keys(block, {"text", "bbox", "confidence"}, "OCRBlock")
        _require(isinstance(block.get("text"), str), "block.text 必须是字符串")
        bbox = block.get("bbox")
        _require(isinstance(bbox, list) and len(bbox) == 4, "bbox 必须是 4 个数字")
        _require(all(_num(v) and v >= 0 for v in bbox), "bbox 数值必须 >= 0")
        x0, y0, x1, y1 = bbox
        _require(x0 <= x1 and y0 <= y1, "bbox 必须满足 x0<=x1, y0<=y1")
        _require(x1 <= w and y1 <= h, "bbox 必须在图像范围内")
        conf = block.get("confidence")
        _require("confidence" in block, "block.confidence 缺失")
        _require(conf is None or (_num(conf) and 0 <= conf <= 1), "confidence 必须在 0..1 或 null")


def validate_asr_result(result: dict, input_artifact_id: str) -> None:
    _check_common(result, "asr.file.v1", input_artifact_id)
    _check_keys(
        result,
        {"schema_version", "task_type", "input_artifact_id", "duration_ms", "language", "text", "segments"},
        "ASRResult",
    )
    duration = result.get("duration_ms")
    _require(
        isinstance(duration, int) and not isinstance(duration, bool) and 1 <= duration <= 60000,
        "duration_ms 必须在 1..60000",
    )
    lang = result.get("language")
    _require("language" in result, "language 缺失")
    _require(lang is None or (isinstance(lang, str) and len(lang) >= 1), "language 必须是字符串或 null")
    _require(isinstance(result.get("text"), str), "text 必须是字符串")
    segments = result.get("segments")
    _require(isinstance(segments, list), "segments 必须是数组")
    last_end: Optional[int] = None
    for seg in segments:
        _require(isinstance(seg, dict), "segment 必须是对象")
        _check_keys(seg, {"start_ms", "end_ms", "text"}, "ASRSegment")
        s, e = seg.get("start_ms"), seg.get("end_ms")
        _require(isinstance(s, int) and not isinstance(s, bool) and s >= 0, "start_ms 非法")
        _require(isinstance(e, int) and not isinstance(e, bool) and e >= 1, "end_ms 非法")
        _require(s < e <= duration, "segment 必须满足 start_ms < end_ms <= duration_ms")
        _require(last_end is None or s >= last_end, "segments 必须按时间排序且不重叠")
        _require(isinstance(seg.get("text"), str), "segment.text 必须是字符串")
        last_end = e
