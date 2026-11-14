import asyncio
import copy
import io
import json
import zipfile

import pytest

from task_service.app import PREFIX
from task_service.contract import validate
from task_service.store import Store


async def test_health_catalog_and_roles(rig):
    rig.mock.unreachable = True
    response = await rig.client.get(PREFIX + "/healthz", headers={"Authorization": ""})
    validate("Health", response.json())
    assert response.json()["service"] == "p2_task"
    await rig.request("GET", "/task-types", status=503)
    rig.mock.unreachable = False
    catalog = (await rig.request("GET", "/task-types", schema="TypeCatalog")).json()
    assert [t["task_type"] for t in catalog["task_types"]] == ["ocr.image.v1"]
    for token, status in [("", 401), ("invalid", 401), (rig.settings.task_token, 403)]:
        await rig.request(
            "GET", "/task-types", status=status, headers={"Authorization": f"Bearer {token}"}
        )


@pytest.mark.parametrize(
    "case,expected,code",
    [
        ("unconfigured", 503, "PROFILE_UNCONFIGURED"),
        ("asr", 422, "UNSUPPORTED_TASK_TYPE"),
        ("unknown", 422, "UNSUPPORTED_TASK_TYPE"),
        ("profile", 409, "PROFILE_MISMATCH"),
        ("language", 422, "PARAMETER_UNSUPPORTED"),
        ("missing", 422, "INPUT_INVALID"),
        ("mime", 422, "INPUT_INVALID"),
        ("size", 413, "INPUT_TOO_LARGE"),
        ("pixels", 413, "INPUT_TOO_LARGE"),
        ("duplicate", 422, "INPUT_INVALID"),
        ("extra", 422, "INVALID_REQUEST"),
        ("uuid", 422, "INVALID_REQUEST"),
        ("empty", 422, "INVALID_REQUEST"),
        ("too_many", 422, "INVALID_REQUEST"),
    ],
)
async def test_T01_input_validation(rig, case, expected, code):
    payload = rig.payload()
    artifact = rig.mock.artifacts["mock_image_1"]
    if case == "unconfigured":
        rig.mock.descriptor.update(configuration_state="UNCONFIGURED", profile_digest=None)
    elif case in {"asr", "unknown"}:
        payload["task_type"] = "asr.file.v1" if case == "asr" else "other"
    elif case == "profile":
        payload["profile_digest"] = "f" * 64
    elif case == "language":
        rig.mock.descriptor["supported_languages"] = ["en"]
    elif case == "missing":
        payload["input_artifact_ids"] = ["missing"]
    elif case == "mime":
        artifact["content_type"] = "audio/wav"
    elif case == "size":
        artifact["size_bytes"] = 10485761
    elif case == "pixels":
        artifact["media_info"].update(width=5000, height=5000)
    elif case == "duplicate":
        rig.mock.artifacts["mock_image_2"]["sha256"] = artifact["sha256"]
    elif case == "extra":
        payload["execute_code"] = "forbidden field"
    elif case == "uuid":
        payload["client_request_id"] = "bad-uuid"
    elif case == "empty":
        payload["input_artifact_ids"] = []
    elif case == "too_many":
        payload["input_artifact_ids"] = [f"image_{i}" for i in range(201)]
    response = await rig.request("POST", "/jobs", status=expected, json=payload)
    assert response.json()["code"] == code
    assert not rig.service.store.active() and not rig.mock.posts


async def test_T02_concurrent_create_replay_and_conflict(rig):
    payload = rig.payload()
    responses = await asyncio.gather(
        *[rig.client.post(PREFIX + "/jobs", json=payload) for _ in range(6)]
    )
    assert sorted(r.status_code for r in responses) == [200] * 5 + [201]
    assert len({r.json()["job_id"] for r in responses}) == 1
    first = responses[0].json()
    rig.mock.unreachable = True
    replay = await rig.request("POST", "/jobs", json=dict(reversed(list(payload.items()))))
    assert replay.json() == first
    payload["input_artifact_ids"].reverse()
    await rig.request("POST", "/jobs", status=409, json=payload)
    assert len(rig.service.store.active()) == 1


