"""Real TCP/process smoke test against explicit MOCK P1. No OCR quality claims."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

from task_service.contract import ROOT, validate


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process):
    if process.poll() is None:
        if os.name == "nt":
            # A Windows venv launcher can leave its Python child holding the data lock.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_smoke():
    p1_port, p2_port = free_port(), free_port()
    while p2_port == p1_port:
        p2_port = free_port()
    processes = []
    with tempfile.TemporaryDirectory(prefix="p2-smoke-") as temp:
        directory = Path(temp)
        env = {
            **os.environ,
            "P2_ENABLE_MOCK_P1": "1",
            "TASK_TOKEN": "smoke-task-token",
            "DEMO_TOKEN": "smoke-demo-token",
            "MOCK_P1_PORT": str(p1_port),
            "RESOURCE_BASE_URL": f"http://127.0.0.1:{p1_port}",
            "TASK_BIND_HOST": "127.0.0.1",
            "TASK_PORT": str(p2_port),
            "TASK_DATA_DIR": str(directory / "p2-data"),
        }
        with (
            (directory / "processes.log").open("w+") as logs,
            httpx.Client(timeout=2, trust_env=False) as client,
        ):

            def launch(module):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        f"import sys,runpy; sys.path.insert(0,{str(ROOT)!r}); runpy.run_module({module!r},run_name='__main__')",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=logs,
                    stderr=subprocess.STDOUT,
                )
                processes.append(process)
                return process

            def wait_health(base, prefix, process):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        logs.seek(0)
                        raise AssertionError(f"Service exited: {logs.read()}")
                    try:
                        response = client.get(base + prefix + "/healthz")
                        if response.status_code == 200:
                            validate("Health", response.json())
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                raise AssertionError("Service did not start")

            p1_base, p2_base = env["RESOURCE_BASE_URL"], f"http://127.0.0.1:{p2_port}"
            task_path = p2_base + "/api/task/v1"
            headers = {"Authorization": "Bearer " + env["DEMO_TOKEN"]}

            def get(path, schema):
                response = client.get(task_path + path, headers=headers)
                assert response.status_code == 200, response.text
                return validate(schema, response.json())

            def wait_terminal(job_id):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    job = get(f"/jobs/{job_id}", "Job")
                    if job["status"] in {"SUCCEEDED", "FAILED", "PARTIAL_FAILED", "CANCELLED"}:
                        return job
                    time.sleep(0.1)
                raise AssertionError("Job did not reach a terminal state")

            try:
                p1 = launch("tests.tasks.mock_p1")
                wait_health(p1_base, "/api/resource/v1", p1)
                p2 = launch("task_service")
                wait_health(p2_base, "/api/task/v1", p2)
                catalog = get("/task-types", "TypeCatalog")["task_types"][0]
                payload = {
                    "client_request_id": "00000000-0000-4000-8000-000000000901",
                    "task_type": catalog["task_type"],
                    "profile_id": catalog["profile_id"],
                    "profile_digest": catalog["profile_digest"],
                    "input_artifact_ids": ["mock_image_1", "mock_image_2", "mock_image_3"],
                    "parameters": {"language": "auto"},
                }
                response = client.post(task_path + "/jobs", headers=headers, json=payload)
                assert response.status_code == 201, response.text
                job = wait_terminal(response.json()["job_id"])
                assert job["status"] == "SUCCEEDED" and job["counts"]["succeeded"] == 3
                result = get(f"/jobs/{job['job_id']}/results", "JobResults")
                cancellation = {
                    **payload,
                    "client_request_id": "00000000-0000-4000-8000-000000000902",
                }
                response = client.post(task_path + "/jobs", headers=headers, json=cancellation)
                assert response.status_code == 201
                cancel_id = response.json()["job_id"]
                response = client.post(task_path + f"/jobs/{cancel_id}/cancel", headers=headers)
                assert response.status_code in {200, 202}
                assert wait_terminal(cancel_id)["status"] == "CANCELLED"
                stop(p2)
                stop(p1)
                restarted = launch("task_service")
                wait_health(p2_base, "/api/task/v1", restarted)
                assert get(f"/jobs/{job['job_id']}/results", "JobResults") == result
                response = client.post(task_path + "/jobs", headers=headers, json=payload)
                assert response.status_code == 200 and response.json()["job_id"] == job["job_id"]
                response = client.get(
                    task_path + f"/jobs/{job['job_id']}/download", headers=headers
                )
                assert response.status_code == 200
                with zipfile.ZipFile(BytesIO(response.content)) as archive:
                    assert json.loads(archive.read("results.json")) == result
                    assert len(archive.namelist()) == 6
                return {
                    "status": "PASS",
                    "scope": "MOCK_P1_REAL_TCP",
                    "checks": [
                        "service startup",
                        "create/split/schedule/query",
                        "ordered results",
                        "cancel",
                        "P2 process restart",
                        "P1 offline replay/results/ZIP",
                    ],
                    "real_ocr": False,
                    "physical_multinode": False,
                }
            finally:
                for process in reversed(processes):
                    stop(process)


if __name__ == "__main__":
    print(json.dumps(run_smoke(), ensure_ascii=False, indent=2))
