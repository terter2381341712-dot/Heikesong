"""CUDA sessions below are simulated; resident processes and CPU OCR run locally."""

import hashlib
import json
import os
import time
import uuid
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from node_agent import compute
from resource_service.config import Config
from resource_service.service import create_app
from runners import profile
from runners.rapidocr_runner import RapidOCRRunner
from tests.resource.conftest import DEMO, TASK, register_node, upload_png
from tests.resource.test_executions import make_execution_request, setup_ready


def resident_echo(pipe, server, name, identity, options):
    key = None
    while True:
        kind, payload = pipe.recv()
        if kind == "preparation":
            key = tuple(
                payload[k] for k in ("task_type", "profile_id", "profile_digest")
            )
        Path(payload["marker"]).write_text(str(os.getpid()))
        if payload.get("crash"):
            os._exit(7)
        if payload.get("block"):
            while True:
                time.sleep(0.01)
        pipe.send({"prepared": key, "session_stale": False})


def fake_agent():
    return SimpleNamespace(
        server="",
        name="",
        worker_id="w",
        session_id="s",
        node_secret="n",
        _prepared=None,
        _sharing_disabled=False,
        _session_stale=False,
        _cancel_ids=set(),
        _stop=False,
        _ack_cancel=lambda _: None,
    )


def test_resident_reuses_process_and_prepared_state(tmp_path, monkeypatch):
    monkeypatch.setattr(compute, "compute", resident_echo)
    a = fake_agent()
    payload = dict(
        task_type="ocr.image.v1",
        profile_id="p",
        profile_digest="a" * 64,
        marker=str(tmp_path / "pid"),
    )
    try:
        compute.supervise(a, "preparation", payload)
        pid = a._runtime[0].pid
        assert a._prepared[0] == ("ocr.image.v1", "p", "a" * 64)
        for _ in range(3):
            compute.supervise(a, "execution", {**payload, "execution_id": "e"})
            assert int(Path(payload["marker"]).read_text()) == pid
            assert a._runtime[0].is_alive()
    finally:
        compute.close_runtime(a)
    assert a._prepared is None and a._runtime_reset_pending


@pytest.mark.parametrize(
    "reason", ["cancel", "off", "heartbeat", "session", "exit", "deadline"]
)
def test_resident_stopped_before_ack(tmp_path, monkeypatch, reason):
    monkeypatch.setattr(compute, "compute", resident_echo)
    a = fake_agent()
    payload = dict(
        task_type="ocr.image.v1",
        profile_id="p",
        profile_digest="a" * 64,
        marker=str(tmp_path / "pid"),
    )
    compute.supervise(a, "preparation", payload)
    process = a._runtime[0]
    ack = []

    def confirmed(eid):
        assert not process.is_alive()
        ack.append(eid)

    a._ack_cancel = confirmed
    if reason == "cancel":
        a._cancel_ids.add("e")
    if reason == "off":
        a._sharing_disabled = True
    if reason == "heartbeat":
        a._last_heartbeat = time.monotonic() - 13
    if reason == "session":
        a._session_stale = True
    if reason == "exit":
        a._stop = True
    if reason == "deadline":
        payload["hard_deadline_at"] = "2020-01-01T00:00:00Z"
    try:
        compute.supervise(
            a, "execution", {**payload, "execution_id": "e", "block": True}
        )
        assert ack == ["e"] and a._runtime is None and a._runtime_reset_pending
    finally:
        compute.close_runtime(a)


def test_crash_requests_runtime_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(compute, "compute", resident_echo)
    a = fake_agent()
    compute.supervise(
        a,
        "execution",
        {"execution_id": "e", "marker": str(tmp_path / "pid"), "crash": True},
    )
    assert a._runtime is None and a._runtime_reset_pending


