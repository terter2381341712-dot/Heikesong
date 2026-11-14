"""Unified plan 1.0: P2 protocol checks, with synthetic P1 responses only."""

import copy
import io
import json
import zipfile

import pytest

from task_service.contract import uid, validate
from tests.tasks.mock_p1 import CAPABILITIES, DIGEST


def set_capability(rig, name="not_prepared_null"):
    rig.mock.nodes = rig.mock.nodes[:1]
    node = rig.mock.nodes[0]
    node["capabilities"] = [copy.deepcopy(CAPABILITIES[name])]
    return node


def test_shared_capabilities_follow_unmodified_schema():
    for capability in CAPABILITIES.values():
        validate("Capability", capability)


@pytest.mark.parametrize("shape", ["not_prepared_null", "not_prepared_matching"])
async def test_L03_cold_node_prepares_frozen_target_then_executes_and_downloads(
    rig, shape, monkeypatch
):
    node = set_capability(rig, shape)
    rig.mock.hold_preparation = True
    job = await rig.create(["mock_image_1"])
    original_json = rig.service.p1.json

    async def check_persisted_before_send(method, path, schema, payload=None):
        if method == "POST" and path == "/preparations":
            validate("PreparationRequest", payload)
            assert any(
                record["request"] == payload for record in rig.service.store.preparations().values()
            )
        return await original_json(method, path, schema, payload)

    monkeypatch.setattr(rig.service.p1, "json", check_persisted_before_send)
    # A later catalog change must not replace the target already frozen in this Job.
    rig.mock.descriptor["profile_digest"] = "b" * 64
    assert await rig.tick()
    assert len(rig.mock.preparation_posts) == 1
    request = rig.mock.preparation_posts[0]
    assert request == {
        "preparation_id": request["preparation_id"],
        "worker_id": node["worker_id"],
        "worker_session_id": node["session_id"],
        **{k: job[k] for k in ("task_type", "profile_id", "profile_digest")},
    }
    assert request["profile_digest"] == DIGEST
    rig.mock.descriptor["profile_digest"] = DIGEST
    for _ in range(2):
        assert await rig.tick()
        waiting = (await rig.job(job["job_id"]))["tasks"][0]
        assert waiting["status"] == "WAITING_FOR_RESOURCES"
        assert "准备" in waiting["waiting_reason"]
        assert waiting["attempts_used"] == 0 and waiting["dispatches"] == []
        assert waiting["current_execution_id"] is None
    await rig.restart()
    assert await rig.tick()
    assert rig.mock.preparation_posts == [request] and not rig.mock.posts
    rig.mock.hold_preparation = False
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED" and final["profile_digest"] == DIGEST
    assert final["tasks"][0]["attempts_used"] == 1
    assert len(rig.mock.posts) == 1
    execution = validate("ExecutionRequest", rig.mock.posts[0])
    assert execution["worker_id"] == request["worker_id"]
    assert execution["worker_session_id"] == request["worker_session_id"]
    assert execution["profile_digest"] == request["profile_digest"]
    assert execution["input_artifact_id"] == "mock_image_1" and execution["attempt_no"] == 1
    assert execution["timeout_seconds"] == rig.mock.descriptor["default_timeout_seconds"]
    rig.mock.unreachable = True
    path = f"/jobs/{job['job_id']}"
    result = (await rig.request("GET", path + "/results", schema="JobResults")).json()
    assert result["items"][0]["result"]["input_artifact_id"] == "mock_image_1"
    response = await rig.request("GET", path + "/download", schema=None)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("results.json")) == result


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"profile_digest": None}, "摘要"),
        ({"profile_digest": "b" * 64}, "摘要不匹配"),
        ({"verified": False}, "verified"),
        ({"calibration": None}, "自测"),
        ({"calibration": {**CAPABILITIES["ready"]["calibration"], "passed": False}}, "自测"),
        ({"backend": None}, "后端"),
        ({"runtime_provider": None}, "后端"),
        ({"runtime_provider": ""}, "后端"),
        ({"model_sha256": None}, "核验证据"),
    ],
)
async def test_L05_invalid_ready_waits_without_preparing_or_dispatching(rig, changes, reason):
    node = set_capability(rig, "ready")
    node["capabilities"][0].update(changes)
    job = await rig.create(["mock_image_1"])
    for _ in range(2):
        assert await rig.tick()
    snapshot = await rig.job(job["job_id"])
    task = snapshot["tasks"][0]
    assert task["status"] == "WAITING_FOR_RESOURCES" and reason in task["waiting_reason"]
    assert task["attempts_used"] == 0 and snapshot["profile_digest"] == DIGEST
    assert not rig.mock.posts and not rig.mock.preparation_posts
    node["capabilities"] = [copy.deepcopy(CAPABILITIES["ready"])]
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"


