"""Synthetic timings exercise scheduling, never GPU performance evidence."""

import asyncio
import copy
from dataclasses import replace

import pytest
import pytest_asyncio

from task_service.contract import uid
from task_service.scheduling import features, measurement, resource_key
from tests.tasks.mock_p1 import CAPABILITIES


@pytest_asyncio.fixture(autouse=True)
async def enable_resource_aware_policy(rig):
    # These tests explicitly exercise the opt-in policy; other suites exercise
    # the production round-robin default.
    rig.settings = replace(rig.settings, scheduler_policy="resource_aware")
    rig.service.settings = rig.settings
    rig.service.scheduler.settings = rig.settings


def timing(compute=100, download=10, overhead=1):
    return dict(
        inference_ms=compute,
        download_ms=download,
        postprocess_ms=0,
        write_output_ms=overhead,
        total_worker_ms=compute + download + overhead,
    )


def gpu(rig):
    node = rig.mock.nodes[1]
    node["capabilities"][0].update(backend="cuda", runtime_provider="CUDAExecutionProvider")
    return node


async def train(rig, node, measured, image="mock_image_1"):
    nodes = rig.mock.nodes
    rig.mock.nodes = [node]
    rig.mock.worker_timings[node["worker_id"]] = measured
    job = await rig.create([image])
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    rig.mock.nodes = nodes
    return job


def samples(rig):
    return rig.service.store.db.execute("SELECT COUNT(*) FROM scheduling_samples").fetchone()[0]


def decision(rig, job):
    state = rig.service.store.get(job["job_id"])
    eid = state["job"]["tasks"][0]["current_execution_id"]
    return rig.service.store.scheduling_decision(eid)


async def test_observations_beat_calibration_and_survive_restart(rig):
    cpu, cuda = rig.mock.nodes[0], gpu(rig)
    cpu["capabilities"][0]["calibration"]["median_compute_ms"] = 1
    cuda["capabilities"][0]["calibration"]["median_compute_ms"] = 2000
    await train(rig, cpu, timing(500))
    await train(rig, cuda, timing(50))
    assert samples(rig) == 2
    await rig.restart()
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    d = decision(rig, job)
    assert d["worker_id"] == cuda["worker_id"] and d["source"] == "execution_history"
    assert d["backend"] == "cuda" and d["predicted_ms"] == 61
    assert samples(rig) == 2


async def test_cpu_wins_when_gpu_transfer_exceeds_compute_saving(rig):
    cpu, cuda = rig.mock.nodes[0], gpu(rig)
    await train(rig, cpu, timing(200, 1))
    await train(rig, cuda, timing(10, 500))
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    assert decision(rig, job)["worker_id"] == cpu["worker_id"]


async def test_calibration_bootstrap_never_waits_for_busy_gpu(rig):
    cuda = gpu(rig)
    cuda["capabilities"][0]["calibration"]["median_compute_ms"] = 10
    rig.mock.finish_after["mock_image_1"] = 1000
    first = await rig.create(["mock_image_1"])
    await rig.tick()
    assert decision(rig, first)["backend"] == "cuda"
    second = await rig.create(["mock_image_2"])
    await rig.tick()
    assert decision(rig, second)["backend"] == "cpu"
    assert rig.mock.nodes[0]["available_slots"] == cuda["available_slots"] == 0


@pytest.mark.parametrize("change", ["backend", "provider", "cpu_budget"])
async def test_capability_budget_gate_precedes_ranking(rig, change):
    node = rig.mock.nodes[0]
    cap = node["capabilities"][0]
    cap["calibration"]["median_compute_ms"] = 0.01
    if change == "backend":
        cap.update(backend="cuda")
    if change == "provider":
        cap.update(runtime_provider="UnknownProvider")
    if change == "cpu_budget":
        node.update(max_slots=2, available_slots=2, threads_per_slot=2, cpu_thread_budget=2)
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    assert decision(rig, job)["worker_id"] == rig.mock.nodes[1]["worker_id"]


