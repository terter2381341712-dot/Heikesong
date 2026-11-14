"""Explicit, in-memory P1 test double. No hardware detection, OCR, or performance evidence.

Executions advance on GET requests, allowing deterministic fault tests without sleeps.
Standalone mode seeds three PNGs; it intentionally does not implement P1 uploads.
"""

import copy
import hashlib
import json
import os
import struct
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from task_service.contract import ROOT, canonical, error, now, uid, validate

PREFIX = "/api/resource/v1"
EXAMPLES = json.loads(
    (ROOT / "tests/tasks/fixtures/contract-examples.json").read_text(encoding="utf-8")
)
CAPABILITIES = json.loads(
    (ROOT / "tests/tasks/fixtures/p1-p2-capabilities.json").read_text(encoding="utf-8")
)["capabilities"]
DIGEST = "a" * 64


class MockP1:
    def __init__(self, *, task_token="mock-task-token", demo_token="mock-demo-token", nodes=2):
        self.task_token, self.demo_token = task_token, demo_token
        self.descriptor = copy.deepcopy(EXAMPLES["catalog_configured"]["task_types"][0])
        self.descriptor.update(profile_digest=DIGEST, reason="MOCK：仅用于 P2 协议测试")
        self.nodes = []
        for i in range(nodes):
            node = copy.deepcopy(EXAMPLES["node_ready"])
            node.update(
                worker_id=f"mock_worker_{i + 1}", session_id=uid(), name=f"MOCK node {i + 1}"
            )
            self.nodes.append(node)
        self.artifacts, self.contents, self.executions, self.preparations = {}, {}, {}, {}
        self.posts, self.preparation_posts, self.cancels = [], [], []
        self.polls = defaultdict(int)
        self.finish_after = defaultdict(lambda: 2)
        self.outcomes = defaultdict(lambda: ["success"])
        self.unreachable = False
        self.drop_post_responses = 0
        self.post_before_accept_503 = 0
        self.get_404 = 0
        self.busy_rejections = 0
        self.session_rejections = 0
        self.preparation_failure = False
        self.drop_preparation_responses = 0
        self.preparation_before_accept_503 = 0
        self.preparation_get_404 = 0
        self.preparation_rejections = []
        self.hold_preparation = False
        self.result_unavailable = False
        self.unavailable_result_ids = set()
        self.cancel_finishes_success = False
        self.hold_cancel = False
        self.app = self._app()
        for i in range(1, 4):
            self.add_image(f"mock_image_{i}", i)

    def add_image(self, artifact_id, color=0):
        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        scanline = b"\0" + bytes([color % 256, (color // 256) % 256, 128]) * 20
        raw = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 20, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanline * 20))
            + chunk(b"IEND", b"")
        )
        meta = self.add_artifact(artifact_id, raw, "INPUT", "image/png")
        meta["media_info"].update(width=20, height=20)
        return meta

    def add_artifact(self, artifact_id, raw, kind, content_type):
        meta = {
            "artifact_id": artifact_id,
            "kind": kind,
            "original_name": artifact_id + (".png" if kind == "INPUT" else ".json"),
            "content_type": content_type,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "media_info": dict.fromkeys(
                ["width", "height", "duration_ms", "sample_rate_hz", "channels", "audio_encoding"]
            ),
            "download_path": f"{PREFIX}/artifacts/{artifact_id}/content",
            "created_at": now(),
        }
        self.artifacts[artifact_id], self.contents[artifact_id] = meta, raw
        return meta

    def _node(self, worker_id):
        return next((n for n in self.nodes if n["worker_id"] == worker_id), None)

    def _release(self, execution):
        node = self._node(execution["worker_id"])
        if node and execution["execution_id"] in node["active_execution_ids"]:
            node.update(active_execution_ids=[], reserved_slots=0, available_slots=1)

    def finish(self, execution_id):
        execution = self.executions[execution_id]
        if execution["status"] not in {"ACCEPTED", "RUNNING", "CANCEL_REQUESTED"}:
            return
        artifact_id = execution["input_artifact_id"]
        outcomes = self.outcomes[artifact_id]
        outcome = outcomes[min(execution["attempt_no"] - 1, len(outcomes) - 1)]
        execution["finished_at"] = now()
        self._release(execution)
        if outcome in {"retryable", "permanent"}:
            execution.update(
                status="FAILED",
                error=error("EXECUTION_ERROR", "MOCK 执行失败", outcome == "retryable"),
            )
            return
        result = {
            "schema_version": "1.0",
            "task_type": "ocr.image.v1",
            "input_artifact_id": artifact_id,
            "image_width": 20,
            "image_height": 20,
            "text": f"MOCK OCR: {artifact_id}",
            "blocks": [
                {"text": f"MOCK OCR: {artifact_id}", "bbox": [1, 1, 19, 19], "confidence": 0.9}
            ],
        }
        if outcome == "wrong_input":
            result["input_artifact_id"] = "wrong_input"
        elif outcome == "wrong_type":
            result["task_type"] = "asr.file.v1"
        elif outcome == "wrong_size":
            result["image_width"] = 30
        elif outcome == "bad_bbox":
            result["blocks"][0]["bbox"] = [19, 1, 25, 19]
        elif outcome == "empty":
            result.update(text="", blocks=[])
        meta = self.add_artifact(
            "result_" + execution_id, canonical(result).encode(), "RESULT", "application/json"
        )
        if outcome == "bad_hash":
            meta["sha256"] = "0" * 64
        execution.update(status="SUCCEEDED", result_artifact=meta, error=None)

    def _app(self):
        app = FastAPI(title="MOCK P1 — protocol tests only")

        def ok(schema, body, status=200):
            return JSONResponse(validate(schema, body), status_code=status)

        def fail(status, code, message="MOCK", retryable=False, **details):
            return ok("Error", error(code, message, retryable, **details), status)

        @app.middleware("http")
        async def guard(request, call_next):
            if self.unreachable:
                return fail(503, "DEPENDENCY_UNAVAILABLE", retryable=True)
            if request.url.path != PREFIX + "/healthz":
                token = request.headers.get("authorization", "")
                if token != f"Bearer {self.task_token}":
                    return fail(
                        403 if token == f"Bearer {self.demo_token}" else 401,
                        "FORBIDDEN" if token == f"Bearer {self.demo_token}" else "UNAUTHORIZED",
                    )
            response = await call_next(request)
            response.headers["X-Mock-P1"] = "true"
            return response

        @app.get(PREFIX + "/healthz")
        async def health():
            return ok(
                "Health",
                {
                    "service": "p1_resource",
                    "contract_version": "0.2.0",
                    "status": "ok",
                    "server_time": now(),
                },
            )

        @app.get(PREFIX + "/task-types")
        async def catalog():
            return ok(
                "TypeCatalog",
                {
                    "contract_version": "0.2.0",
                    "server_time": now(),
                    "task_types": [self.descriptor],
                },
            )

        @app.get(PREFIX + "/resources")
        async def resources():
            return ok(
                "ResourceSnapshot",
                {"contract_version": "0.2.0", "server_time": now(), "resources": self.nodes},
            )

        @app.get(PREFIX + "/artifacts/{artifact_id}")
        async def artifact(artifact_id: str):
            if artifact_id not in self.artifacts:
                return fail(404, "NOT_FOUND")
            return ok("Artifact", self.artifacts[artifact_id])

        @app.get(PREFIX + "/artifacts/{artifact_id}/content")
        async def content(artifact_id: str):
            if (
                self.result_unavailable and artifact_id.startswith("result_")
            ) or artifact_id in self.unavailable_result_ids:
                return fail(503, "DEPENDENCY_UNAVAILABLE", retryable=True)
            if artifact_id not in self.contents:
                return fail(404, "NOT_FOUND")
            return Response(
                self.contents[artifact_id], media_type=self.artifacts[artifact_id]["content_type"]
            )

        @app.post(PREFIX + "/executions")
        async def post_execution(request: Request):
            body = validate("ExecutionRequest", await request.json())
            self.posts.append(copy.deepcopy(body))
            eid = body["execution_id"]
            if eid in self.executions:
                existing = self.executions[eid]
                if any(existing[k] != v for k, v in body.items()):
                    return fail(409, "IDEMPOTENCY_CONFLICT")
                if existing["status"] == "REJECTED":
                    return JSONResponse(existing["error"], status_code=409)
                return ok("Execution", existing)
            if self.post_before_accept_503:
                self.post_before_accept_503 -= 1
                return fail(503, "DEPENDENCY_UNAVAILABLE", retryable=True)
            node = self._node(body["worker_id"])
            failure = None
            if not node or node["connection"] != "ONLINE" or node["mode"] != "ACTIVE":
                failure = ("NODE_UNAVAILABLE", True)
            elif self.session_rejections or node["session_id"] != body["worker_session_id"]:
                self.session_rejections = max(0, self.session_rejections - 1)
                node["session_id"] = uid()
                failure = ("SESSION_CHANGED", True)
            elif self.busy_rejections or not node["available_slots"] or node["cleanup_pending"]:
                self.busy_rejections = max(0, self.busy_rejections - 1)
                failure = ("RESOURCE_BUSY", True)
            elif any(body[k] != self.descriptor[k] for k in ("profile_id", "profile_digest")):
                failure = ("PROFILE_MISMATCH", False)
            elif node["capabilities"][0]["state"] != "READY":
                failure = ("CAPABILITY_NOT_READY", True)
            execution = {
                **body,
                "status": "REJECTED" if failure else "ACCEPTED",
                "input_sha256": None
                if failure
                else self.artifacts[body["input_artifact_id"]]["sha256"],
                "created_at": now(),
                "accepted_at": None if failure else now(),
                "started_at": None,
                "finished_at": now() if failure else None,
                "lease_expires_at": None,
                "hard_deadline_at": None,
                "result_artifact": None,
                "timings": None,
                "error": None,
            }
            if failure:
                execution["error"] = error(
                    failure[0], "MOCK 拒绝执行", failure[1], execution_id=eid
                )
            else:
                execution["lease_expires_at"] = (
                    (datetime.now(timezone.utc) + timedelta(seconds=15))
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                execution["hard_deadline_at"] = (
                    (datetime.now(timezone.utc) + timedelta(seconds=body["timeout_seconds"]))
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                node.update(available_slots=0, reserved_slots=1, active_execution_ids=[eid])
            self.executions[eid] = execution
            if failure:
                return JSONResponse(execution["error"], status_code=409)
            if self.drop_post_responses:
                self.drop_post_responses -= 1
                return fail(503, "DEPENDENCY_UNAVAILABLE", "MOCK 接纳后丢失响应", True)
            return ok("Execution", execution, 201)

        @app.get(PREFIX + "/executions/{execution_id}")
        async def get_execution(execution_id: str):
            if execution_id not in self.executions:
                return fail(404, "NOT_FOUND")
            if self.get_404:
                self.get_404 -= 1
                return fail(404, "NOT_FOUND")
            execution = self.executions[execution_id]
            if execution["status"] in {"ACCEPTED", "RUNNING"}:
                self.polls[execution_id] += 1
                execution.update(status="RUNNING", started_at=execution["started_at"] or now())
                if self.polls[execution_id] >= self.finish_after[execution["input_artifact_id"]]:
                    self.finish(execution_id)
            return ok("Execution", execution)

        @app.post(PREFIX + "/executions/{execution_id}/cancel")
        async def cancel(execution_id: str):
            if execution_id not in self.executions:
                return fail(404, "NOT_FOUND")
            self.cancels.append(execution_id)
            execution = self.executions[execution_id]
            if execution["status"] in {"ACCEPTED", "RUNNING", "CANCEL_REQUESTED"}:
                if self.cancel_finishes_success:
                    self.finish(execution_id)
                elif self.hold_cancel:
                    execution["status"] = "CANCEL_REQUESTED"
                else:
                    execution.update(status="CANCELLED", finished_at=now())
                    self._release(execution)
            return ok(
                "Execution", execution, 202 if execution["status"] == "CANCEL_REQUESTED" else 200
            )

        @app.post(PREFIX + "/preparations")
        async def prepare(request: Request):
            body = validate("PreparationRequest", await request.json())
            self.preparation_posts.append(copy.deepcopy(body))
            pid = body["preparation_id"]
            if pid in self.preparations:
                record = self.preparations[pid]
                if any(record[k] != v for k, v in body.items()):
                    return fail(409, "IDEMPOTENCY_CONFLICT")
                if record["status"] == "REJECTED":
                    return JSONResponse(record["error"], status_code=409)
                return ok("Preparation", record)
            if self.preparation_before_accept_503:
                self.preparation_before_accept_503 -= 1
                return fail(503, "DEPENDENCY_UNAVAILABLE", retryable=True)
            node = self._node(body["worker_id"])
            rejection = self.preparation_rejections.pop(0) if self.preparation_rejections else None
            if rejection or not node or not node["available_slots"]:
                status, code, retryable = rejection or (409, "RESOURCE_BUSY", True)
                record = {
                    **body,
                    "status": "REJECTED",
                    "created_at": now(),
                    "finished_at": now(),
                    "progress": None,
                    "error": error(code, "MOCK 准备被拒绝", retryable, preparation_id=pid),
                }
                self.preparations[pid] = record
                return JSONResponse(record["error"], status_code=status)
            record = {
                **body,
                "status": "LOADING",
                "created_at": now(),
                "finished_at": None,
                "progress": None,
                "error": None,
            }
            self.preparations[pid] = record
            node.update(active_preparation_id=pid, available_slots=0, reserved_slots=1)
            node["capabilities"][0] = {
                **copy.deepcopy(CAPABILITIES["preparing"]),
                **{k: body[k] for k in ("task_type", "profile_id", "profile_digest")},
            }
            if self.drop_preparation_responses:
                self.drop_preparation_responses -= 1
                return fail(503, "DEPENDENCY_UNAVAILABLE", retryable=True)
            return ok("Preparation", record, 202)

        @app.get(PREFIX + "/preparations/{preparation_id}")
        async def preparation(preparation_id: str):
            if self.preparation_get_404:
                self.preparation_get_404 -= 1
                return fail(404, "NOT_FOUND")
            if preparation_id not in self.preparations:
                return fail(404, "NOT_FOUND")
            record = self.preparations[preparation_id]
            if record["status"] == "LOADING" and not self.hold_preparation:
                node = self._node(record["worker_id"])
                record.update(
                    status="FAILED" if self.preparation_failure else "READY",
                    finished_at=now(),
                    progress=None if self.preparation_failure else 1,
                    error=error("MODEL_LOAD_FAILED", "MOCK 准备失败")
                    if self.preparation_failure
                    else None,
                )
                node.update(active_preparation_id=None, available_slots=1, reserved_slots=0)
                # Synthetic evidence only: completed preparation reports the requested
                # target, including when the original capability digest was null.
                node["capabilities"][0] = {
                    **copy.deepcopy(CAPABILITIES["error" if self.preparation_failure else "ready"]),
                    **{k: record[k] for k in ("task_type", "profile_id", "profile_digest")},
                }
            return ok("Preparation", record)

        return app


def create_app():
    if os.environ.get("P2_ENABLE_MOCK_P1") != "1":
        raise RuntimeError("仅供测试；须显式设置 P2_ENABLE_MOCK_P1=1")
    mock = MockP1(task_token=os.environ["TASK_TOKEN"], demo_token=os.environ["DEMO_TOKEN"])
    mock.app.state.mock = mock
    return mock.app


if __name__ == "__main__":
    import uvicorn

    print(
        "MOCK P1: synthetic results, no OCR / no real hardware / no benchmark evidence", flush=True
    )
    uvicorn.run(
        "tests.tasks.mock_p1:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(os.environ.get("MOCK_P1_PORT", "18001")),
    )
