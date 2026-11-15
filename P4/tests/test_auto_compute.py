import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import auto_compute
import compute_config
import managed_node


def test_all_adapters_have_separate_workers_and_ordinal_is_preserved(monkeypatch):
    adapters = [{"backend": "cuda", "device_id": 0, "name": "NVIDIA"},
                {"backend": "directml", "device_id": 2, "name": "Intel"}]
    monkeypatch.setattr(auto_compute.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(adapters)))
    monkeypatch.setattr(auto_compute.os, "cpu_count", lambda: 8)
    calls = []
    manager = SimpleNamespace(ensure=lambda component, progress: calls.append(component) or component + "/python")
    plan = auto_compute.build(compute_config.DEFAULTS, "cpu/python", "physical", lambda _: None, manager)
    assert [w["options"]["backend"] for w in plan["workers"]] == ["cpu", "cuda", "directml"]
    assert plan["workers"][2]["options"]["device_id"] == 2
    assert calls == ["CUDA", "DML"]
    assert plan["workers"][0]["options"]["threads"] + 2 <= 8
    assert plan["physical_id"] == "physical"


def test_cuda_setup_failure_attempts_real_directml_not_cpu_relabelling(monkeypatch):
    adapters = [{"backend": "cuda", "device_id": 0, "directml_device_id": 3, "name": "NVIDIA"}]
    monkeypatch.setattr(auto_compute.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(adapters)))
    def ensure(component, progress):
        if component == "CUDA":
            raise ValueError("CUDA libraries unavailable")
        return "/private/dml/python"
    plan = auto_compute.build(compute_config.DEFAULTS, "cpu/python", "p", lambda _: None, SimpleNamespace(ensure=ensure))
    gpu = plan["workers"][1]
    assert gpu["options"]["backend"] == "directml" and gpu["options"]["device_id"] == 3
    assert gpu["python"] == "/private/dml/python"


def test_detection_failure_keeps_original_cpu_path(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("probe", 25)
    monkeypatch.setattr(auto_compute.subprocess, "run", fail)
    assert auto_compute.build(compute_config.DEFAULTS, "python", "p", lambda _: None) == []


def test_cpu_multislot_budget_reserves_host_threads_for_gpu_preprocessing(monkeypatch):
    import copy
    config = copy.deepcopy(compute_config.DEFAULTS)
    config["node"].update(slots=2, threads=4, cpu_budget=8)
    adapters = [{"backend": "directml", "device_id": i, "name": f"GPU{i}"} for i in range(2)]
    monkeypatch.setattr(auto_compute.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(adapters)))
    monkeypatch.setattr(auto_compute.os, "cpu_count", lambda: 8)
    plan = auto_compute.build(config, "cpu/python", "p", lambda _: None, SimpleNamespace(ensure=lambda *a: "gpu/python"))
    cpu = plan["workers"][0]["options"]
    assert cpu["slots"] * cpu["threads"] + 2 <= 8
    assert config["node"]["threads"] == 4


@pytest.mark.parametrize("modes,expected", [(["ACTIVE", "ACTIVE"], "ACTIVE"), (["PAUSED", "DRAINING"], "DRAINING"), (["PAUSED", "PAUSED"], "PAUSED")])
def test_coordinator_uses_shared_switch_but_separate_data_and_stops_every_child(tmp_path, monkeypatch, modes, expected):
    import threading
    import time
    control = tmp_path / "node-control.json"
    control.write_text('{"enabled": false}')
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"physical_id": "physical", "workers": [
        {"key": key, "python": sys.executable, "options": {"backend": backend, "device_id": 0}}
        for key, backend in (("cpu", "cpu"), ("gpu-directml-0", "directml"))]}))
    children, calls = [], []
    class Child:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout): return self.returncode
    def spawn(argv, env):
        calls.append((argv, env))
        child = Child()
        children.append(child)
        status = Path(argv[argv.index("--status-file") + 1])
        status.write_text(json.dumps({"worker_id": f"worker{len(children)}", "mode": modes[len(children)-1], "received_at": time.time()}))
        return child
    class Stop:
        count = 0
        def wait(self, timeout):
            self.count += 1
            return self.count > 1
        def set(self): pass
    monkeypatch.setattr(managed_node.threading, "Event", Stop)
    monkeypatch.setattr(managed_node.subprocess, "Popen", spawn)
    monkeypatch.setattr(managed_node.signal, "signal", lambda *args: None)
    assert managed_node.run_devices(SimpleNamespace(plan_file=plan, control_file=control, name="pc", server="http://localhost:1")) == 0
    assert len(children) == 2 and all(child.returncode == 0 for child in children)
    assert len({env["NODE_AGENT_DATA_DIR"] for _, env in calls}) == 2
    assert all(str(control) in argv for argv, _ in calls)
    assert json.loads((tmp_path / "node-status.json").read_text())["worker_ids"] == ["worker1", "worker2"]
    assert json.loads((tmp_path / "node-status.json").read_text())["mode"] == expected
