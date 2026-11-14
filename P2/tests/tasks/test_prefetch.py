import copy

import pytest
from fastapi import Request

from task_service.contract import uid, validate
from task_service.p1 import DependencyUnavailable
from task_service.scheduling import measurement, resource_key
from tests.tasks.test_resource_scheduling import timing


def setup(rig):
    rig.mock.nodes = [rig.mock.nodes[1]]
    node = rig.mock.nodes[0]
    node.update(prefetch_capacity=1, prefetch_revision=0, threads_per_slot=1, cpu_thread_budget=2)
    node["capabilities"][0].update(backend="cuda", runtime_provider="CUDAExecutionProvider")
    rig.mock.finish_after_worker[node["worker_id"]] = 100
    calls = []

    @rig.mock.app.post("/api/resource/v1/resources/{worker}/prefetch")
    async def publish(worker: str, request: Request):
        body = validate("PrefetchRequest", await request.json())
        calls.append(copy.deepcopy(body))
        node["prefetch_revision"] = body["revision"]
        return dict(
            worker_id=worker,
            worker_session_id=body["worker_session_id"],
            revision=body["revision"],
            expires_at=None,
        )

    return node, calls


async def test_busy_gpu_hint_does_not_dispatch_or_use_attempt(rig):
    node, calls = setup(rig)
    job = await rig.create()
    await rig.tick()
    state = await rig.job(job["job_id"])
    assert len(rig.mock.posts) == 1 and node["reserved_slots"] == 1
    assert calls[-1]["hint"]["task_id"] == state["tasks"][1]["task_id"]
    assert [t["attempts_used"] for t in state["tasks"]] == [1, 0, 0]
    revision = calls[-1]["revision"]
    await rig.tick()
    assert calls[-1]["revision"] == revision
    assert len(rig.mock.posts) == 1


async def test_cancel_revokes_and_restart_replays_clear(rig):
    node, calls = setup(rig)
    job = await rig.create()
    await rig.tick()
    old = calls[-1]
    await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
    await rig.tick()
    assert calls[-1]["hint"] is None and calls[-1]["revision"] > old["revision"]
    cleared = calls[-1]
    await rig.restart()
    await rig.tick()
    assert calls[-1] == cleared
    assert all(t["attempts_used"] == 0 for t in (await rig.job(job["job_id"]))["tasks"][1:])


async def test_lost_clear_is_durable_and_retried(rig, monkeypatch):
    node, calls = setup(rig)
    job = await rig.create()
    await rig.tick()
    original = rig.service.p1.prefetch

    async def unavailable(*args):
        raise DependencyUnavailable("lost response")

    monkeypatch.setattr(rig.service.p1, "prefetch", unavailable)
    await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
    await rig.tick()
    pending = rig.service.store.prefetches()[node["worker_id"]]
    assert pending["hint"] is None
    monkeypatch.setattr(rig.service.p1, "prefetch", original)
    await rig.tick()
    assert calls[-1] == pending


async def test_new_session_uses_higher_revision(rig):
    node, calls = setup(rig)
    await rig.create()
    await rig.tick()
    old = calls[-1]
    node["session_id"] = uid()
    node["prefetch_revision"] = old["revision"] + 8
    await rig.service.prefetch.sync([node], {node["worker_id"]: 0}, set(), {})
    assert calls[-1]["revision"] > node["prefetch_revision"] - 1
    assert calls[-1]["worker_session_id"] == node["session_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "PAUSED"},
        {"cleanup_pending": True},
        {"connection": "OFFLINE"},
        {"prefetch_capacity": 0},
    ],
)
async def test_unavailable_node_clears_hint(rig, change):
    node, calls = setup(rig)
    await rig.create()
    await rig.tick()
    node.update(change)
    await rig.service.prefetch.sync([node], {node["worker_id"]: 0}, set(), {})
    assert calls[-1]["hint"] is None


async def test_other_node_can_execute_speculated_task(rig):
    cpu = rig.mock.nodes[0]
    node, calls = setup(rig)
    job = await rig.create()
    await rig.tick()
    hinted = calls[-1]["hint"]["task_id"]
    rig.mock.nodes.append(cpu)
    await rig.tick()
    result = await rig.job(job["job_id"])
    task = next(t for t in result["tasks"] if t["task_id"] == hinted)
    assert task["attempts_used"] == 1
    assert task["dispatches"][-1]["worker_id"] == cpu["worker_id"]
    assert node["reserved_slots"] == 1


async def test_prefetch_config_separates_history_and_overlapped_cost_not_double_counted(rig):
    node, calls = setup(rig)
    job = await rig.create()
    assert resource_key(node, job) != resource_key(node | {"prefetch_capacity": 0}, job)
    await rig.tick()
    eid = rig.mock.posts[0]["execution_id"]
    decision = rig.service.store.scheduling_decision(eid)
    execution = rig.mock.executions[eid] | {
        "status": "SUCCEEDED",
        "timings": timing()
        | {
            "prefetch_hit": True,
            "decode_ms": 1,
            "prefetch_download_ms": 5000,
            "prefetch_decode_ms": 4000,
        },
    }
    sample = measurement(execution, decision)
    assert sample and sample["total_ms"] == execution["timings"]["total_worker_ms"]


@pytest.mark.parametrize(
    "change", [{"threads_per_slot": 0}, {"cpu_thread_budget": 1}, {"max_slots": 2}]
)
async def test_prefetch_thread_budget_blocks_invalid_ready_node(rig, change):
    from task_service.service import candidate_action

    node, calls = setup(rig)
    job = await rig.create()
    assert candidate_action(node | change, job)[0] is None
