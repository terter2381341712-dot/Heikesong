"""artifact 上传/下载：原始字节、幂等、媒体探测、角色限制。"""
import uuid

import pytest

from tests.resource.conftest import DEMO, TASK, png_bytes, upload_png, wav_bytes


def test_upload_png_returns_media_info(client):
    resp = upload_png(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "INPUT"
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(png_bytes())
    assert len(body["sha256"]) == 64
    assert body["media_info"]["width"] == 64
    assert body["media_info"]["height"] == 32
    assert body["download_path"].startswith("/api/resource/v1/artifacts/")
    assert body["download_path"].endswith("/content")


def test_upload_replay_same_bytes_200(client):
    first = upload_png(client)
    second = upload_png(client)  # conftest 每次生成新 upload_id？
    # 同 upload_id 同内容重放
    upload_id = first.json()["artifact_id"]  # 仅用于确保第一次成功
    fixed_id = str(uuid.uuid4())
    r1 = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": fixed_id},
        files={"file": ("a.png", png_bytes(), "image/png")},
    )
    r2 = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": fixed_id},
        files={"file": ("a.png", png_bytes(), "image/png")},
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["artifact_id"] == r2.json()["artifact_id"]


def test_upload_same_id_different_bytes_409(client):
    fixed_id = str(uuid.uuid4())
    r1 = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": fixed_id},
        files={"file": ("a.png", png_bytes(color=(0, 0, 255)), "image/png")},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": fixed_id},
        files={"file": ("a.png", png_bytes(color=(255, 255, 0)), "image/png")},
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_download_bytes_match(client):
    content = png_bytes()
    artifact = upload_png(client, content=content).json()
    resp = client.get(
        f"/api/resource/v1/artifacts/{artifact['artifact_id']}/content", headers=DEMO
    )
    assert resp.status_code == 200
    assert resp.content == content


def test_demo_cannot_upload_json(client):
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": str(uuid.uuid4())},
        files={"file": ("r.json", b"{}", "application/json")},
    )
    assert resp.status_code == 403


def test_task_can_upload_json(client):
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=TASK,
        data={"upload_id": str(uuid.uuid4())},
        files={"file": ("r.json", b"{}", "application/json")},
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "RESULT"


def test_wav_media_info(client):
    content = wav_bytes(seconds=1.0, rate=8000)
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": str(uuid.uuid4())},
        files={"file": ("a.wav", content, "audio/wav")},
    )
    assert resp.status_code == 201
    media = resp.json()["media_info"]
    assert media["duration_ms"] == 1000
    assert media["sample_rate_hz"] == 8000
    assert media["channels"] == 1
    assert media["audio_encoding"] == "PCM_S16LE"


def test_wav_too_long_413(client):
    content = wav_bytes(seconds=61.0, rate=8000)
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": str(uuid.uuid4())},
        files={"file": ("long.wav", content, "audio/wav")},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "INPUT_TOO_LARGE"


def test_undecodable_image_422(client):
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": str(uuid.uuid4())},
        files={"file": ("fake.png", b"not-a-png", "image/png")},
    )
    assert resp.status_code == 422


def test_unsupported_media_415(client):
    resp = client.post(
        "/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": str(uuid.uuid4())},
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 415


def test_get_missing_artifact_404(client):
    resp = client.get("/api/resource/v1/artifacts/does-not-exist", headers=DEMO)
    assert resp.status_code == 404
