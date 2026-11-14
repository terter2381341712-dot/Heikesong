"""统一错误：HTTP 状态 + OpenAPI Error 结构（code/message/retryable/request_id/details）。"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        body = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": str(uuid.uuid4()),
            "details": self.details,
        }
        return JSONResponse(status_code=self.status_code, content=body)


def error_response(
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: Optional[dict] = None,
) -> JSONResponse:
    return ApiError(status_code, code, message, retryable, details).to_response()


def unauthorized() -> ApiError:
    return ApiError(401, "UNAUTHORIZED", "缺失或无效令牌")


def forbidden() -> ApiError:
    return ApiError(403, "FORBIDDEN", "令牌角色不允许该操作")


def not_found(message: str = "资源不存在") -> ApiError:
    return ApiError(404, "NOT_FOUND", message, retryable=False)


def internal_error(message: str = "内部错误") -> ApiError:
    return ApiError(500, "INTERNAL_ERROR", message, retryable=True)


def ensure_details_keys(details: dict) -> dict:
    allowed = {
        "execution_id",
        "preparation_id",
        "artifact_id",
        "job_id",
        "retry_after_seconds",
        "field",
    }
    return {k: v for k, v in details.items() if k in allowed and v is not None}


def detail_of(error: Optional[dict]) -> dict:
    """从持久化的 error JSON 里恢复 details（供重放 409 使用）。"""
    if not error:
        return {}
    if isinstance(error, str):
        import json

        error = json.loads(error)
    return ensure_details_keys(error.get("details") or {})