async def test_T03_order_partial_results_zip_and_offline_download(rig):
    rig.mock.finish_after["mock_image_1"] = 5
    rig.mock.finish_after["mock_image_2"] = 1
    job = await rig.create()
    path = f"/jobs/{job['job_id']}"
    await rig.request("GET", path + "/download", status=409)
    await rig.tick()
    await rig.tick()
    partial = (await rig.request("GET", path + "/results", schema="JobResults")).json()
    assert len(partial["items"]) == 3
    assert partial["items"][0]["result"] is None
    assert partial["items"][1]["result"] is not None
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert final["counts"]["succeeded"] == 3
    assert len({d["worker_id"] for t in final["tasks"] for d in t["dispatches"]}) == 2
    rig.mock.unreachable = True
    result = (await rig.request("GET", path + "/results", schema="JobResults")).json()
    assert result["combined_text"] == "\n\n".join(f"MOCK OCR: mock_image_{i}" for i in range(1, 4))
    download = await rig.request("GET", path + "/download", schema=None)
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert len(archive.namelist()) == 6
        assert json.loads(archive.read("results.json")) == result
        manifest = archive.read("manifest.json").decode()
        for forbidden in [
            rig.settings.task_token,
            rig.settings.demo_token,
            str(rig.settings.data_dir),
            "lease_expires_at",
        ]:
            assert forbidden not in manifest
        for task in final["tasks"]:
            assert f"results/{task['input_index']:04d}-{task['task_id']}.json" in archive.namelist()


async def test_T04_profile_filter_and_stale_session(rig):
    rig.mock.nodes[0]["capabilities"][0]["profile_digest"] = "b" * 64
    rig.mock.session_rejections = 1
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["tasks"][0]["attempts_used"] == 0
    assert rig.mock.posts[0]["worker_id"] == rig.mock.nodes[1]["worker_id"]
    old_session = rig.mock.posts[0]["worker_session_id"]
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert rig.mock.posts[-1]["worker_session_id"] != old_session
    assert final["tasks"][0]["attempts_used"] == 1


@pytest.mark.parametrize("state", ["OFFLINE", "PAUSED", "cleanup", "unverified", "no_nodes"])
async def test_T05_no_eligible_resources_wait(rig, state):
    for node in rig.mock.nodes:
        if state == "OFFLINE":
            node["connection"] = "OFFLINE"
        elif state == "PAUSED":
            node["mode"] = "PAUSED"
        elif state == "cleanup":
            node["cleanup_pending"] = True
        elif state == "unverified":
            node["capabilities"][0]["verified"] = False
    if state == "no_nodes":
        rig.mock.nodes.clear()
    job = await rig.create()
    await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["status"] == "WAITING_FOR_RESOURCES"
    assert all(t["waiting_reason"] and t["attempts_used"] == 0 for t in snapshot["tasks"])
    assert not rig.mock.posts


async def test_T05_prepare_and_recover_lost_response(rig):
    for node in rig.mock.nodes:
        node["capabilities"][0].update(state="NOT_PREPARED", verified=False)
    rig.mock.drop_preparation_responses = 1
    job = await rig.create()
    await rig.tick()
    assert not rig.mock.posts
    assert all(t["attempts_used"] == 0 for t in (await rig.job(job["job_id"]))["tasks"])
    pid = rig.mock.preparation_posts[0]["preparation_id"]
    await rig.restart()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert pid in rig.mock.preparations
    assert len(rig.mock.preparations) == 2


async def test_T05_failed_preparation_does_not_busy_loop(rig):
    rig.mock.nodes = rig.mock.nodes[:1]
    rig.mock.nodes[0]["capabilities"][0].update(state="NOT_PREPARED", verified=False)
    rig.mock.preparation_failure = True
    job = await rig.create(["mock_image_1"])
    for _ in range(5):
        await rig.tick()
    rig.mock.nodes[0]["capabilities"][0]["state"] = "NOT_PREPARED"
    await rig.tick()
    assert len(rig.mock.preparation_posts) == 1 and not rig.mock.posts
    assert (await rig.job(job["job_id"]))["tasks"][0]["attempts_used"] == 0


async def test_T06_lost_acceptance_then_404_replays_identical_request(rig):
    rig.mock.drop_post_responses = 1
    job = await rig.create(["mock_image_1"])
    assert not await rig.tick()
    pending = (await rig.job(job["job_id"]))["tasks"][0]
    assert pending["status"] == "DISPATCHING" and pending["attempts_used"] == 0
    assert pending["dispatches"] == []  # Never fabricate ACCEPTED for an unknown response.
    rig.mock.get_404 = 1
    await rig.tick()
    assert len(rig.mock.posts) == 2 and rig.mock.posts[0] == rig.mock.posts[1]
    final = await rig.finish(job["job_id"])
    assert len(rig.mock.executions) == 1 and final["tasks"][0]["attempts_used"] == 1


