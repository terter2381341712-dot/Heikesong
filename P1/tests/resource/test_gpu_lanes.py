"""GPU 逻辑多路（方案 B）准入与节点侧校验测试：无需真实 GPU。

覆盖：
- RegisterRequest：GPU 多路在显存预算内的准入、缺显存/超预算的拒绝；CPU 约束不变。
- 节点 Agent：GPU 多路必须声明每路显存；声明后单进程多路成立且预取关闭。
- 资源服务：GPU 多路注册成功并 provision N 槽；缺显存注册被拒。
"""

import time

import pytest

from resource_service.models import HardwareModel, RegisterRequest

GPU_VRAM = 40 * 1024**3
LANE_VRAM = 10 * 1024**3


def _lane_stub_worker(connection, server, name, identity, options, lane_count):
    """模块级桩：模拟 compute_lanes 的父↔子协议（无真实 CUDA 推理）。"""
    evidence = {
        "backend": "cuda",
        "runtime_provider": "CUDAExecutionProvider",
        "model_sha256": "0" * 64,
        "calibration": {
            "sample_count": 3,
            "median_compute_ms": 1,
            "passed": True,
            "finished_at": "2026-11-14T10:00:00Z",
        },
    }
    while True:
        if not connection.poll(0.1):
            continue
        kind, payload = connection.recv()
        if kind == "load":
            connection.send(
                (
                    "load_evidence",
                    {"prepared": None, "session_stale": False, "evidence": evidence, "error": None},
                )
            )
        elif kind == "execution":
            slot_id = payload.get("slot_id")
            connection.send(("accepted", {"slot_id": slot_id}))
            connection.send(
                ("done", {"slot_id": slot_id, "execution_id": payload.get("execution_id"), "session_stale": False})
            )
        elif kind == "stop_lane":
            connection.send(("stopped", {"slot_id": payload.get("slot_id")}))
        elif kind == "stop":
            break
    connection.close()


def gpu_hardware():
    return HardwareModel(
        os="Windows 11",
        arch="x86_64",
        logical_cores=16,
        gpu_name="NVIDIA GeForce",
        gpu_vram_bytes=GPU_VRAM,
        gpu_info_source="detected",
    )


def gpu_hardware_dict():
    return {
        "os": "Windows 11",
        "arch": "x86_64",
        "logical_cores": 16,
        "gpu_name": "NVIDIA GeForce",
        "gpu_vram_bytes": GPU_VRAM,
        "gpu_info_source": "detected",
    }


def test_register_request_gpu_multilane_admits_within_budget():
    r = RegisterRequest(
        worker_id="w1", name="gpu", hardware=gpu_hardware(),
        backend="cuda", max_slots=2, gpu_lane_vram_bytes=LANE_VRAM,
    )
    assert r.max_slots == 2 and r.gpu_lane_vram_bytes == LANE_VRAM


def test_register_request_gpu_multilane_requires_lane_vram():
    with pytest.raises(ValueError):
        RegisterRequest(
            worker_id="w1", name="gpu", hardware=gpu_hardware(),
            backend="cuda", max_slots=2,
        )


def test_register_request_gpu_multilane_rejects_over_budget():
    # 5 路 × 10 GiB = 50 GiB > 40 GiB × 0.9 = 36 GiB
    with pytest.raises(ValueError):
        RegisterRequest(
            worker_id="w1", name="gpu", hardware=gpu_hardware(),
            backend="cuda", max_slots=5, gpu_lane_vram_bytes=LANE_VRAM,
        )


def test_register_request_gpu_single_lane_needs_no_vram():
    r = RegisterRequest(
        worker_id="w1", name="gpu", hardware=gpu_hardware(), backend="cuda", max_slots=1
    )
    assert r.gpu_lane_vram_bytes is None


def test_register_request_cpu_constraints_unchanged():
    cpu_hw = HardwareModel(os="macOS", arch="arm64", logical_cores=8, gpu_info_source="unavailable")
    # CPU 多槽仍要求 backend=cpu 且每槽线程 >= 1。
    with pytest.raises(ValueError):
        RegisterRequest(
            worker_id="w1", name="cpu", hardware=cpu_hw,
            backend="cpu", max_slots=2, threads_per_slot=0,
        )
    # CPU 线程预算校验仍生效（2 槽 × 4 线程 > 4 预算）。
    with pytest.raises(ValueError):
        RegisterRequest(
            worker_id="w1", name="cpu", hardware=cpu_hw,
            backend="cpu", max_slots=2, threads_per_slot=4, cpu_thread_budget=4,
        )


