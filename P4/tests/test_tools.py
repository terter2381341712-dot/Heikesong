import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cluster = load("cluster")
verify = load("verify")
quality = load("quality")


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8001",
        "http://host:8001/",
        "http://host/path",
        "http://user:secret@host",
        "ftp://host",
    ],
)
def test_bad_url(url):
    with pytest.raises(ValueError):
        cluster.base_url(url)


def test_role_credentials_and_restart_identity(tmp_path):
    args = SimpleNamespace(
        out=str(tmp_path),
        resource_url="http://192.168.1.10:8001",
        task_url="http://192.168.1.10:8002",
    )
    cluster.init(args)
    config = json.loads((tmp_path / "center.json").read_text(encoding="utf-8"))
    node = json.loads((tmp_path / "node.json").read_text(encoding="utf-8"))
    assert set(node["tokens"]) == {"JOIN_TOKEN"}
    assert len(set(config["tokens"].values())) == 3
    with pytest.raises(ValueError):
        cluster.init(args)
    for role, allowed in [("node", {"JOIN_TOKEN"}), ("task", {"DEMO_TOKEN", "TASK_TOKEN"})]:
        env = cluster.environment(config, role, tmp_path, "0.0.0.0", 8001, 8002, "PC-B")
        assert set(config["tokens"]) & set(env) == allowed
        assert env["RESOURCE_BASE_URL"] == args.resource_url


def test_static_contracts():
    result = verify.static_check()
    assert result["status"] == "PASS" and result["examples_checked"] >= 50


def test_quality_normalization_and_insert_delete_substitute():
    assert quality.normalize(" e\u0301\r\n  X ") == "é X"
    assert quality.distance("kitten", "sitting") == 3
    assert quality.distance("", "abc") == 3
    assert quality.distance("abc", "") == 3


def test_quality_failure_not_correct_throughput(tmp_path):
    verify.dump(
        tmp_path / "results.json",
        {
            "task_type": "ocr.image.v1",
            "items": [{"input_artifact_id": "a", "status": "FAILED", "result": None}],
        },
    )
    (tmp_path / "inputs.jsonl").write_text(
        json.dumps({"input_artifact_id": "a", "input_sha256": "sha"}), encoding="utf-8"
    )
    verify.dump(tmp_path / "report.json", {"observed_job_seconds": 2})
    reference = tmp_path / "refs.json"
    verify.dump(
        reference,
        {
            "frozen_before_run": True,
            "license": "own",
            "max_cer": 0.1,
            "texts_by_sha256": {"sha": "text"},
        },
    )
    result = quality.score(tmp_path, reference)
    assert result["correct_images"] == 0
    assert result["nonempty_observed_cer"] is None
    assert result["observed_correct_images_per_second"] == 0


def test_missing_configuration_exits_nonzero_with_report(tmp_path):
    output = tmp_path / "failed"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "verify.py"), "--out", str(output)], capture_output=True
    )
    assert result.returncode == 1
    assert json.loads((output / "report.json").read_text(encoding="utf-8"))["status"] == "FAIL"


def test_http_records_no_authorization_and_rejects_bad_schema(tmp_path):
    config = {
        "resource_url": "http://p1",
        "task_url": "http://p2",
        "tokens": {"DEMO_TOKEN": "private-demo", "TASK_TOKEN": "private-task"},
    }
    probe = verify.Probe(config, tmp_path)
    probe.client.close()

    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-demo"
        return httpx.Response(200, json={"broken": True})

    probe.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(Exception):
            probe.call("resource", "GET", "/healthz", "Health")
        trace = (tmp_path / "http.jsonl").read_text(encoding="utf-8")
        assert "private-demo" not in trace and "Authorization" not in trace
        assert "private-task" not in (tmp_path / "config.json").read_text(encoding="utf-8")
    finally:
        probe.client.close()


def test_http_wrong_status_cannot_pass(tmp_path):
    probe = verify.Probe(
        {"resource_url": "http://p1", "task_url": "http://p2", "tokens": {}}, tmp_path
    )
    probe.client.close()
    probe.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    try:
        with pytest.raises(ValueError, match="503"):
            probe.call("resource", "GET", "/healthz", role=None)
    finally:
        probe.client.close()


@pytest.mark.parametrize(
    "roles",
    [
        ["resource"],
        ["task"],
        ["node"],
        ["resource", "task"],
        ["resource", "node"],
        ["task", "node"],
        ["resource", "task", "node"],
    ],
)
def test_arbitrary_role_combinations(roles):
    assert set(cluster.select_roles(roles=roles)) == set(roles)


def test_legacy_and_node_combinations():
    assert cluster.select_roles(role="center", with_node=True) == ["resource", "task", "node"]
    assert cluster.select_roles(role="task", with_node=True) == ["task", "node"]
    with pytest.raises(ValueError):
        cluster.select_roles(roles=["node", "node"])
    with pytest.raises(ValueError):
        cluster.select_roles(roles=["p1"])


@pytest.mark.parametrize("windows,suffix", [(True, "Scripts/python.exe"), (False, "bin/python")])
def test_platform_paths(windows, suffix):
    assert cluster.interpreter_for("node", windows=windows) == cluster.ROOT / "P1/.venv" / suffix
    assert cluster.interpreter_for("task", windows=windows) == cluster.ROOT / "P2/.venv" / suffix


def test_machine_file_and_command_line_override(tmp_path):
    path = tmp_path / "machine.json"
    path.write_text(
        json.dumps(
            {
                "roles": ["task", "node"],
                "config": "P4/var/config/center.json",
                "data_dir": "P4/var/local",
                "node_name": "PC-03",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        machine=str(path),
        roles=None,
        role=None,
        config=None,
        data_dir=None,
        with_node=False,
        node_name="CLI-NAME",
    )
    cluster.configure_run(args)
    assert args.roles == ["task", "node"] and args.node_name == "CLI-NAME"
    assert Path(args.config) == cluster.ROOT / "P4/var/config/center.json"
    assert args.task_port == 8002


def test_plan_no_process_no_data_no_tokens(tmp_path, monkeypatch, capsys):
    config = tmp_path / "node.json"
    config.write_text(
        json.dumps({"resource_url": "http://p1:8001", "tokens": {"JOIN_TOKEN": "secret-join"}}),
        encoding="utf-8",
    )
    data_dir = tmp_path / "must-not-exist"
    args = SimpleNamespace(
        config=str(config),
        data_dir=str(data_dir),
        roles=["node"],
        role=None,
        with_node=False,
        node_name="PC-01",
        p1_python=sys.executable,
        p2_python=None,
        bind="0.0.0.0",
        resource_port=8001,
        task_port=8002,
        interactive=False,
        plan=True,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("--plan must not launch processes")

    monkeypatch.setattr(cluster.subprocess, "Popen", forbidden)
    cluster.run(args)
    printed = capsys.readouterr().out
    assert "secret-join" not in printed and '"node"' in printed
    assert not data_dir.exists()