async def test_L05_cache_flag_does_not_replace_ready_evidence(rig):
    node = set_capability(rig, "ready")
    node["capabilities"][0]["model_cached"] = False
    job = await rig.create(["mock_image_1"])
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    assert not rig.mock.preparation_posts


@pytest.mark.parametrize(
    "shape,changes,reason",
    [
        ("not_prepared_null", {"profile_digest": "b" * 64}, "摘要不匹配"),
        ("not_prepared_null", {"verified": True}, "verified"),
        ("not_prepared_null", {"profile_id": "other.profile"}, "未发布"),
        ("not_prepared_null", {"task_type": "asr.file.v1"}, "未发布"),
        ("preparing", {}, "准备中"),
        ("error", {}, "MOCK 自测失败"),
        ("unavailable", {}, "MOCK 执行器不兼容"),
    ],
)
async def test_L04_capability_wait_reasons_and_recovery(rig, shape, changes, reason):
    node = set_capability(rig, shape)
    node["capabilities"][0].update(changes)
    job = await rig.create(["mock_image_1"])
    for _ in range(3):
        assert await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["status"] == "WAITING_FOR_RESOURCES"
    assert reason in snapshot["tasks"][0]["waiting_reason"]
    assert snapshot["profile_digest"] == DIGEST and snapshot["tasks"][0]["attempts_used"] == 0
    assert not rig.mock.posts and not rig.mock.preparation_posts
    node["capabilities"] = [copy.deepcopy(CAPABILITIES["not_prepared_null"])]
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"connection": "OFFLINE", "available_slots": 0}, "离线"),
        ({"mode": "PAUSED", "available_slots": 0}, "暂停"),
        ({"mode": "DRAINING", "available_slots": 0}, "DRAINING"),
        ({"cleanup_pending": True, "available_slots": 0}, "清理"),
        ({"available_slots": 0, "reserved_slots": 1}, "占槽"),
        ({"active_execution_ids": [uid()]}, "执行占槽"),
        ({"active_preparation_id": uid()}, "准备中"),
        ({"reserved_slots": 1}, "占槽"),
    ],
)
async def test_L04_slot_wait_reasons_and_recovery(rig, changes, reason):
    node = set_capability(rig)
    original = copy.deepcopy(node)
    node.update(changes)
    job = await rig.create(["mock_image_1"])
    assert await rig.tick()
    task = (await rig.job(job["job_id"]))["tasks"][0]
    assert task["status"] == "WAITING_FOR_RESOURCES" and reason in task["waiting_reason"]
    assert not rig.mock.posts and not rig.mock.preparation_posts
    node.update(original)
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("missing", ["nodes", "capabilities"])
async def test_L04_missing_resources_do_not_infer_capability_from_hardware(rig, missing):
    node = set_capability(rig)
    if missing == "nodes":
        rig.mock.nodes.clear()
    else:
        node["capabilities"].clear()
    job = await rig.create(["mock_image_1"])
    assert await rig.tick()
    task = (await rig.job(job["job_id"]))["tasks"][0]
    assert ("没有资源节点" if missing == "nodes" else "未发布") in task["waiting_reason"]
    assert not rig.mock.posts and not rig.mock.preparation_posts


async def test_L04_other_ready_node_can_execute_while_first_reports_error(rig):
    rig.mock.nodes[0]["capabilities"] = [copy.deepcopy(CAPABILITIES["error"])]
    job = await rig.create(["mock_image_1"])
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert rig.mock.posts[0]["worker_id"] == rig.mock.nodes[1]["worker_id"]
    assert not rig.mock.preparation_posts


async def test_L04_preparation_shared_slot_across_jobs_and_restart(rig):
    for node in rig.mock.nodes:
        node["capabilities"] = [copy.deepcopy(CAPABILITIES["not_prepared_null"])]
    rig.mock.hold_preparation = True
    jobs = [await rig.create(), await rig.create()]
    for _ in range(2):
        assert await rig.tick()
    await rig.restart()
    assert await rig.tick()
    assert len(rig.mock.preparation_posts) == 2 and not rig.mock.posts
    for job in jobs:
        snapshot = await rig.job(job["job_id"])
        assert all("准备" in task["waiting_reason"] for task in snapshot["tasks"])
        assert all(task["attempts_used"] == 0 for task in snapshot["tasks"])
    rig.mock.hold_preparation = False
    for job in jobs:
        assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    assert len(rig.mock.preparation_posts) == 2


