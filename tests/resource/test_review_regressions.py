"""PR #1 审查意见的回归测试（F01/F02/F07/F08/F09/F10 可静态验证部分）。"""
import uuid

from tests.resource.conftest import (
    DEMO,
    READY_CAP,
    TASK,
    minimal_hardware,
    png_bytes,
    register_node,
    upload_png,
)


def ready_node(client, digest):
    cap = dict(READY_CAP, profile_digest=digest, verified=True)
    return register_node(client, capabilities=[cap])


def exec_body(node, artifact_id, digest, **overrides):
    body = {
        "execution_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "attempt_no": 1,
        "worker_id": node["worker_id"],
        "worker_session_id": node["session_id"],
        "task_type": "ocr.image.v1",
        "profile_id": "ocr.default.v1",
        "profile_digest": digest,
        "input_artifact_id": artifact_id,
        "parameters": {"language": "zh"},
        "timeout_seconds": 120,
    }
    body.update(overrides)
    return body


def test_f01_result_artifact_has_full_media_info(client_with_profile):
    """F01：结果 Artifact 的 media_info 必须是六字段对象。"""
    client, digest = client_with_profile
    node = ready_node(client, digest)
    artifact = upload_png(client).json()
    body = exec_body(node, artifact["artifact_id"], digest)
    assert client.post("/api/resource/v1/executions", json=body, headers=TASK).status_code == 201
    client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/claim",
        json={"session_id": node["session_id"], "node_secret": node["node_secret"]},
    )
    result = {
        "schema_version": "1.0",
        "task_type": "ocr.image.v1",
        "input_artifact_id": artifact["artifact_id"],
        "image_width": 64,
        "image_height": 32,
        "text": "ok",
        "blocks": [],
    }
    done = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{body['execution_id']}/complete",
        json={
            "session_id": node["session_id"],
            "node_secret": node["node_secret"],
            "outcome": "SUCCEEDED",
            "result": result,
        },
    ).json()
    media = done["result_artifact"]["media_info"]
    assert set(media) == {
        "width",
        "height",
        "duration_ms",
        "sample_rate_hz",
        "channels",
        "audio_encoding",
    }
    assert all(v is None for v in media.values())


def test_f02_registration_publishes_not_prepared_capabilities(client_with_profile):
    """F02：注册后按目录出现 NOT_PREPARED 能力；心跳空 capabilities 不清空。"""
    client, digest = client_with_profile
    node = register_node(client)
    resources = client.get("/api/resource/v1/resources", headers=DEMO).json()["resources"]
    caps = resources[0]["capabilities"]
    assert any(c["task_type"] == "ocr.image.v1" and c["state"] == "NOT_PREPARED" for c in caps)
    # 心跳不带 capabilities，能力应保留
    hb = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/heartbeat",
        json={"session_id": node["session_id"], "node_secret": node["node_secret"]},
    )
    assert hb.status_code == 200
    caps_after = client.get("/api/resource/v1/resources", headers=DEMO).json()["resources"][0]["capabilities"]
    assert len(caps_after) == len(caps)


def test_f07_demo_token_cannot_impersonate_node(client_with_profile):
    """F07：拿到公开 session_id 也不能冒充节点（需 node_secret）。"""
    client, digest = client_with_profile
    node = ready_node(client, digest)
    # 只有公开 session_id，没有 secret → 拒绝
    resp = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/claim",
        json={"session_id": node["session_id"], "node_secret": "wrong-secret"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "SESSION_CHANGED"
    # 下载接口同样要求 secret
    dl = client.get(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/artifacts/x/content",
        headers={"Authorization": f"Bearer {node['session_id']}"},
    )
    assert dl.status_code == 409


def test_f08_preparation_idempotency_conflict(client_with_profile):
    """F08：同 preparation_id 不同载荷 → IDEMPOTENCY_CONFLICT。"""
    client, digest = client_with_profile
    node = ready_node(client, digest)
    prep_id = str(uuid.uuid4())
    base = {
        "preparation_id": prep_id,
        "worker_id": node["worker_id"],
        "worker_session_id": node["session_id"],
        "task_type": "ocr.image.v1",
        "profile_id": "ocr.default.v1",
        "profile_digest": digest,
    }
    first = client.post("/api/resource/v1/preparations", json=base, headers=TASK)
    assert first.status_code == 202
    replay = client.post("/api/resource/v1/preparations", json=base, headers=TASK)
    assert replay.status_code == 200
    changed = dict(base, profile_digest="a" * 64)
    conflict = client.post("/api/resource/v1/preparations", json=changed, headers=TASK)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_f09_offline_node_session_id_still_uuid(tmp_path):
    """F09：服务重启把旧会话置为 OFFLINE 后，公开 session_id 仍必须是合法 UUID。"""
    import re

    import json as jsonlib

    from fastapi.testclient import TestClient

    from resource_service.config import Config
    from resource_service.service import create_app

    data_dir = tmp_path / "data"
    profiles_dir = data_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        bind_host="127.0.0.1",
        port=0,
        data_dir=data_dir,
        demo_token="demo-t",
        task_token="task-t",
        join_token="join-t",
        profiles_dir=profiles_dir,
    )
    with TestClient(create_app(cfg)) as c:
        register_node(c, name="restart-node")

    # 重启：同一数据目录重建应用，_recover 会把节点置 OFFLINE 并轮换会话
    with TestClient(create_app(cfg)) as c:
        resources = c.get("/api/resource/v1/resources", headers=DEMO).json()["resources"]
        assert resources, "节点记录应保留"
        node = resources[0]
        assert node["connection"] == "OFFLINE"
        assert re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            node["session_id"],
        ), f"session_id 必须是 UUID，实际 {node['session_id']!r}"


def test_f10_calibration_not_passed_blocks_ready(client_with_profile):
    """F10：校准 passed=false 时节点程序不会上报 READY（服务端侧校验契约字段）。"""
    from node_agent.agent import Agent

    # 直接测试自测返回结构：非 OCR 内容（全白图）应 non_empty=0、passed=False
    agent = Agent.__new__(Agent)
    from pathlib import Path
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    agent.data_dir = tmp

    class BlankRunner:
        task_type = "ocr.image.v1"

        def execute(self, *a, **k):
            return {"blocks": [], "text": ""}

    out = Agent._self_test(agent, "ocr.image.v1", BlankRunner())
    assert out["non_empty"] == 0
    assert out["passed"] is False