def install_fake_rapidocr(
    monkeypatch, providers, actual="CUDAExecutionProvider", device="1"
):
    import rapidocr
    import onnxruntime

    seen = {}

    class Session:
        def get_providers(self):
            return [actual, "CPUExecutionProvider"]

        def disable_fallback(self):
            seen["disabled"] = seen.get("disabled", 0) + 1

        def get_provider_options(self):
            return {"CUDAExecutionProvider": {"device_id": device}}

    class Engine:
        def __init__(self, params):
            seen["params"] = params
            self.text_det = SimpleNamespace(session=SimpleNamespace(session=Session()))
            self.text_cls = SimpleNamespace(session=SimpleNamespace(session=Session()))
            self.text_rec = SimpleNamespace(session=SimpleNamespace(session=Session()))

        def _load_det_model(self):
            pass

        def _load_cls_model(self):
            pass

        def _load_rec_model(self):
            pass

    monkeypatch.setattr(rapidocr, "RapidOCR", Engine)
    monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: providers)
    return seen


def test_cuda_wiring_requires_all_sessions_and_selected_device(monkeypatch):
    seen = install_fake_rapidocr(
        monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"]
    )
    runner = RapidOCRRunner(backend="cuda", device_id=1)
    assert runner.runtime_provider() == "CUDAExecutionProvider"
    assert seen["params"]["EngineConfig.onnxruntime.use_cuda"] is True
    assert seen["params"]["EngineConfig.onnxruntime.cuda_ep_cfg.device_id"] == 1
    assert seen["disabled"] >= 3


@pytest.mark.parametrize(
    "providers,actual,device",
    [
        (["CPUExecutionProvider"], "CPUExecutionProvider", "1"),
        (["CUDAExecutionProvider"], "CPUExecutionProvider", "1"),
        (["CUDAExecutionProvider"], "CUDAExecutionProvider", "0"),
    ],
)
def test_cuda_cannot_silently_fallback(monkeypatch, providers, actual, device):
    install_fake_rapidocr(monkeypatch, providers, actual, device)
    with pytest.raises(ValueError):
        RapidOCRRunner(backend="cuda", device_id=1).runtime_provider()


def test_cuda_dependencies_use_gpu_distribution(monkeypatch):
    p = json.loads(
        Path("profiles/heterogeneous/ocr.heterogeneous.v1.lock.json").read_bytes()
    )
    raw = json.dumps(p).encode()
    expected = p["backend_dependencies"]["cuda"]
    monkeypatch.setattr(profile.platform, "python_version", lambda: expected["python"])

    def version(package):
        if package not in expected:
            raise PackageNotFoundError(package)
        return expected[package]

    monkeypatch.setattr(profile, "version", version)
    seen = install_fake_rapidocr(monkeypatch, ["CUDAExecutionProvider"], device="0")
    _, _, provider = profile.load_locked_runner(
        raw, p["task_type"], p["profile_id"], hashlib.sha256(raw).hexdigest(), "cuda"
    )
    assert (
        provider == "CUDAExecutionProvider"
        and seen["params"]["EngineConfig.onnxruntime.use_cuda"]
    )


def test_heterogeneous_cpu_loads_real_model():
    raw = Path("profiles/heterogeneous/ocr.heterogeneous.v1.lock.json").read_bytes()
    p = json.loads(raw)
    runner, _, provider = profile.load_locked_runner(
        raw, p["task_type"], p["profile_id"], hashlib.sha256(raw).hexdigest()
    )
    assert provider == "CPUExecutionProvider"
    assert runner.ensure_engine() is runner.ensure_engine()


def test_gpu_probe_counts_operator_placement_not_provider_availability():
    from node_agent.gpu_probe import provider_counts

    assert provider_counts(
        [
            {"cat": "Session", "args": {"provider": "CUDAExecutionProvider"}},
            {"cat": "Node", "args": {"provider": "CPUExecutionProvider"}},
            {"cat": "Node", "args": {"provider": "CUDAExecutionProvider"}},
            {"cat": "Node", "args": {"provider": "CUDAExecutionProvider"}},
        ]
    ) == {"CPUExecutionProvider": 1, "CUDAExecutionProvider": 2}


