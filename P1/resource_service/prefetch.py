"""One expiring advisory input per node; never reserves an execution slot."""

from typing import Optional

from fastapi import Depends, Request
from pydantic import Field

from .auth import require_task
from .db import parse_json
from .errors import ApiError, not_found
from .models import (
    ARTIFACT_PAT,
    HEX64_PAT,
    PROFILE_PAT,
    UUID_PAT,
    ClaimRequest,
    _Strict,
)
from .timeutil import canonical_json, iso, plus, utcnow


class InputHint(_Strict):
    job_id: str = Field(pattern=UUID_PAT)
    task_id: str = Field(pattern=UUID_PAT)
    task_type: str = Field(pattern=r"^ocr\.image\.v1$")
    profile_id: str = Field(pattern=PROFILE_PAT)
    profile_digest: str = Field(pattern=HEX64_PAT)
    input_artifact_id: str = Field(pattern=ARTIFACT_PAT)


class PrefetchRequest(_Strict):
    worker_session_id: str = Field(pattern=UUID_PAT)
    revision: int = Field(ge=1, le=9223372036854775807)
    hint: Optional[InputHint]


def usable(node, hint):
    return (
        node["prefetch_capacity"] == 1
        and node["max_slots"] == 1
        and node["connection"] == "ONLINE"
        and node["mode"] == "ACTIVE"
        and node["sharing_enabled"]
        and not node["cleanup_pending"]
        and any(
            c.get("state") == "READY"
            and c.get("verified")
            and all(
                c.get(k) == hint[k]
                for k in ("task_type", "profile_id", "profile_digest")
            )
            for c in parse_json(node["capabilities"], [])
        )
    )


def attach_routes(app, state, get_node, authenticate):
    def task_auth(request: Request):
        require_task(request, state().config)

    @app.post(
        "/api/resource/v1/resources/{worker_id}/prefetch",
        dependencies=[Depends(task_auth)],
    )
    def set_prefetch(worker_id: str, body: PrefetchRequest, request: Request):
        s = state()
        require_task(request, s.config)
        raw = canonical_json(body.hint.model_dump()) if body.hint else None
        with s.db.write() as conn:
            node = get_node(s, worker_id)
            if node["session_id"] != body.worker_session_id:
                raise ApiError(409, "SESSION_CHANGED", "预取属于旧会话")
            old = conn.execute(
                "SELECT * FROM input_prefetches WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if old and (
                body.revision < old["revision"]
                or (
                    body.revision == old["revision"]
                    and (
                        raw != old["hint"]
                        or old["session_id"] != body.worker_session_id
                    )
                )
            ):
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "预取修订号已失效或载荷不同"
                )
            expires = None
            if body.hint:
                hint = body.hint.model_dump()
                if not usable(node, hint):
                    raise ApiError(
                        409, "RESOURCE_BUSY", "节点尚不允许预取", retryable=True
                    )
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=?",
                    (hint["input_artifact_id"],),
                ).fetchone()
                if artifact is None:
                    raise not_found("预取输入不存在")
                media = parse_json(artifact["media_info"], {})
                if (
                    artifact["kind"] != "INPUT"
                    or artifact["content_type"] not in {"image/png", "image/jpeg"}
                    or artifact["size_bytes"] > 10 * 1024**2
                    or not media.get("width")
                    or not media.get("height")
                    or media["width"] * media["height"] > 20_000_000
                ):
                    raise ApiError(422, "INPUT_INVALID", "预取输入超过图片限制")
                expires = iso(plus(utcnow(), 15))
            conn.execute(
                "INSERT INTO input_prefetches VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
                "session_id=excluded.session_id, revision=excluded.revision, hint=excluded.hint, expires_at=excluded.expires_at",
                (worker_id, body.worker_session_id, body.revision, raw, expires),
            )
        return {
            "worker_id": worker_id,
            "worker_session_id": body.worker_session_id,
            "revision": body.revision,
            "expires_at": expires,
        }

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/prefetch")
    def get_prefetch(worker_id: str, body: ClaimRequest):
        s = state()
        with s.db.write() as conn:
            node = get_node(s, worker_id)
            authenticate(node, body.session_id, body.node_secret)
            row = conn.execute(
                "SELECT * FROM input_prefetches WHERE worker_id=?", (worker_id,)
            ).fetchone()
            hint = parse_json(row["hint"]) if row else None
            if not (
                hint
                and row["session_id"] == body.session_id
                and row["expires_at"] > iso(utcnow())
                and usable(node, hint)
            ):
                hint = None
            active = conn.execute(
                "SELECT job_id,task_id,status FROM executions WHERE worker_id=? "
                "AND worker_session_id=? AND status IN ('ACCEPTED','RUNNING','CANCEL_REQUESTED')",
                (worker_id, body.session_id),
            ).fetchall()
            cancelled = {
                (r["job_id"], r["task_id"])
                for r in active
                if r["status"] == "CANCEL_REQUESTED"
            }
            if hint and (hint["job_id"], hint["task_id"]) in cancelled:
                hint = None
            if hint:
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=?",
                    (hint["input_artifact_id"],),
                ).fetchone()
                hint.update(
                    input_sha256=artifact["sha256"],
                    input_size_bytes=artifact["size_bytes"],
                    input_media_info=parse_json(artifact["media_info"]),
                    input_content_type=artifact["content_type"],
                    expires_at=row["expires_at"],
                )
            return {
                "hint": hint,
                "owned_tasks": [
                    [r["job_id"], r["task_id"]]
                    for r in active
                    if r["status"] != "CANCEL_REQUESTED"
                ],
                "enabled": bool(
                    node["sharing_enabled"]
                    and node["mode"] == "ACTIVE"
                    and not node["cleanup_pending"]
                ),
            }