async def test_gpu_multislot_is_valid_candidate(rig):
    from task_service.service import candidate_action

    node = rig.mock.nodes[0]
    cap = node["capabilities"][0]
    cap.update(backend="cuda", runtime_provider="CUDAExecutionProvider", model_sha256="f" * 64)
    cap["calibration"]["median_compute_ms"] = 0.01
    node.update(max_slots=2, available_slots=2)
    job = await rig.create(["mock_image_1"])
    # GPU 逻辑多路不再被单槽限制拦截，且不走 CPU 线程预算校验。
    assert candidate_action(node, job)[0] == "execute"
    await rig.tick()
    assert decision(rig, job)["worker_id"] == node["worker_id"]


async def test_buckets_configuration_ttl_isolate_history(rig):
    node = rig.mock.nodes[0]
    small = await train(rig, node, timing(100))
    rig.mock.add_image("large", 50)["media_info"].update(width=3000, height=3000)
    big = await train(rig, node, timing(900), "large")
    scheduler = rig.service.scheduler
    task = small["tasks"][0]
    assert scheduler.estimate(node, small, task, 1)["predicted_ms"] == 111
    assert scheduler.estimate(node, big, big["tasks"][0], 1)["predicted_ms"] == 911
    for field, value in [("threads_per_slot", 2), ("max_slots", 2)]:
        changed = copy.deepcopy(node)
        changed[field] = value
        assert scheduler.estimate(changed, small, task, 1)["source"] == "calibration"
    changed = copy.deepcopy(node)
    changed["capabilities"][0]["model_sha256"] = "b" * 64
    assert resource_key(changed, small) != resource_key(node, small)
    assert resource_key(node, dict(small, profile_digest="c" * 64)) != resource_key(node, small)
    assert (
        scheduler.estimate(dict(node, session_id=uid()), small, task, 1)["source"]
        == "execution_history"
    )
    rig.time += rig.settings.history_ttl_seconds + 1
    assert scheduler.estimate(node, small, task, 1)["source"] == "calibration"


async def test_periodic_exploration_keeps_other_nodes_measured(rig):
    cpu, cuda = rig.mock.nodes[0], gpu(rig)
    await train(rig, cpu, timing(800))
    await train(rig, cuda, timing(10))
    selections = []
    for _ in range(12):
        job = await rig.create(["mock_image_1"])
        await rig.finish(job["job_id"])
        selections.append(decision(rig, job))
    assert sum(d["backend"] == "cuda" for d in selections) >= 9
    assert any(d["exploration"] and d["backend"] == "cpu" for d in selections)
    assert all(d["source"] == "execution_history" for d in selections)


async def test_fast_node_consumes_more_backlog_and_slow_slot_also_runs(rig):
    cpu, cuda = rig.mock.nodes[0], gpu(rig)
    await train(rig, cpu, timing(600))
    await train(rig, cuda, timing(50))
    rig.mock.finish_after_worker = {cpu["worker_id"]: 6, cuda["worker_id"]: 1}
    ids = [f"batch{i}" for i in range(16)]
    for i, artifact in enumerate(ids):
        rig.mock.add_image(artifact, i + 30)
    job = await rig.create(ids)
    await rig.tick()
    used = {e["worker_id"] for e in rig.mock.executions.values() if e["job_id"] == job["job_id"]}
    assert used == {cpu["worker_id"], cuda["worker_id"]}
    final = await rig.finish(job["job_id"])
    workers = [t["dispatches"][-1]["worker_id"] for t in final["tasks"]]
    assert workers.count(cuda["worker_id"]) > workers.count(cpu["worker_id"]) > 0
    assert all(t["attempts_used"] == 1 for t in final["tasks"])


