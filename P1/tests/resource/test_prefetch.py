"""Real CPU decode/process tests. Windows CUDA throughput is a separate gate."""

import hashlib
import io
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image, ImageOps

from node_agent import compute, input_pipeline
from node_agent.agent import Agent
from resource_service import prefetch
from resource_service.timeutil import iso, utcnow
from tests.resource.conftest import DEMO, JOIN, TASK, minimal_hardware, upload_png
from tests.resource.test_executions import make_execution_request
from tests.resource.test_gpu_resident import fake_agent, resident_echo


def setup(pair):
    c, digest = pair
    response = c.post(
        "/api/resource/internal/v1/nodes/register",
        headers=JOIN,
        json={
            "name": "prefetch",
            "hardware": minimal_hardware(),
            "backend": "cpu",
            "threads_per_slot": 1,
            "cpu_thread_budget": 2,
            "prefetch_capacity": 1,
        },
    )
    assert response.status_code == 200, response.text
    n = response.json()
    auth = {k: n[k] for k in ("session_id", "node_secret")}
    internal = f"/api/resource/internal/v1/nodes/{n['worker_id']}"
    public = f"/api/resource/v1/resources/{n['worker_id']}/prefetch"
    profile = dict(
        task_type="ocr.image.v1", profile_id="ocr.default.v1", profile_digest=digest
    )
    pid = str(uuid.uuid4())
    c.post(
        "/api/resource/v1/preparations",
        headers=TASK,
        json=dict(
            preparation_id=pid,
            worker_id=n["worker_id"],
            worker_session_id=n["session_id"],
            **profile,
        ),
    )
    c.post(internal + "/claim", json=auth)
    response = c.post(
        internal + f"/preparations/{pid}/status",
        json=dict(
            **auth,
            status="READY",
            backend="cpu",
            runtime_provider="CPUExecutionProvider",
            model_sha256="0" * 64,
            calibration=dict(
                sample_count=3,
                median_compute_ms=1,
                passed=True,
                finished_at=iso(utcnow()),
            ),
        ),
    )
    assert response.json()["status"] == "READY"
    artifact = upload_png(c).json()
    hint = dict(
        **profile,
        job_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        input_artifact_id=artifact["artifact_id"],
    )
    return (
        c,
        n,
        auth,
        internal,
        public,
        dict(worker_session_id=n["session_id"], revision=1, hint=hint),
    )


def test_hint_does_not_reserve_and_clear_fences_late_put(client_with_profile):
    c, n, auth, internal, public, body = setup(client_with_profile)
    assert c.post(public, headers=DEMO, json=body).status_code == 403
    for _ in range(2):
        assert c.post(public, headers=TASK, json=body).status_code == 200
    snapshot = c.get("/api/resource/v1/resources", headers=TASK).json()["resources"][0]
    assert snapshot["available_slots"] == 1 and snapshot["reserved_slots"] == 0
    assert snapshot["prefetch_capacity"] == snapshot["prefetch_revision"] == 1
    hint = c.post(internal + "/prefetch", json=auth).json()["hint"]
    assert hint["input_sha256"] and hint["input_media_info"]["width"] == 64
    assert c.post(internal + "/claim", json=auth).json()["execution"] is None
    assert (
        c.post(
            public, headers=TASK, json=body | {"revision": 2, "hint": None}
        ).status_code
        == 200
    )
    assert c.post(public, headers=TASK, json=body).status_code == 409
    assert c.post(public, headers=TASK, json=body | {"revision": 2}).status_code == 409
    assert c.post(internal + "/prefetch", json=auth).json()["hint"] is None


def test_hint_ttl_off_and_session_fence(client_with_profile, monkeypatch):
    c, n, auth, internal, public, body = setup(client_with_profile)
    assert c.post(public, headers=TASK, json=body).status_code == 200
    current = utcnow()
    monkeypatch.setattr(prefetch, "utcnow", lambda: current + timedelta(seconds=16))
    assert c.post(internal + "/prefetch", json=auth).json()["hint"] is None
    monkeypatch.setattr(prefetch, "utcnow", lambda: current)
    assert (
        c.post(internal + "/sharing", json=auth | {"enabled": False}).status_code == 200
    )
    assert c.post(internal + "/prefetch", json=auth).json()["hint"] is None
    assert c.post(public, headers=TASK, json=body | {"revision": 2}).status_code == 409
    assert c.post(internal + "/runtime_stopped", json=auth).status_code == 200
    assert c.post(public, headers=TASK, json=body | {"revision": 3}).status_code == 409
    assert c.post(internal + "/prefetch", json=auth).status_code == 409


