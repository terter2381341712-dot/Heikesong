import asyncio
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from aggregators.ocr import archive, results
from task_service.config import Settings
from task_service.contract import SPEC, APIError, error, now, validate, validate_request
from task_service.p1 import DependencyUnavailable, P1Client, P1Rejected
from task_service.service import TaskService
from task_service.store import Store

PREFIX = "/api/task/v1"


def create_app(settings=None, *, transport=None, background=True, clock=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings.data_dir)
        client = P1Client(settings, transport)
        service = TaskService(store, client, settings, **({"clock": clock} if clock else {}))
        app.state.service = service
        runner = asyncio.create_task(service.run()) if background else None
        try:
            yield
        finally:
            if runner:
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner
            await client.close()
            store.close()

    app = FastAPI(title="P2 OCR Task Service", version="0.3.0", lifespan=lifespan)
    app.openapi = lambda: {
        **SPEC,
        "paths": {
            p: {**v, "servers": [{"url": "/", "description": "当前 P2 服务地址"}]}
            for p, v in SPEC["paths"].items()
            if p.startswith(PREFIX)
        },
    }

    def response(schema, value, status=200):
        return JSONResponse(validate(schema, value), status_code=status)

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return response("Error", exc.body, exc.status)

    @app.exception_handler(DependencyUnavailable)
    @app.exception_handler(P1Rejected)
    async def dependency_error(request, exc):
        return response(
            "Error",
            error("DEPENDENCY_UNAVAILABLE", "P1 依赖不可用，请核对连接、鉴权及契约", True),
            503,
        )

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        return response("Error", error("INVALID_REQUEST", "请求结构不符合契约"), 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return response(
            "Error",
            error(
                "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST",
                "路由不存在" if exc.status_code == 404 else "HTTP 请求无效",
            ),
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return response(
            "Error", error("INTERNAL_ERROR", "P2 内部错误；写请求请保留原 ID 对账", True), 500
        )

    async def authorize(request: Request):
        pieces = request.headers.get("authorization", "").split()
        token = pieces[1] if len(pieces) == 2 and pieces[0].lower() == "bearer" else ""
        if secrets.compare_digest(token.encode(), settings.demo_token.encode()):
            return
        if secrets.compare_digest(token.encode(), settings.task_token.encode()):
            raise APIError(403, "FORBIDDEN", "此接口需要 DEMO_TOKEN")
        raise APIError(401, "UNAUTHORIZED", "缺少或无效的演示令牌")

    @app.get(PREFIX + "/healthz")
    async def health():
        return response(
            "Health",
            {
                "service": "p2_task",
                "contract_version": "0.3.0",
                "status": "ok",
                "server_time": now(),
            },
        )

    auth = [Depends(authorize)]

    @app.get(PREFIX + "/task-types", dependencies=auth)
    async def catalog(request: Request):
        return response("TypeCatalog", await request.app.state.service.catalog())

    @app.post(PREFIX + "/jobs", dependencies=auth)
    async def create_job(request: Request):
        try:
            payload = await request.json()
        except (ValueError, UnicodeError) as exc:
            raise APIError(422, "INVALID_REQUEST", "请求必须为合法 JSON") from exc
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("task_type"), str)
            and payload["task_type"] not in {"ocr.image.v1", "asr.file.v1"}
        ):
            raise APIError(422, "UNSUPPORTED_TASK_TYPE", "未知任务类型")
        validate_request("JobCreateRequest", payload)
        job, created = await request.app.state.service.create(payload)
        return response("Job", job, 201 if created else 200)

    @app.get(PREFIX + "/jobs/{job_id}", dependencies=auth)
    async def get_job(job_id: str, request: Request):
        return response("Job", request.app.state.service.store.get(job_id)["job"])

    @app.post(PREFIX + "/jobs/{job_id}/cancel", dependencies=auth)
    async def cancel_job(job_id: str, request: Request):
        job, status = request.app.state.service.cancel(job_id)
        return response("Job", job, status)

    @app.get(PREFIX + "/jobs/{job_id}/results", dependencies=auth)
    async def get_results(job_id: str, request: Request):
        return response("JobResults", results(request.app.state.service.store.get(job_id)))

    @app.get(PREFIX + "/jobs/{job_id}/download", dependencies=auth)
    async def download(job_id: str, request: Request):
        data = archive(request.app.state.service.store.get(job_id))
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.zip"'},
        )

    return app
