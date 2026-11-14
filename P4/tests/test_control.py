import base64
import http.client
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import control  # noqa: E402


def test_managed_node_imports_in_isolated_python_from_other_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(control.ROOT / "P4/scripts/managed_node.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"--control-file" in result.stdout


def test_dead_joined_node_reports_failure_and_can_restart(panel, monkeypatch):
    panel.mode = "node"
    panel.processes["node"] = SimpleNamespace(poll=lambda: 1)
    state = panel.status()
    assert state["message"].startswith("未完成：")
    assert "本机计算" in state["message"]
    starts = []
    monkeypatch.setattr(panel.runtime, "ensure", lambda *args: None)

    def spawn(role):
        starts.append(role)
        panel.processes[role] = SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(panel, "spawn", spawn)
    panel.set_computing(True)
    panel.set_computing(True)
    assert starts == ["node"]
    assert panel.sharing_requested


def test_uploaded_images_use_original_bytes_and_timestamped_results(panel, monkeypatch):
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8uoAAAAASUVORK5CYII="
    )
    image = {"name": "../my-image.png", "data": base64.b64encode(raw).decode()}
    panel.runtime.selected = {"P1": sys.executable, "P2": sys.executable}
    observed = []

    def run(argv, **kwargs):
        inputs = Path(argv[argv.index("--inputs") + 1])
        output = Path(argv[argv.index("--out") + 1])
        # Match the real verifier's contract: its output directory must be new.
        output.mkdir(parents=True, exist_ok=False)
        assert list(inputs.iterdir())[0].read_bytes() == raw
        assert list(inputs.iterdir())[0].name == "001-my-image.png"
        observed.append(output.parent)
        control.save(output / "report.json", {"successful_workers": ["worker-a"]})
        control.save(
            output / "executions.json",
            [{"worker_id": "worker-a", "execution_id": "exec-a", "status": "SUCCEEDED"}],
        )
        (output / "results.zip").write_bytes(b"zip-result")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(control.subprocess, "run", run)
    monkeypatch.setattr(control, "request_json", lambda *a, **k: {"resources": []})
    panel.test_job([image])
    panel.test_job([image])
    assert observed[0] != observed[1]
    assert re.fullmatch(r"test-\d{8}-\d{6}-[a-f0-9]{8}", observed[0].name)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}（北京时间）", panel.last_result_title)
    assert control.read_json(observed[0] / "test-info.json")["source"] == "upload"
    assert panel.status()["result_filename"] == observed[-1].name + ".zip"


@pytest.mark.parametrize(
    "images", [[], [{"name": "bad.png", "data": "invalid!"}], [{"name": "bad.exe", "data": "YWJj"}]]
)
def test_invalid_upload_creates_no_test_directory(panel, images):
    with pytest.raises(ValueError):
        panel.test_job(images)
    assert not list(panel.directory.glob("test-*"))


def test_upload_endpoint_requires_session_and_accepts_larger_image_payload(
    panel, server, monkeypatch
):
    images = [{"name": "photo.png", "data": "A" * 10000}]
    received = []
    monkeypatch.setattr(panel, "test_job", received.append)
    assert call(server, "/api/test-images", {"images": images})[0] == 403
    assert not received
    assert (
        call(server, "/api/test-images", {"images": images}, {"X-P4-Session": panel.session})[0]
        == 202
    )
    panel.worker.join(2)
    assert received == [images]


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "lan_addresses", lambda: ["192.168.1.10"])
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
    status, css = call(server, "/styles.css")
    assert status == 200 and b"--device-glass" in css
    assert call(server, "/api/bridge-session")[0] == 404
    assert (
        call(server, "/api/bridge-session", headers={"Host": "evil.example", "X-P4-Bridge": "1"})[0]
        == 403
    )
    status, body = call(server, "/api/bridge-session", headers={"X-P4-Bridge": "1"})
    assert status == 200 and json.loads(body) == {"session": panel.session}
    assert call(server, "/api/status")[0] != 200
    headers = {"X-P4-Session": panel.session}
    status, body = call(server, "/api/status", headers=headers)
    assert status == 200 and b"join-private" not in body
    assert (
        call(server, "/api/stop", {}, headers={**headers, "Origin": "http://evil.example"})[0]
        == 403
    )
    assert call(server, "/api/stop", {}, headers={})[0] == 403
    assert b"demo-private" not in body
    assert call(server, "/api/workbench-token", {})[0] == 403
    assert (
        call(
            server, "/api/workbench-token", {}, headers={**headers, "Origin": "http://evil.example"}
        )[0]
        == 403
    )
    status, body = call(server, "/api/workbench-token", {}, headers=headers)
    assert status == 200 and json.loads(body) == {"token": "demo-private"}


def test_real_http_pair_and_automatic_node_configuration(panel, server, tmp_path, monkeypatch):
    node = control.Panel(tmp_path / "mac-like-node")
    monkeypatch.setattr(node.runtime, "ensure", lambda *args: sys.executable)
    commands = []

    def spawn(role):
        commands.append(role)
        node.processes[role] = SimpleNamespace(stdin=None, poll=lambda: None)

    monkeypatch.setattr(node, "spawn", spawn)
    connection_code = control.encode_connection_code("127.0.0.1", server.server_port, panel.code)
    node.join_connection(connection_code)
    stored = control.read_json(node.directory / "node.json")
    assert stored["tokens"] == {"JOIN_TOKEN": "join-private"}
    assert stored["task_url"] == panel.config["task_url"]
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


