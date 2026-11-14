"""Multi-slot admission, lease fencing, cancellation and CPU budget tests."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from time import monotonic, sleep

import pytest

from resource_service import service
from resource_service.timeutil import parse_iso
from tests.resource.conftest import DEMO, JOIN, TASK, minimal_hardware, upload_png
from tests.resource.test_executions import make_execution_request


def setup_pool(pair):
    c, digest = pair
    response = c.post(
        "/api/resource/internal/v1/nodes/register",
        headers=JOIN,
        json={
            "name": "pool",
            "hardware": minimal_hardware(),
            "backend": "cpu",
            "max_slots": 2,
            "threads_per_slot": 2,
            "cpu_thread_budget": 4,
        },
    )
    assert response.status_code == 200, response.text
    n = response.json()
    auth = {k: n[k] for k in ("session_id", "node_secret")}
    prefix = f"/api/resource/internal/v1/nodes/{n['worker_id']}"
    profile = dict(
        task_type="ocr.image.v1", profile_id="ocr.default.v1", profile_digest=digest
    )
    pid = str(uuid.uuid4())
    c.post(
        "/api/resource/v1/preparations",
        headers=TASK,
        json={
            "preparation_id": pid,
            "worker_id": n["worker_id"],
            "worker_session_id": n["session_id"],
            **profile,
        },
    )
    assert (
        c.post(prefix + "/claim", json=auth).json()["preparation"]["preparation_id"]
        == pid
    )
    assert resource(c)["available_slots"] == 0 and resource(c)["reserved_slots"] == 2
    # A lost preparation-claim response is retrievable, without a second preparation.
    assert (
        c.post(prefix + "/claim", json=auth).json()["preparation"]["preparation_id"]
        == pid
    )
    evidence = dict(
        backend="cpu",
        runtime_provider="CPUExecutionProvider",
        model_sha256="0" * 64,
        calibration=dict(
            sample_count=3,
            median_compute_ms=1,
            passed=True,
            finished_at="2026-11-14T10:00:00Z",
        ),
    )
    response = c.post(
        prefix + f"/preparations/{pid}/status",
        json={**auth, "status": "READY", **evidence},
    )
    assert response.json()["status"] == "READY", response.text
    artifact = upload_png(c).json()["artifact_id"]
    return c, n, auth, prefix, digest, artifact, profile, evidence


def resource(c):
    return c.get("/api/resource/v1/resources", headers=DEMO).json()["resources"][0]


def execution(c, eid):
    return c.get("/api/resource/v1/executions/" + eid, headers=TASK).json()


def admit(c, n, digest, artifact):
    response = c.post(
        "/api/resource/v1/executions",
        headers=TASK,
        json=make_execution_request(n, artifact, digest),
    )
    assert response.status_code == 201, response.text
    return response.json()


def claim(c, prefix, auth, slot):
    return c.post(
        prefix + "/claim",
        json={**auth, "slot_id": slot["slot_id"], "generation": slot["generation"]},
    ).json()


def test_atomic_capacity_and_slot_claim(client_with_profile):
    c, n, auth, prefix, digest, artifact, _, _ = setup_pool(client_with_profile)
    assert resource(c)["available_slots"] == 2
    barrier = Barrier(5)

    def submit(_):
        barrier.wait()
        return c.post(
            "/api/resource/v1/executions",
            headers=TASK,
            json=make_execution_request(n, artifact, digest),
        )

    with ThreadPoolExecutor(max_workers=5) as workers:
        replies = list(workers.map(submit, range(5)))
    assert sorted(r.status_code for r in replies) == [201, 201, 409, 409, 409]
    assert {r.json()["slot_id"] for r in replies if r.status_code == 201} == {0, 1}
    assert resource(c)["reserved_slots"] == 2 and resource(c)["available_slots"] == 0
    assert c.post(prefix + "/claim", json=auth).json()["execution"] is None
    for slot in n["slots"]:
        assert claim(c, prefix, auth, slot)["execution"]["slot_id"] == slot["slot_id"]
        assert claim(c, prefix, auth, slot)["execution"] is None


@pytest.mark.parametrize("claimed", [True, False])
def test_single_cancel_fences_only_one_slot(client_with_profile, claimed):
    c, n, auth, prefix, digest, artifact, profile, evidence = setup_pool(
        client_with_profile
    )
    first, second = [admit(c, n, digest, artifact) for _ in range(2)]
    for slot in n["slots"]:
        if claimed or slot["slot_id"] == second["slot_id"]:
            claim(c, prefix, auth, slot)
    eid = first["execution_id"]
    c.post("/api/resource/v1/executions/" + eid + "/cancel", headers=TASK)
    assert (
        c.post(prefix + f"/executions/{eid}/cancel_ack", json=auth).status_code == 409
    )
    slot = n["slots"][first["slot_id"]]
    stopped = {**auth, "generation": slot["generation"]}
    new = c.post(prefix + f"/slots/{slot['slot_id']}/stopped", json=stopped).json()
    assert execution(c, eid)["status"] == "CANCELLED"
    assert execution(c, second["execution_id"])["status"] == "RUNNING"
    assert resource(c)["available_slots"] == 0  # reload required
    ready = {**auth, **profile, **evidence, "generation": new["generation"]}
    assert (
        c.post(prefix + f"/slots/{slot['slot_id']}/ready", json=ready).status_code
        == 200
    )
    third = admit(c, n, digest, artifact)
    assert third["slot_id"] == slot["slot_id"]
    assert (
        c.post(prefix + f"/slots/{slot['slot_id']}/stopped", json=stopped).json()[
            "generation"
        ]
        == new["generation"]
    )
    assert execution(c, third["execution_id"])["status"] == "ACCEPTED"
    assert (
        c.post(
            prefix + f"/slots/{slot['slot_id']}/ready",
            json={**ready, "generation": slot["generation"]},
        ).status_code
        == 409
    )
    late = c.post(
        prefix + f"/executions/{eid}/complete",
        json={**auth, "outcome": "FAILED", "error_code": "EXECUTION_ERROR"},
    )
    assert late.status_code == 409


def test_heartbeat_only_renews_live_slots(client_with_profile, monkeypatch):
    c, n, auth, prefix, digest, artifact, profile, evidence = setup_pool(
        client_with_profile
    )
    first, second = [admit(c, n, digest, artifact) for _ in range(2)]
    for slot in n["slots"]:
        claim(c, prefix, auth, slot)
    first, second = [execution(c, e["execution_id"]) for e in (first, second)]
    point = parse_iso(second["lease_expires_at"]) - timedelta(seconds=2)
    monkeypatch.setattr(service, "utcnow", lambda: point)
    c.post(
        prefix + "/heartbeat",
        json={**auth, "running_execution_ids": [second["execution_id"]]},
    )
    assert (
        execution(c, first["execution_id"])["lease_expires_at"]
        == first["lease_expires_at"]
    )
    assert (
        execution(c, second["execution_id"])["lease_expires_at"]
        > second["lease_expires_at"]
    )
    point += timedelta(seconds=3)
    deadline = monotonic() + 4
    while (
        execution(c, first["execution_id"])["status"] == "RUNNING"
        and monotonic() < deadline
    ):
        sleep(0.1)
    assert execution(c, first["execution_id"])["status"] == "FAILED"
    assert execution(c, second["execution_id"])["status"] == "RUNNING"
    assert not resource(c)["cleanup_pending"] and resource(c)["available_slots"] == 0
    slot = n["slots"][first["slot_id"]]
    # Late ready from the timed-out process cannot reopen a blocked slot.
    assert (
        c.post(
            prefix + f"/slots/{slot['slot_id']}/ready",
            json={**auth, **profile, **evidence, "generation": slot["generation"]},
        ).status_code
        == 409
    )


def test_sharing_off_preserves_migration_for_all_slots(client_with_profile):
    c, n, auth, prefix, digest, artifact, _, _ = setup_pool(client_with_profile)
    executions = [admit(c, n, digest, artifact) for _ in range(2)]
    for slot in n["slots"]:
        claim(c, prefix, auth, slot)
    assert (
        c.post(prefix + "/sharing", json={**auth, "enabled": False}).status_code == 200
    )
    assert resource(c)["available_slots"] == 0
    assert (
        c.post(prefix + "/sharing", json={**auth, "enabled": True}).status_code == 409
    )
    assert c.post(prefix + "/runtime_stopped", json=auth).status_code == 200
    for e in executions:
        final = execution(c, e["execution_id"])
        assert (
            final["status"] == "CANCELLED"
            and final["error"]["code"] == "NODE_UNAVAILABLE"
        )
        assert final["error"]["retryable"]


@pytest.mark.parametrize(
    "backend,count,threads,budget",
    [("cuda", 2, 2, 4), ("cpu", 3, 2, 4), ("cpu", 2, 0, 4), ("cpu", 2, 2, 12)],
)
def test_reject_invalid_budget(client, backend, count, threads, budget):
    reply = client.post(
        "/api/resource/internal/v1/nodes/register",
        headers=JOIN,
        json={
            "name": "bad",
            "hardware": minimal_hardware(),
            "backend": backend,
            "max_slots": count,
            "threads_per_slot": threads,
            "cpu_thread_budget": budget,
        },
    )
    assert reply.status_code == 422


def test_agent_budget_auto_and_gpu_single_slot(tmp_path, monkeypatch):
    from node_agent.agent import Agent

    monkeypatch.setenv("JOIN_TOKEN", "test")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("NODE_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    a = Agent("http://localhost", "test", slots=3, cpu_budget=7)
    assert a.threads == 2 and a.cpu_budget == 7
    a._client.close()
    with pytest.raises(ValueError):
        Agent("http://localhost", "test", slots=3, threads=3, cpu_budget=7)
    with pytest.raises(ValueError):
        Agent("http://localhost", "test", backend="cuda", slots=2)


def resident_test_worker(pipe, server, name, identity, options):
    """Real processes with controlled blocking, to exercise kill/join ordering."""
    import os
    from pathlib import Path

    while True:
        kind, payload = pipe.recv()
        assert os.environ["OMP_NUM_THREADS"] == str(options["threads"])
        if kind == "execution":
            Path(payload["marker"]).write_text(str(os.getpid()))
            if payload.get("crash"):
                os._exit(7)
            while payload.get("block"):
                sleep(0.01)
        pipe.send(
            {
                "session_stale": False,
                "evidence": {"calibration": {"median_compute_ms": 1}},
                "error": None,
            }
        )


@pytest.mark.parametrize("reason", ["cancel", "deadline", "blocked", "crash"])
def test_pool_stops_only_affected_process_before_release(tmp_path, monkeypatch, reason):
    from types import SimpleNamespace
    from node_agent import compute
    from node_agent.pool import Pool

    monkeypatch.setattr(compute, "compute", resident_test_worker)
    a = SimpleNamespace(
        _slot_metadata=[
            {"slot_id": i, "generation": str(uuid.uuid4())} for i in range(2)
        ],
        server="",
        name="",
        worker_id="w",
        session_id="s",
        node_secret="n",
        threads=1,
        _cancel_ids=set(),
        _session_stale=False,
    )
    pool = Pool(a)
    released = []

    def call(suffix, payload=None):
        if suffix.endswith("/stopped"):
            assert not first.is_alive() and second.is_alive()
            released.append(suffix)
            return {"generation": str(uuid.uuid4())}
        return {"ready": True}

    pool.call = call
    try:
        for i, slot in enumerate(pool.slots):
            pool.submit(
                slot,
                "execution",
                {
                    "execution_id": f"e{i}",
                    "block": True,
                    "crash": reason == "crash" and i == 0,
                    "marker": str(tmp_path / str(i)),
                },
            )
        first, second = [s.process for s in pool.slots]
        deadline = monotonic() + 8
        while (
            not all((tmp_path / str(i)).exists() for i in range(2))
            and monotonic() < deadline
        ):
            sleep(0.01)
        assert all((tmp_path / str(i)).exists() for i in range(2))
        if reason == "cancel":
            a._cancel_ids.add("e0")
        if reason == "deadline":
            pool.slots[0].payload["hard_deadline_at"] = "2020-01-01T00:00:00Z"
        if reason == "blocked":
            a._slot_metadata[0]["blocked"] = True
        deadline = monotonic() + 5
        while not released and monotonic() < deadline:
            pool.poll(pool.slots[0])
            sleep(0.01)
        assert released == ["slots/0/stopped"]
        assert not first.is_alive() and second.is_alive()
        assert pool.running_ids() == ["e1"]
        pool.stop()
        assert not second.is_alive()
    finally:
        pool.stop()


def test_real_ort_sessions_use_thread_budget():
    from runners.rapidocr_runner import RapidOCRRunner

    engine = RapidOCRRunner(threads=2).ensure_engine()
    for stage in (engine.text_det, engine.text_cls, engine.text_rec):
        options = stage.session.session.get_session_options()
        assert options.intra_op_num_threads == 2 and options.inter_op_num_threads == 1


def test_pool_fetches_repreparation_even_when_all_slots_cold(monkeypatch):
    from types import SimpleNamespace
    from node_agent.pool import Pool

    a = SimpleNamespace(
        _slot_metadata=[{"slot_id": 0, "generation": "g"}],
        _session_stale=False,
        _sharing_disabled=False,
    )
    pool = Pool(a)
    pool.profile = {"profile_id": "previous"}
    prep = {"preparation_id": "new"}
    seen = []
    monkeypatch.setattr(pool, "poll", lambda slot: None)
    monkeypatch.setattr(pool, "call", lambda *args: {"preparation": prep})
    monkeypatch.setattr(pool, "begin_preparation", seen.append)
    pool.tick()
    assert seen == [prep]


def test_late_heartbeat_cannot_replace_new_session_slot_generations(
    tmp_path, monkeypatch
):
    from node_agent.agent import Agent

    monkeypatch.setenv("JOIN_TOKEN", "test")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("NODE_AGENT_DATA_DIR", str(tmp_path))
    a = Agent("http://localhost", "test")
    a.worker_id, a.session_id, a.node_secret = "worker", "old", "secret"
    a._slot_metadata = [{"generation": "new"}]

    def late(*args):
        a.session_id = "new-session"
        return {
            "sharing_enabled": False,
            "slots": [{"generation": "old"}],
            "cancel_requested": ["old"],
        }

    monkeypatch.setattr(a, "_internal", late)
    monkeypatch.setattr("node_agent.agent.sample", lambda *_: {})
    try:
        a._heartbeat()
        assert a._slot_metadata == [{"generation": "new"}]
        assert not a._sharing_disabled and not a._cancel_ids
    finally:
        a._client.close()
