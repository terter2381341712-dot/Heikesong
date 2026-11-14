import copy

import pytest

from task_service.contract import error, now


@pytest.mark.parametrize("restart", [False, True])
async def test_55_successes_preserved_and_45_redistributed(rig, restart):
    ids = [f"image_{i}" for i in range(100)]
    for i, aid in enumerate(ids):
        rig.mock.add_image(aid, i + 100)
        rig.mock.finish_after[aid] = 1 if i < 55 else 10000
    other = rig.mock.nodes.pop()
    job = await rig.create(ids)
    for _ in range(65):
        await rig.tick()
        current = await rig.job(job["job_id"])
        if sum(t["status"] == "SUCCEEDED" for t in current["tasks"]) == 55:
            break
    saved = copy.deepcopy(current["tasks"][:55])
    assert all(t["status"] == "SUCCEEDED" for t in saved)
    active = next(e for e in rig.mock.executions.values() if e["status"] in {"RUNNING", "ACCEPTED"})
    node = rig.mock.nodes[0]
    node.update(mode="PAUSED", available_slots=0)
    active.update(status="CANCEL_REQUESTED")
    before = len(rig.mock.executions)
    await rig.tick()
    assert len(rig.mock.executions) == before
    failure = error("NODE_UNAVAILABLE", "共享关闭", True, execution_id=active["execution_id"])
    active.update(status="CANCELLED", error=failure, finished_at=now())
    await rig.tick()
    if restart:
        await rig.restart()
    waiting = await rig.job(job["job_id"])
    assert waiting["tasks"][:55] == saved
    assert waiting["tasks"][55]["status"] == "WAITING_FOR_RESOURCES"
    rig.mock.nodes.append(other)
    for aid in ids[55:]:
        rig.mock.finish_after[aid] = 1
    final = await rig.finish(job["job_id"], limit=100)
    assert final["status"] == "SUCCEEDED"
    assert final["tasks"][:55] == saved
    assert all(t["dispatches"][-1]["worker_id"] == other["worker_id"] for t in final["tasks"][55:])
    assert len(rig.mock.executions) == 101


@pytest.mark.parametrize("job_cancel,withdrawn", [(True, True), (False, False)])
async def test_normal_cancellation_is_not_requeued(rig, job_cancel, withdrawn):
    job = await rig.create(["mock_image_1"])
    rig.mock.finish_after["mock_image_1"] = 1000
    await rig.tick()
    e = next(iter(rig.mock.executions.values()))
    if job_cancel:
        await rig.request("POST", f"/jobs/{job['job_id']}/cancel", status=202)
    e.update(
        status="CANCELLED",
        finished_at=now(),
        error=(
            error("NODE_UNAVAILABLE", "共享关闭", True, execution_id=e["execution_id"])
            if withdrawn
            else None
        ),
    )
    final = await rig.finish(job["job_id"])
    assert final["tasks"][0]["status"] == "CANCELLED"
    assert final["status"] == ("CANCELLED" if job_cancel else "FAILED")
    assert len(rig.mock.executions) == 1


async def test_withdrawal_does_not_reset_attempt_budget(rig):
    job = await rig.create(["mock_image_1"])
    rig.mock.finish_after["mock_image_1"] = 1000
    await rig.tick()
    execution = copy.deepcopy(next(iter(rig.mock.executions.values())))
    execution.update(
        status="CANCELLED",
        attempt_no=3,
        finished_at=now(),
        error=error("NODE_UNAVAILABLE", "共享关闭", True, execution_id=execution["execution_id"]),
    )
    rig.service._record_execution(job["job_id"], job["tasks"][0]["task_id"], execution)
    final = await rig.job(job["job_id"])
    assert final["tasks"][0]["status"] == "FAILED"
    assert final["tasks"][0]["attempts_used"] == 3
    await rig.tick()
    assert len(rig.mock.executions) == 1
