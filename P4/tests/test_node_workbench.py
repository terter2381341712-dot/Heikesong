"""A compute-only machine's workbench must proxy the paired center, not local P2."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_control
from test_control import call, control

panel = test_control.panel
server = test_control.server


def test_node_bridge_exposes_addresses_without_granting_user_credentials(panel, server):
    panel.mode = "node"
    panel.config["tokens"] = {"JOIN_TOKEN": "join-private"}
    status, raw = call(server, "/api/bridge-upstreams", headers={"X-P4-Bridge": "1"})
    assert status == 200
    assert json.loads(raw) == {
        "resource": panel.config["resource_url"],
        "task": panel.config["task_url"],
        "lan": False,
    }
    assert b"join-private" not in raw
    assert panel.status()["workbench_connected"] is True
    assert call(server, "/api/workbench-token", {}, {"X-P4-Session": panel.session})[0] == 409
    del panel.config["task_url"]
    assert panel.status()["workbench_connected"] is False
    assert (
        json.loads(call(server, "/api/bridge-upstreams", headers={"X-P4-Bridge": "1"})[1])["task"]
        is None
    )
    panel.mode = "idle"
    assert call(server, "/api/bridge-upstreams", headers={"X-P4-Bridge": "1"})[0] == 503


def test_real_node_workbench_proxy_upload_and_job_submission(panel, server):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the real workbench proxy test")
    observed = []

    class Center(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.reply()

        def reply(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            health = self.path.endswith("/healthz")
            authorized = health or self.headers.get("Authorization") == "Bearer user-test"
            observed.append((self.command, self.path, raw, self.headers.get("Authorization")))
            body = json.dumps(
                {"status": "ok", "artifact_id": "image-test", "job_id": "job-test"}
            ).encode()
            self.send_response((201 if self.command == "POST" else 200) if authorized else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    center = ThreadingHTTPServer(("127.0.0.1", 0), Center)
    center_thread = threading.Thread(target=center.serve_forever, daemon=True)
    center_thread.start()
    panel.mode = "node"
    panel.config = {
        "resource_url": f"http://127.0.0.1:{center.server_port}",
        "task_url": f"http://127.0.0.1:{center.server_port}",
        "tokens": {"JOIN_TOKEN": "join-private"},
    }
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        web_port = reservation.getsockname()[1]
    env = dict(
        os.environ,
        WEB_HOST="127.0.0.1",
        WEB_PORT=str(web_port),
        P4_CONTROL_PORT=str(server.server_port),
        P4_MANAGED_WORKBENCH="1",
        DEMO_TOKEN="",
    )
    process = subprocess.Popen(
        [node, "server.js"],
        cwd=control.ROOT / "web",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def web(path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        deadline = time.monotonic() + 8
        while True:
            assert process.poll() is None, process.communicate()[0].decode(errors="replace")
            try:
                assert web("/config")[0] == 200
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert web("/api/resource/v1/healthz")[0] == 200
        assert web("/api/task/v1/healthz")[0] == 200
        assert web("/api/resource/v1/resources")[0] == 401
        user = {"Authorization": "Bearer user-test"}
        assert web("/api/resource/v1/resources", headers=user)[0] == 200
        multipart = b'--image-boundary\r\nContent-Disposition: form-data; name="file"; filename="one.png"\r\nContent-Type: image/png\r\n\r\noriginal-image-bytes\r\n--image-boundary--\r\n'
        assert (
            web(
                "/api/resource/v1/artifacts",
                "POST",
                multipart,
                {**user, "Content-Type": "multipart/form-data; boundary=image-boundary"},
            )[0]
            == 201
        )
        assert (
            web(
                "/api/task/v1/jobs",
                "POST",
                b'{"inputs":["image-test"]}',
                {**user, "Content-Type": "application/json"},
            )[0]
            == 201
        )
        assert next(item[2] for item in observed if item[1].endswith("/artifacts")) == multipart
        assert next(item[3] for item in observed if item[1].endswith("/jobs")) == "Bearer user-test"
        assert not panel.processes  # No local P1 resource or P2 service was needed.
    finally:
        process.terminate()
        process.communicate(timeout=5)
        center.shutdown()
        center.server_close()
        center_thread.join(3)