async def test_T07_rejected_does_not_consume_attempt_and_backoff(rig):
    rig.mock.nodes = rig.mock.nodes[:1]
    rig.mock.busy_rejections = 1
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["tasks"][0]["dispatches"][0]["status"] == "REJECTED"
    assert snapshot["tasks"][0]["attempts_used"] == 0
    await rig.tick(advance=0)
    assert len(rig.mock.posts) == 1
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert [r["attempt_no"] for r in rig.mock.posts] == [1, 1]
    assert len({r["execution_id"] for r in rig.mock.posts}) == 2


async def test_T08_three_accepted_failures_and_partial_success(rig):
    rig.mock.outcomes["mock_image_1"] = ["retryable"]
    job = await rig.create()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "PARTIAL_FAILED"
    assert final["tasks"][0]["attempts_used"] == 3
    assert final["counts"]["succeeded"] == 2 and final["counts"]["failed"] == 1
    attempts = [p["attempt_no"] for p in rig.mock.posts if p["input_artifact_id"] == "mock_image_1"]
    assert attempts == [1, 2, 3]
    archive = await rig.request("GET", f"/jobs/{job['job_id']}/download", schema=None)
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert len(zipped.namelist()) == 5


@pytest.mark.parametrize(
    "outcome",
    ["wrong_input", "wrong_type", "wrong_size", "bad_bbox", "bad_hash", "empty", "permanent"],
)
async def test_T09_validate_real_result_not_just_execution_status(rig, outcome):
    rig.mock.outcomes["mock_image_1"] = [outcome]
    job = await rig.create(["mock_image_1"])
    final = await rig.finish(job["job_id"])
    assert final["status"] == ("SUCCEEDED" if outcome == "empty" else "FAILED")
    assert final["tasks"][0]["attempts_used"] == 1
    result = (
        await rig.request("GET", f"/jobs/{job['job_id']}/results", schema="JobResults")
    ).json()
    if outcome == "empty":
        assert result["items"][0]["result"]["text"] == ""
        assert result["items"][0]["result"]["blocks"] == []
    else:
        assert result["items"][0]["result"] is None and result["items"][0]["error"]
    await rig.request("GET", f"/jobs/{job['job_id']}/download", schema=None)


@pytest.mark.parametrize("phase", ["queued", "dispatching", "running"])
async def test_T10_cancel_at_each_phase_stops_new_dispatch(rig, phase):
    rig.mock.nodes = rig.mock.nodes[:1]
    rig.mock.finish_after["mock_image_1"] = 100
    if phase == "dispatching":
        rig.mock.post_before_accept_503 = 1
    job = await rig.create()
    if phase != "queued":
        await rig.tick()
    response = await rig.request(
        "POST", f"/jobs/{job['job_id']}/cancel", status=200 if phase == "queued" else 202
    )
    assert response.json()["cancel_requested"]
    final = await rig.finish(job["job_id"])
    assert final["status"] == "CANCELLED"
    assert len(rig.mock.executions) == (0 if phase == "queued" else 1)
    assert all(p["input_artifact_id"] == "mock_image_1" for p in rig.mock.posts)
    if phase == "dispatching":
        assert rig.mock.posts[0] == rig.mock.posts[1]
    again = await rig.request("POST", f"/jobs/{job['job_id']}/cancel")
    assert again.json() == final
    await rig.request("GET", f"/jobs/{job['job_id']}/download", schema=None)


async def test_T10_wait_for_cancel_confirmation_and_preserve_success(rig):
    rig.mock.finish_after["mock_image_1"] = 1
    rig.mock.finish_after["mock_image_2"] = 100
    rig.mock.finish_after["mock_image_3"] = 100
    job = await rig.create()
    await rig.tick()
    await rig.tick()
    rig.mock.hold_cancel = True
    await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
    await rig.tick()
    assert (await rig.job(job["job_id"]))["status"] == "CANCELLING"
    rig.mock.hold_cancel = False
    final = await rig.finish(job["job_id"])
    assert final["status"] == "CANCELLED" and final["counts"]["succeeded"] == 1
    result = (
        await rig.request("GET", f"/jobs/{job['job_id']}/results", schema="JobResults")
    ).json()
    assert result["items"][0]["result"] is not None


