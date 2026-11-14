import hashlib
import json

import httpx
from jsonschema.exceptions import ValidationError

from task_service.contract import validate

PREFIX = "/api/resource/v1"


class DependencyUnavailable(Exception):
    """Transport, authentication or protocol problem; never infer execution failure."""


class P1Rejected(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body


class InvalidResult(Exception):
    pass


class P1Client:
    def __init__(self, settings, transport=None):
        self.http = httpx.AsyncClient(
            base_url=settings.resource_base_url,
            headers={"Authorization": f"Bearer {settings.task_token}"},
            timeout=httpx.Timeout(10, connect=2),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    async def json(self, method, path, schema, payload=None):
        try:
            response = await self.http.request(method, PREFIX + path, json=payload)
            if response.status_code >= 500 or response.status_code in {401, 403}:
                raise DependencyUnavailable("P1 不可用或服务端鉴权配置不匹配")
            if response.is_error:
                body = validate("Error", response.json())
                raise P1Rejected(response.status_code, body)
            if response.status_code not in {200, 201, 202}:
                raise DependencyUnavailable("P1 返回未约定的 HTTP 状态")
            return validate(schema, response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise DependencyUnavailable("P1 网络异常或响应不符合 0.3.0 契约") from exc

    async def catalog(self):
        return await self.json("GET", "/task-types", "TypeCatalog")

    async def resources(self):
        return await self.json("GET", "/resources", "ResourceSnapshot")

    async def artifact(self, artifact_id):
        artifact = await self.json("GET", f"/artifacts/{artifact_id}", "Artifact")
        if artifact["artifact_id"] != artifact_id:
            raise DependencyUnavailable("P1 文件元信息与请求 ID 不匹配")
        return artifact

    async def reconcile(self, kind, request):
        schema, id_field = (
            ("Execution", "execution_id")
            if kind == "executions"
            else ("Preparation", "preparation_id")
        )
        path = f"/{kind}/{request[id_field]}"
        try:
            return await self.json("GET", path, schema)
        except P1Rejected as exc:
            if exc.status != 404:
                raise DependencyUnavailable("P1 无法对账原请求") from exc
        # A GET 404 is NOT proof the original POST cannot arrive. Replay only.
        try:
            return await self.json("POST", f"/{kind}", schema, request)
        except P1Rejected as exc:
            if exc.status == 409 and exc.body["details"].get(id_field) == request[id_field]:
                try:
                    return await self.json("GET", path, schema)
                except P1Rejected as missing:
                    raise DependencyUnavailable("P1 拒绝记录尚不可对账，保留原 ID") from missing
            raise DependencyUnavailable("P1 拒绝请求但未提供可对账记录；请核对契约") from exc

    async def cancel(self, execution_id):
        try:
            return await self.json("POST", f"/executions/{execution_id}/cancel", "Execution")
        except P1Rejected as exc:
            raise DependencyUnavailable("取消结果尚未确定，继续查询原执行") from exc

    async def result(self, artifact):
        if artifact["kind"] != "RESULT" or artifact["content_type"] != "application/json":
            raise InvalidResult("结果 artifact 必须为 RESULT/application/json")
        path = PREFIX + f"/artifacts/{artifact['artifact_id']}/content"
        try:
            async with self.http.stream(
                "GET", path, timeout=httpx.Timeout(60, connect=2)
            ) as response:
                if response.status_code != 200:
                    raise DependencyUnavailable("结果待取回：P1 文件暂不可读")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > artifact["size_bytes"]:
                        raise InvalidResult("结果字节数超过 artifact 声明")
                    chunks.append(chunk)
            raw = b"".join(chunks)
        except httpx.HTTPError as exc:
            raise DependencyUnavailable("结果待取回：文件传输中断") from exc
        if (
            len(raw) != artifact["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != artifact["sha256"]
        ):
            raise InvalidResult("结果长度或 SHA-256 不匹配")
        try:

            def reject_constant(value):
                raise ValueError(value)

            return validate("OCRResult", json.loads(raw, parse_constant=reject_constant))
        except (ValueError, UnicodeError, ValidationError) as exc:
            raise InvalidResult("结果不是合法 OCRResult") from exc
