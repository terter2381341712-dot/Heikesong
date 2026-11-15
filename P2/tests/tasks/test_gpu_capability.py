"""Protocol-only CUDA scheduling; no GPU execution is claimed by these tests."""

from task_service.contract import error, now
import pytest


async def test_cuda_node_executes_same_profile_as_cpu(rig):
    node = rig.mock.nodes[0]
    node["capabilities"][0].update(backend="cuda", runtime_provider="CUDAExecutionProvider")
    job = await rig.create()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    used = {t["dispatches"][-1]["worker_id"] for t in final["tasks"]}
    assert used == {n["worker_id"] for n in rig.mock.nodes}
    assert all(t["attempts_used"] == 1 for t in final["tasks"])


@pytest.mark.parametrize("backend,provider", [("directml", "DmlExecutionProvider"), ("coreml", "CoreMLExecutionProvider")])
async def test_integrated_gpu_routes_are_schedulable_with_same_cpu_profile(rig, backend, provider):
    node = rig.mock.nodes[0]
    node["capabilities"][0].update(backend=backend, runtime_provider=provider)
    job = await rig.create()
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    assert node["worker_id"] in {t["dispatches"][-1]["worker_id"] for t in final["tasks"]}


async def test_resource_aware_policy_prefers_faster_gpu_over_cpu_rotation(rig):
    from dataclasses import replace
    rig.service.settings = replace(rig.service.settings, scheduler_policy="resource_aware")
    rig.service.scheduler.settings = rig.service.settings
    cpu, gpu = rig.mock.nodes
    cpu["capabilities"][0]["calibration"]["median_compute_ms"] = 1000
    gpu["capabilities"][0].update(backend="directml", runtime_provider="DmlExecutionProvider")
    gpu["capabilities"][0]["calibration"]["median_compute_ms"] = 10
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    assert rig.mock.posts[-1]["worker_id"] == gpu["worker_id"]


async def test_cuda_withdrawal_migrates_to_cpu_without_changing_profile(rig):
    gpu, cpu = rig.mock.nodes
    gpu["capabilities"][0].update(backend="cuda", runtime_provider="CUDAExecutionProvider")
    rig.mock.finish_after["mock_image_1"] = 1000
    job = await rig.create(["mock_image_1"])
    await rig.tick()
    execution = next(iter(rig.mock.executions.values()))
    assert execution["worker_id"] == gpu["worker_id"]
    gpu.update(mode="PAUSED", available_slots=0)
    execution.update(
        status="CANCELLED",
        finished_at=now(),
        error=error(
            "NODE_UNAVAILABLE",
            "GPU owner disabled sharing",
            True,
            execution_id=execution["execution_id"],
        ),
    )
    rig.mock.finish_after["mock_image_1"] = 1
    final = await rig.finish(job["job_id"])
    assert final["status"] == "SUCCEEDED"
    task = final["tasks"][0]
    assert task["attempts_used"] == 2
    assert task["dispatches"][-1]["worker_id"] == cpu["worker_id"]
    assert final["profile_digest"] == job["profile_digest"]