async def test_ready_before_cold_but_backlog_wakes_cold_node(rig):
    cold, ready = rig.mock.nodes
    cold["capabilities"] = [copy.deepcopy(CAPABILITIES["not_prepared_null"])]
    rig.mock.finish_after["mock_image_1"] = 1000
    first = await rig.create(["mock_image_1"])
    await rig.tick()
    assert (
        not rig.mock.preparation_posts and decision(rig, first)["worker_id"] == ready["worker_id"]
    )
    await rig.create(["mock_image_2"])
    await rig.tick()
    assert len(rig.mock.preparation_posts) == 1
    assert rig.mock.preparation_posts[0]["worker_id"] == cold["worker_id"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        timing(0),
        timing(-1),
        {**timing(), "total_worker_ms": 1},
        {**timing(), "inference_ms": float("nan")},
        timing(1_000_000),
    ],
)
def test_bad_timings_never_train(bad):
    e = dict(status="SUCCEEDED", timings=bad, timeout_seconds=120)
    d = dict(resource_key="key", bucket=0, pixels=100, input_bytes=10)
    assert measurement(e, d) is None


@pytest.mark.parametrize("outcome", ["permanent", "wrong_input", "bad_hash", "bad_bbox"])
async def test_failed_or_invalid_result_never_trains(rig, outcome):
    rig.mock.worker_timings = {n["worker_id"]: timing() for n in rig.mock.nodes}
    rig.mock.outcomes["mock_image_1"] = [outcome]
    job = await rig.create(["mock_image_1"])
    assert (await rig.finish(job["job_id"]))["status"] == "FAILED"
    assert samples(rig) == 0


async def test_cancel_and_lost_response_keep_identity(rig):
    rig.mock.worker_timings = {n["worker_id"]: timing() for n in rig.mock.nodes}
    rig.mock.finish_after["mock_image_1"] = 1000
    rig.mock.drop_post_responses = 1
    job = await rig.create(["mock_image_1"])
    assert not await rig.tick()
    eid = next(iter(rig.mock.executions))
    assert rig.service.store.scheduling_decision(eid) is not None
    await rig.request("POST", "/jobs/" + job["job_id"] + "/cancel", status=202)
    await rig.restart()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "CANCELLED" and samples(rig) == 0
    assert {p["execution_id"] for p in rig.mock.posts} == {eid}
    assert rig.service.store.scheduling_sequence() == 1


async def test_slow_result_does_not_reserve_completed_slot(rig, monkeypatch):
    rig.mock.nodes = rig.mock.nodes[:1]
    rig.service.settings = replace(rig.settings, scheduler_io_timeout_seconds=0.05)
    first = await rig.create(["mock_image_1"])
    await rig.tick()
    rig.mock.finish(next(iter(rig.mock.executions)))
    original = rig.service.p1.result
    blocked_id = next(iter(rig.mock.executions.values()))["result_artifact"]["artifact_id"]

    async def slow(artifact):
        if artifact["artifact_id"] == blocked_id:
            await asyncio.Event().wait()
        return await original(artifact)

    monkeypatch.setattr(rig.service.p1, "result", slow)
    second = await rig.create(["mock_image_2"])
    assert await rig.tick()
    assert decision(rig, second) is not None
    assert (await rig.job(first["job_id"]))["status"] == "RUNNING"
    assert samples(rig) == 0


async def test_uncertain_worker_does_not_block_healthy_worker(rig, monkeypatch):
    rig.service.settings = replace(rig.settings, scheduler_io_timeout_seconds=0.05)
    rig.mock.finish_after["mock_image_1"] = 1000
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    first_worker = decision(rig, job)["worker_id"]
    original = rig.service.p1.reconcile

    async def slow(kind, request):
        if request["worker_id"] == first_worker:
            await asyncio.Event().wait()
        return await original(kind, request)

    monkeypatch.setattr(rig.service.p1, "reconcile", slow)
    second = await rig.create(["mock_image_2"])
    assert not await rig.tick()
    assert decision(rig, second)["worker_id"] != first_worker
    assert rig.service.healthy_workers
    assert (await rig.job(job["job_id"]))["dependency_status"] == "UNREACHABLE"


