"""时间与 JSON 工具：统一 UTC + Z 后缀（OpenAPI 日期格式 pattern Z$）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_now() -> str:
    return iso(utcnow())


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def plus(dt: datetime, seconds: float) -> datetime:
    return dt + timedelta(seconds=seconds)


def canonical_json(payload: Any) -> str:
    """幂等载荷指纹：schema 校验后的字段值；键序不影响相等，数组序保留。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