def test_agent_gpu_multilane_requires_lane_vram(tmp_path, monkeypatch):
    from node_agent.agent import Agent

    monkeypatch.setenv("JOIN_TOKEN", "test")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("NODE_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    with pytest.raises(ValueError):
        Agent("http://localhost", "test", backend="cuda", slots=2)
    a = Agent(
        "http://localhost", "test", backend="cuda", slots=2, gpu_lane_vram_bytes=LANE_VRAM
    )
    assert a.slots == 2 and a.prefetch_capacity == 0
    a._client.close()


def test_service_register_gpu_multilane(client):
    resp = client.post(
        "/api/resource/internal/v1/nodes/register",
        headers={"Authorization": "Bearer join-t"},
        json={
            "name": "gpu",
            "hardware": gpu_hardware_dict(),
            "backend": "cuda",
            "max_slots": 2,
            "gpu_lane_vram_bytes": LANE_VRAM,
        },
    )
    assert resp.status_code == 200, resp.text
    node = resp.json()
    assert len(node["slots"]) == 2
    res = client.get(
        "/api/resource/v1/resources", headers={"Authorization": "Bearer demo-t"}
    ).json()["resources"][0]
    assert res["max_slots"] == 2


def test_service_register_gpu_multilane_without_vram_rejected(client):
    resp = client.post(
        "/api/resource/internal/v1/nodes/register",
        headers={"Authorization": "Bearer join-t"},
        json={
            "name": "gpu",
            "hardware": gpu_hardware_dict(),
            "backend": "cuda",
            "max_slots": 2,
        },
    )
    assert resp.status_code == 422


def test_lane_pool_running_ids_and_lane_lookup():
    from types import SimpleNamespace
    from node_agent.gpu_lanes import LanePool

    a = SimpleNamespace(
        _slot_metadata=[
            {"slot_id": 0, "generation": "g0"},
            {"slot_id": 1, "generation": "g1"},
        ],
    )
    pool = LanePool(a)
    assert pool.running_ids() == []
    pool.lanes[0].execution_id = "e0"
    assert pool.running_ids() == ["e0"]
    assert pool._lane(0).execution_id == "e0"
    assert pool._lane(1).execution_id == ""
    pool.lanes[0].execution_id = ""
    assert pool.running_ids() == []


def test_lane_pool_child_protocol_load_and_dispatch(monkeypatch):
    """验证 gpu_lanes 父↔子进程协议：load→evidence、execution→accepted+done、stop。

    用模块级桩替代 compute_lanes，以真实子进程跑通控制面，不依赖 CUDA。
    """
    from types import SimpleNamespace
    from node_agent import compute
    from node_agent.gpu_lanes import LanePool

    monkeypatch.setattr(compute, "compute_lanes", _lane_stub_worker)
    a = SimpleNamespace(
        _slot_metadata=[{"slot_id": 0, "generation": "g0"}],
        server="",
        name="",
        worker_id="w",
        session_id="s",
        node_secret="n",
        backend="cuda",
        device_id=0,
        threads=0,
        _cancel_ids=set(),
        _session_stale=False,
        _sharing_disabled=False,
    )
    pool = LanePool(a)
    try:
        pool._ensure_child()
        assert pool.child is not None
        # load -> load_evidence
        pool.pipe.send(("load", {"task_type": "ocr.image.v1"}))
        deadline = time.monotonic() + 5
        while pool._evidence is None and time.monotonic() < deadline:
            pool._drain()
            time.sleep(0.02)
        assert pool._evidence is not None and pool._evidence["backend"] == "cuda"
        # execution -> accepted + done（done 清空该路 execution_id）
        pool.lanes[0].execution_id = "e0"
        pool.pipe.send(("execution", {"slot_id": 0, "execution_id": "e0"}))
        deadline = time.monotonic() + 5
        while pool.lanes[0].execution_id and time.monotonic() < deadline:
            pool._drain()
            time.sleep(0.02)
        assert pool.lanes[0].execution_id == ""
    finally:
        pool.stop()


class _FakePipe:
    def __init__(self):
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)

    def poll(self):
        return False

    def recv(self):
        return ("stop", None)

    def close(self):
        pass


class _FakeChild:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def kill(self):
        self.alive = False

    def join(self, timeout=None):
        pass


