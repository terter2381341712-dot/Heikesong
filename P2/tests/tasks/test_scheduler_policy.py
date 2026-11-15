from dataclasses import replace

import pytest


async def test_default_policy_rotates_even_when_gpu_looks_faster(rig):
    assert rig.settings.scheduler_policy == "round_robin"
    rig.mock.nodes[1]["capabilities"][0]["calibration"]["median_compute_ms"] = 0.001
    selected = []
    for _ in range(2):
        job = await rig.create(["mock_image_1"])
        await rig.finish(job["job_id"])
        state = rig.service.store.get(job["job_id"])
        eid = state["job"]["tasks"][0]["current_execution_id"]
        d = rig.service.store.scheduling_decision(eid)
        assert d["policy"] == "round-robin-v1" and not d["exploration"]
        selected.append(d["worker_id"])
    assert len(set(selected)) == 2


async def test_unknown_policy_is_rejected(rig):
    with pytest.raises(ValueError):
        replace(rig.settings, scheduler_policy="typo")