async def test_cancel_success_race_preserves_committed_p1_result(rig):
    rig.mock.finish_after["mock_image_1"] = 100
    rig.mock.cancel_finishes_success = True
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
    final = await rig.finish(job["job_id"])
    assert final["status"] == "CANCELLED"
    assert final["tasks"][0]["status"] == "SUCCEEDED"


async def test_result_download_failure_does_not_recompute(rig):
    rig.mock.result_unavailable = True
    job = await rig.create(["mock_image_1"])
    for _ in range(4):
        await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["tasks"][0]["status"] == "RUNNING"
    assert len(rig.mock.posts) == 1
    assert next(iter(rig.mock.executions.values()))["status"] == "SUCCEEDED"
    rig.mock.result_unavailable = False
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("stage", ["before_post", "lost_response", "before_result_commit"])
async def test_X02_restart_preserves_request_and_result_identity(rig, stage, monkeypatch):
    job = await rig.create(["mock_image_1"])
    if stage == "before_post":

        async def stop_before_network(*args):
            raise asyncio.CancelledError()

        monkeypatch.setattr(rig.service.p1, "reconcile", stop_before_network)
        with pytest.raises(asyncio.CancelledError):
            await rig.tick()
        assert not rig.mock.posts
    elif stage == "lost_response":
        rig.mock.drop_post_responses = 1
        await rig.tick()
    else:
        original = rig.service._record_execution

        def stop_before_commit(*args, **kwargs):
            if kwargs.get("result") is not None:
                raise asyncio.CancelledError()
            return original(*args, **kwargs)

        monkeypatch.setattr(rig.service, "_record_execution", stop_before_commit)
        await rig.tick()
        await rig.tick()
        with pytest.raises(asyncio.CancelledError):
            await rig.tick()
    saved = (await rig.job(job["job_id"]))["tasks"][0]
    assert saved["current_execution_id"]
    await rig.restart()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert final["tasks"][0]["task_id"] == saved["task_id"]
    assert {p["execution_id"] for p in rig.mock.posts} == {saved["current_execution_id"]}
    assert len(rig.mock.executions) == 1


async def test_dependency_outage_preserves_running_execution(rig):
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    before = (await rig.job(job["job_id"]))["tasks"][0]["current_execution_id"]
    rig.mock.unreachable = True
    await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["dependency_status"] == "UNREACHABLE"
    assert snapshot["tasks"][0]["current_execution_id"] == before
    assert snapshot["status"] == "RUNNING"
    rig.mock.unreachable = False
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED" and len(rig.mock.posts) == 1
    assert final["dependency_status"] == "AVAILABLE"


async def test_profile_change_does_not_silently_switch(rig):
    job = await rig.create(["mock_image_1"])
    rig.mock.descriptor["profile_digest"] = "b" * 64
    final = await rig.finish(job["job_id"])
    assert final["status"] == "FAILED"
    assert final["profile_digest"] == job["profile_digest"]
    assert final["tasks"][0]["error"]["code"] == "PROFILE_MISMATCH"
    assert final["tasks"][0]["attempts_used"] == 0


async def test_global_capacity_multiple_jobs_and_terminal_immutable(rig):
    rig.mock.nodes = rig.mock.nodes[:1]
    first, second = await rig.create(["mock_image_1"]), await rig.create(["mock_image_2"])
    await rig.tick()
    assert len(rig.mock.posts) == 1
    await rig.finish(first["job_id"])
    final = await rig.finish(second["job_id"])
    original = copy.deepcopy(final)
    for execution in rig.mock.executions.values():
        execution.update(status="FAILED", error=None)
    await rig.tick()
    assert (await rig.job(second["job_id"])) == original
    assert all(d["status"] != "REJECTED" for t in final["tasks"] for d in t["dispatches"])


async def test_upper_batch_limit_200(rig):
    ids = [f"batch_{i}" for i in range(200)]
    for i, artifact_id in enumerate(ids):
        rig.mock.add_image(artifact_id, i + 100)
    job = await rig.create(ids)
    assert len(job["tasks"]) == 200
    assert [t["input_index"] for t in job["tasks"]] == list(range(200))
    await rig.tick()
    assert len(rig.mock.executions) == 2
    await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
    assert (await rig.finish(job["job_id"]))["status"] == "CANCELLED"


