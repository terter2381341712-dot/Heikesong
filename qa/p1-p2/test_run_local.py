"""Regression tests for the local integration launcher, without a mock P1."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("run_local", Path(__file__).with_name("run_local.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def cluster(tmp_path):
    instance = runner.Cluster(tmp_path, 0, 0)
    yield instance
    instance.close()


def test_loopback_client_survives_inherited_ipv6_proxy_configuration(cluster, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    before = dict(os.environ)
    process = subprocess.run(
        [sys.executable, "-c", "import httpx; httpx.Client(timeout=10).close()"],
        env=cluster.child_environment("node"),
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr.decode()
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    ("name", "roles"),
    [
        ("resource", {"DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN"}),
        ("task", {"DEMO_TOKEN", "TASK_TOKEN"}),
        ("node", {"JOIN_TOKEN"}),
    ],
)
def test_only_required_credentials_reach_each_process(cluster, monkeypatch, name, roles):
    for key in cluster.tokens:
        monkeypatch.setenv(key, "inherited-credential")
    monkeypatch.setenv("P2_ENABLE_MOCK_P1", "1")
    monkeypatch.setenv("PYTHONPATH", "/old/p1/modules")
    env = cluster.child_environment(name)
    assert set(env) & set(cluster.tokens) == roles
    assert all(env[key] == cluster.tokens[key] for key in roles)
    assert "P2_ENABLE_MOCK_P1" not in env and "PYTHONPATH" not in env
    assert os.environ["JOIN_TOKEN"] == "inherited-credential"


def test_capability_order_does_not_change_ocr_match():
    expected = {"task_type": "ocr.image.v1", "profile_id": "ocr.default.v1", "state": "READY"}
    node = {
        "capabilities": [
            {"task_type": "asr.file.v1", "profile_id": "asr.default.v1"},
            {"task_type": "ocr.image.v1", "profile_id": "ocr.other.v1"},
            expected,
        ]
    }
    assert runner.ocr_capability(node) is expected


def test_occupied_port_is_rejected_without_disturbing_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(OSError):
            runner.checked_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            accepted, _ = listener.accept()
            accepted.close()


def test_same_data_directory_cannot_start_twice_and_unlocks(tmp_path):
    with runner.acquire_run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="数据目录已被使用"):
            runner.acquire_run_lock(tmp_path)
    with runner.acquire_run_lock(tmp_path):
        pass


@pytest.mark.parametrize(
    ("exception", "exit_code", "status"),
    [(RuntimeError("test failure"), 1, "FAIL"), (KeyboardInterrupt(), 130, "INTERRUPTED")],
)
def test_failed_or_interrupted_check_stops_children_and_never_passes(
    tmp_path, monkeypatch, exception, exit_code, status
):
    children = []

    def start(instance, name):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        instance.processes[name] = child
        children.append(child)

    def verify(instance):
        raise exception

    monkeypatch.setattr(runner.Cluster, "start", start)
    monkeypatch.setattr(runner.Cluster, "verify", verify)
    monkeypatch.setattr(sys, "argv", ["run_local.py", "check", "--data-dir", str(tmp_path)])
    old_handler = runner.signal.getsignal(runner.signal.SIGTERM)
    try:
        assert runner.main() == exit_code
        assert len(children) == 3 and all(child.poll() is not None for child in children)
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["status"] == status and report["checks"] == []
        for token in json.loads((tmp_path / "connection.json").read_text()).values():
            assert token not in (tmp_path / "report.json").read_text()
        with runner.acquire_run_lock(tmp_path):
            pass
    finally:
        runner.signal.signal(runner.signal.SIGTERM, old_handler)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
