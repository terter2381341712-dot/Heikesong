import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compute_config as config
import control


def write(tmp_path, body):
    path = tmp_path / "compute.json"
    path.write_text(json.dumps(body))
    return path


def test_defaults_preserve_cpu_single_slot_and_disable_prefetch(tmp_path):
    c = config.load(tmp_path / "absent.json")
    assert c["node"]["slots"] == 1 and c["node"]["backend"] == "cpu"
    assert c["node"]["prefetch"] == 0 and c["scheduler"]["policy"] == "round_robin"


@pytest.mark.parametrize("body", [
    {"tokens": {}}, {"node": {"slots": True}}, {"node": {"slots": 0}},
    {"node": {"slots": 2}}, {"node": {"prefetch": 1}},
    {"node": {"slots": 2, "threads": 2, "cpu_budget": 2}},
    {"node": {"backend": "cuda"}}, {"scheduler": {"policy": "unknown"}},
    {"node": {"slots": 2, "threads": 1, "cpu_budget": 4, "prefetch": 1}},
])
def test_invalid_config_fails_without_silent_defaults(tmp_path, body):
    with pytest.raises(ValueError):
        config.load(write(tmp_path, body))


def test_cuda_environment_and_cpu_center_are_separate(tmp_path):
    p = write(tmp_path, {"profile_set": "heterogeneous", "node": {
        "backend": "cuda", "python": "gpu/python", "slots": 2, "gpu_lane_vram_bytes": 1024}})
    c = config.load(p)
    assert c["node"]["python"] == str(tmp_path / "gpu/python")
    panel = control.Panel(tmp_path / "panel", compute_path=p)
    panel.runtime.selected["P1"] = "/cpu/python"
    assert config.node_interpreter(c, "/cpu/python") == tmp_path / "gpu/python"
    assert panel.runtime.selected["P1"] == "/cpu/python"
    assert panel.profile_dir.name == "heterogeneous"
    assert control.fingerprint(c) != control.fingerprint()


def test_multislot_parameters_reach_managed_node(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "cpu_count", lambda: 8)
    p = write(tmp_path, {"node": {"slots": 2, "threads": 2, "cpu_budget": 4}})
    panel = control.Panel(tmp_path / "panel", compute_path=p)
    panel.runtime.selected = {"P1": sys.executable, "node": sys.executable}
    panel.config = {"resource_url": "http://127.0.0.1:8001", "tokens": {"JOIN_TOKEN": "test-only"}}
    launched = []
    monkeypatch.setattr(panel, "launch", lambda *args: launched.append(args))
    panel.spawn("node")
    role, argv, cwd, env = launched[0]
    assert role == "node" and argv[argv.index("--slots") + 1] == "2"
    assert argv[argv.index("--cpu-budget") + 1] == "4"
    assert argv[argv.index("--prefetch") + 1] == "0"
    assert env["TASK_SCHEDULER_POLICY"] == "round_robin"
    assert env["JOIN_TOKEN"] == "test-only" and "DEMO_TOKEN" not in env


def test_malformed_json_does_not_fall_back(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{")
    with pytest.raises(ValueError):
        config.load(path)