async def test_errors_are_contract_shaped_and_single_store_owner(rig):
    await rig.request("GET", "/jobs/not-a-job", status=404)
    await rig.request(
        "POST",
        "/jobs",
        status=422,
        content="{invalid",
        headers={"Content-Type": "application/json"},
    )
    await rig.request("POST", "/jobs", status=422, json={"client_request_id": "bad"})
    with pytest.raises(RuntimeError, match="占用"):
        Store(rig.settings.data_dir)
    await rig.request("GET", "/healthz", schema="Health")


async def test_one_unavailable_result_does_not_starve_other_inputs(rig):
    job = await rig.create()
    await rig.tick()
    first = next(
        e for e in rig.mock.executions.values() if e["input_artifact_id"] == "mock_image_1"
    )
    rig.mock.finish(first["execution_id"])
    result_id = first["result_artifact"]["artifact_id"]
    rig.mock.unavailable_result_ids.add(result_id)
    for _ in range(7):
        await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["counts"]["succeeded"] == 2
    assert snapshot["tasks"][0]["status"] == "RUNNING"
    assert "结果待取回" in snapshot["tasks"][0]["waiting_reason"]
    assert len(rig.mock.posts) == 3
    rig.mock.unavailable_result_ids.clear()
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"


async def test_known_nonretryable_rejection_fails_without_attempt(rig):
    job = await rig.create(["mock_image_1"])
    rig.mock.descriptor["profile_id"] = "different.profile"
    final = await rig.finish(job["job_id"])
    assert final["status"] == "FAILED"
    assert final["tasks"][0]["attempts_used"] == 0
    assert len(rig.mock.posts) == 1


async def test_p1_restart_failure_is_retried_only_after_explicit_terminal(rig):
    from task_service.contract import error, now, uid

    job = await rig.create(["mock_image_1"])
    await rig.tick()
    old = next(iter(rig.mock.executions.values()))
    old.update(
        status="FAILED",
        finished_at=now(),
        error=error("RESOURCE_SERVICE_RESTARTED", "MOCK P1 restart", True),
    )
    rig.mock._release(old)
    rig.mock.nodes[0]["session_id"] = uid()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert final["tasks"][0]["attempts_used"] == 2
    assert len(rig.mock.executions) == 2


async def test_wrong_execution_identity_is_not_used(rig):
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    execution = next(iter(rig.mock.executions.values()))
    expected = execution["profile_digest"]
    execution["profile_digest"] = "c" * 64
    await rig.tick()
    snapshot = await rig.job(job["job_id"])
    assert snapshot["dependency_status"] == "UNREACHABLE"
    assert snapshot["tasks"][0]["status"] == "RUNNING"
    assert len(rig.mock.posts) == 1
    execution["profile_digest"] = expected
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"


async def test_request_transport_timeout_uses_same_execution_id(rig, monkeypatch):
    import httpx

    original = rig.service.p1.http.request
    first = True

    async def timeout_after_accept(method, url, **kwargs):
        nonlocal first
        response = await original(method, url, **kwargs)
        if method == "POST" and url.endswith("/executions") and first:
            first = False
            raise httpx.ReadTimeout("MOCK lost response")
        return response

    monkeypatch.setattr(rig.service.p1.http, "request", timeout_after_accept)
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    assert (await rig.job(job["job_id"]))["tasks"][0]["attempts_used"] == 0
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED" and len(rig.mock.executions) == 1


async def test_cancel_while_acceptance_response_is_in_flight(rig, monkeypatch):
    received, release = asyncio.Event(), asyncio.Event()
    original = rig.service.p1.http.request

    async def delay_acceptance(method, url, **kwargs):
        response = await original(method, url, **kwargs)
        if method == "POST" and url.endswith("/executions"):
            received.set()
            await release.wait()
        return response

    monkeypatch.setattr(rig.service.p1.http, "request", delay_acceptance)
    job = await rig.create()
    ticking = asyncio.create_task(rig.tick())
    try:
        await asyncio.wait_for(received.wait(), timeout=2)
        response = await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
        assert response.json()["cancel_requested"]
    finally:
        release.set()
        await ticking
    final = await rig.finish(job["job_id"])
    assert final["status"] == "CANCELLED" and final["cancel_requested"]
    assert len(rig.mock.executions) == 1 and len(rig.mock.cancels) == 1


async def test_internal_error_has_public_error_shape(rig, monkeypatch):
    def broken_store(job_id):
        raise RuntimeError("private internal implementation detail")

    monkeypatch.setattr(rig.service.store, "get", broken_store)
    response = await rig.request("GET", "/jobs/anything", status=500)
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "private internal" not in response.text
