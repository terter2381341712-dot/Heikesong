"""执行生命周期：幂等、业务拒绝持久化、领取、完成、取消、晚到拒收。"""
import uuid

from tests.resource.conftest import (
    DEMO,
    READY_CAP,
    TASK,
    png_bytes,
    register_node,
    upload_png,
)


def ready_node(client, digest=None, worker_id=None):
    cap = dict(READY_CAP)
    if digest is not None:
        cap["profile_digest"] = digest
        cap["verified"] = True
    return register_node(client, capabilities=[cap], worker_id=worker_id)


def make_execution_request(node, artifact_id, digest, execution_id=None, **overrides):
    body = {
        "execution_id": execution_id or str(uuid.uuid4()),
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


def setup_ready(client_with_profile):
    client, digest = client_with_profile
    node = ready_node(client, digest=digest)
    artifact = upload_png(client).json()
    return client, digest, node, artifact


def test_demo_forbidden_on_executions(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    resp = client.post(
        "/api/resource/v1/executions",
        json=make_execution_request(node, artifact["artifact_id"], digest),
        headers=DEMO,
    )
    assert resp.status_code == 403


def test_preparation_requires_task_token(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    resp = client.post(
        "/api/resource/v1/preparations",
        json={
            "preparation_id": str(uuid.uuid4()),
            "worker_id": node["worker_id"],
            "worker_session_id": node["session_id"],
            "task_type": "ocr.image.v1",
            "profile_id": "ocr.default.v1",
            "profile_digest": digest,
        },
        headers=DEMO,
    )
    assert resp.status_code == 403


def test_execution_unconfigured_profile_rejected(client):
    """未发布 profile（digest 不匹配目录）→ PROFILE_MISMATCH 持久 REJECTED。"""
    node = ready_node(client)
    artifact = upload_png(client).json()
    body = make_execution_request(node, artifact["artifact_id"], "0" * 64)
    resp = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert resp.status_code == 409
    assert resp.json()["code"] == "PROFILE_MISMATCH"
    assert resp.json()["details"]["execution_id"] == body["execution_id"]
    got = client.get(f"/api/resource/v1/executions/{body['execution_id']}", headers=TASK)
    assert got.status_code == 200
    assert got.json()["status"] == "REJECTED"
    # 同 ID 重放仍 409，返回原拒绝
    again = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert again.status_code == 409
    assert again.json()["code"] == "PROFILE_MISMATCH"


def test_execution_replay_and_conflict(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, artifact["artifact_id"], digest)
    first = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert first.status_code == 201
    assert first.json()["status"] == "ACCEPTED"
    assert first.json()["hard_deadline_at"].endswith("Z")
    # 同 ID 同载荷重放 → 200 当前快照
    replay = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert replay.status_code == 200
    assert replay.json()["execution_id"] == body["execution_id"]
    # 同 ID 不同载荷 → 409 IDEMPOTENCY_CONFLICT
    modified = dict(body, timeout_seconds=60)
    conflict = client.post("/api/resource/v1/executions", json=modified, headers=TASK)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_busy_rejected_and_persisted(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body1 = make_execution_request(node, artifact["artifact_id"], digest)
    assert client.post("/api/resource/v1/executions", json=body1, headers=TASK).status_code == 201
    # 第二个执行：槽位被占 → RESOURCE_BUSY 持久 REJECTED
    body2 = make_execution_request(node, artifact["artifact_id"], digest)
    r2 = client.post("/api/resource/v1/executions", json=body2, headers=TASK)
    assert r2.status_code == 409
    assert r2.json()["code"] == "RESOURCE_BUSY"
    got = client.get(f"/api/resource/v1/executions/{body2['execution_id']}", headers=TASK)
    assert got.json()["status"] == "REJECTED"
    # REJECTED 不占槽：取消第一个后可重新申请同一 body2？——不，被拒 ID 永不复活；
    # 用新 ID 应可接受
    body3 = make_execution_request(node, artifact["artifact_id"], digest)
    client.post(
        f"/api/resource/v1/executions/{body1['execution_id']}/cancel", headers=TASK
    )
    # cancel 后进入 CANCEL_REQUESTED，仍占槽直到节点确认
    r3 = client.post("/api/resource/v1/executions", json=body3, headers=TASK)
    assert r3.status_code == 409
    assert r3.json()["code"] == "RESOURCE_BUSY"


def test_missing_input_persisted_rejected(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, "missing0000", digest)
    resp = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert resp.status_code == 409
    assert resp.json()["code"] == "NOT_FOUND"
    got = client.get(f"/api/resource/v1/executions/{body['execution_id']}", headers=TASK)
    assert got.json()["status"] == "REJECTED"


def test_input_type_mismatch_rejected(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(
        node, artifact["artifact_id"], digest, task_type="asr.file.v1"
    )
    resp = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert resp.status_code == 409
    assert resp.json()["code"] == "INPUT_INVALID"


def test_parameter_unsupported_rejected(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(
        node, artifact["artifact_id"], digest, parameters={"language": "fr"}
    )
    resp = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    # fr 不在 schema 枚举里 → 422；schema 枚举内但不被 profile 支持才是 PARAMETER_UNSUPPORTED
    assert resp.status_code == 422


def test_schema_error_no_record(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, artifact["artifact_id"], digest)
    body["attempt_no"] = 9
    resp = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_REQUEST"
    got = client.get(f"/api/resource/v1/executions/{body['execution_id']}", headers=TASK)
    assert got.status_code == 404


def test_session_changed_rejected(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(
        node, artifact["artifact_id"], digest, worker_session_id=str(uuid.uuid4())
    )
    resp = client.post("/api/resource/v1/executions", json=body, headers=TASK)
    assert resp.status_code == 409
    assert resp.json()["code"] == "SESSION_CHANGED"


def test_claim_run_complete_download(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, artifact["artifact_id"], digest)
    assert client.post("/api/resource/v1/executions", json=body, headers=TASK).status_code == 201

    # 节点领取
    claim = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/claim",
        json={"session_id": node["session_id"]},
    )
    assert claim.status_code == 200
    execution = claim.json()["execution"]
    assert execution["execution_id"] == body["execution_id"]
    assert execution["input_download_path"].endswith("/content")

    # 节点下载输入（会话凭证）
    download = client.get(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/artifacts/{artifact['artifact_id']}/content",
        headers={"Authorization": f"Bearer {node['session_id']}"},
    )
    assert download.status_code == 200
    assert download.content == png_bytes()

    # 完成：合法 OCRResult
    result = {
        "schema_version": "1.0",
        "task_type": "ocr.image.v1",
        "input_artifact_id": artifact["artifact_id"],
        "image_width": 64,
        "image_height": 32,
        "text": "你好世界",
        "blocks": [
            {"text": "你好世界", "bbox": [0, 0, 10, 10], "confidence": 0.9}
        ],
    }
    complete = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{body['execution_id']}/complete",
        json={
            "session_id": node["session_id"],
            "outcome": "SUCCEEDED",
            "result": result,
            "timings": {
                "download_ms": 1.0,
                "inference_ms": 2.0,
                "postprocess_ms": 0.5,
                "write_output_ms": 0.2,
                "total_worker_ms": 3.7,
            },
        },
    )
    assert complete.status_code == 200, complete.text
    final = complete.json()
    assert final["status"] == "SUCCEEDED"
    assert final["result_artifact"]["kind"] == "RESULT"
    assert final["timings"]["total_worker_ms"] == 3.7

    # 结果可下载且字节一致
    content = client.get(
        f"/api/resource/v1/artifacts/{final['result_artifact']['artifact_id']}/content",
        headers=TASK,
    )
    assert content.status_code == 200
    assert "你好世界".encode() in content.content

    # 终态后重复提交 → STALE 拒收
    stale = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{body['execution_id']}/complete",
        json={"session_id": node["session_id"], "outcome": "SUCCEEDED", "result": result},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "STALE_EXECUTION"


def test_invalid_result_rejected_not_published(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, artifact["artifact_id"], digest)
    client.post("/api/resource/v1/executions", json=body, headers=TASK)
    client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/claim",
        json={"session_id": node["session_id"]},
    )
    bad_result = {
        "schema_version": "1.0",
        "task_type": "ocr.image.v1",
        "input_artifact_id": artifact["artifact_id"],
        "image_width": 64,
        "image_height": 32,
        "text": 123,  # 类型错误
        "blocks": [],
    }
    complete = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{body['execution_id']}/complete",
        json={"session_id": node["session_id"], "outcome": "SUCCEEDED", "result": bad_result},
    )
    assert complete.status_code == 409
    assert complete.json()["code"] == "RESULT_INVALID"
    got = client.get(f"/api/resource/v1/executions/{body['execution_id']}", headers=TASK)
    assert got.json()["status"] == "RUNNING"  # 未发布，保持 RUNNING


def test_cancel_flow(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, artifact["artifact_id"], digest)
    client.post("/api/resource/v1/executions", json=body, headers=TASK)
    cancel = client.post(
        f"/api/resource/v1/executions/{body['execution_id']}/cancel", headers=TASK
    )
    assert cancel.status_code == 202
    assert cancel.json()["status"] == "CANCEL_REQUESTED"
    # 取消后提交结果 → 拒收
    result = {
        "schema_version": "1.0",
        "task_type": "ocr.image.v1",
        "input_artifact_id": artifact["artifact_id"],
        "image_width": 64,
        "image_height": 32,
        "text": "",
        "blocks": [],
    }
    submit = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{body['execution_id']}/complete",
        json={"session_id": node["session_id"], "outcome": "SUCCEEDED", "result": result},
    )
    assert submit.status_code == 409
    assert submit.json()["code"] == "STALE_EXECUTION"
    # 节点确认停止 → CANCELLED，槽位释放
    ack = client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{body['execution_id']}/cancel_ack",
        json={"session_id": node["session_id"]},
    )
    assert ack.status_code == 200
    assert ack.json()["status"] == "CANCELLED"
    # 终态重复 cancel → 200 原记录
    again = client.post(
        f"/api/resource/v1/executions/{body['execution_id']}/cancel", headers=TASK
    )
    assert again.status_code == 200
    assert again.json()["status"] == "CANCELLED"
    # 槽位已释放：新执行可接受
    body2 = make_execution_request(node, artifact["artifact_id"], digest)
    r2 = client.post("/api/resource/v1/executions", json=body2, headers=TASK)
    assert r2.status_code == 201


def test_events_record_execution_transitions(client_with_profile):
    client, digest, node, artifact = setup_ready(client_with_profile)
    body = make_execution_request(node, artifact["artifact_id"], digest)
    client.post("/api/resource/v1/executions", json=body, headers=TASK)
    client.post(
        f"/api/resource/internal/v1/nodes/{node['worker_id']}/claim",
        json={"session_id": node["session_id"]},
    )
    events = client.get("/api/resource/v1/events", headers=DEMO).json()["events"]
    types = [e["type"] for e in events]
    assert "execution.accepted" in types
    assert "execution.running" in types
    # 游标分页
    page = client.get(
        f"/api/resource/v1/events?after={events[0]['event_id']}&limit=1", headers=DEMO
    ).json()
    assert page["next_after"] == events[1]["event_id"]
    assert page["has_more"] in (True, False)