def _fake_agent(n_slots=2):
    from types import SimpleNamespace

    a = SimpleNamespace(
        _slot_metadata=[{"slot_id": i, "generation": f"g{i}"} for i in range(n_slots)],
        server="",
        name="",
        worker_id="w",
        session_id="s",
        node_secret="n",
        backend="cuda",
        device_id=0,
        threads=0,
        _cancel_ids=set(),
        _session_stale=False,
        _sharing_disabled=False,
        _runtime_reset_pending=False,
        prep_calls=[],
    )

    def auth_payload():
        return {"session_id": "s", "node_secret": "n"}

    a._auth_payload = auth_payload

    def internal(path, payload):
        return None

    a._internal = internal

    def prep_status(pid, payload):
        a.prep_calls.append(dict(payload))
        return {"status": "READY"}

    a._prep_status = prep_status
    return a


def _evidence():
    return {
        "backend": "cuda",
        "runtime_provider": "CUDAExecutionProvider",
        "model_sha256": "0" * 64,
        "calibration": {
            "sample_count": 3,
            "median_compute_ms": 1,
            "passed": True,
            "finished_at": "2026-11-14T10:00:00Z",
        },
    }


def _profile():
    return {
        "task_type": "ocr.image.v1",
        "profile_id": "ocr.default.v1",
        "profile_digest": "d" * 64,
    }


def _live_pool(a):
    from node_agent.gpu_lanes import LanePool

    pool = LanePool(a)
    pool.child = _FakeChild(alive=True)
    pool.pipe = _FakePipe()
    return pool


def test_lane_pool_preparation_readies_all_lanes_via_whole_node():
    """整机 READY 的触发条件是共享自测证据，而非逐槽 ready（准备期间后者必然 409）。"""
    a = _fake_agent()
    pool = _live_pool(a)
    ready_calls = []

    def call(suffix, extra=None):
        if suffix.startswith("slots/") and suffix.endswith("/ready"):
            ready_calls.append(suffix)
            return None  # 准备期间能力 PREPARING，slots/ready 一律 409
        if suffix == "claim":
            return {"execution": None, "preparation": None}
        return None

    pool.call = call
    pool.profile = _profile()
    pool.preparation = dict(_profile(), preparation_id="p1")
    pool.preparation_started = time.monotonic()
    pool._evidence = _evidence()
    pool.tick()
    assert len(a.prep_calls) == 1 and a.prep_calls[0]["status"] == "READY"
    assert all(lane.ready for lane in pool.lanes)
    assert pool.preparation is None
    assert ready_calls == []


def test_lane_pool_cancel_recovers_lane_ready():
    """取消一路：stopped 轮换代次后，下一轮自动用共享证据重报 ready 恢复该路。"""
    a = _fake_agent()
    pool = _live_pool(a)
    stopped_calls = []
    ready_calls = []

    def call(suffix, extra=None):
        if suffix.startswith("slots/") and suffix.endswith("/ready"):
            ready_calls.append(suffix)
            return {"ready": True}
        if suffix.startswith("slots/") and suffix.endswith("/stopped"):
            stopped_calls.append(suffix)
            return {"generation": "g-new"}
        if suffix == "claim":
            return {"execution": None, "preparation": None}
        return None

    pool.call = call
    pool.profile = _profile()
    pool._evidence = _evidence()
    for lane in pool.lanes:
        lane.ready = True
    lane0 = pool.lanes[0]
    lane0.execution_id = "e0"
    a._cancel_ids.add("e0")
    pool.tick()
    assert stopped_calls == ["slots/0/stopped"]
    assert lane0.generation == "g-new" and lane0.execution_id == ""
    assert lane0.ready is False
    # 等待 stopped 后设置的 0.5s 退避过去，再触发下一轮恢复
    lane0.retry_at = 0
    pool.tick()
    assert "slots/0/ready" in ready_calls
    assert lane0.ready is True


def test_lane_pool_child_death_triggers_whole_node_reset():
    """共享子进程崩溃：所有路失效，走整机兜底（runtime_reset_pending → 重新注册）。"""
    a = _fake_agent()
    pool = _live_pool(a)
    pool.child.alive = False
    pool._evidence = _evidence()
    pool.tick()
    assert a._runtime_reset_pending is True
    assert pool.child is None and pool.pipe is None and pool._evidence is None


def test_nvidia_mem_used_bytes_parses_and_degrades(monkeypatch):
    """nvidia-smi 显存查询：正常解析 MiB→字节；不可用时返回 None（不用 0 冒充）。"""
    import subprocess as sp

    from node_agent import gpu_probe

    class _Result:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        return _Result(0, "1234\n")

    monkeypatch.setattr(sp, "run", fake_run)
    assert gpu_probe.nvidia_mem_used_bytes(0) == 1234 * 1024 * 1024

    def missing(cmd, **kwargs):
        raise OSError("nvidia-smi not found")

    monkeypatch.setattr(sp, "run", missing)
    assert gpu_probe.nvidia_mem_used_bytes(0) is None
