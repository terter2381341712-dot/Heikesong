import http.client
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import control  # noqa: E402


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "lan_addresses", lambda: ["127.0.0.1"])
    p = control.Panel(tmp_path / "panel")
    p.config = {
        "resource_url": "http://127.0.0.1:1",
        "task_url": "http://127.0.0.1:2",
        "tokens": {
            "JOIN_TOKEN": "join-private",
            "DEMO_TOKEN": "demo-private",
            "TASK_TOKEN": "task-private",
        },
    }
    p.mode = "center"
    p.new_code()
    yield p
    p.closed.set()
    p.processes.clear()  # All test processes are fakes; never kill a real PID.


@pytest.fixture
def server(panel):
    server = control.ControlHTTPServer(("127.0.0.1", 0), control.handler_for(panel))
    panel.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(3)


def call(server, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
    try:
        connection.request(
            "GET" if body is None else "POST",
            path,
            body=None if body is None else json.dumps(body),
            headers=headers or {},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_pair_code_expiry_fingerprint_and_role_isolation(panel):
    good = {"code": panel.code, "protocol": control.PROTOCOL, "fingerprint": control.fingerprint()}
    response = panel.pair(good)
    assert response["tokens"] == {"JOIN_TOKEN": "join-private"}
    assert "demo-private" not in json.dumps(response) and "task-private" not in json.dumps(response)
    with pytest.raises(ValueError, match="不同"):
        panel.pair(dict(good, fingerprint="other-version"))
    panel.code_until = time.monotonic() - 1
    with pytest.raises(ValueError, match="失效"):
        panel.pair(good)


def test_pair_guess_limit_and_refresh(panel):
    wrong = "000000" if panel.code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(ValueError):
            panel.pair({"code": wrong})
    with pytest.raises(ValueError, match="失效"):
        panel.pair(
            {"code": panel.code, "protocol": control.PROTOCOL, "fingerprint": control.fingerprint()}
        )
    panel.new_code()
    assert panel.bad_codes == 0


def test_http_panel_locality_session_and_origin(panel, server):
    assert call(server, "/")[0] == 200
    assert call(server, "/", headers={"Host": "evil.example"})[0] == 403
    assert call(server, "/api/status")[0] != 200
    headers = {"X-P4-Session": panel.session}
    status, body = call(server, "/api/status", headers=headers)
    assert status == 200 and b"join-private" not in body
    assert (
        call(server, "/api/stop", {}, headers={**headers, "Origin": "http://evil.example"})[0]
        == 403
    )
    assert call(server, "/api/stop", {}, headers={})[0] == 403


def test_real_http_pair_and_automatic_node_configuration(panel, server, tmp_path, monkeypatch):
    node = control.Panel(tmp_path / "mac-like-node")
    monkeypatch.setattr(node.runtime, "ensure", lambda *args: sys.executable)
    commands = []

    def spawn(role):
        commands.append(role)
        node.processes[role] = SimpleNamespace(stdin=None, poll=lambda: None)

    monkeypatch.setattr(node, "spawn", spawn)
    node.join(f"127.0.0.1:{server.server_port}", panel.code)
    stored = control.read_json(node.directory / "node.json")
    assert stored["tokens"] == {"JOIN_TOKEN": "join-private"}
    assert node.mode == "node" and commands == ["node"]
    assert control.read_json(node.directory / "node-control.json")["enabled"] is True
    assert (node.directory / "last-center.json").exists()
    monkeypatch.setattr(
        control, "request_json", lambda *args, **kwargs: {"contract_version": "0.2.0"}
    )
    node.processes.clear()
    node.mode = "idle"
    node.reconnect()
    assert commands == ["node", "node"]
    node.processes.clear()


def test_sharing_buttons_send_requests_without_claiming_confirmation(panel):
    panel.processes["node"] = SimpleNamespace(stdin=None, poll=lambda: None)
    panel.sharing(False)
    assert control.read_json(panel.directory / "node-control.json")["enabled"] is False
    assert panel.sharing_requested is False and "等待" in panel.message


def test_runtime_detects_version_without_installing(tmp_path):
    runtime = control.Runtime(tmp_path)
    assert runtime.probe(Path(sys.executable), "P2")["compatible"] is True
    assert runtime.probe(Path(sys.executable), "P1")["compatible"] is (
        sys.version_info[:3] == (3, 9, 6)
    )


def test_missing_runtime_does_not_silently_change_profile(tmp_path, monkeypatch):
    runtime = control.Runtime(tmp_path)
    monkeypatch.setattr(runtime, "candidates", lambda component: [])
    with pytest.raises(ValueError, match="3.9.6"):
        runtime.ensure("P1", lambda msg: None)


def test_ready_runtime_selected_automatically(tmp_path, monkeypatch):
    runtime = control.Runtime(tmp_path)
    monkeypatch.setattr(runtime, "candidates", lambda component: [Path(sys.executable)])
    monkeypatch.setattr(runtime, "probe", lambda *args: {"compatible": True, "ready": True})
    assert runtime.ensure("P1", lambda msg: None) == str(Path(sys.executable).absolute())


def test_background_failure_is_visible_and_releases_busy(panel):
    def fail():
        raise ValueError("expected-failure")

    panel.background(fail)
    panel.worker.join(2)
    assert panel.busy is False and "expected-failure" in panel.message


def test_closed_panel_cannot_start_late_process(panel):
    panel.closed.set()
    with pytest.raises(ValueError, match="关闭"):
        panel.spawn("node")


def test_copied_identity_is_replaced_on_another_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "lan_addresses", lambda: [])
    p = control.Panel(tmp_path)
    original = p.identity
    data = control.read_json(tmp_path / "identity.json")
    data["machine_marker"] = "another-computer"
    control.save(tmp_path / "identity.json", data)
    assert control.Panel(tmp_path).identity != original


def test_zip_requires_local_session(panel, server, tmp_path):
    path = tmp_path / "results.zip"
    path.write_bytes(b"example-zip-content")
    panel.last_result = path
    assert call(server, "/api/result.zip")[0] == 404
    code, raw = call(server, "/api/result.zip", headers={"X-P4-Session": panel.session})
    assert code == 200 and raw == b"example-zip-content"


def test_profiles_match_across_windows_and_unix_line_endings(tmp_path, monkeypatch):
    source = tmp_path / "P1/profiles"
    source.mkdir(parents=True)
    path = source / "ocr.default.v1.lock.json"
    windows_bytes = b'{\r\n  "dependencies": {"python": "3.9.6"}\r\n}\r\n'
    path.write_bytes(windows_bytes)
    monkeypatch.setattr(control, "ROOT", tmp_path)
    windows_fingerprint = control.fingerprint()
    first = control.materialize_profiles(source, tmp_path / "win-runtime")
    path.write_bytes(windows_bytes.replace(b"\r\n", b"\n"))
    second = control.materialize_profiles(source, tmp_path / "mac-runtime")
    assert control.fingerprint() == windows_fingerprint
    assert (
        first["files"][path.name]["runtime_sha256"] == second["files"][path.name]["runtime_sha256"]
    )
    assert first["files"][path.name]["source_sha256"] != second["files"][path.name]["source_sha256"]
    path.write_bytes(b'{"dependencies":{"python":"0.0.0"}}')
    assert control.fingerprint() != windows_fingerprint


def test_control_port_cannot_have_two_owners(panel, server):
    with pytest.raises(OSError):
        second = control.ControlHTTPServer(
            ("127.0.0.1", server.server_port), control.handler_for(panel)
        )
        second.server_close()


def test_p2_reuses_selected_p1_python_instead_of_requiring_311(tmp_path, monkeypatch):
    runtime = control.Runtime(tmp_path)
    runtime.selected["P1"] = str(Path(sys.executable).absolute())
    monkeypatch.setattr(runtime, "probe", lambda *args: {"compatible": True, "ready": True})

    def forbidden(*args):
        raise AssertionError("Should reuse the selected P1 interpreter")

    monkeypatch.setattr(runtime, "candidates", forbidden)
    assert runtime.ensure("P2", lambda msg: None) == runtime.selected["P1"]


def test_shared_environment_installs_missing_p2_dependencies(tmp_path, monkeypatch):
    runtime = control.Runtime(tmp_path)
    runtime.selected["P1"] = str(Path(sys.executable).absolute())
    installed = []
    monkeypatch.setattr(
        runtime, "probe", lambda p, component: {"ready": component == "P1" or bool(installed)}
    )
    monkeypatch.setattr(runtime, "setup_command", lambda command, output: installed.append(command))
    monkeypatch.setattr(
        control.subprocess, "run", lambda *args, **kw: SimpleNamespace(returncode=0)
    )
    runtime.ensure("P2", lambda msg: None)
    assert installed[0][0] == runtime.selected["P1"]
    assert "jsonschema>=4.23,<5" in installed[0]
    assert runtime.selected["P2"] == runtime.selected["P1"]