def test_accepted_input_retained_but_cancel_not_owned(client_with_profile):
    c, n, auth, internal, public, body = setup(client_with_profile)
    hint = body["hint"]
    request = make_execution_request(
        n,
        hint["input_artifact_id"],
        hint["profile_digest"],
        job_id=hint["job_id"],
        task_id=hint["task_id"],
    )
    assert c.post(public, headers=TASK, json=body).status_code == 200
    assert (
        c.post("/api/resource/v1/executions", headers=TASK, json=request).status_code
        == 201
    )
    assert c.post(internal + "/prefetch", json=auth).json()["owned_tasks"] == [
        [hint["job_id"], hint["task_id"]]
    ]
    c.post(
        f"/api/resource/v1/executions/{request['execution_id']}/cancel", headers=TASK
    )
    got = c.post(internal + "/prefetch", json=auth).json()
    assert got["owned_tasks"] == [] and got["hint"] is None


@pytest.mark.parametrize(
    "options",
    [
        dict(prefetch=1, slots=2),
        dict(prefetch=1, cpu_budget=1),
        dict(prefetch=1, threads=2, cpu_budget=2),
    ],
)
def test_agent_prefetch_thread_budget(options, monkeypatch, tmp_path):
    monkeypatch.setenv("JOIN_TOKEN", "test")
    monkeypatch.setenv("NODE_AGENT_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        Agent("http://localhost", "test", **options)


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_real_decode_cache_matches_exif_rgb(tmp_path, orientation):
    image = Image.new("RGB", (20, 10), "red")
    exif = Image.Exif()
    exif[274] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    content = buffer.getvalue()
    with Image.open(io.BytesIO(content)) as im:
        expected = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    media = dict(width=expected.shape[1], height=expected.shape[0])
    hint = dict(
        input_artifact_id="a1",
        input_sha256=hashlib.sha256(content).hexdigest(),
        input_size_bytes=len(content),
        input_media_info=media,
        input_content_type="image/jpeg",
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
    ) as client:
        result = input_pipeline.prepare_input(
            client, "http://local", "w", "secret", hint, tmp_path
        )
        assert np.array_equal(input_pipeline.load_decoded(result, media), expected)
        with pytest.raises(ValueError):
            input_pipeline.prepare_input(
                client,
                "http://local",
                "w",
                "s",
                hint | {"input_sha256": "0" * 64},
                tmp_path,
            )
    Path(result["path"]).write_bytes(b"broken")
    with pytest.raises(ValueError):
        input_pipeline.load_decoded(result, media)


def blocking_input(pipe, server, worker, secret):
    while True:
        hint, directory = pipe.recv()
        Path(directory).mkdir(parents=True, exist_ok=True)
        Path(directory, "started").write_text("started")
        while True:
            time.sleep(0.01)


def pipeline_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(input_pipeline, "input_worker", blocking_input)
    a = fake_agent()
    a.data_dir = tmp_path
    hint = dict(
        job_id="j",
        task_id="t",
        input_artifact_id="a",
        input_sha256="0" * 64,
        profile_digest="1" * 64,
        expires_at=iso(utcnow() + timedelta(seconds=15)),
    )
    snapshot = dict(hint=hint, owned_tasks=[], enabled=True)
    a._auth_payload = lambda: {}
    a._internal = lambda *args, **kwargs: snapshot
    a._input_pipeline = input_pipeline.InputPipeline(a)
    a._input_pipeline.tick(force=True)
    deadline = time.monotonic() + 10
    while not list(a._input_pipeline.directory.glob("*/started")):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return a, snapshot


def test_future_cancel_only_stops_input_worker(tmp_path, monkeypatch):
    a, snapshot = pipeline_agent(tmp_path, monkeypatch)
    process = a._input_pipeline.process
    monkeypatch.setattr(compute, "compute", resident_echo)
    try:
        compute.supervise(
            a,
            "preparation",
            dict(
                task_type="t",
                profile_id="p",
                profile_digest="d",
                marker=str(tmp_path / "model"),
            ),
        )
        model = a._runtime[0]
        snapshot["hint"] = None
        a._input_pipeline.tick(force=True)
        assert not process.is_alive() and model.is_alive()
        assert not a._input_pipeline.directory.exists()
    finally:
        compute.close_runtime(a)


@pytest.mark.parametrize(
    "reason", ["cancel", "off", "session", "heartbeat", "exit", "deadline"]
)
def test_active_interrupt_joins_both_before_ack(tmp_path, monkeypatch, reason):
    a, snapshot = pipeline_agent(tmp_path, monkeypatch)
    decoder = a._input_pipeline.process
    monkeypatch.setattr(compute, "compute", resident_echo)
    payload = dict(
        task_type="t",
        profile_id="p",
        profile_digest="d",
        marker=str(tmp_path / "model"),
    )
    compute.supervise(a, "preparation", payload)
    model = a._runtime[0]
    ack = []

    def confirm(eid):
        assert not model.is_alive() and not decoder.is_alive()
        ack.append(eid)

    a._ack_cancel = confirm
    if reason == "cancel":
        a._cancel_ids.add("e")
    if reason == "off":
        a._sharing_disabled = True
    if reason == "session":
        a._session_stale = True
    if reason == "heartbeat":
        a._last_heartbeat = time.monotonic() - 13
    if reason == "exit":
        a._stop = True
    if reason == "deadline":
        payload["hard_deadline_at"] = "2020-01-01T00:00:00Z"
    try:
        compute.supervise(
            a, "execution", payload | {"execution_id": "e", "block": True}
        )
        assert ack == ["e"] and not (tmp_path / "prefetch").exists()
    finally:
        compute.close_runtime(a)


def test_owned_input_survives_hint_replacement_and_inflight_miss_stops(
    tmp_path, monkeypatch
):
    a, snapshot = pipeline_agent(tmp_path, monkeypatch)
    process = a._input_pipeline.process
    execution = snapshot["hint"] | {"execution_id": "e"}
    snapshot.update(hint=None, owned_tasks=[["j", "t"]])
    try:
        a._input_pipeline.tick(force=True)
        assert process.is_alive()
        assert a._input_pipeline.take(execution) is None
        assert not process.is_alive() and not a._input_pipeline.directory.exists()
    finally:
        compute.close_runtime(a)


def test_bad_cache_falls_back_without_failed_attempt(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("JOIN_TOKEN", "test")
    monkeypatch.setenv("NODE_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    a = Agent("http://localhost", "test", prefetch=0)
    a.worker_id, a.session_id, a.node_secret = "w", "s", "secret"
    source = tmp_path / "image.png"
    Image.new("RGB", (20, 10), "red").save(source)
    cache = tmp_path / "bad.npy"
    cache.write_bytes(b"corrupt")
    calls = []
    a._internal = lambda path, body: calls.append(body) or {"status": "SUCCEEDED"}
    a._download_input = lambda _: str(source)

    def execute_array(array, parameters, workdir):
        assert array.shape == (10, 20, 3)
        return {"text": "real input decoded"}

    a._prepared = (
        ("ocr.image.v1", "p", "d"),
        SimpleNamespace(execute_array=execute_array),
    )
    try:
        a._do_execution(
            dict(
                execution_id="e",
                task_type="ocr.image.v1",
                profile_id="p",
                profile_digest="d",
                input_artifact_id="a",
                input_media_info={"width": 20, "height": 10},
                _prefetched={"path": str(cache), "sha256": "0" * 64},
            )
        )
        assert calls[-1]["outcome"] == "SUCCEEDED"
        assert not calls[-1]["timings"].get("prefetch_hit") and not cache.exists()
        assert calls[-1]["timings"]["decode_ms"] > 0
    finally:
        a._client.close()


def test_download_and_decode_limits(tmp_path):
    content = b"x" * (input_pipeline.MAX_BYTES + 1)
    hint = dict(
        input_artifact_id="a", input_size_bytes=len(content), input_sha256="0" * 64
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
    ) as client:
        with pytest.raises(ValueError, match="大小"):
            input_pipeline.prepare_input(
                client, "http://local", "w", "s", hint, tmp_path
            )
    from resource_service.media import probe_image

    image = Image.new("RGB", (64, 32), "red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    truncated = buffer.getvalue()[:-20]
    probe_image(truncated, "image/jpeg")  # Header-only verification is insufficient.
    path = tmp_path / "truncated.jpg"
    path.write_bytes(truncated)
    with pytest.raises(OSError):
        input_pipeline.decode_image(path, {})


def test_pixel_limit_precedes_allocation(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    monkeypatch.setattr(
        Image, "open", lambda _: nullcontext(SimpleNamespace(width=5000, height=5000))
    )
    with pytest.raises(ValueError, match="像素"):
        input_pipeline.decode_image(tmp_path / "oversized.png", {})


@pytest.mark.parametrize(
    "extra", [dict(threads_per_slot=0), dict(cpu_thread_budget=1), dict(max_slots=2)]
)
def test_center_enforces_prefetch_budget(client, extra):
    body = (
        dict(
            name="invalid",
            hardware=minimal_hardware(),
            backend="cpu",
            max_slots=1,
            threads_per_slot=1,
            cpu_thread_budget=2,
            prefetch_capacity=1,
        )
        | extra
    )
    assert (
        client.post(
            "/api/resource/internal/v1/nodes/register", headers=JOIN, json=body
        ).status_code
        == 422
    )