def test_connection_code_roundtrip_and_typo_rejected():
    code = control.encode_connection_code("10.4.142.66", 8003, "000042")
    assert control.decode_connection_code("  " + code.lower() + "  ") == (
        "10.4.142.66:8003",
        "000042",
    )
    wrong = code[:8] + ("A" if code[8] != "A" else "B") + code[9:]
    for invalid in (wrong, code[:-1], "123456", "P4-2-" + code[5:], None):
        with pytest.raises(ValueError, match="连接码"):
            control.decode_connection_code(invalid)


def test_connection_code_expires_and_refresh_invalidates_old_code(panel, monkeypatch):
    connection = control.encode_connection_code("10.4.142.66", 8003, panel.code)
    _, secret = control.decode_connection_code(connection)
    body = {"code": secret, "protocol": control.PROTOCOL, "fingerprint": control.fingerprint()}
    assert panel.pair(body)["tokens"] == {"JOIN_TOKEN": "join-private"}
    panel.code_until = time.monotonic() - 1
    with pytest.raises(ValueError, match="失效"):
        panel.pair(body)
    # Even a random collision must not leave the previous connection code valid.
    monkeypatch.setattr(control.secrets, "randbelow", lambda _: int(secret))
    panel.new_code()
    with pytest.raises(ValueError, match="不正确"):
        panel.pair(body)


def test_connect_endpoint_accepts_code_without_address_or_discovery(panel, server, monkeypatch):
    received = []
    monkeypatch.setattr(panel, "join_connection", received.append)
    connection = control.encode_connection_code("10.4.142.66", 8003, panel.code)
    headers = {"X-P4-Session": panel.session}
    assert call(server, "/api/connect", {"connection_code": connection}, headers)[0] == 202
    panel.worker.join(2)
    assert received == [connection]


def test_sharing_buttons_send_requests_without_claiming_confirmation(panel):
    panel.processes["node"] = SimpleNamespace(stdin=None, poll=lambda: None)
    panel.sharing(False)
    assert control.read_json(panel.directory / "node-control.json")["enabled"] is False
    assert panel.sharing_requested is False and "等待" in panel.message


def test_runtime_detects_version_without_installing(tmp_path):
    runtime = control.Runtime(tmp_path)
    assert runtime.probe(Path(sys.executable), "P2")["compatible"] is (sys.version_info[:2] >= (3, 11))
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


def test_center_can_enable_compute_after_start_without_local_node(panel, monkeypatch):
    started = []
    monkeypatch.setattr(panel.runtime, "ensure", lambda *args: None)

    def spawn(role):
        started.append(role)
        panel.processes[role] = SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(panel, "spawn", spawn)
    panel.set_computing(True)
    panel.set_computing(False)
    panel.set_computing(True)
    assert started == ["node"]
    assert panel.sharing_requested is True
    assert all(panel.name in line for line in panel.events)


def test_compute_request_rejects_conflicting_background_operation(panel, server):
    panel.busy = True
    code, _ = call(server, "/api/on", {}, headers={"X-P4-Session": panel.session})
    assert code == 400
    assert panel.sharing_requested is False


def test_occupied_center_port_reports_number_without_starting_services(panel, monkeypatch):
    panel.mode = "idle"
    monkeypatch.setattr(panel.runtime, "ensure", lambda *args: None)

    class OccupiedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            raise OSError(10048, "address already in use")

    monkeypatch.setattr(control.socket, "socket", OccupiedSocket)
    with pytest.raises(ValueError, match="8001"):
        panel.start_center()
    assert panel.processes == {} and panel.mode == "idle"


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


def test_control_status_exposes_p4_job_reference_and_confirmed_heartbeat(panel, monkeypatch):
    monkeypatch.setattr(control, "request_json", lambda *args, **kwargs: {"resources": []})
    output = panel.directory / "test"
    panel.test_run = (output, "running")
    panel.processes["node"] = SimpleNamespace(poll=lambda: None)
    assert panel.status()["ocr_test"] == {"state": "running", "job_id": None}
    assert panel.status()["local_node"]["connection"] == "PENDING"
    control.save(output / "verification" / "job-reference.json", {"job_id": "test-job"})
    status_file = panel.directory / "node-status.json"
    control.save(status_file, {"worker_id": "worker", "mode": "PAUSED", "received_at": time.time()})
    status = panel.status()
    assert status["ocr_test"] == {"state": "running", "job_id": "test-job"}
    assert status["local_node"] == {"worker_id": "worker", "mode": "PAUSED", "connection": "ONLINE"}
    control.save(
        status_file, {"worker_id": "worker", "mode": "PAUSED", "received_at": time.time() - 30}
    )
    assert panel.status()["local_node"] == {
        "worker_id": "worker",
        "mode": None,
        "connection": "STALE",
    }


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


def test_p2_rejects_python_396_even_with_dependencies(tmp_path, monkeypatch):
    runtime = control.Runtime(tmp_path)
    executable = Path(sys.executable).absolute()
    runtime.selected["P1"] = str(executable)
    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda *args, **kw: SimpleNamespace(
            returncode=0, stdout=b'{"version":"3.9.6","ready":true}'
        ),
    )
    monkeypatch.setattr(runtime, "candidates", lambda component: [executable])
    with pytest.raises(ValueError, match="3.11"):
        runtime.ensure("P2", lambda msg: None)
    assert runtime.selected == {"P1": str(executable)}