@pytest.mark.parametrize("before_accept", [False, True])
async def test_L08_preparation_unknown_then_404_replays_same_id_after_restart(rig, before_accept):
    set_capability(rig)
    if before_accept:
        rig.mock.preparation_before_accept_503 = 1
    else:
        rig.mock.drop_preparation_responses = 1
    job = await rig.create(["mock_image_1"])
    assert not await rig.tick()
    request = copy.deepcopy(rig.mock.preparation_posts[0])
    persisted = next(iter(rig.service.store.preparations().values()))
    assert persisted["status"] == "UNKNOWN" and persisted["request"] == request
    assert (await rig.job(job["job_id"]))["tasks"][0]["attempts_used"] == 0
    await rig.restart()
    rig.mock.preparation_get_404 = 1
    assert await rig.tick()
    assert rig.mock.preparation_posts == [request, request]
    assert not rig.mock.posts
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED" and final["tasks"][0]["attempts_used"] == 1
    assert len(rig.mock.preparations) == 1 and len(rig.mock.executions) == 1


@pytest.mark.parametrize(
    "status,code,retryable", [(409, "RESOURCE_BUSY", True), (503, "PROFILE_UNCONFIGURED", False)]
)
async def test_L08_preparation_rejection_is_reconciled_before_any_new_id(
    rig, status, code, retryable
):
    set_capability(rig)
    rig.mock.preparation_rejections = [(status, code, retryable)]
    job = await rig.create(["mock_image_1"])
    assert await rig.tick() is (status != 503)
    request = copy.deepcopy(rig.mock.preparation_posts[0])
    assert await rig.tick(advance=0)
    record = next(iter(rig.service.store.preparations().values()))
    assert record["status"] == "REJECTED" and record["error"]["code"] == code
    assert await rig.tick(advance=0.99)
    assert rig.mock.preparation_posts == [request]
    task = (await rig.job(job["job_id"]))["tasks"][0]
    assert task["attempts_used"] == 0 and code in task["waiting_reason"]
    assert not rig.mock.posts
    assert await rig.tick(advance=0.02 if retryable else 10)
    if retryable:
        new_request = rig.mock.preparation_posts[1]
        assert new_request["preparation_id"] != request["preparation_id"]
        assert {**new_request, "preparation_id": request["preparation_id"]} == request
        assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    else:
        assert rig.mock.preparation_posts == [request] and not rig.mock.posts


@pytest.mark.parametrize("recovery", ["new_session", "ready"])
async def test_L04_failed_preparation_waits_for_repair_or_new_session(rig, recovery):
    node = set_capability(rig)
    rig.mock.preparation_failure = True
    job = await rig.create(["mock_image_1"])
    assert await rig.tick()
    assert await rig.tick()
    node["capabilities"] = [copy.deepcopy(CAPABILITIES["not_prepared_null"])]
    for _ in range(3):
        assert await rig.tick()
    task = (await rig.job(job["job_id"]))["tasks"][0]
    assert "MODEL_LOAD_FAILED" in task["waiting_reason"] and task["attempts_used"] == 0
    assert len(rig.mock.preparation_posts) == 1 and not rig.mock.posts
    rig.mock.preparation_failure = False
    if recovery == "new_session":
        node["session_id"] = uid()
    else:
        node["capabilities"] = [copy.deepcopy(CAPABILITIES["ready"])]
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    assert len(rig.mock.preparation_posts) == (2 if recovery == "new_session" else 1)


async def test_L08_mismatched_preparation_response_keeps_original_request(rig, monkeypatch):
    set_capability(rig)
    original = rig.service.p1.reconcile

    async def wrong_identity(kind, request):
        result = copy.deepcopy(await original(kind, request))
        result["worker_session_id"] = uid()
        return result

    monkeypatch.setattr(rig.service.p1, "reconcile", wrong_identity)
    job = await rig.create(["mock_image_1"])
    assert not await rig.tick()
    persisted = next(iter(rig.service.store.preparations().values()))
    assert persisted["status"] == "UNKNOWN"
    assert persisted["request"] == rig.mock.preparation_posts[0] and not rig.mock.posts
    monkeypatch.setattr(rig.service.p1, "reconcile", original)
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    assert len(rig.mock.preparation_posts) == 1
