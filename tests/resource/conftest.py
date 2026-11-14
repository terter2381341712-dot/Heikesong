"""测试公共设施：临时数据目录的应用实例与鉴权头。"""
from __future__ import annotations

import io
import uuid
import wave

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from resource_service.config import Config
from resource_service.service import create_app

DEMO = {"Authorization": "Bearer demo-t"}
TASK = {"Authorization": "Bearer task-t"}
JOIN = {"Authorization": "Bearer join-t"}


@pytest.fixture()
def client(tmp_path):
    cfg = Config(
        bind_host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        demo_token="demo-t",
        task_token="task-t",
        join_token="join-t",
        profiles_dir=tmp_path / "data" / "profiles",
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_with_profile(tmp_path):
    """已发布 ocr.default.v1 锁文件的应用；返回 (client, profile_digest)。"""
    import hashlib
    import json as jsonlib

    data_dir = tmp_path / "data"
    profiles_dir = data_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    lock_content = jsonlib.dumps(
        {
            "profile_id": "ocr.default.v1",
            "task_type": "ocr.image.v1",
            "model_sha256": "0" * 64,
            "allowed_backends": ["cpu"],
            "weights_license_verified": True,
        }
    ).encode("utf-8")
    (profiles_dir / "ocr.default.v1.lock.json").write_bytes(lock_content)
    digest = hashlib.sha256(lock_content).hexdigest()

    cfg = Config(
        bind_host="127.0.0.1",
        port=0,
        data_dir=data_dir,
        demo_token="demo-t",
        task_token="task-t",
        join_token="join-t",
        profiles_dir=profiles_dir,
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c, digest


def png_bytes(width: int = 64, height: int = 32, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    frames = int(rate * seconds)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


READY_CAP = {
    "task_type": "ocr.image.v1",
    "profile_id": "ocr.default.v1",
    "profile_digest": None,
    "state": "READY",
    "model_cached": False,
    "backend": "cpu",
    "runtime_provider": "devstub",
    "model_sha256": None,
    "verified": False,
    "calibration": None,
    "reason": None,
}


def register_node(client, name="test-node", capabilities=None, worker_id=None):
    body = {"name": name, "hardware": minimal_hardware()}
    if capabilities is not None:
        body["capabilities"] = capabilities
    if worker_id:
        body["worker_id"] = worker_id
    resp = client.post(
        "/api/resource/internal/v1/nodes/register", json=body, headers=JOIN
    )
    assert resp.status_code == 200, resp.text
    node = resp.json()
    for capability in capabilities or []:
        if capability["state"] != "READY" or not capability["profile_digest"]:
            continue
        prep_id = str(uuid.uuid4())
        assert client.post("/api/resource/v1/preparations", headers=TASK, json={
            "preparation_id": prep_id,
            "worker_id": node["worker_id"],
            "worker_session_id": node["session_id"],
            **{k: capability[k] for k in ("task_type", "profile_id", "profile_digest")},
        }).status_code == 202
        auth = {k: node[k] for k in ("session_id", "node_secret")}
        prefix = f"/api/resource/internal/v1/nodes/{node['worker_id']}"
        assert client.post(prefix + "/claim", json=auth).json()["preparation"]["preparation_id"] == prep_id
        prepared = client.post(f"{prefix}/preparations/{prep_id}/status", json={
            **auth,
            "status": "READY",
            "backend": "cpu",
            "runtime_provider": "CPUExecutionProvider",
            "model_sha256": "0" * 64,
            "calibration": {
                "sample_count": 3, "median_compute_ms": 100, "passed": True,
                "finished_at": "2026-11-14T04:30:00Z",
            },
        })
        assert prepared.json()["status"] == "READY", prepared.text
    return node


def minimal_hardware() -> dict:
    return {
        "os": "macOS 15.1",
        "arch": "arm64",
        "cpu_name": "Apple M4",
        "logical_cores": 10,
        "ram_total_bytes": 16 * 1024**3,
        "ram_available_bytes": 8 * 1024**3,
        "gpu_name": None,
        "gpu_vram_bytes": None,
        "gpu_info_source": "unavailable",
        "manual_labels": {},
        "detection_errors": [],
    }


def upload_png(client, content: bytes = None, name="sample.png", upload_id=None):
    content = content or png_bytes()
    upload_id = upload_id or str(__import__("uuid").uuid4())
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": upload_id},
        files={"file": (name, content, "image/png")},
    )
    return resp
