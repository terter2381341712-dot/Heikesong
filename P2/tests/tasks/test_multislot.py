"""Capacity accounting, including uncertain acceptance and completed result fetches."""

from task_service.contract import uid


def pool(rig, slots=3):
    rig.mock.nodes[:] = rig.mock.nodes[:1]
    n = rig.mock.nodes[0]
    n.update(max_slots=slots, available_slots=slots, threads_per_slot=1, cpu_thread_budget=slots)
    for image in ("mock_image_1", "mock_image_2", "mock_image_3"):
        rig.mock.finish_after[image] = 1000
    return n


async def test_fills_all_free_slots_in_one_tick_and_does_not_overbook(rig):
    n = pool(rig)
    await rig.create()
    await rig.create()
    assert await rig.tick()
    assert len(rig.mock.posts) == 3 and n["reserved_slots"] == 3
    for _ in range(3):
        assert await rig.tick()
    assert len(rig.mock.posts) == 3
    rig.mock.finish(n["active_execution_ids"][0])
    assert await rig.tick()
    assert len(rig.mock.posts) == 4 and n["reserved_slots"] == 3


async def test_existing_snapshot_reservation_not_double_subtracted(rig):
    n = pool(rig)
    await rig.create(["mock_image_1"])
    await rig.tick()
    assert n["reserved_slots"] == 1
    await rig.create(["mock_image_2", "mock_image_3"])
    await rig.restart()
    await rig.tick()
    assert len(rig.mock.posts) == 3 and n["available_slots"] == 0


async def test_lost_acceptance_and_restart_reconciles_same_execution(rig):
    n = pool(rig, 2)
    rig.mock.drop_post_responses = 1
    await rig.create()
    await rig.tick()
    first = rig.mock.posts[0]["execution_id"]
    await rig.restart()
    for _ in range(3):
        await rig.tick()
    assert len(rig.mock.executions) == 2
    assert first in n["active_execution_ids"] and n["available_slots"] == 0
    assert len({p["execution_id"] for p in rig.mock.posts}) == 2


async def test_completed_result_download_does_not_keep_slot_reserved(rig):
    n = pool(rig, 2)
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    eid = n["active_execution_ids"][0]
    rig.mock.finish(eid)
    rig.mock.unavailable_result_ids.add(rig.mock.executions[eid]["result_artifact"]["artifact_id"])
    await rig.create(["mock_image_2", "mock_image_3"])
    await rig.tick()
    assert len(rig.mock.posts) == 3 and len(n["active_execution_ids"]) == 2
    assert (await rig.job(job["job_id"]))["status"] != "SUCCEEDED"


async def test_external_busy_slot_leaves_remaining_capacity(rig):
    n = pool(rig)
    n.update(active_execution_ids=[uid()], reserved_slots=1, available_slots=2)
    await rig.create()
    await rig.tick()
    assert len(rig.mock.posts) == 2 and n["reserved_slots"] == 3
