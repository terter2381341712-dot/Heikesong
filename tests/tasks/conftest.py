import httpx
import pytest_asyncio

from task_service.app import PREFIX, create_app
from task_service.config import Settings
from task_service.contract import JOB_TERMINAL, uid, validate
from tests.tasks.mock_p1 import DIGEST, MockP1


class Rig:
    def __init__(self, tmp_path):
        self.mock = MockP1()
        self.time = 1000.0
        self.settings = Settings(
            "http://mock-p1", self.mock.task_token, self.mock.demo_token, tmp_path
        )

    async def start(self):
        self.app = create_app(
            self.settings,
            transport=httpx.ASGITransport(app=self.mock.app),
            background=False,
            clock=lambda: self.time,
        )
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.service = self.app.state.service
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://p2",
            headers={"Authorization": f"Bearer {self.settings.demo_token}"},
        )

    async def close(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def restart(self):
        await self.close()
        await self.start()

    def payload(self, ids=None):
        return {
            "client_request_id": uid(),
            "task_type": "ocr.image.v1",
            "profile_id": "ocr.default.v1",
            "profile_digest": DIGEST,
            "input_artifact_ids": ids or ["mock_image_1", "mock_image_2", "mock_image_3"],
            "parameters": {"language": "auto"},
        }

    async def request(self, method, suffix, *, status=200, schema="Job", **kwargs):
        response = await self.client.request(method, PREFIX + suffix, **kwargs)
        assert response.status_code == status, response.text
        if schema:
            validate("Error" if status >= 400 else schema, response.json())
        return response

    async def create(self, ids=None):
        return (await self.request("POST", "/jobs", status=201, json=self.payload(ids))).json()

    async def job(self, job_id):
        return (await self.request("GET", f"/jobs/{job_id}")).json()

    async def tick(self, advance=1):
        self.time += advance
        return await self.service.tick()

    async def finish(self, job_id, limit=40):
        for _ in range(limit):
            job = await self.job(job_id)
            if job["status"] in JOB_TERMINAL:
                return job
            await self.tick()
        raise AssertionError(f"Job did not finish: {await self.job(job_id)}")


@pytest_asyncio.fixture
async def rig(tmp_path):
    instance = Rig(tmp_path)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()
