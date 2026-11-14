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
from typing import Optional

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from . import prefetch, slots
from .auth import ROLE_DEMO, require_demo_or_task, require_join, require_task
from .catalog import Catalog
from .config import CONTRACT_VERSION, Config
from .db import Database, parse_json
from .errors import (
    ApiError,
    ensure_details_keys,
    error_response,
    forbidden,
    internal_error,
    not_found,
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
    SharingRequest,
    SlotReadyRequest,
    as_uuid,
)
from .resultschema import ResultInvalid, validate_asr_result, validate_ocr_result
from .timeutil import canonical_json, iso, iso_now, plus, utcnow

LEASE_SECONDS = 15.0
OFFLINE_AFTER_SECONDS = 15.0
SCAN_INTERVAL_SECONDS = 1.0
PREPARATION_TIMEOUT_SECONDS = 900.0

# MediaInfo 必须是含六字段的对象；JSON 结果无媒体属性，字段值为 null（F01）
_NULL_MEDIA = {
    "width": None,
    "height": None,
    "duration_ms": None,
    "sample_rate_hz": None,
    "channels": None,
    "audio_encoding": None,
}

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
        self.catalog = Catalog(config.profiles_dir)
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
            # F09：旧会话全部撤销——用新的随机 UUID/secret 覆盖（保持公开 schema 合法），
            # 旧 agent 的凭证因此失效，心跳 409 后自动重注册
            for node_row in conn.execute("SELECT worker_id FROM nodes").fetchall():
                conn.execute(
                    "UPDATE nodes SET connection='OFFLINE', session_id=?, node_secret=?, last_seen_at=NULL,"
                    " cleanup_pending=1 WHERE worker_id=?",
                    (
                        str(uuid.uuid4()),
                        secrets.token_hex(16),
                        node_row["worker_id"],
                    ),
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
            "slot_id": row["slot_id"],
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
            "SELECT execution_id, slot_id FROM executions WHERE worker_id=? AND status IN (?,?,?) ORDER BY created_at",
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
        maximum = row["max_slots"]
        reserved = maximum if prep else len(active)
        cold = not any(c.get("state") == "READY" for c in parse_json(row["capabilities"], []))
        capacity = (maximum if cold else len(slots.free(self.db, row["worker_id"]))) if maximum > 1 else 1
        available = capacity if (
            row["connection"] == "ONLINE"
            and row["mode"] == "ACTIVE"
            and row["sharing_enabled"]
            and not row["cleanup_pending"]
            and not prep
            and (maximum > 1 or reserved == 0)
        ) else 0
        last_seen = row["last_seen_at"]
        if row["connection"] != "ONLINE":
            last_seen = None
        telemetry = parse_json(row["telemetry"])
        hint = self.db.query_one("SELECT revision FROM input_prefetches WHERE worker_id=?", (row["worker_id"],))
        return {
            "prefetch_capacity": row["prefetch_capacity"],
            "prefetch_revision": hint["revision"] if hint else 0,
            "worker_id": row["worker_id"],
            "session_id": row["session_id"],
            "name": row["name"],
            "connection": row["connection"],
            "mode": row["mode"],
            "hardware": parse_json(row["hardware"]),
            "capabilities": parse_json(row["capabilities"], []),
            "max_slots": maximum,
            "threads_per_slot": row["threads_per_slot"],
            "cpu_thread_budget": row["cpu_thread_budget"],
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


def _require_node_auth(node_row, session_id: str, node_secret: str) -> None:
    """内部工作接口鉴权：公开会话标识 + 节点秘密凭证双因子（F07）。"""
    if (
        node_row["session_id"] != session_id
        or not node_secret
        or node_row["node_secret"] != node_secret
    ):
        raise ApiError(409, "SESSION_CHANGED", "节点会话或凭证已失效，需重新注册", retryable=True)


def _handle_replay(
    existing_fingerprint: str,
    incoming_fingerprint: str,
    status: str,
    error_raw: Optional[str],
    id_field: str,
    id_value: str,
    snapshot_fn,
) -> dict:
    """幂等重放三规则：不同载荷 409；REJECTED 重放仍 409 原拒绝；其余返回当前快照。"""
    if existing_fingerprint != incoming_fingerprint:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同幂等 ID 不同载荷")
    if status == "REJECTED":
        error = parse_json(error_raw) or {}
        raise ApiError(
            409,
            error.get("code", "INTERNAL_ERROR"),
            error.get("message", "先前已拒绝"),
            retryable=bool(error.get("retryable", False)),
            details=ensure_details_keys(error.get("details") or {id_field: id_value}),
        )
    return snapshot_fn()


def create_app(config: Optional[Config] = None) -> FastAPI:
    cfg = config or Config(
        bind_host="0.0.0.0",  # 测试工厂默认值；生产路径走 load_config
        port=8001,
        data_dir=Path("./data/resource-test").resolve(),
        demo_token="demo-token",
        task_token="task-token",
        join_token="join-token",
        profiles_dir=Path("./profiles").resolve(),
    )
    state_holder: dict[str, State] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg.profiles_dir.mkdir(parents=True, exist_ok=True)
        state = State(cfg)
        state_holder["state"] = state
        stop_scanner = asyncio.Event()
        scanner = asyncio.create_task(_scan_loop(state, stop_scanner))
        try:
            yield
        finally:
            stop_scanner.set()
            await scanner
            state.db.close()

    app = FastAPI(title="P1 资源服务", version=CONTRACT_VERSION, lifespan=lifespan)

    def state() -> State:
        s = state_holder.get("state")
        if s is None:
            raise internal_error("服务尚未完成初始化")
        return s

    # 鉴权用 FastAPI 依赖：依赖先于 body 校验执行，保证角色错误一律 403（R03）
    def auth_demo_or_task(request: Request) -> str:
        return require_demo_or_task(request, state().config)

    def auth_task(request: Request) -> str:
        return require_task(request, state().config)

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
    def get_resources(role: str = Depends(auth_demo_or_task)):
        s = state()
        rows = s.db.query("SELECT * FROM nodes ORDER BY registered_at")
        return {
            "contract_version": CONTRACT_VERSION,
            "server_time": iso_now(),
            "resources": [s.node_dict(r) for r in rows],
        }

    @app.post("/api/resource/v1/resources/{worker_id}/mode")
    def set_mode(worker_id: str, body: ModeRequest, role: str = Depends(auth_demo_or_task)):
        s = state()
        if role != ROLE_DEMO:
            raise forbidden()
        _get_node_or_404(s, worker_id)
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
    def get_task_types(role: str = Depends(auth_demo_or_task)):
        s = state()
        return {
            "contract_version": CONTRACT_VERSION,
            "server_time": iso_now(),
            "task_types": s.catalog.all(),
        }

    # ---------------- artifacts ----------------

    @app.post("/api/resource/v1/artifacts")
    async def upload_artifact(upload_id: str = Form(...), file: UploadFile = Form(...), role: str = Depends(auth_demo_or_task)):
        s = state()
        try:
            upload_id = as_uuid(upload_id)
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
        original_name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or "unnamed"
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
    def get_artifact(artifact_id: str, role: str = Depends(auth_demo_or_task)):
        s = state()
        row = s.db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise not_found(f"artifact {artifact_id} 不存在")
        return s.artifact_dict(row)

    @app.get("/api/resource/v1/artifacts/{artifact_id}/content")
    def download_artifact(artifact_id: str, role: str = Depends(auth_demo_or_task)):
        s = state()
        row = s.db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise not_found(f"artifact {artifact_id} 不存在")
        path = s.config.artifacts_dir / f"{artifact_id}.bin"
        if not path.exists():
            raise not_found(f"artifact {artifact_id} 文件缺失")
        return FileResponse(path, media_type=row["content_type"], filename=row["original_name"])

    # ---------------- preparations ----------------

    @app.post("/api/resource/v1/preparations")
    def create_preparation(body: PreparationRequest, role: str = Depends(auth_task)):
        s = state()
        preparation_id = as_uuid(body.preparation_id)
        fingerprint = canonical_json(body.model_dump())

        # F03/F08：幂等查重、资源检查与接纳/拒绝落库在同一临界区
        with s.db.write() as conn:
            existing = conn.execute(
                "SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,)
            ).fetchone()
            if existing is not None:
                return _handle_replay(
                    existing["request_fingerprint"],
                    fingerprint,
                    existing["status"],
                    existing["error"],
                    "preparation_id",
                    preparation_id,
                    lambda: s.preparation_dict(existing),
                )

            node = conn.execute(
                "SELECT * FROM nodes WHERE worker_id=?", (body.worker_id,)
            ).fetchone()
            now_dt = utcnow()
            now = iso(now_dt)
            reject = (
                _check_node_for_work(s, node, body.worker_session_id, preparing=True)
                if node is not None else (409, "NOT_FOUND", "目标节点不存在", True)
            )
            if reject is None:
                type_desc = s.catalog.get(body.task_type)
                if (
                    type_desc is None
                    or (body.profile_id, body.profile_digest)
                    != (type_desc["profile_id"], type_desc["profile_digest"])
                ):
                    reject = (503, "PROFILE_UNCONFIGURED", "profile 未配置或摘要不匹配", False)
                elif any(
                    cap["task_type"] == body.task_type and cap["profile_id"] == body.profile_id
                    and cap["state"] == "UNAVAILABLE"
                    for cap in parse_json(node["capabilities"], [])
                ):
                    reject = (409, "NODE_UNAVAILABLE", "节点无法准备该 profile", False)
            if reject is not None:
                status_code, code, message, retryable = reject
                error = {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": {"preparation_id": preparation_id},
                }
                conn.execute(
                    "INSERT INTO preparations (preparation_id, request_fingerprint, worker_id, worker_session_id, task_type, profile_id, profile_digest, status, created_at, finished_at, progress, error)"
                    " VALUES (?,?,?,?,?,?,?, 'REJECTED', ?, ?, NULL, ?)",
                    (
                        preparation_id,
                        fingerprint,
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
                event_payload = (status_code, code, message, retryable, preparation_id, error)
            else:
                conn.execute(
                    "INSERT INTO preparations (preparation_id, request_fingerprint, worker_id, worker_session_id, task_type, profile_id, profile_digest, status, created_at, finished_at, progress, error)"
                    " VALUES (?,?,?,?,?,?,?, 'ACCEPTED', ?, NULL, NULL, NULL)",
                    (
                        preparation_id,
                        fingerprint,
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
                event_payload = None

        if event_payload is not None:
            status_code, code, message, retryable, preparation_id, error = event_payload
            s.db.insert_event(
                now, "profile.failed", f"准备被拒绝：{code}", worker_id=body.worker_id, preparation_id=preparation_id
            )
            raise ApiError(status_code, code, message, retryable, {"preparation_id": preparation_id})
        s.db.insert_event(
            now, "profile.preparing", f"节点 {body.worker_id} 开始准备 {body.profile_id}",
            worker_id=body.worker_id, preparation_id=preparation_id,
        )
        row = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,))
        return JSONResponse(status_code=202, content=s.preparation_dict(row))

    @app.get("/api/resource/v1/preparations/{preparation_id}")
    def get_preparation(preparation_id: str, role: str = Depends(auth_task)):
        s = state()
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
    def create_execution(body: ExecutionRequest, role: str = Depends(auth_task)):
        s = state()
        execution_id = as_uuid(body.execution_id)
        fingerprint = canonical_json(body.model_dump())

        with s.db.write() as conn:
            existing = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
            if existing is not None:
                return _handle_replay(
                    existing["request_fingerprint"],
                    fingerprint,
                    existing["status"],
                    existing["error"],
                    "execution_id",
                    execution_id,
                    lambda: s.execution_dict(existing),
                )
            node = conn.execute(
                "SELECT * FROM nodes WHERE worker_id=?", (body.worker_id,)
            ).fetchone()
            now_dt = utcnow()
            now = iso(now_dt)
            reject = (
                _check_node_for_work(s, node, body.worker_session_id)
                if node is not None else (409, "NOT_FOUND", "目标节点不存在", True)
            )
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
                conn.execute(
                    "INSERT INTO executions (execution_id, request_fingerprint, request, job_id, task_id, attempt_no,"
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
                rejected = (status_code, code, message, retryable, error)
            else:
                accepted_at = now_dt
                hard_deadline = plus(accepted_at, body.timeout_seconds)
                lease_expires = min(plus(accepted_at, LEASE_SECONDS), hard_deadline)
                input_row = conn.execute(
                    "SELECT sha256 FROM artifacts WHERE artifact_id=?", (body.input_artifact_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO executions (execution_id, request_fingerprint, request, job_id, task_id, attempt_no,"
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
                slot_id = slots.free(s.db, body.worker_id)[0]["slot_id"] if node["max_slots"] > 1 else 0
                conn.execute("UPDATE executions SET slot_id=? WHERE execution_id=?", (slot_id, execution_id))
                s.db.insert_event(
                    now, "execution.accepted", f"执行已接受：{body.task_type} on {body.worker_id}",
                    worker_id=body.worker_id, execution_id=execution_id,
                )
                rejected = None
        if rejected is not None:
            status_code, code, message, retryable, _error = rejected
            s.db.insert_event(
                now, "execution.rejected", f"执行被拒绝：{code}", worker_id=body.worker_id, execution_id=execution_id
            )
            raise ApiError(
                status_code, code, message, retryable, {"execution_id": execution_id}
            )
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        return JSONResponse(status_code=201, content=s.execution_dict(row))

    @app.get("/api/resource/v1/executions/{execution_id}")
    def get_execution(execution_id: str, role: str = Depends(auth_task)):
        s = state()
        row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if row is None:
            raise not_found("execution 不存在")
        return s.execution_dict(row)

    @app.post("/api/resource/v1/executions/{execution_id}/cancel")
    def cancel_execution(execution_id: str, role: str = Depends(auth_task)):
        s = state()
        with s.db.write() as conn:
            row = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
            if row is None:
                raise not_found("execution 不存在")
            if row["status"] in TERMINAL_EXEC_STATUSES:
                return s.execution_dict(row)
            now = iso_now()
            if row["status"] != "CANCEL_REQUESTED":
                conn.execute("UPDATE executions SET status='CANCEL_REQUESTED' WHERE execution_id=?", (execution_id,))
                s.db.insert_event(
                    now, "execution.cancel_requested", f"请求取消执行 {execution_id}",
                    worker_id=row["worker_id"], execution_id=execution_id,
                )
            fresh = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        return JSONResponse(status_code=202, content=s.execution_dict(fresh))

    # ---------------- events ----------------

    @app.get("/api/resource/v1/events")
    def get_events(after: int = 0, limit: int = 100, role: str = Depends(auth_demo_or_task)):
        s = state()
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
        """节点程序下载输入文件：Bearer 节点秘密凭证（F07，会话旋转后自动失效）。"""
        s = state()
        node = _get_node_or_404(s, worker_id)
        secret = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not secret or node["node_secret"] != secret:
            raise ApiError(409, "SESSION_CHANGED", "节点凭证已失效，需重新注册", retryable=True)
        row = s.db.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise not_found(f"artifact {artifact_id} 不存在")
        path = s.config.artifacts_dir / f"{artifact_id}.bin"
        if not path.exists():
            raise not_found(f"artifact {artifact_id} 文件缺失")
        return FileResponse(path, media_type=row["content_type"])

    prefetch.attach_routes(app, state, _get_node_or_404, _require_node_auth)

    @app.post("/api/resource/internal/v1/nodes/register")
    def node_register(body: RegisterRequest, request: Request):
        s = state()
        require_join(request, s.config)
        now = iso_now()
        worker_id = body.worker_id or ("w" + uuid.uuid4().hex[:16])
        worker_id = _sanitize_worker_id(worker_id)
        session_id = str(uuid.uuid4())
        node_secret = secrets.token_hex(16)
        unavailable = {
            (cap.task_type, cap.profile_id): cap.reason
            for cap in body.capabilities if cap.state == "UNAVAILABLE" and cap.reason
        }
        caps_init = [
            {
                "task_type": t["task_type"],
                "profile_id": t["profile_id"],
                "profile_digest": None,
                "state": "NOT_PREPARED" if t["profile_digest"] else "UNAVAILABLE",
                "model_cached": False,
                "backend": None,
                "runtime_provider": None,
                "model_sha256": None,
                "verified": False,
                "calibration": None,
                "reason": "等待准备" if t["profile_digest"] else t["reason"],
            }
            for t in s.catalog.all()
        ]
        for cap in caps_init:
            reason = unavailable.get((cap["task_type"], cap["profile_id"]))
            if reason:
                cap.update(state="UNAVAILABLE", reason=reason)
        with s.db.write() as conn:
            existing = conn.execute(
                "SELECT * FROM nodes WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if existing:
                if existing["connection"] == "ONLINE" and (
                    s.node_active_executions(worker_id) or s.node_active_preparation(worker_id)
                ):
                    raise ApiError(409, "RESOURCE_BUSY", "旧会话仍有工作", retryable=True)
                conn.execute(
                    "UPDATE nodes SET session_id=?, node_secret=?, name=?, hardware=?, capabilities=?, connection='ONLINE',"
                    " last_seen_at=?, telemetry=NULL WHERE worker_id=?",
                    (
                        session_id,
                        node_secret,
                        body.name,
                        canonical_json(body.hardware.model_dump()),
                        canonical_json(caps_init),
                        now,
                        worker_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO nodes (worker_id, session_id, node_secret, name, mode, connection, hardware, capabilities,"
                    " cleanup_pending, last_seen_at, registered_at, telemetry)"
                    " VALUES (?,?,?,?,'ACTIVE','ONLINE',?,?,0,?,?,NULL)",
                    (
                        worker_id,
                        session_id,
                        node_secret,
                        body.name,
                        canonical_json(body.hardware.model_dump()),
                        canonical_json(caps_init),
                        now,
                        now,
                    ),
                )
            conn.execute("UPDATE nodes SET max_slots=?, threads_per_slot=?, cpu_thread_budget=?, prefetch_capacity=? WHERE worker_id=?",
                         (body.max_slots, body.threads_per_slot, body.cpu_thread_budget, body.prefetch_capacity, worker_id))
            slots.provision(conn, worker_id, body.max_slots)
        s.db.insert_event(now, "node.registered", f"节点 {body.name} 注册 ({worker_id})", worker_id=worker_id)
        return {
            "worker_id": worker_id,
            "session_id": session_id,
            "node_secret": node_secret,
            "confirm_idle": existing is not None and bool(existing["cleanup_pending"]),
            "slots": slots.snapshot(s.db, worker_id),
            "heartbeat_interval_s": 3,
            "lease_interval_s": LEASE_SECONDS,
        }

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/slots/{slot_id}/stopped")
    def slot_stopped(worker_id: str, slot_id: int, body: ClaimRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            slot = conn.execute("SELECT * FROM execution_slots WHERE worker_id=? AND slot_id=?",
                                (worker_id, slot_id)).fetchone()
            if node["max_slots"] <= 1 or slot is None or not body.generation:
                raise ApiError(422, "INVALID_REQUEST", "多槽停止确认缺少有效槽位/进程代次")
            # A replay cannot terminate a replacement process or its new work.
            if slot["generation"] != body.generation:
                return dict(slot)
            for row in conn.execute("SELECT * FROM executions WHERE worker_id=? AND worker_session_id=?"
                                    " AND slot_id=? AND status IN (?,?,?)",
                                    (worker_id, body.session_id, slot_id, *ACTIVE_EXEC_STATUSES)).fetchall():
                if row["status"] == "CANCEL_REQUESTED":
                    _finish_execution(s, conn, row, iso_now(), "CANCELLED", parse_json(row["error"]))
                else:
                    _finish_execution(s, conn, row, iso_now(), "FAILED",
                                      _err("EXECUTION_ERROR", "计算槽进程已停止，任务可重试", True))
            conn.execute("UPDATE execution_slots SET generation=?, ready=0, blocked=0 WHERE worker_id=? AND slot_id=?",
                         (str(uuid.uuid4()), worker_id, slot_id))
            return dict(conn.execute("SELECT * FROM execution_slots WHERE worker_id=? AND slot_id=?",
                                     (worker_id, slot_id)).fetchone())

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/slots/{slot_id}/ready")
    def slot_ready(worker_id: str, slot_id: int, body: SlotReadyRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            slot = conn.execute("SELECT * FROM execution_slots WHERE worker_id=? AND slot_id=?",
                                (worker_id, slot_id)).fetchone()
            if (node["max_slots"] <= 1 or slot is None or slot["generation"] != body.generation
                    or slot["blocked"] or node["cleanup_pending"] or not node["sharing_enabled"]):
                raise ApiError(409, "STALE_EXECUTION", "计算槽代次失效或节点正在停止")
            valid = body.calibration.passed and any(
                c.get("state") == "READY" and all(c.get(k) == getattr(body, k) for k in
                    ("task_type", "profile_id", "profile_digest", "backend", "runtime_provider", "model_sha256"))
                for c in parse_json(node["capabilities"], []))
            if not valid or not s.catalog.backend_allowed(body.profile_id, body.backend, body.runtime_provider):
                raise ApiError(409, "CAPABILITY_NOT_READY", "计算槽未通过同模型自测")
            conn.execute("UPDATE execution_slots SET ready=1, blocked=0 WHERE worker_id=? AND slot_id=?",
                         (worker_id, slot_id))
            return {"ready": True, "generation": slot["generation"]}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/runtime_stopped")
    def runtime_stopped(worker_id: str, body: ClaimRequest):
        """Confirm process exit and retire the session atomically.

        Lost response: replay of the old credentials yields SESSION_CHANGED.
        Registration and preparation are required before accepting new work.
        """
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            _end_node_work(s, conn, worker_id, iso_now(), "模型运行进程已确认停止，需重新准备")
            caps = parse_json(node["capabilities"], [])
            for cap in caps:
                if cap["state"] != "UNAVAILABLE":
                    cap.update(state="NOT_PREPARED", verified=False, model_cached=False,
                               calibration=None, backend=None, runtime_provider=None,
                               model_sha256=None, reason="模型进程已停止，等待重新准备")
            _revoke_node_session(conn, worker_id)
            conn.execute("UPDATE nodes SET cleanup_pending=0, capabilities=? WHERE worker_id=?",
                         (canonical_json(caps), worker_id))
            s.db.insert_event(iso_now(), "node.offline", "节点模型进程已停止，会话已撤销", worker_id=worker_id)
        return {"ok": True}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/confirm_idle")
    def node_confirm_idle(worker_id: str, body: ClaimRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            if s.node_active_executions(worker_id) or (node["cleanup_pending"] and _cleanup_execution(conn, worker_id, body.session_id)):
                raise ApiError(409, "RESOURCE_BUSY", "等待当前会话确认停止", retryable=True)
            conn.execute("UPDATE nodes SET cleanup_pending=0 WHERE worker_id=?", (worker_id,))
        return {"ok": True}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/sharing")
    def node_sharing(worker_id: str, body: SharingRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            if not body.enabled and not node["sharing_enabled"]:
                return {"enabled": False, "mode": node["mode"],
                        "cleanup_pending": bool(node["cleanup_pending"])}
            if body.enabled and (node["cleanup_pending"] or s.node_active_executions(worker_id)
                                 or s.node_active_preparation(worker_id)):
                raise ApiError(409, "RESOURCE_BUSY", "等待旧计算停止确认", retryable=True)
            conn.execute("UPDATE nodes SET sharing_enabled=?, mode=? WHERE worker_id=?",
                         (int(body.enabled), "ACTIVE" if body.enabled else "PAUSED", worker_id))
            if not body.enabled:
                caps = parse_json(node["capabilities"], [])
                for cap in caps:
                    if cap["state"] != "UNAVAILABLE":
                        cap.update(state="NOT_PREPARED", profile_digest=None, verified=False,
                                   calibration=None, backend=None, runtime_provider=None,
                                   model_sha256=None, reason="共享已关闭，等待重新准备")
                conn.execute("UPDATE nodes SET capabilities=? WHERE worker_id=?", (canonical_json(caps), worker_id))
                for row in conn.execute("SELECT * FROM executions WHERE worker_id=? AND status IN (?,?,?)",
                                        (worker_id, *ACTIVE_EXEC_STATUSES)).fetchall():
                    failure = _err("NODE_UNAVAILABLE", "设备已关闭资源共享", True)
                    failure["details"]["execution_id"] = row["execution_id"]
                    conn.execute("UPDATE executions SET status='CANCEL_REQUESTED', error=? WHERE execution_id=?",
                                 (canonical_json(failure), row["execution_id"]))
                for row in conn.execute("SELECT * FROM preparations WHERE worker_id=? AND status IN (?,?,?,?)",
                                        (worker_id, *ACTIVE_PREP_STATUSES)).fetchall():
                    _fail_preparation(s, conn, row, iso_now(), _err("NODE_UNAVAILABLE", "设备已关闭资源共享", True))
                # Cleared only by an authenticated idle confirmation after the child exits.
                conn.execute("UPDATE nodes SET cleanup_pending=1 WHERE worker_id=?", (worker_id,))
            fresh = _get_node_or_404(s, worker_id)
            return {"enabled": bool(fresh["sharing_enabled"]), "mode": fresh["mode"],
                    "cleanup_pending": bool(fresh["cleanup_pending"])}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/heartbeat")
    def node_heartbeat(worker_id: str, body: HeartbeatRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            now_dt = utcnow()
            now = iso(now_dt)
            caps = {
                (cap["task_type"], cap["profile_id"]): cap
                for cap in parse_json(node["capabilities"], [])
            }
            for cap in body.capabilities:
                previous = caps.get((cap.task_type, cap.profile_id))
                if (previous and previous["state"] == "NOT_PREPARED"
                        and cap.state == "UNAVAILABLE" and cap.reason):
                    previous.update(state="UNAVAILABLE", reason=cap.reason)
            conn.execute(
                "UPDATE nodes SET last_seen_at=?, connection='ONLINE', telemetry=?, capabilities=? WHERE worker_id=?",
                (now, canonical_json(body.telemetry.model_dump()) if body.telemetry else None,
                 canonical_json(list(caps.values())), worker_id),
            )
            running = body.running_execution_ids
            if node["max_slots"] > 1 and running is None:
                running = []
            sql = ("UPDATE executions SET lease_expires_at=MIN(?, hard_deadline_at)"
                   " WHERE worker_id=? AND worker_session_id=? AND status='RUNNING' AND lease_expires_at>?")
            params = [iso(plus(now_dt, LEASE_SECONDS)), worker_id, body.session_id, now]
            if running is not None:
                sql += " AND execution_id IN (" + ",".join("?" for _ in running) + ")"
                params.extend(running)
            conn.execute(sql, params)
            cancelled = [
                row["execution_id"] for row in conn.execute(
                    "SELECT execution_id FROM executions WHERE worker_id=? AND status='CANCEL_REQUESTED'",
                    (worker_id,),
                )
            ]
        return {"ok": True, "mode": node["mode"], "cancel_requested": cancelled,
                "sharing_enabled": bool(node["sharing_enabled"]), "slots": slots.snapshot(s.db, worker_id)}

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/claim")
    def node_claim(worker_id: str, body: ClaimRequest):
        s = state()
        empty = {"execution": None, "preparation": None}
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            if node["connection"] != "ONLINE":
                return empty
            pending = conn.execute(
                "SELECT execution_id FROM executions WHERE worker_id=? AND worker_session_id=?"
                " AND status='CANCEL_REQUESTED'",
                (worker_id, body.session_id),
            ).fetchone()
            if node["cleanup_pending"]:
                pending = pending or _cleanup_execution(conn, worker_id, body.session_id)
            if pending and node["max_slots"] == 1:
                return {**empty, "stop_ack_required": [pending["execution_id"]]}
            if node["cleanup_pending"] or node["mode"] == "PAUSED":
                return empty

            now_dt = utcnow()
            now = iso(now_dt)
            prep = conn.execute(
                "SELECT * FROM preparations WHERE worker_id=? AND worker_session_id=?"
                " AND (status='ACCEPTED' OR (? AND status='LOADING')) LIMIT 1",
                (worker_id, body.session_id, node["max_slots"] > 1),
            ).fetchone()
            if prep is not None:
                conn.execute(
                    "UPDATE preparations SET status='LOADING' WHERE preparation_id=?",
                    (prep["preparation_id"],),
                )
                return {**empty, "preparation": dict(s.preparation_dict(prep), status="LOADING")}
            query = ("SELECT * FROM executions WHERE worker_id=? AND worker_session_id=? AND status='ACCEPTED'"
                     " AND lease_expires_at>? AND hard_deadline_at>?")
            params = [worker_id, body.session_id, now, now]
            if node["max_slots"] > 1:
                slot = conn.execute("SELECT * FROM execution_slots WHERE worker_id=? AND slot_id=?",
                                    (worker_id, body.slot_id)).fetchone()
                if slot is None or slot["generation"] != body.generation or not slot["ready"] or slot["blocked"]:
                    return empty
                query += " AND slot_id=?"
                params.append(body.slot_id)
            row = conn.execute(query + " ORDER BY accepted_at LIMIT 1", params).fetchone()
            if row is None:
                return empty
            conn.execute(
                "UPDATE executions SET status='RUNNING', started_at=?, lease_expires_at=MIN(?, hard_deadline_at)"
                " WHERE execution_id=?",
                (now, iso(plus(now_dt, LEASE_SECONDS)), row["execution_id"]),
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
            "slot_id": row["slot_id"],
            "job_id": row["job_id"],
            "task_id": row["task_id"],
            "attempt_no": row["attempt_no"],
            "task_type": row["task_type"],
            "profile_id": row["profile_id"],
            "profile_digest": row["profile_digest"],
            "input_artifact_id": row["input_artifact_id"],
            "input_sha256": row["input_sha256"],
            "input_media_info": parse_json(artifact["media_info"], {}) if artifact else {},
            "input_content_type": artifact["content_type"] if artifact else None,
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
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            row = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,))
            if row is None or row["worker_id"] != worker_id:
                raise not_found("preparation 不存在")
            if row["status"] not in ACTIVE_PREP_STATUSES:
                if row["status"] == body.status == "FAILED" and row["worker_session_id"] == body.session_id:
                    return s.preparation_dict(row)
                if row["status"] == body.status == "READY" and row["worker_session_id"] == body.session_id:
                    if any(c.get("profile_digest") == row["profile_digest"] and c.get("state") == "READY"
                           and c.get("backend") == body.backend and c.get("runtime_provider") == body.runtime_provider
                           and c.get("model_sha256") == body.model_sha256
                           for c in parse_json(node["capabilities"], [])):
                        return s.preparation_dict(row)
                raise ApiError(409, "STALE_EXECUTION", "准备已结束，状态更新被拒收")
            now_dt = utcnow()
            now = iso(now_dt)
            if row["worker_session_id"] != body.session_id:
                raise ApiError(409, "STALE_EXECUTION", "准备属于已失效的会话")
            if row["created_at"] <= iso(plus(now_dt, -PREPARATION_TIMEOUT_SECONDS)):
                raise ApiError(409, "STALE_EXECUTION", "准备已超过硬期限")
            status = body.status
            error_code, error_message = body.error_code, body.error_message
            if status == "READY":
                expected = s.catalog.model_sha256(row["profile_id"])
                if (not body.calibration or not body.calibration.passed
                        or not s.catalog.backend_allowed(row["profile_id"], body.backend, body.runtime_provider)):
                    status, error_code, error_message = "FAILED", "MODEL_LOAD_FAILED", "缺少通过的自测或实际后端"
                elif not expected or body.model_sha256 != expected:
                    status, error_code, error_message = "FAILED", "MODEL_HASH_MISMATCH", "模型 SHA 与锁文件不符"
            error_json = None
            if status == "FAILED":
                error_json = canonical_json(
                    {
                        "code": error_code or "MODEL_LOAD_FAILED",
                        "message": error_message or "模型准备失败",
                        "retryable": bool(body.error_retryable),
                        "details": {"preparation_id": preparation_id},
                    }
                )
            finished = now if status in ("READY", "FAILED") else None
            conn.execute(
                "UPDATE preparations SET status=?, progress=?, finished_at=?, error=? WHERE preparation_id=?",
                (status, body.progress, finished, error_json, preparation_id),
            )
            if status == "READY":
                conn.execute("UPDATE execution_slots SET ready=1, blocked=0 WHERE worker_id=?", (worker_id,))
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
            elif status == "FAILED":
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
                        "reason": error_message or "模型准备失败",
                    },
                )
        if status == "READY":
            s.db.insert_event(
                now, "profile.ready", f"节点 {worker_id} 模型就绪 {row['profile_id']}",
                worker_id=worker_id, preparation_id=preparation_id,
            )
        elif status == "FAILED":
            s.db.insert_event(
                now, "profile.failed", f"节点 {worker_id} 模型准备失败：{error_message}",
                worker_id=worker_id, preparation_id=preparation_id,
            )
        fresh = s.db.query_one("SELECT * FROM preparations WHERE preparation_id=?", (preparation_id,))
        return s.preparation_dict(fresh)

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/executions/{execution_id}/complete")
    def execution_complete(worker_id: str, execution_id: str, body: ExecutionCompleteRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            row = _session_execution(conn, worker_id, execution_id, body.session_id)
            now = iso(utcnow())
            result_bytes = None
            if body.outcome == "SUCCEEDED" and body.result is not None:
                result_bytes = canonical_json(body.result).encode("utf-8")
                sha = hashlib.sha256(result_bytes).hexdigest()
                if row["status"] == "SUCCEEDED":
                    saved = s.execution_dict(row)
                    if saved["result_artifact"] and saved["result_artifact"]["sha256"] == sha:
                        return saved
            stale_reason = _submission_rejection(row, now)
            error = None
            if not stale_reason and body.outcome == "SUCCEEDED":
                try:
                    if body.result is None:
                        raise ResultInvalid("SUCCEEDED 提交缺少 result")
                    _validate_execution_result(s, row, body.result)
                except ResultInvalid as exc:
                    error = _err("RESULT_INVALID", exc.message, False)
            elif not stale_reason:
                code = body.error_code or "EXECUTION_ERROR"
                error = _err(
                    code, body.error_message or "执行失败",
                    bool(body.error_retryable) and code not in ("INPUT_INVALID", "RESULT_INVALID", "PROFILE_MISMATCH"),
                )

            if not stale_reason and not error:
                artifact_id = new_artifact_id()
                target = s.config.artifacts_dir / f"{artifact_id}.bin"
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(result_bytes)
                tmp.replace(target)
                now = iso(utcnow())
                stale_reason = _submission_rejection(row, now)
                if stale_reason:
                    target.unlink()

            if stale_reason:
                s.db.insert_event(
                    now, "execution.stale_rejected", f"结果提交被拒收 {execution_id}",
                    worker_id=worker_id, execution_id=execution_id,
                )
            elif error:
                _finish_execution(s, conn, row, now, "FAILED", error)
            else:
                timings = body.timings.model_dump() if body.timings is not None else {
                    "download_ms": 0, "inference_ms": 0, "postprocess_ms": 0,
                    "write_output_ms": 0, "total_worker_ms": 0,
                }
                conn.execute(
                    "INSERT INTO artifacts (artifact_id, upload_id, kind, original_name, content_type,"
                    " sha256, size_bytes, media_info, created_at)"
                    " VALUES (?,NULL,'RESULT',?,'application/json',?,?,?,?)",
                    (artifact_id, f"execution-{execution_id}.json", sha, len(result_bytes),
                     canonical_json(_NULL_MEDIA), now),
                )
                updated = conn.execute(
                    "UPDATE executions SET status='SUCCEEDED', finished_at=?, result_artifact_id=?,"
                    " timings=?, error=NULL WHERE execution_id=? AND worker_session_id=? AND status='RUNNING'"
                    " AND lease_expires_at>? AND hard_deadline_at>?",
                    (now, artifact_id, canonical_json(timings), execution_id, body.session_id, now, now),
                )
                if updated.rowcount != 1:
                    target.unlink()
                    raise ApiError(409, "STALE_EXECUTION", "提交权已撤销")
                s.db.insert_event(
                    now, "execution.succeeded", f"执行成功 {execution_id}",
                    worker_id=worker_id, execution_id=execution_id,
                )
            fresh = s.db.query_one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if stale_reason:
            raise ApiError(409, "STALE_EXECUTION", stale_reason)
        return s.execution_dict(fresh)

    @app.post("/api/resource/internal/v1/nodes/{worker_id}/executions/{execution_id}/cancel_ack")
    def execution_cancel_ack(worker_id: str, execution_id: str, body: CancelAckRequest):
        s = state()
        with s.db.write() as conn:
            node = _get_node_or_404(s, worker_id)
            _require_node_auth(node, body.session_id, body.node_secret)
            if node["max_slots"] > 1:
                raise ApiError(409, "STALE_EXECUTION", "多槽节点必须按槽位及进程代次确认停止")
            row = _session_execution(conn, worker_id, execution_id, body.session_id)
            if row["status"] == "CANCEL_REQUESTED":
                _finish_execution(s, conn, row, iso_now(), "CANCELLED", parse_json(row["error"]))
            elif node["cleanup_pending"]:
                pending = _cleanup_execution(conn, worker_id, body.session_id)
                if pending and pending["execution_id"] == execution_id:
                    conn.execute("UPDATE nodes SET cleanup_pending=0 WHERE worker_id=?", (worker_id,))
            return s.execution_dict(s.db.query_one(
                "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
            ))

    # ---------------- 后台扫描 ----------------

    async def _scan_loop(st: State, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await asyncio.to_thread(run_scan, st)
            except Exception:
                import traceback

                traceback.print_exc()
            try:
                await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def run_scan(st: State) -> None:
        now_dt = utcnow()
        now = iso(now_dt)
        with st.db.write() as conn:
            for node in conn.execute(
                "SELECT * FROM nodes WHERE connection='ONLINE' AND last_seen_at<=?",
                (iso(plus(now_dt, -OFFLINE_AFTER_SECONDS)),),
            ).fetchall():
                _end_node_work(st, conn, node["worker_id"], now, "节点失联")
                _revoke_node_session(conn, node["worker_id"])
                st.db.insert_event(now, "node.offline", "节点失联", worker_id=node["worker_id"])

            expired = conn.execute(
                "SELECT * FROM executions WHERE status IN ('ACCEPTED','RUNNING')"
                " AND (hard_deadline_at<=? OR lease_expires_at<=?)",
                (now, now),
            ).fetchall()
            for row in expired:
                error = (
                    _err("EXECUTION_TIMEOUT", "执行超过硬期限", True)
                    if row["hard_deadline_at"] <= now
                    else _err("LEASE_EXPIRED", "执行租约过期", True)
                )
                _finish_execution(st, conn, row, now, "FAILED", error)
                if row["status"] == "RUNNING":
                    node = conn.execute("SELECT max_slots FROM nodes WHERE worker_id=?", (row["worker_id"],)).fetchone()
                    if node["max_slots"] > 1:
                        conn.execute("UPDATE execution_slots SET blocked=1, ready=0 WHERE worker_id=? AND slot_id=?",
                                     (row["worker_id"], row["slot_id"]))
                    else:
                        conn.execute("UPDATE nodes SET cleanup_pending=1 WHERE worker_id=?", (row["worker_id"],))

            expired_preps = conn.execute(
                "SELECT * FROM preparations WHERE status IN (?,?,?,?) AND created_at<=?",
                (*ACTIVE_PREP_STATUSES, iso(plus(now_dt, -PREPARATION_TIMEOUT_SECONDS))),
            ).fetchall()
            for row in expired_preps:
                error = _err("PREPARATION_TIMEOUT", "准备超过 900 秒硬期限", True)
                _fail_preparation(st, conn, row, now, error)
                # 旧节点须在准备退出后重新建立会话。
                _revoke_node_session(conn, row["worker_id"])

            for node in conn.execute("SELECT * FROM nodes WHERE mode='DRAINING' AND cleanup_pending=0").fetchall():
                worker_id = node["worker_id"]
                if not st.node_active_executions(worker_id) and not st.node_active_preparation(worker_id):
                    conn.execute("UPDATE nodes SET mode='PAUSED' WHERE worker_id=?", (worker_id,))
                    st.db.insert_event(now, "node.mode_changed", "节点完成工作并进入 PAUSED", worker_id=worker_id)

    def _submission_rejection(row, now: str):
        if row["status"] != "RUNNING":
            return "执行未运行或提交权已撤销"
        if row["hard_deadline_at"] <= now or row["lease_expires_at"] <= now:
            return "执行硬期限或租约已到期"
        return None

    def _session_execution(conn, worker_id: str, execution_id: str, session_id: str):
        row = conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None or row["worker_id"] != worker_id:
            raise not_found("execution 不存在")
        if row["worker_session_id"] != session_id:
            raise ApiError(409, "STALE_EXECUTION", "执行属于已失效的会话")
        return row

    def _cleanup_execution(conn, worker_id: str, session_id: str):
        return conn.execute(
            "SELECT execution_id FROM executions WHERE worker_id=? AND worker_session_id=?"
            " AND status='FAILED' AND started_at IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (worker_id, session_id),
        ).fetchone()

    def _finish_execution(s: State, conn, row, now: str, status: str, error=None) -> None:
        conn.execute(
            "UPDATE executions SET status=?, finished_at=?, error=? WHERE execution_id=?",
            (status, now, canonical_json(error) if error else None, row["execution_id"]),
        )
        s.db.insert_event(
            now, f"execution.{status.lower()}", error["message"] if error else "执行已取消",
            worker_id=row["worker_id"], execution_id=row["execution_id"],
        )

    def _fail_preparation(s: State, conn, row, now: str, error: dict) -> None:
        conn.execute(
            "UPDATE preparations SET status='FAILED', finished_at=?, error=? WHERE preparation_id=?",
            (now, canonical_json(error), row["preparation_id"]),
        )
        s.db.insert_event(
            now, "profile.failed", error["message"],
            worker_id=row["worker_id"], preparation_id=row["preparation_id"],
        )

    def _end_node_work(s: State, conn, worker_id: str, now: str, reason: str) -> None:
        for row in conn.execute(
            "SELECT * FROM executions WHERE worker_id=? AND status IN (?,?,?)",
            (worker_id, *ACTIVE_EXEC_STATUSES),
        ).fetchall():
            if row["status"] == "CANCEL_REQUESTED":
                _finish_execution(s, conn, row, now, "CANCELLED", parse_json(row["error"]))
            else:
                _finish_execution(s, conn, row, now, "FAILED", _err("LEASE_EXPIRED", reason, True))
        for row in conn.execute(
            "SELECT * FROM preparations WHERE worker_id=? AND status IN (?,?,?,?)",
            (worker_id, *ACTIVE_PREP_STATUSES),
        ).fetchall():
            _fail_preparation(s, conn, row, now, _err("MODEL_LOAD_FAILED", reason, True))

    def _revoke_node_session(conn, worker_id: str) -> None:
        conn.execute(
            "UPDATE nodes SET connection='OFFLINE', session_id=?, node_secret=?, last_seen_at=NULL,"
            " cleanup_pending=1 WHERE worker_id=?",
            (str(uuid.uuid4()), secrets.token_hex(16), worker_id),
        )

    # ---------------- helpers ----------------

    def _err(code: str, message: str, retryable: bool) -> dict:
        return {"code": code, "message": message, "retryable": retryable, "details": {}}

    def _sanitize_worker_id(raw: str) -> str:
        cleaned = "".join(c for c in raw if c.isalnum() or c in "_-")
        return cleaned[:80] if cleaned else "w" + secrets.token_hex(8)

    def _check_node_for_work(s: State, node_row, worker_session_id: str, preparing=False):
        """返回 (status_code, code, message, retryable) 或 None。"""
        if node_row["connection"] != "ONLINE":
            return (409, "NODE_OFFLINE", "节点离线", True)
        if node_row["session_id"] != worker_session_id:
            return (409, "SESSION_CHANGED", "节点会话已变化，请刷新资源快照", True)
        if not node_row["sharing_enabled"] or node_row["mode"] != "ACTIVE":
            return (409, "NODE_UNAVAILABLE", "节点不在 ACTIVE 模式", True)
        if node_row["cleanup_pending"]:
            return (409, "RESOURCE_BUSY", "节点存在未确认的清理，暂不可接单", True)
        active_exec = s.node_active_executions(node_row["worker_id"])
        active_prep = s.node_active_preparation(node_row["worker_id"])
        if active_prep or (preparing and active_exec):
            return (409, "RESOURCE_BUSY", "节点槽位被占用", True)
        if not preparing and ((node_row["max_slots"] == 1 and active_exec)
                              or (node_row["max_slots"] > 1 and not slots.free(s.db, node_row["worker_id"]))):
            return (409, "RESOURCE_BUSY", "节点没有已准备的空闲槽位", True)
        return None

    def _check_execution_business(s: State, body: ExecutionRequest):
        type_desc = s.catalog.get(body.task_type)
        if type_desc is None:
            return (409, "UNSUPPORTED_TASK_TYPE", f"类型 {body.task_type} 不受支持", False)
        if body.parameters.language not in type_desc["supported_languages"]:
            return (409, "PARAMETER_UNSUPPORTED", f"语言 {body.parameters.language} 不受支持", False)
        artifact = s.db.query_one(
            "SELECT * FROM artifacts WHERE artifact_id=?", (body.input_artifact_id,)
        )
        if artifact is None:
            return (409, "NOT_FOUND", f"输入 artifact {body.input_artifact_id} 不存在", True)
        if artifact["content_type"] not in type_desc["input_mime_types"]:
            return (409, "INPUT_INVALID", f"输入类型 {artifact['content_type']} 与任务不符", False)
        if (body.profile_id, body.profile_digest) != (type_desc["profile_id"], type_desc["profile_digest"]):
            return (409, "PROFILE_MISMATCH", "profile 或摘要与目录不符", False)
        # 能力就绪检查
        node = s.db.query_one("SELECT * FROM nodes WHERE worker_id=?", (body.worker_id,))
        capabilities = parse_json(node["capabilities"], [])
        ready = any(
            c.get("task_type") == body.task_type
            and c.get("profile_id") == body.profile_id
            and c.get("profile_digest") == body.profile_digest
            and c.get("state") == "READY"
            and c.get("verified") is True
            and (c.get("calibration") or {}).get("passed") is True
            and s.catalog.backend_allowed(body.profile_id, c.get("backend"), c.get("runtime_provider"))
            and c.get("model_sha256") == s.catalog.model_sha256(body.profile_id)
            for c in capabilities
        )
        if not ready:
            return (409, "CAPABILITY_NOT_READY", "节点能力未就绪或 profile 不匹配", True)
        return None

    def _probe_or_422(content_type: str, data: bytes, max_pixels: Optional[int]) -> dict:
        try:
            media = probe_media(content_type, data)
        except Image.DecompressionBombError as exc:
            raise ApiError(413, "INPUT_TOO_LARGE", "图像像素超过上限") from exc
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
            artifact = s.db.query_one(
                "SELECT media_info FROM artifacts WHERE artifact_id=?", (row["input_artifact_id"],)
            )
            media = parse_json(artifact["media_info"])
            if (result["image_width"], result["image_height"]) != (media["width"], media["height"]):
                raise ResultInvalid("结果图像尺寸与输入不符")
        else:
            validate_asr_result(result, row["input_artifact_id"])

    def _upsert_capability(conn, worker_id: str, capability: dict) -> None:
        row = conn.execute(
            "SELECT capabilities FROM nodes WHERE worker_id=?", (worker_id,)
        ).fetchone()
        caps = parse_json(row["capabilities"], []) if row else []
        caps = [
            c for c in caps
            if (c["task_type"], c["profile_id"]) != (capability["task_type"], capability["profile_id"])
        ]
        caps.append(capability)
        conn.execute(
            "UPDATE nodes SET capabilities=? WHERE worker_id=?",
            (canonical_json(caps), worker_id),
        )

    return app
