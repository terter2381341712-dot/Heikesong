"""Bearer 令牌鉴权：DEMO_TOKEN（用户/前端/验收）与 TASK_TOKEN（调度服务）。"""
from __future__ import annotations

from fastapi import Request

from .config import Config
from .errors import forbidden, unauthorized

ROLE_DEMO = "demo"
ROLE_TASK = "task"


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise unauthorized()
    return auth[len("Bearer "):].strip()


def require_demo_or_task(request: Request, config: Config) -> str:
    token = _bearer(request)
    if token == config.demo_token:
        return ROLE_DEMO
    if token == config.task_token:
        return ROLE_TASK
    raise unauthorized()


def require_task(request: Request, config: Config) -> str:
    token = _bearer(request)
    if token == config.task_token:
        return ROLE_TASK
    if token == config.demo_token:
        # 角色存在但无权限：403 而不是 401
        raise forbidden()
    raise unauthorized()


def require_join(request: Request, config: Config) -> None:
    token = _bearer(request)
    if token != config.join_token:
        raise unauthorized()