@pytest.mark.parametrize(
    "allowed,provider,expected",
    [
        (["cpu", "cuda"], "CUDAExecutionProvider", "READY"),
        (["cpu"], "CUDAExecutionProvider", "FAILED"),
        (["cpu", "cuda"], "CPUExecutionProvider", "FAILED"),
    ],
)
def test_centre_checks_cuda_against_frozen_profile(
    tmp_path, allowed, provider, expected
):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    raw = json.dumps(
        dict(
            profile_id="ocr.default.v1",
            task_type="ocr.image.v1",
            model_sha256="0" * 64,
            allowed_backends=allowed,
            weights_license_verified=True,
        )
    ).encode()
    (pdir / "ocr.default.v1.lock.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    cfg = Config(
        bind_host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        profiles_dir=pdir,
        demo_token="demo-t",
        task_token="task-t",
        join_token="join-t",
    )
    with TestClient(create_app(cfg)) as c:
        n = register_node(c)
        auth = {k: n[k] for k in ("session_id", "node_secret")}
        prefix = f"/api/resource/internal/v1/nodes/{n['worker_id']}"
        pid = str(uuid.uuid4())
        request = dict(
            preparation_id=pid,
            worker_id=n["worker_id"],
            worker_session_id=n["session_id"],
            task_type="ocr.image.v1",
            profile_id="ocr.default.v1",
            profile_digest=digest,
        )
        assert (
            c.post(
                "/api/resource/v1/preparations", headers=TASK, json=request
            ).status_code
            == 202
        )
        c.post(prefix + "/claim", json=auth)
        prepared = c.post(
            prefix + f"/preparations/{pid}/status",
            json={
                **auth,
                "status": "READY",
                "backend": "cuda",
                "runtime_provider": provider,
                "model_sha256": "0" * 64,
                "calibration": {
                    "passed": True,
                    "sample_count": 3,
                    "median_compute_ms": 1,
                    "finished_at": "2026-11-14T10:00:00Z",
                },
            },
        )
        assert prepared.json()["status"] == expected
        artifact = upload_png(c).json()
        execution = c.post(
            "/api/resource/v1/executions",
            headers=TASK,
            json=make_execution_request(n, artifact["artifact_id"], digest),
        )
        assert execution.status_code == (201 if expected == "READY" else 409)


@pytest.mark.parametrize("withdrawn", [False, True])
def test_runtime_stop_fences_session_and_preserves_withdrawal(
    client_with_profile, withdrawn
):
    c, digest, n, a = setup_ready(client_with_profile)
    auth = {k: n[k] for k in ("session_id", "node_secret")}
    prefix = f"/api/resource/internal/v1/nodes/{n['worker_id']}"
    request = make_execution_request(n, a["artifact_id"], digest)
    c.post("/api/resource/v1/executions", headers=TASK, json=request)
    c.post(prefix + "/claim", json=auth)
    if withdrawn:
        c.post(prefix + "/sharing", json={**auth, "enabled": False})
    assert c.post(prefix + "/runtime_stopped", json=auth).status_code == 200
    replay = c.post(prefix + "/runtime_stopped", json=auth)
    assert replay.status_code == 409 and replay.json()["code"] == "SESSION_CHANGED"
    state = c.get("/api/resource/v1/resources", headers=DEMO).json()["resources"][0]
    assert state["connection"] == "OFFLINE" and state["available_slots"] == 0
    execution = c.get(
        "/api/resource/v1/executions/" + request["execution_id"], headers=TASK
    ).json()
    assert execution["status"] == ("CANCELLED" if withdrawn else "FAILED")
    if withdrawn:
        assert (
            execution["error"]["code"] == "NODE_UNAVAILABLE"
            and execution["error"]["retryable"]
        )
    next_node = register_node(c, worker_id=n["worker_id"])
    assert next_node["session_id"] != n["session_id"]
    assert c.post(prefix + "/claim", json=auth).status_code == 409