async def test_reconcile_concurrency_bounded_and_cancellation_joins_tasks(rig, monkeypatch):
    node = rig.mock.nodes[0]
    rig.mock.nodes = [node]
    node.update(max_slots=4, available_slots=4, threads_per_slot=1, cpu_thread_budget=4)
    rig.mock.add_image("fourth", 42)
    for key in rig.mock.artifacts:
        rig.mock.finish_after[key] = 1000
    await rig.create(["mock_image_1", "mock_image_2", "mock_image_3", "fourth"])
    await rig.tick()
    rig.service.io_slots = asyncio.Semaphore(2)
    gate, entered = asyncio.Event(), asyncio.Event()
    alive = peak = 0

    async def hold(*args):
        nonlocal alive, peak
        alive += 1
        peak = max(peak, alive)
        if peak == 2:
            entered.set()
        try:
            await gate.wait()
        finally:
            alive -= 1

    monkeypatch.setattr(rig.service.p1, "reconcile", hold)
    tick = asyncio.create_task(rig.tick())
    await asyncio.wait_for(entered.wait(), 2)
    assert peak == 2
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert alive == 0


def test_feature_bucket_edges():
    def task(pixels):
        return {"input": {"media_info": {"width": pixels, "height": 1}, "size_bytes": 10}}

    assert [
        features(task(p))["bucket"]
        for p in (250_000, 250_001, 1_000_000, 1_000_001, 4_000_000, 4_000_001)
    ] == [0, 1, 1, 2, 2, 3]


async def test_late_result_is_not_fresh_training_data(rig):
    rig.mock.worker_timings = {n["worker_id"]: timing() for n in rig.mock.nodes}
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    rig.mock.finish(next(iter(rig.mock.executions)))
    rig.time += rig.settings.history_ttl_seconds + 1
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    assert samples(rig) == 0


async def test_periodic_prepare_discovers_cold_accelerator_without_delaying_ready_work(rig):
    cold = rig.mock.nodes[1]
    cold["capabilities"] = [copy.deepcopy(CAPABILITIES["not_prepared_null"])]
    cold["hardware"]["gpu_name"] = "synthetic NVIDIA GPU"
    for _ in range(8):
        job = await rig.create(["mock_image_1"])
        await rig.finish(job["job_id"])
        assert decision(rig, job)["worker_id"] == rig.mock.nodes[0]["worker_id"]
    assert len(rig.mock.preparation_posts) == 1
    assert rig.mock.preparation_posts[0]["worker_id"] == cold["worker_id"]


@pytest.mark.parametrize("backend", ["cuda", "directml", "coreml"])
async def test_first_small_job_prepares_known_gpu_immediately_while_cpu_runs(rig, backend):
    cold = rig.mock.nodes[1]
    cold["capabilities"] = [copy.deepcopy(CAPABILITIES["not_prepared_null"])]
    cold["hardware"].setdefault("manual_labels", {})["execution_backend"] = backend
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    assert decision(rig, job)["worker_id"] == rig.mock.nodes[0]["worker_id"]
    assert len(rig.mock.preparation_posts) == 1
    assert rig.mock.preparation_posts[0]["worker_id"] == cold["worker_id"]


async def test_sample_and_success_commit_atomically_on_interrupted_commit(rig, monkeypatch):
    rig.mock.worker_timings = {n["worker_id"]: timing() for n in rig.mock.nodes}
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    rig.mock.finish(next(iter(rig.mock.executions)))
    original = rig.service.store.record_scheduling_sample

    def interrupt(*args):
        original(*args)
        raise asyncio.CancelledError()

    monkeypatch.setattr(rig.service.store, "record_scheduling_sample", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await rig.tick()
    assert samples(rig) == 0
    await rig.restart()
    assert (await rig.finish(job["job_id"]))["status"] == "SUCCEEDED"
    assert samples(rig) == 1
    assert rig.service.store.scheduling_sequence() == 1
