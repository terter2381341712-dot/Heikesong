"""P1 资源服务：注册/心跳/文件/准备/单次执行/模式/事件。

语义依据 05-接口契约与联调手册 v0.2.0：
- 幂等记录先于资源检查；业务拒绝持久化 REJECTED，同 ID 重放仍返回原拒绝。
- 首次接受 201，重放 200；同 ID 不同载荷 409 IDEMPOTENCY_CONFLICT。
- 401/403/422 不创建任何记录。
- 心跳 3s、失联 15s、扫描 1s、准备硬期限 900s、租约 15s。
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .auth import require_demo_or_task, require_task, ROLE_DEMO
from .catalog import Catalog
from .config import Config, CONTRACT_VERSION
from .db import Database, parse_json
from .errors import (
    ApiError,
    error_response,
    ensure_details_keys,
    forbidden,
    internal_error,
    not_found,
    unauthorized,
)
from .media import probe_media
from .models import (
    CancelAckRequest,
    ClaimRequest,
    ExecutionCompleteRequest,
    ExecutionRequest,
    HeartbeatRequest,
    ModeRequest,
    PreparationRequest,
    PreparationStatusRequest,
    RegisterRequest,
    as_uuid,
)
from .resultschema import ResultInvalid, validate_asr_result, validate_ocr_result
from .timeutil import canonical_json, iso, iso_now, parse_iso, plus, utcnow

LEASE_SECONDS = 15.0
OFFLINE_AFTER_SECONDS = 15.0
SCAN_INTERVAL_SECONDS = 1.0
PREPARATION_TIMEOUT_SECONDS = 900.0

ACTIVE_EXEC_STATUSES = ("ACCEPTED", "RUNNING", "CANCEL_REQUESTED")
ACTIVE_PREP_STATUSES = ("ACCEPTED", "DOWNLOADING", "LOADING", "VERIFYING")
TERMINAL_EXEC_STATUSES = ("REJECTED", "SUCCEEDED", "FAILED", "CANCELLED")

INPUT_LIMITS: dict[str, tuple[int, Optional[int]]] = {
    # content_type -> (max_file_bytes, max_pixels)
    "image/jpeg": (10 * 1024 * 1024, 20_000_000),
    "image/png": (10 * 1024 * 1024, 20_000_000),
    "audio/wav": (2 * 1024 * 1024, None),
    "application/json": (100 * 1024 * 1024, None),
}
DEMO_UPLOAD_TYPES = ("image/jpeg", "image/png", "audio/wav")
WAV_MAX_DURATION_MS = 60_000


def new_artifact_id() -> str:
    return "a" + uuid.uuid4().hex


def _event_dict(row) -> dict:
    return {
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"],
        "type": row["type"],
        "worker_id": row["worker_id"],
        "job_id": row["job_id"],
        "task_id": row["task_id"],
        "execution_id": row["execution_id"],
        "preparation_id": row["preparation_id"],
        "message": row["message"],
    }


class State:
    """应用级共享状态：DB、目录、类型目录。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(config.db_path)
        self.catalog = Catalog(config.data_dir / "profiles")
        self._recover()

    def _recover(self) -> None:
        """服务重启：未终态执行 FAILED、活动准备 FAILED、旧会话失效；文件与终态保留。"""
        now = iso_now()
        with self.db.write() as conn:
            rows = conn.execute(
                "SELECT execution_id FROM executions WHERE status IN (?,?,?)",
                ACTIVE_EXEC_STATUSES,
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE executions SET status='FAILED', finished_at=?, error=? WHERE execution_id=?",
                    (
                        now,
                        canonical_json(
                            {
                                "code": "RESOURCE_SERVICE_RESTARTED",
                                "message": "服务重启，执行结果未知",
                                "retryable": True,
                                "details": {},
                            }
                        ),
                        row["execution_id"],
                    ),
                )
            conn.execute(
                "UPDATE preparations SET status='FAILED', finished_at=?, error=? WHERE status IN (?,?,?,?)",
                (
                    now,
                    canonical_json(
                        {
                            "code": "MODEL_LOAD_FAILED",
                            "message": "服务重启中断模型准备",
                            "retryable": True,
                            "details": {},
                        }
                    ),
                    *ACTIVE_PREP_STATUSES,
                ),
            )
            conn.execute(
                "UPDATE nodes SET connection='OFFLINE', session_id='', last_seen_at=NULL"
            )
            conn.execute(
                "INSERT INTO events (occurred_at, type, message) VALUES (?,?,?)",
                (now, "service.restarted", "资源服务重启，未终态执行与旧会话已失效"),
            )

    # ---------- 序列化 ----------

    def artifact_dict(self, row) -> dict:
        return {
            "artifact_id": row["artifact_id"],
            "kind": row["kind"],
            "original_name": row["original_name"],
            "content_type": row["content_type"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "media_info": parse_json(row["media_info"]),
            "download_path": f"/api/resource/v1/artifacts/{row['artifact_id']}/content",
            "created_at": row["created_at"],
        }

    def execution_dict(self, row) -> dict:
        result_artifact = None
        if row["result_artifact_id"]:
            arow = self.db.query_one(
                "SELECT * FROM artifacts WHERE artifact_id=?", (row["result_artifact_id"],)
            )
            if arow is not None:
                result_artifact = self.artifact_dict(arow)
        error = parse_json(row["error"])
        if error is not None:
            error = {
                "code": error["code"],
                "message": error["message"],
                "retryable": bool(error.get("retryable", False)),
                "request_id": str(uuid.uuid4()),
                "details": ensure_details_keys(error.get("details") or {}),
            }
        return {
            "execution_id": row["execution_id"],
            "job_id": row["job_id"],
            "task_id": row["task_id"],
            "attempt_no": row["attempt_no"],
            "worker_id": row["worker_id"],
            "worker_session_id": row["worker_session_id"],
            "task_type": row["task_type"],
            "profile_id": row["profile_id"],
            "profile_digest": row["profile_digest"],
            "input_artifact_id": row["input_artifact_id"],
            "parameters": parse_json(row["parameters"]),
            "timeout_seconds": row["timeout_seconds"],
            "status": row["status"],
            "input_sha256": row["input_sha256"],
            "created_at": row["created_at"],
            "accepted_at": row["accepted_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "lease_expires_at": row["lease_expires_at"],
            "hard_deadline_at": row["hard_deadline_at"],
            "result_artifact": result_artifact,
            "timings": parse_json(row["timings"]),
            "error": error,
        }

    def preparation_dict(self, row) -> dict:
        error = parse_json(row["error"])
        if error is not None:
            error = {
                "code": error["code"],
                "message": error["message"],
                "retryable": bool(error.get("retryable", False)),
                "request_id": str(uuid.uuid4()),
                "details": ensure_details_keys(error.get("details") or {}),
            }
        return {
            "preparation_id": row["preparation_id"],
            "worker_id": row["worker_id"],
            "worker_session_id": row["worker_session_id"],
            "task_type": row["task_type"],
            "profile_id": row["profile_id"],
            "profile_digest": row["profile_digest"],
            "status": row["status"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "progress": row["progress"],
            "error": error,
        }

    def node_active_executions(self, worker_id: str) -> list:
        return self.db.query(
            "SELECT execution_id FROM executions WHERE worker_id=? AND status IN (?,?,?) ORDER BY created_at",
            (worker_id, *ACTIVE_EXEC_STATUSES),
        )

    def node_active_preparation(self, worker_id: str):
        return self.db.query_one(
            "SELECT preparation_id FROM preparations WHERE worker_id=? AND status IN (?,?,?,?) ORDER BY created_at LIMIT 1",
            (worker_id, *ACTIVE_PREP_STATUSES),
        )

    def node_dict(self, row) -> dict:
        active = self.node_active_executions(row["worker_id"])
        prep = self.node_active_preparation(row["worker_id"])
        reserved = 1 if (active or prep) else 0
        available = 1 if (
            row["connection"] == "ONLINE"
            and row["mode"] == "ACTIVE"
            and not row["cleanup_pending"]
            and reserved == 0
        ) else 0
        last_seen = row["last_seen_at"]
        if row["connection"] != "ONLINE":
            last_seen = None
        telemetry = parse_json(row["telemetry"])
        return {
            "worker_id": row["worker_id"],
            "session_id": row["session_id"],
            "name": row["name"],
            "connection": row["connection"],
            "mode": row["mode"],
            "hardware": parse_json(row["hardware"]),
            "capabilities": parse_json(row["capabilities"], []),
            "max_slots": 1,
            "reserved_slots": reserved,
            "available_slots": available,
            "active_execution_ids": [r["execution_id"] for r in active],
            "active_preparation_id": prep["preparation_id"] if prep else None,
            "cleanup_pending": bool(row["cleanup_pending"]),
            "last_seen_at": last_seen,
            "telemetry": telemetry,
        }


def _get_node_or_404(state: State, worker_id: str):
    row = state.db.query_one("SELECT * FROM nodes WHERE worker_id=?", (worker_id,))
    if row is None:
        raise not_found(f"节点 {worker_id} 不存在")
    return row


def _require_session(node_row, session_id: str) -> None:
    if node_row["session_id"] != session_id:
        raise ApiError(409, "SESSION_CHANGED", "节点会话已失效，需重新注册", retryable=True)


def create_app(config: Optional[Config] = None) -> FastAPI:
    cfg = config or Config(
        bind_host="0.0.0.0",  # 测试工厂默认值；生产路径走 load_config
        port=8001,
        data_dir=Path("./data/resource-test").resolve(),
        demo_token="demo-token",
        task_token="task-token",
        join_token="join-token",
    )
    state_holder: dict[str, State] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = State(cfg)
        state_holder["state"] = state
        scanner = asyncio.create_task(_scan_loop(state))
        try:
            yield
        finally:
            scanner.cancel()
            state.db.close()

    app = FastAPI(title="P1 资源服务", version=CONTRACT_VERSION, lifespan=lifespan)

    def state() -> State:
        s = state_holder.get("state")
        if s is None:
            raise internal_error("服务尚未完成初始化")
        return s

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        return error_response(422, "INVALID_REQUEST", "请求结构不合法", retryable=False)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        import traceback

        traceback.print_exc()
        return internal_error().to_response()

    # ---------------- health ----------------

    @app.get("/api/resource/v1/healthz")
    def healthz():
        return {
            "service": "p1_resource",
            "contract_version": CONTRACT_VERSION,
            "status": "ok",
            "server_time": iso_now(),
        }

    # ---------------- resources ----------------

    @app.get("/api/resource/v1/resources")
    def get_resources(request: Request):
        s = state()
        require_demo_or_task(request, s.config)
        rows = s.db.query("SELECT * FROM nodes ORDER BY registered_at")
        return {
            "contract_version": CONTRACT_VERSION,
            "server_time": iso_now(),
            "resources": [s.node_dict(r) for r in rows],
        }

    @app.post("/api/resource/v1/resources/{worker_id}/mode")
    def set_mode(worker_id: str, body: ModeRequest, request: Request):
        s = state()
        require_demo_or_task(request, s.config)
        if request_state_role(request, s.config) != ROLE_DEMO:
            raise forbidden()
        node = _get_node_or_404(s, worker_id)
        now = iso_now()
        with s.db.write() as conn:
            conn.execute(
                "UPDATE nodes SET mode=? WHERE worker_id=?", (body.mode, worker_id)
            )
            s.db.insert_event(
                now, "node.mode_changed", f"节点 {worker_id} 模式 -> {body.mode}", worker_id=worker_id
            )
        return s.node_dict(_get_node_or_404(s, worker_id))

    # ---------------- task types ----------------

    @app.get("/api/resource/v1/task-types")
    def get_task_types(request: Request):
        s = state()
        require_demo_or_task(request, s.config)
        return {
            "contract_version": CONTRACT_VERSION,
            "server_time": iso_now(),
            "task_types": s.catalog.all(),
        }

    # ---------------- artifacts ----------------

    @app.post("/api/resource/v1/artifacts")
    async def upload_artifact(request: Request, upload_id: str = Form(...), file: UploadFile = Form(...)):
        s = state()
        role = require_demo_or_task(request, s.config)
        try:
            as_uuid(upload_id)
        except ValueError:
            raise ApiError(422, "INVALID_REQUEST", "upload_id 必须是 UUID")
        content_type = file.content_type or ""
        if content_type not in INPUT_LIMITS:
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", f"不支持的媒体类型 {content_type}")
        if role == ROLE_DEMO and content_type not in DEMO_UPLOAD_TYPES:
            raise forbidden()
        max_bytes, max_pixels = INPUT_LIMITS[content_type]

        chunks, size = [], 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ApiError(413, "INPUT_TOO_LARGE", f"文件超过 {max_bytes} 字节上限")
            chunks.append(chunk)
        data = b"".join(chunks)
        if size == 0:
            raise ApiError(422, "INVALID_REQUEST", "空文件")

        sha = hashlib.sha256(data).hexdigest()
        original_name = (file.filename or "unnamed").strip() or "unnamed"
        if len(original_name) > 255:
            original_name = original_name[-255:]

        media = _probe_or_422(content_type, data, max_pixels)

        db = s.db
        existing = db.query_one("SELECT * FROM artifacts WHERE upload_id=?", (upload_id,))
        if existing is not None:
            same = (
                existing["sha256"] == sha
                and existing["original_name"] == original_name
                and existing["content_type"] == content_type
            )
            if same:
                return s.artifact_dict(existing)
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同 upload_id 不同内容")

        artifact_id = new_artifact_id()
        now = iso_now()
        target = s.config.artifacts_dir / f"{artifact_id}.bin"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)  # 写盘完成后才可能返回成功
        with db.write() as conn:
            conn.execute(
                "INSERT INTO artifacts (artifact_id, upload_id, kind, original_name, content_type, sha256, size_bytes, media_info, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    upload_id,
                    "RESULT" if content_type == "application/json" else "INPUT",
                    original_name,
                    content_type,
                    sha,
                    size,
                    canonical_json(media),
                    now,
                ),
            )
        row = db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        return JSONResponse(status_code=201, content=s.artifact_dict(row))

    @app.get("/api/resource/v1/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str, request: Request):
        s = state()
        require_demo_or_task(request, s.config)
        row = s.db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise not_found(f"artifact {artifact_id} 不存在")
        return s.artifact_dict(row)

    @app.get("/api/resource/v1/artifacts/{artifact_id}/content")
    def download_artifact(artifact_id: str, request: Request):
        s = state()
        require_demo_or_task(request, s.config)
        row = s.db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise not_found(f"artifact {artifact_id} 不存在")
        path = s.config.artifacts_dir / f"{artifact_id}.bin"
        if not path.exists():
            raise not_found(f"artifact {artifact_id} 文件缺失")
        return FileResponse(path, media_type=row["content_type"], filename=row["original_name"])

    # ---------------- preparations ----------------

    @app.post("/api/resource/v1/preparations")
    def create_preparation(body: PreparationRequest, request: Request):
        s = state()
        require_task(request, s.config)
        preparation_id = as_uuid(body.preparation_id)
        fingerprint = canonical_json(body.model_dump())

        existing = s.db.query_one(
            "SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,)
        )
        if existing is not None:
            if existing["status"] == "REJECTED":
                error = parse_json(existing["error"]) or {}
                raise ApiError(
                    409,
                    error.get("code", "INTERNAL_ERROR"),
                    error.get("message", "先前已拒绝"),
                    retryable=bool(error.get("retryable", False)),
                    details=ensure_details_keys({"preparation_id": preparation_id}),
                )
            return s.preparation_dict(existing)

        node = _get_node_or_404(s, body.worker_id)
        now_dt = utcnow()
        now = iso(now_dt)
        reject = _check_node_for_work(s, node, body.worker_session_id)
        if reject is None:
            type_desc = s.catalog.get(body.task_type)
            if type_desc is None or s.catalog.find_profile(body.profile_id, body.profile_digest) is None:
                reject = (
                    503,
                    "PROFILE_UNCONFIGURED",
                    "profile 未配置或摘要不匹配",
                    False,
                )
        if reject is not None:
            status_code, code, message, retryable = reject
            error = {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": {"preparation_id": preparation_id},
            }
            with s.db.write() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO preparations (preparation_id, worker_id, worker_session_id, task_type, profile_id, profile_digest, status, created_at, finished_at, progress, error)"
                    " VALUES (?,?,?,?,?,?, 'REJECTED', ?, ?, NULL, ?)",
                    (
                        preparation_id,
                        body.worker_id,
                        body.worker_session_id,
                        body.task_type,
                        body.profile_id,
                        body.profile_digest,
                        now,
                        now,
                        canonical_json(error),
                    ),
                )
            s.db.insert_event(
                now, "profile.failed", f"准备被拒绝：{code}", worker_id=body.worker_id, preparation_id=preparation_id
            )
            raise ApiError(status_code, code, message, retryable, {"preparation_id": preparation_id})

        with s.db.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO preparations (preparation_id, worker_id, worker_session_id, task_type, profile_id, profile_digest, status, created_at, finished_at, progress, error)"
                " VALUES (?,?,?,?,?,?, 'ACCEPTED', ?, NULL, NULL, NULL)",
                (
                    preparation_id,
                    body.worker_id,
                    body.worker_session_id,
                    body.task_type,
                    body.profile_id,
                    body.profile_digest,
                    now,
                ),
            )
            _upsert_capability(
                conn,
                body.worker_id,
                {
                    "task_type": body.task_type,
                    "profile_id": body.profile_id,
                    "profile_digest": body.profile_digest,
                    "state": "PREPARING",
                    "model_cached": False,
                    "backend": None,
                    "runtime_provider": None,
                    "model_sha256": None,
                    "verified": False,
                    "calibration": None,
                    "reason": None,
                },
            )
        s.db.insert_event(
            now, "profile.preparing", f"节点 {body.worker_id} 开始准备 {body.profile_id}",
            worker_id=body.worker_id, preparation_id=preparation_id,
        )
        row = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,))
        return JSONResponse(status_code=202, content=s.preparation_dict(row))

    @app.get("/api/resource/v1/preparations/{preparation_id}")
    def get_preparation(preparation_id: str, request: Request):
        s = state()
        require_task(request, s.config)
        try:
            pid = as_uuid(preparation_id)
        except ValueError:
            raise not_found("preparation 不存在")
        row = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (pid,))
        if row is None:
            raise not_found("preparation 不存在")
        return s.preparation_dict(row)

    # ---------------- executions ----------------

    @app.post("/api/resource/v1/executions")
    def create_execution(body: ExecutionRequest, request: Request):
        s = state()
        require_task(request, s.config)
        execution_id = as_uuid(body.execution_id)
        fingerprint = canonical_json(body.model_dump())

        existing = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同 execution_id 不同载荷")
            if existing["status"] == "REJECTED":
                error = parse_json(existing["error"]) or {}
                raise ApiError(
                    409,
                    error.get("code", "INTERNAL_ERROR"),
                    error.get("message", "先前已拒绝"),
                    retryable=bool(error.get("retryable", False)),
                    details=ensure_details_keys(
                        error.get("details") or {"execution_id": execution_id}
                    ),
                )
            return s.execution_dict(existing)

        node = _get_node_or_404(s, body.worker_id)
        now_dt = utcnow()
        now = iso(now_dt)
        reject = _check_node_for_work(s, node, body.worker_session_id)
        if reject is None:
            reject = _check_execution_business(s, body)
        if reject is not None:
            status_code, code, message, retryable = reject
            error = {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": {"execution_id": execution_id},
            }
            with s.db.write() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO executions (execution_id, request_fingerprint, request, job_id, task_id, attempt_no,"
                    " worker_id, worker_session_id, task_type, profile_id, profile_digest, input_artifact_id, parameters,"
                    " timeout_seconds, status, input_sha256, created_at, accepted_at, started_at, finished_at,"
                    " lease_expires_at, hard_deadline_at, result_artifact_id, timings, error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'REJECTED', NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)",
                    (
                        execution_id,
                        fingerprint,
                        canonical_json(body.model_dump()),
                        body.job_id,
                        body.task_id,
                        body.attempt_no,
                        body.worker_id,
                        body.worker_session_id,
                        body.task_type,
                        body.profile_id,
                        body.profile_digest,
                        body.input_artifact_id,
                        canonical_json(body.parameters.model_dump()),
                        body.timeout_seconds,
                        now,
                        canonical_json(error),
                    ),
                )
            s.db.insert_event(
                now, "execution.rejected", f"执行被拒绝：{code}", worker_id=body.worker_id, execution_id=execution_id
            )
            raise ApiError(
                status_code, code, message, retryable, {"execution_id": execution_id}
            )

        accepted_at = now_dt
        lease_expires = plus(accepted_at, LEASE_SECONDS)
        hard_deadline = plus(accepted_at, body.timeout_seconds)
        input_row = s.db.query_one(
            "SELECT sha256 FROM artifacts WHERE artifact_id=?", (body.input_artifact_id,)
        )
        with s.db.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO executions (execution_id, request_fingerprint, request, job_id, task_id, attempt_no,"
                " worker_id, worker_session_id, task_type, profile_id, profile_digest, input_artifact_id, parameters,"
                " timeout_seconds, status, input_sha256, created_at, accepted_at, started_at, finished_at,"
                " lease_expires_at, hard_deadline_at, result_artifact_id, timings, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'ACCEPTED', ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL)",
                (
                    execution_id,
                    fingerprint,
                    canonical_json(body.model_dump()),
                    body.job_id,
                    body.task_id,
                    body.attempt_no,
                    body.worker_id,
                    body.worker_session_id,
                    body.task_type,
                    body.profile_id,
                    body.profile_digest,
                    body.input_artifact_id,
                    canonical_json(body.parameters.model_dump()),
                    body.timeout_seconds,
                    input_row["sha256"] if input_row else None,
                    iso(accepted_at),
                    iso(accepted_at),
                    iso(lease_expires),
                    iso(hard_deadline),
                ),
            )
        s.db.insert_event(
            now, "execution.accepted", f"执行已接受：{body.task_type} on {body.worker_id}",
            worker_id=body.worker_id, execution_id=execution_id,
        )
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        return JSONResponse(status_code=201, content=s.execution_dict(row))

    @app.get("/api/resource/v1/executions/{execution_id}")
    def get_execution(execution_id: str, request: Request):
        s = state()
        require_task(request, s.config)
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if row is None:
            raise not_found("execution 不存在")
        return s.execution_dict(row)

    @app.post("/api/resource/v1/executions/{execution_id}/cancel")
    def cancel_execution(execution_id: str, request: Request):
        s = state()
        require_task(request, s.config)
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if row is None:
            raise not_found("execution 不存在")
        if row["status"] in TERMINAL_EXEC_STATUSES:
            return s.execution_dict(row)
        now = iso_now()
        if row["status"] in ("ACCEPTED", "RUNNING", "CANCEL_REQUESTED"):
            with s.db.write() as conn:
                if row["status"] != "CANCEL_REQUESTED":
                    conn.execute(
                        "UPDATE executions SET status='CANCEL_REQUESTED' WHERE execution_id=?",
                        (execution_id,),
                    )
            if row["status"] != "CANCEL_REQUESTED":
                s.db.insert_event(
                    now, "execution.cancel_requested", f"请求取消执行 {execution_id}",
                    worker_id=row["worker_id"], execution_id=execution_id,
                )
            fresh = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
            return JSONResponse(status_code=202, content=s.execution_dict(fresh))
        # 终态原样返回
        return s.execution_dict(row)

    # ---------------- events ----------------

    @app.get("/api/resource/v1/events")
    def get_events(request: Request, after: int = 0, limit: int = 100):
        s = state()
        require_demo_or_task(request, s.config)
        if after < 0 or not (1 <= limit <= 100):
            raise ApiError(422, "INVALID_REQUEST", "after/limit 超出范围")
        rows = s.db.query(
            "SELECT * FROM events WHERE event_id>? ORDER BY event_id LIMIT ?",
            (after, limit + 1),
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [_event_dict(r) for r in rows]
        next_after = rows[-1]["event_id"] if rows else after
        return {"events": events, "next_after": next_after, "has_more": has_more}

    # ---------------- 节点内部协议（P1 私有，不经公共契约）----------------

    @app.get("/api/resource/internal/v1/nodes/{worker_id}/artifacts/{artifact_id}/content")
    def node_download_artifact(worker_id: str, artifact_id: str, request: Request):
        """节点程序用会话凭证（Bearer session_id）下载输入文件。"""
        s = state()
        node = _get_node_or_404(s, worker_id)
        session = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        _require_session(node, session)
        row = s.db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise not_found(f"artifact {artifact_id} 不存在")
        path = s.config.artifacts_dir / f"{artifact_id}.bin"
        if not path.exists():
            raise not_found(f"artifact {artifact_id} 文件缺失")
        return FileResponse(path, media_type=row["content_type"])

    @app.post("/api/resource/internal/v1/nodes/register")
    def node_register(body: RegisterRequest, request: Request):
        s = state()
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {s.config.join_token}":
            raise unauthorized()
        now = iso_now()
        worker_id = body.worker_id or ("w" + uuid.uuid4().hex[:16])
        worker_id = _sanitize_worker_id(worker_id)
        session_id = str(uuid.uuid4())
        with s.db.write() as conn:
            existing = conn.execute(
                "SELECT worker_id FROM nodes WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE nodes SET session_id=?, name=?, hardware=?, capabilities=?, connection='ONLINE',"
                    " cleanup_pending=0, last_seen_at=?, telemetry=NULL WHERE worker_id=?",
                    (
                        session_id,
                        body.name,
                        canonical_json(body.hardware.model_dump()),
                        canonical_json([c.model_dump() for c in body.capabilities]),
                        now,
                        worker_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO nodes (worker_id, session_id, name, mode, connection, hardware, capabilities,"
                    " cleanup_pending, last_seen_at, registered_at, telemetry)"
                    " VALUES (?,?,?, 'ACTIVE', 'ONLINE', ?, ?, 0, ?, ?, NULL)",
                    (
                        worker_id,
                        session_id,
                        body.name,
                        canonical_json(body.hardware.model_dump()),
                        canonical_json([c.model_dump() for c in body.capabilities]),
                        now,
                        now,
                    ),
                )
        s.db.insert_event(now, "node.registered", f"节点 {body.name} 注册 ({worker_id})", worker_id=worker_id)
        return {
            "worker_id": worker_id,
            "session_id": session_id,
            "heartbeat_interval_s": 3,
            "lease_interval_s": LEASE_SECONDS,
        }

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/heartbeat")
    def node_heartbeat(worker_id: str, body: HeartbeatRequest):
        s = state()
        node = _get_node_or_404(s, worker_id)
        _require_session(node, body.session_id)
        now = iso_now()
        with s.db.write() as conn:
            conn.execute(
                "UPDATE nodes SET last_seen_at=?, connection='ONLINE', telemetry=?, capabilities=? WHERE worker_id=?",
                (
                    now,
                    canonical_json(body.telemetry.model_dump()) if body.telemetry else None,
                    canonical_json([c.model_dump() for c in body.capabilities]),
                    worker_id,
                ),
            )
            # 续租：本节点 RUNNING 执行的心跳续租（不能越过硬期限，扫描器兜底）
            conn.execute(
                "UPDATE executions SET lease_expires_at=? WHERE worker_id=? AND status='RUNNING'",
                (iso(plus(utcnow(), LEASE_SECONDS)), worker_id),
            )
        cancelled = [
            r["execution_id"]
            for r in s.db.query(
                "SELECT execution_id FROM executions WHERE worker_id=? AND status='CANCEL_REQUESTED'",
                (worker_id,),
            )
        ]
        node = _get_node_or_404(s, worker_id)
        return {"ok": True, "mode": node["mode"], "cancel_requested": cancelled}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/claim")
    def node_claim(worker_id: str, body: ClaimRequest):
        s = state()
        node = _get_node_or_404(s, worker_id)
        _require_session(node, body.session_id)
        if node["connection"] != "ONLINE" or node["mode"] != "ACTIVE":
            return {"execution": None, "preparation": None}

        # 模型准备优先（准备与执行同槽）
        prep = s.db.query_one(
            "SELECT * FROM preparations WHERE worker_id=? AND status='ACCEPTED' ORDER BY created_at LIMIT 1",
            (worker_id,),
        )
        if prep is not None:
            return {"execution": None, "preparation": s.preparation_dict(prep)}

        row = s.db.query_one(
            "SELECT * FROM executions WHERE worker_id=? AND status='ACCEPTED' AND lease_expires_at>? ORDER BY accepted_at LIMIT 1",
            (worker_id, iso_now()),
        )
        if row is None:
            return {"execution": None, "preparation": None}
        now = iso_now()
        with s.db.write() as conn:
            conn.execute(
                "UPDATE executions SET status='RUNNING', started_at=?, lease_expires_at=? WHERE execution_id=? AND status='ACCEPTED'",
                (now, iso(plus(utcnow(), LEASE_SECONDS)), row["execution_id"]),
            )
        s.db.insert_event(
            now, "execution.running", f"节点 {worker_id} 领取执行 {row['execution_id']}",
            worker_id=worker_id, execution_id=row["execution_id"],
        )
        artifact = s.db.query_one(
            "SELECT * FROM artifacts WHERE artifact_id=?", (row["input_artifact_id"],)
        )
        exec_payload = {
            "execution_id": row["execution_id"],
            "job_id": row["job_id"],
            "task_id": row["task_id"],
            "attempt_no": row["attempt_no"],
            "task_type": row["task_type"],
            "profile_id": row["profile_id"],
            "profile_digest": row["profile_digest"],
            "input_artifact_id": row["input_artifact_id"],
            "parameters": parse_json(row["parameters"]),
            "timeout_seconds": row["timeout_seconds"],
            "hard_deadline_at": row["hard_deadline_at"],
            "input_download_path": (
                f"/api/resource/v1/artifacts/{row['input_artifact_id']}/content" if artifact else None
            ),
        }
        return {"execution": exec_payload, "preparation": None}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/preparations/{preparation_id}/status")
    def preparation_status(worker_id: str, preparation_id: str, body: PreparationStatusRequest):
        s = state()
        node = _get_node_or_404(s, worker_id)
        _require_session(node, body.session_id)
        row = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,))
        if row is None or row["worker_id"] != worker_id:
            raise not_found("preparation 不存在")
        if row["status"] not in ACTIVE_PREP_STATUSES:
            raise ApiError(409, "STALE_EXECUTION", "准备已结束，状态更新被拒收")
        now = iso_now()
        error_json = None
        if body.status == "FAILED":
            error_json = canonical_json(
                {
                    "code": body.error_code or "MODEL_LOAD_FAILED",
                    "message": body.error_message or "模型准备失败",
                    "retryable": bool(body.error_retryable),
                    "details": {"preparation_id": preparation_id},
                }
            )
        finished = now if body.status in ("READY", "FAILED") else None
        with s.db.write() as conn:
            conn.execute(
                "UPDATE preparations SET status=?, progress=?, finished_at=?, error=? WHERE preparation_id=?",
                (body.status, body.progress, finished, error_json, preparation_id),
            )
            if body.status == "READY":
                _upsert_capability(
                    conn,
                    worker_id,
                    {
                        "task_type": row["task_type"],
                        "profile_id": row["profile_id"],
                        "profile_digest": row["profile_digest"],
                        "state": "READY",
                        "model_cached": body.model_cached if body.model_cached is not None else True,
                        "backend": body.backend,
                        "runtime_provider": body.runtime_provider,
                        "model_sha256": body.model_sha256,
                        "verified": True,
                        "calibration": body.calibration.model_dump() if body.calibration else None,
                        "reason": None,
                    },
                )
            elif body.status == "FAILED":
                _upsert_capability(
                    conn,
                    worker_id,
                    {
                        "task_type": row["task_type"],
                        "profile_id": row["profile_id"],
                        "profile_digest": row["profile_digest"],
                        "state": "ERROR",
                        "model_cached": False,
                        "backend": None,
                        "runtime_provider": None,
                        "model_sha256": None,
                        "verified": False,
                        "calibration": None,
                        "reason": body.error_message or "模型准备失败",
                    },
                )
        if body.status == "READY":
            s.db.insert_event(
                now, "profile.ready", f"节点 {worker_id} 模型就绪 {row['profile_id']}",
                worker_id=worker_id, preparation_id=preparation_id,
            )
        elif body.status == "FAILED":
            s.db.insert_event(
                now, "profile.failed", f"节点 {worker_id} 模型准备失败：{body.error_message}",
                worker_id=worker_id, preparation_id=preparation_id,
            )
        fresh = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,))
        return s.preparation_dict(fresh)

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/executions/{execution_id}/complete")
    def execution_complete(worker_id: str, execution_id: str, body: ExecutionCompleteRequest):
        s = state()
        node = _get_node_or_404(s, worker_id)
        _require_session(node, body.session_id)
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if row is None or row["worker_id"] != worker_id:
            raise not_found("execution 不存在")
        now_dt = utcnow()
        now = iso(now_dt)

        if row["status"] in TERMINAL_EXEC_STATUSES:
            s.db.insert_event(
                now, "execution.stale_rejected", f"晚到/重复提交被拒收 {execution_id}",
                worker_id=worker_id, execution_id=execution_id,
            )
            raise ApiError(409, "STALE_EXECUTION", "执行已终态，提交被拒收")
        if row["status"] == "CANCEL_REQUESTED":
            # 取消先撤权：拒收之后的结果，节点应转而确认停止
            raise ApiError(409, "STALE_EXECUTION", "执行已请求取消，提交权已撤销")

        if body.outcome == "SUCCEEDED":
            if body.result is None:
                raise ApiError(409, "RESULT_INVALID", "SUCCEEDED 提交缺少 result")
            try:
                _validate_execution_result(s, row, body.result)
            except ResultInvalid as e:
                raise ApiError(409, "RESULT_INVALID", f"结果不合规：{e.message}")
            result_bytes = canonical_json(body.result).encode("utf-8")
            sha = hashlib.sha256(result_bytes).hexdigest()
            artifact_id = new_artifact_id()
            target = s.config.artifacts_dir / f"{artifact_id}.bin"
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(result_bytes)
            tmp.replace(target)
            timings = body.timings if body.timings is not None else {
                "download_ms": 0, "inference_ms": 0, "postprocess_ms": 0,
                "write_output_ms": 0, "total_worker_ms": 0,
            }
            with s.db.write() as conn:
                conn.execute(
                    "INSERT INTO artifacts (artifact_id, upload_id, kind, original_name, content_type, sha256, size_bytes, media_info, created_at)"
                    " VALUES (?,?, 'RESULT', ?, 'application/json', ?, ?, ?, ?)",
                    (
                        artifact_id,
                        None,
                        f"execution-{execution_id}.json",
                        sha,
                        len(result_bytes),
                        "null",
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE executions SET status='SUCCEEDED', finished_at=?, result_artifact_id=?, timings=?, error=NULL WHERE execution_id=?",
                    (now, artifact_id, canonical_json(timings), execution_id),
                )
            s.db.insert_event(
                now, "execution.succeeded", f"执行成功 {execution_id}",
                worker_id=worker_id, execution_id=execution_id,
            )
        else:
            error = {
                "code": body.error_code or "EXECUTION_ERROR",
                "message": body.error_message or "执行失败",
                "retryable": bool(body.error_retryable),
                "details": {"execution_id": execution_id},
            }
            with s.db.write() as conn:
                conn.execute(
                    "UPDATE executions SET status='FAILED', finished_at=?, error=? WHERE execution_id=?",
                    (now, canonical_json(error), execution_id),
                )
            s.db.insert_event(
                now, "execution.failed", f"执行失败 {execution_id}: {error['code']}",
                worker_id=worker_id, execution_id=execution_id,
            )
        fresh = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        return s.execution_dict(fresh)

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/executions/{execution_id}/cancel_ack")
    def execution_cancel_ack(worker_id: str, execution_id: str, body: CancelAckRequest):
        s = state()
        node = _get_node_or_404(s, worker_id)
        _require_session(node, body.session_id)
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if row is None or row["worker_id"] != worker_id:
            raise not_found("execution 不存在")
        if row["status"] == "CANCEL_REQUESTED":
            with s.db.write() as conn:
                conn.execute(
                    "UPDATE executions SET status='CANCELLED', finished_at=? WHERE execution_id=?",
                    (iso_now(), execution_id),
                )
                conn.execute(
                    "UPDATE nodes SET cleanup_pending=0 WHERE worker_id=?", (worker_id,)
                )
            s.db.insert_event(
                iso_now(), "execution.cancelled", f"执行已取消 {execution_id}",
                worker_id=worker_id, execution_id=execution_id,
            )
        fresh = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        return s.execution_dict(fresh)

    # ---------------- 后台扫描 ----------------

    async def _scan_loop(st: State):
        while True:
            try:
                await asyncio.to_thread(run_scan, st)
            except Exception:
                import traceback

                traceback.print_exc()
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def run_scan(st: State) -> None:
        now_dt = utcnow()
        now = iso(now_dt)
        db = st.db
        # 1) 硬期限（先于租约判断）
        for row in db.query(
            "SELECT * FROM executions WHERE status IN ('ACCEPTED','RUNNING') AND hard_deadline_at IS NOT NULL AND hard_deadline_at<?",
            (now,),
        ):
            with db.write() as conn:
                conn.execute(
                    "UPDATE executions SET status='FAILED', finished_at=?, error=? WHERE execution_id=? AND status IN ('ACCEPTED','RUNNING')",
                    (
                        now,
                        canonical_json(_err("EXECUTION_TIMEOUT", "执行超过硬期限", True)),
                        row["execution_id"],
                    ),
                )
                if row["status"] == "RUNNING":
                    conn.execute(
                        "UPDATE nodes SET cleanup_pending=1 WHERE worker_id=?", (row["worker_id"],)
                    )
            db.insert_event(
                now, "execution.failed", f"执行硬超时 {row['execution_id']}",
                worker_id=row["worker_id"], execution_id=row["execution_id"],
            )
        # 2) 租约过期
        for row in db.query(
            "SELECT * FROM executions WHERE status='ACCEPTED' AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
            (now,),
        ):
            with db.write() as conn:
                conn.execute(
                    "UPDATE executions SET status='FAILED', finished_at=?, error=? WHERE execution_id=? AND status='ACCEPTED'",
                    (now, canonical_json(_err("LEASE_EXPIRED", "接受后未在租约内领取", True)), row["execution_id"]),
                )
            db.insert_event(
                now, "execution.failed", f"租约过期未领取 {row['execution_id']}",
                worker_id=row["worker_id"], execution_id=row["execution_id"],
            )
        for row in db.query(
            "SELECT * FROM executions WHERE status='RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
            (now,),
        ):
            with db.write() as conn:
                conn.execute(
                    "UPDATE executions SET status='FAILED', finished_at=?, error=? WHERE execution_id=? AND status='RUNNING'",
                    (now, canonical_json(_err("LEASE_EXPIRED", "执行期间心跳续租中断", True)), row["execution_id"]),
                )
                conn.execute(
                    "UPDATE nodes SET cleanup_pending=1 WHERE worker_id=?", (row["worker_id"],)
                )
            db.insert_event(
                now, "execution.failed", f"执行租约过期 {row['execution_id']}",
                worker_id=row["worker_id"], execution_id=row["execution_id"],
            )
        # 3) 节点失联（阈值 15s）
        for row in db.query(
            "SELECT * FROM nodes WHERE connection='ONLINE' AND last_seen_at IS NOT NULL AND last_seen_at<?",
            (iso(plus(now_dt, -OFFLINE_AFTER_SECONDS)),),
        ):
            with db.write() as conn:
                conn.execute(
                    "UPDATE nodes SET connection='OFFLINE', session_id='', cleanup_pending=CASE WHEN EXISTS("
                    "SELECT 1 FROM executions WHERE worker_id=? AND status IN ('ACCEPTED','RUNNING','CANCEL_REQUESTED')"
                    ") THEN 1 ELSE cleanup_pending END WHERE worker_id=?",
                    (row["worker_id"], row["worker_id"]),
                )
                for exec_row in conn.execute(
                    "SELECT execution_id FROM executions WHERE worker_id=? AND status IN ('ACCEPTED','RUNNING')",
                    (row["worker_id"],),
                ).fetchall():
                    conn.execute(
                        "UPDATE executions SET status='FAILED', finished_at=?, error=? WHERE execution_id=? AND status IN ('ACCEPTED','RUNNING')",
                        (
                            now,
                            canonical_json(_err("LEASE_EXPIRED", "节点失联", True)),
                            exec_row["execution_id"],
                        ),
                    )
                for prep_row in conn.execute(
                    "SELECT preparation_id FROM preparations WHERE worker_id=? AND status IN (?,?,?,?)",
                    ACTIVE_PREP_STATUSES,
                ).fetchall():
                    conn.execute(
                        "UPDATE preparations SET status='FAILED', finished_at=?, error=? WHERE preparation_id=?",
                        (
                            now,
                            canonical_json(_err("MODEL_LOAD_FAILED", "节点失联，准备中断", True)),
                            prep_row["preparation_id"],
                        ),
                    )
            db.insert_event(now, "node.offline", f"节点失联 {row['worker_id']}", worker_id=row["worker_id"])
        # 4) 准备硬期限 900s
        for row in db.query(
            "SELECT * FROM preparations WHERE status IN (?,?,?,?) AND created_at<?",
            (*ACTIVE_PREP_STATUSES, iso(plus(now_dt, -PREPARATION_TIMEOUT_SECONDS))),
        ):
            with db.write() as conn:
                conn.execute(
                    "UPDATE preparations SET status='FAILED', finished_at=?, error=? WHERE preparation_id=?",
                    (
                        now,
                        canonical_json(_err("PREPARATION_TIMEOUT", "准备超过 900 秒硬期限", True)),
                        row["preparation_id"],
                    ),
                )
            db.insert_event(
                now, "profile.failed", f"准备超时 {row['preparation_id']}",
                worker_id=row["worker_id"], preparation_id=row["preparation_id"],
            )
        # 5) DRAINING 且无活动工作 -> PAUSED
        for row in db.query("SELECT * FROM nodes WHERE mode='DRAINING'"):
            active_exec = st.node_active_executions(row["worker_id"])
            active_prep = st.node_active_preparation(row["worker_id"])
            if not active_exec and not active_prep and not row["cleanup_pending"]:
                with db.write() as conn:
                    conn.execute(
                        "UPDATE nodes SET mode='PAUSED' WHERE worker_id=?", (row["worker_id"],)
                    )
                db.insert_event(
                    now, "node.mode_changed", f"节点 {row['worker_id']} 完成工作，DRAINING -> PAUSED",
                    worker_id=row["worker_id"],
                )

    # ---------------- helpers ----------------

    def request_state_role(request: Request, config: Config) -> str:
        header = request.headers.get("Authorization", "")
        if header == f"Bearer {config.demo_token}":
            return ROLE_DEMO
        return "task"

    def _err(code: str, message: str, retryable: bool) -> dict:
        return {"code": code, "message": message, "retryable": retryable, "details": {}}

    def _sanitize_worker_id(raw: str) -> str:
        cleaned = "".join(c for c in raw if c.isalnum() or c in "_-")
        return cleaned[:80] if cleaned else "w" + secrets.token_hex(8)

    def _check_node_for_work(s: State, node_row, worker_session_id: str):
        """返回 (status_code, code, message, retryable) 或 None。"""
        if node_row["connection"] != "ONLINE":
            return (409, "NODE_OFFLINE", "节点离线", True)
        if node_row["session_id"] != worker_session_id:
            return (409, "SESSION_CHANGED", "节点会话已变化，请刷新资源快照", True)
        if node_row["mode"] != "ACTIVE":
            return (409, "NODE_UNAVAILABLE", "节点不在 ACTIVE 模式", True)
        if node_row["cleanup_pending"]:
            return (409, "RESOURCE_BUSY", "节点存在未确认的清理，暂不可接单", True)
        active_exec = s.node_active_executions(node_row["worker_id"])
        active_prep = s.node_active_preparation(node_row["worker_id"])
        if active_exec or active_prep:
            return (409, "RESOURCE_BUSY", "节点槽位被占用", True)
        return None

    def _check_execution_business(s: State, body: ExecutionRequest):
        type_desc = s.catalog.get(body.task_type)
        if type_desc is None:
            return (409, "UNSUPPORTED_TASK_TYPE", f"类型 {body.task_type} 不受支持", False)
        if body.parameters.language not in type_desc["supported_languages"]:
            return (409, "PARAMETER_UNSUPPORTED", f"语言 {body.parameters.language} 不受支持", False)
        if s.catalog.find_profile(body.profile_id, body.profile_digest) is None:
            return (409, "PROFILE_MISMATCH", "profile 或摘要与目录不符", False)
        artifact = s.db.query_one(
            "SELECT * FROM artifacts WHERE artifact_id=?", (body.input_artifact_id,)
        )
        if artifact is None:
            return (409, "NOT_FOUND", f"输入 artifact {body.input_artifact_id} 不存在", True)
        if artifact["content_type"] not in type_desc["input_mime_types"]:
            return (409, "INPUT_INVALID", f"输入类型 {artifact['content_type']} 与任务不符", False)
        # 能力就绪检查
        node = s.db.query_one("SELECT * FROM nodes WHERE worker_id=?", (body.worker_id,))
        capabilities = parse_json(node["capabilities"], [])
        ready = any(
            c.get("task_type") == body.task_type
            and c.get("profile_id") == body.profile_id
            and c.get("profile_digest") == body.profile_digest
            and c.get("state") == "READY"
            for c in capabilities
        )
        if not ready:
            return (409, "CAPABILITY_NOT_READY", "节点能力未就绪或 profile 不匹配", True)
        return None

    def _probe_or_422(content_type: str, data: bytes, max_pixels: Optional[int]) -> dict:
        try:
            media = probe_media(content_type, data)
        except ValueError as e:
            raise ApiError(422, "INVALID_REQUEST", f"内容解码失败：{e}")
        if (
            content_type == "audio/wav"
            and media["duration_ms"] is not None
            and media["duration_ms"] > WAV_MAX_DURATION_MS
        ):
            raise ApiError(413, "INPUT_TOO_LARGE", "WAV 时长超过 60 秒上限")
        if (
            max_pixels is not None
            and media["width"] is not None
            and media["height"] is not None
            and media["width"] * media["height"] > max_pixels
        ):
            raise ApiError(413, "INPUT_TOO_LARGE", "图像像素超过上限")
        return media

    def _validate_execution_result(s: State, row, result: dict) -> None:
        if row["task_type"] == "ocr.image.v1":
            validate_ocr_result(result, row["input_artifact_id"])
        else:
            validate_asr_result(result, row["input_artifact_id"])

    def _upsert_capability(conn, worker_id: str, capability: dict) -> None:
        row = conn.execute(
            "SELECT capabilities FROM nodes WHERE worker_id=?", (worker_id,)
        ).fetchone()
        caps = parse_json(row["capabilities"], []) if row else []
        caps = [c for c in caps if c.get("task_type") != capability["task_type"]]
        caps.append(capability)
        conn.execute(
            "UPDATE nodes SET capabilities=? WHERE worker_id=?",
            (canonical_json(caps), worker_id),
        )

    return app
