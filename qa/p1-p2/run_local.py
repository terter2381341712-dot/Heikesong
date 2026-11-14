#!/usr/bin/env python3
"""Run the committed P1 and local P2 in separate environments, over public HTTP.

From the project root: P2/.venv/bin/python qa/p1-p2/run_local.py check
Use `serve` to keep the services and one real local OCR node running.
This never calls a P1 internal API or submits preparation/execution itself.
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "P2"))

from task_service.contract import JOB_TERMINAL, validate  # noqa: E402


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ocr_capability(node):
    return next(
        capability
        for capability in node["capabilities"]
        if (capability["task_type"], capability["profile_id"]) == ("ocr.image.v1", "ocr.default.v1")
    )


def checked_port(port):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        return listener.getsockname()[1]


def private_config(directory):
    path = directory / "connection.json"
    if path.exists():
        return json.loads(path.read_text())
    config = {
        name: secrets.token_urlsafe(32) for name in ("DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN")
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    return config


def acquire_run_lock(directory):
    """Keep two launchers from opening the same services' data directories."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / ".run.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise RuntimeError(f"联调数据目录已被使用：{directory}") from exc
    return lock


def python_for(component):
    return ROOT / component / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class Cluster:
    def __init__(self, directory, resource_port, task_port):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.processes, self.logs = {}, {}
        self.tokens = private_config(self.directory)
        self.resource_port, self.task_port = (
            checked_port(resource_port),
            checked_port(task_port),
        )
        while self.task_port == self.resource_port:
            if task_port:
                raise ValueError("P1 和 P2 必须使用不同端口")
            self.task_port = checked_port(0)
        self.resource_url = f"http://127.0.0.1:{self.resource_port}"
        self.task_url = f"http://127.0.0.1:{self.task_port}"
        self.client = httpx.Client(timeout=httpx.Timeout(10, connect=2), trust_env=False)
        self.report = {
            "status": "RUNNING",
            "mode": "REAL_P1_P2_OCR_SINGLE_MACHINE",
            "started_at": stamp(),
            "contract_version": "0.2.0",
            "plan_version": "1.0",
            "p1_source": json.loads((ROOT / "P1/SOURCE.json").read_text())["commit"],
            "p2_service_sha256": hashlib.sha256(
                (ROOT / "P2/task_service/service.py").read_bytes()
            ).hexdigest(),
            "resource_base_url": self.resource_url,
            "task_base_url": self.task_url,
            "physical_machines": 1,
            "checks": [],
            "trace": [],
        }
        if (ROOT / "P1/contracts/openapi.yaml").read_bytes() != (
            ROOT / "P2/contracts/openapi.yaml"
        ).read_bytes():
            raise ValueError("P1/P2 OpenAPI 不一致，不能启动联合验收")

    def child_environment(self, name):
        env = {
            **os.environ,
            "RESOURCE_BIND_HOST": "127.0.0.1",
            "RESOURCE_PORT": str(self.resource_port),
            "RESOURCE_BASE_URL": self.resource_url,
            "RESOURCE_DATA_DIR": str(self.directory / "resource"),
            "RESOURCE_PROFILES_DIR": str(ROOT / "P1/profiles"),
            "NODE_PROFILES_DIR": str(ROOT / "P1/profiles"),
            "NODE_AGENT_DATA_DIR": str(self.directory / "node"),
            "TASK_BIND_HOST": "127.0.0.1",
            "TASK_PORT": str(self.task_port),
            "TASK_DATA_DIR": str(self.directory / "task"),
            "PYTHONUNBUFFERED": "1",
            # All addresses in this launcher are loopback. Avoid an inherited
            # IPv6 NO_PROXY entry crashing P1's httpx client before registration.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        # Neither an inherited module path nor a mock flag may change the tested implementation.
        for key in ("PYTHONPATH", "PYTHONHOME", "P2_ENABLE_MOCK_P1", *self.tokens):
            env.pop(key, None)
        roles = {
            "resource": ("DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN"),
            "task": ("DEMO_TOKEN", "TASK_TOKEN"),
            "node": ("JOIN_TOKEN",),
        }[name]
        env.update({key: self.tokens[key] for key in roles})
        return env

    def start(self, name):
        if name != "node":
            checked_port(self.resource_port if name == "resource" else self.task_port)
        p1 = name != "task"
        cwd = ROOT / ("P1" if p1 else "P2")
        executable = python_for("P1" if p1 else "P2")
        if not executable.exists():
            raise RuntimeError(f"缺少独立 Python 环境：{executable}")
        module = {
            "resource": "resource_service",
            "task": "task_service",
            "node": "node_agent",
        }[name]
        argv = [str(executable), "-m", module]
        if name == "node":
            argv += ["--server", self.resource_url, "--name", "joint-local-ocr"]
        self.logs[name] = (self.directory / f"{name}.log").open("a")
        self.processes[name] = subprocess.Popen(
            argv,
            cwd=cwd,
            env=self.child_environment(name),
            stdout=self.logs[name],
            stderr=subprocess.STDOUT,
        )
        if name != "node":
            base = self.resource_url if p1 else self.task_url
            prefix = "/api/resource/v1" if p1 else "/api/task/v1"

            def healthy():
                try:
                    response = self.client.get(base + prefix + "/healthz")
                    if response.status_code != 200:
                        return False
                    body = validate("Health", response.json())
                    return body["service"] == ("p1_resource" if p1 else "p2_task")
                except httpx.HTTPError:
                    return False

            self.wait(healthy, f"{name} 启动", 20)

    def stop(self, name):
        process = self.processes.pop(name, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log = self.logs.pop(name, None)
        if log:
            log.close()

    def close(self):
        for name in ("node", "task", "resource"):
            self.stop(name)
        self.client.close()

    def wait(self, action, description, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, process in self.processes.items():
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{name} 提前退出（{process.returncode}），请查看 {name}.log"
                    )
            value = action()
            if value:
                return value
            time.sleep(0.2)
        raise TimeoutError(f"{description} 超过 {timeout} 秒；详见资源/任务/节点日志")

    def request(
        self,
        service,
        method,
        suffix,
        schema,
        *,
        status=200,
        role="DEMO_TOKEN",
        record=True,
        **kwargs,
    ):
        base, prefix = (
            (self.resource_url, "/api/resource/v1")
            if service == "resource"
            else (self.task_url, "/api/task/v1")
        )
        headers = {"Authorization": "Bearer " + self.tokens[role]} if role else {}
        response = self.client.request(method, base + prefix + suffix, headers=headers, **kwargs)
        if response.status_code != status:
            raise RuntimeError(
                f"{service} {method} {suffix}: HTTP {response.status_code}，预期 {status}: {response.text[:800]}"
            )
        body = validate(schema, response.json()) if schema else response.content
        if record and schema:
            self.report["trace"].append(
                {
                    "at": stamp(),
                    "service": service,
                    "method": method,
                    "path": prefix + suffix,
                    "request": kwargs.get("json"),
                    "status": response.status_code,
                    "response": body,
                }
            )
        return body

    def nodes(self):
        return self.request("resource", "GET", "/resources", "ResourceSnapshot", record=False)[
            "resources"
        ]

    def check(self, name):
        self.report["checks"].append({"name": name, "status": "PASS", "at": stamp()})
        print("PASS", name, flush=True)

    def create_job(self, descriptor, artifact_ids):
        payload = validate(
            "JobCreateRequest",
            {
                "client_request_id": str(uuid.uuid4()),
                **{k: descriptor[k] for k in ("task_type", "profile_id", "profile_digest")},
                "input_artifact_ids": artifact_ids,
                "parameters": {"language": "auto"},
            },
        )
        job = self.request("task", "POST", "/jobs", "Job", status=201, json=payload)
        return job, payload

    def finish_job(self, job):
        last = None

        def progress():
            nonlocal last
            current = self.request("task", "GET", f"/jobs/{job['job_id']}", "Job", record=False)
            state = [
                (t["status"], t["attempts_used"], t["current_execution_id"])
                for t in current["tasks"]
            ]
            if state != last:
                self.report["trace"].append(
                    {"at": stamp(), "service": "task", "job_observation": current}
                )
                last = state
            if current["status"] in JOB_TERMINAL:
                if current["status"] != "SUCCEEDED":
                    raise RuntimeError(
                        f"Job 未成功：{current['status']}；{[t['error'] for t in current['tasks']]}"
                    )
                return current
            for node in self.nodes():
                for capability in node["capabilities"]:
                    if capability["state"] == "ERROR":
                        raise RuntimeError(f"真实节点准备失败：{capability['reason']}")
                    if capability["state"] == "PREPARING":
                        assert all(t["attempts_used"] == 0 for t in current["tasks"])
            return False

        return self.wait(progress, "真实 OCR Job 完成")

    def results(self, job, expected):
        result = self.request("task", "GET", f"/jobs/{job['job_id']}/results", "JobResults")
        assert [item["result"]["text"].strip() for item in result["items"]] == expected
        for task in job["tasks"]:
            assert task["attempts_used"] == 1
            execution = self.request(
                "resource",
                "GET",
                f"/executions/{task['current_execution_id']}",
                "Execution",
                role="TASK_TOKEN",
            )
            assert (
                execution["status"] == "SUCCEEDED"
                and execution["input_sha256"] == task["input"]["sha256"]
            )
            assert execution["profile_digest"] == job["profile_digest"]
            artifact = execution["result_artifact"]
            raw = self.request(
                "resource",
                "GET",
                f"/artifacts/{artifact['artifact_id']}/content",
                None,
                role="TASK_TOKEN",
            )
            assert (
                len(raw) == artifact["size_bytes"]
                and hashlib.sha256(raw).hexdigest() == artifact["sha256"]
            )
            validate("OCRResult", json.loads(raw))
        self.download(job["job_id"], result)
        return result

    def download(self, job_id, result):
        raw = self.request("task", "GET", f"/jobs/{job_id}/download", None)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert json.loads(archive.read("results.json")) == result
        (self.directory / f"{job_id}.zip").write_bytes(raw)

    def verify(self):
        for service in ("resource", "task"):
            self.request(service, "GET", "/healthz", "Health", role=None)
            self.request(service, "GET", "/task-types", "Error", role=None, status=401)
        self.request("resource", "POST", "/executions", "Error", status=403, json={})
        self.request("task", "GET", "/task-types", "Error", role="TASK_TOKEN", status=403)
        descriptor = self.request("task", "GET", "/task-types", "TypeCatalog")["task_types"][0]
        assert descriptor["configuration_state"] == "CONFIGURED"
        digest = hashlib.sha256(
            (ROOT / "P1/profiles/ocr.default.v1.lock.json").read_bytes()
        ).hexdigest()
        assert descriptor["profile_digest"] == digest
        self.check("L01: real services, roles, contract and frozen profile")

        node = self.wait(
            lambda: next(
                (
                    n
                    for n in self.nodes()
                    if n["connection"] == "ONLINE" and n["available_slots"] == 1
                ),
                None,
            ),
            "节点上线",
            30,
        )
        assert ocr_capability(node)["state"] == "NOT_PREPARED"
        assert ocr_capability(node)["profile_digest"] is None
        self.report["initial_node"] = node
        image_dir = self.directory / "inputs"
        image_dir.mkdir(exist_ok=True)
        generator = """from pathlib import Path
import sys
from PIL import Image, ImageDraw, ImageFont
for index,text in enumerate(('HELLO OCR 123','APPROVED 456')):
    image=Image.new('RGB',(640,220),'white')
    ImageDraw.Draw(image).text((30,70),text,font=ImageFont.load_default(size=44),fill='black')
    image.save(Path(sys.argv[1])/f'input-{index}.png')
"""
        subprocess.run(
            [str(python_for("P1")), "-c", generator, str(image_dir)],
            check=True,
            timeout=20,
        )
        artifacts = []
        for path in sorted(image_dir.glob("*.png")):
            raw = path.read_bytes()
            artifact = self.request(
                "resource",
                "POST",
                "/artifacts",
                "Artifact",
                status=201,
                data={"upload_id": str(uuid.uuid4())},
                files={"file": (path.name, raw, "image/png")},
            )
            assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
            assert artifact["size_bytes"] == len(raw)
            assert (
                self.request(
                    "resource",
                    "GET",
                    f"/artifacts/{artifact['artifact_id']}/content",
                    None,
                )
                == raw
            )
            artifacts.append(artifact["artifact_id"])
        self.check("L02 local: upload, metadata and original byte download")

        job, payload = self.create_job(descriptor, artifacts)
        job = self.finish_job(job)
        result = self.results(job, ["HELLO OCR 123", "APPROVED 456"])
        ready = self.nodes()[0]
        capability = ocr_capability(ready)
        assert capability["verified"] and capability["calibration"]["passed"]
        assert capability["runtime_provider"] == "CPUExecutionProvider"
        assert (
            capability["model_sha256"]
            == json.loads((ROOT / "P1/profiles/ocr.default.v1.lock.json").read_bytes())[
                "model_sha256"
            ]
        )
        self.report["ready_node"] = ready
        events = self.request("resource", "GET", "/events", "EventPage")
        preparation_ids = {
            event["preparation_id"] for event in events["events"] if event["preparation_id"]
        }
        assert len(preparation_ids) == 1
        preparation = self.request(
            "resource",
            "GET",
            f"/preparations/{next(iter(preparation_ids))}",
            "Preparation",
            role="TASK_TOKEN",
        )
        assert (
            preparation["status"] == "READY"
            and preparation["profile_digest"] == job["profile_digest"]
        )
        self.check("L03: NOT_PREPARED/null -> automatic preparation -> real OCR -> results/ZIP")

        worker = node["worker_id"]
        self.request(
            "resource",
            "POST",
            f"/resources/{worker}/mode",
            "ResourceNode",
            json={"mode": "DRAINING"},
        )
        self.wait(lambda: self.nodes()[0]["mode"] == "PAUSED", "DRAINING 到 PAUSED", 10)
        waiting, _ = self.create_job(descriptor, artifacts[:1])
        self.wait(
            lambda: (
                self.request("task", "GET", f"/jobs/{waiting['job_id']}", "Job", record=False)[
                    "status"
                ]
                == "WAITING_FOR_RESOURCES"
            ),
            "暂停等待",
            10,
        )
        cancelled = self.request("task", "POST", f"/jobs/{waiting['job_id']}/cancel", "Job")
        assert cancelled["status"] == "CANCELLED" and cancelled["tasks"][0]["attempts_used"] == 0
        self.request(
            "resource",
            "POST",
            f"/resources/{worker}/mode",
            "ResourceNode",
            json={"mode": "ACTIVE"},
        )
        resumed, _ = self.create_job(descriptor, artifacts[:1])
        self.results(self.finish_job(resumed), ["HELLO OCR 123"])
        self.check("L04/L11/L12 subset: pause, waiting cancellation, ACTIVE then actual OCR")

        self.stop("task")
        self.stop("node")
        self.stop("resource")
        self.start("task")
        replay = self.request("task", "POST", "/jobs", "Job", json=payload)
        assert replay["job_id"] == job["job_id"] and replay["status"] == "SUCCEEDED"
        assert self.request("task", "GET", f"/jobs/{job['job_id']}/results", "JobResults") == result
        self.download(job["job_id"], result)
        self.check("L15/L16 subset: P2 restart, same Job replay, P1 offline results/ZIP")

        self.start("resource")
        self.start("node")
        renewed = self.wait(
            lambda: next(
                (
                    n
                    for n in self.nodes()
                    if n["available_slots"] == 1 and n["session_id"] != node["session_id"]
                ),
                None,
            ),
            "P1 重启新会话",
            30,
        )
        assert renewed["worker_id"] == worker and ocr_capability(renewed)["state"] == "NOT_PREPARED"
        recovered, _ = self.create_job(descriptor, artifacts[:1])
        self.results(self.finish_job(recovered), ["HELLO OCR 123"])
        self.check(
            "L14 subset: P1 restart retains inputs, new session automatically prepares and executes"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "serve"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--resource-port", type=int)
    parser.add_argument("--task-port", type=int)
    args = parser.parse_args()
    directory = args.data_dir or Path(__file__).parent / "var" / (
        "live" if args.mode == "serve" else f"check-{uuid.uuid4().hex[:8]}"
    )
    resource_port = (
        args.resource_port
        if args.resource_port is not None
        else (8001 if args.mode == "serve" else 0)
    )
    task_port = (
        args.task_port if args.task_port is not None else (8002 if args.mode == "serve" else 0)
    )
    cluster = None
    run_lock = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        run_lock = acquire_run_lock(directory.resolve())
        cluster = Cluster(directory, resource_port, task_port)
        for name in ("resource", "task", "node"):
            cluster.start(name)
        print(f"P1 {cluster.resource_url} | P2 {cluster.task_url}", flush=True)
        print(f"日志、独立数据与本机令牌文件：{cluster.directory}", flush=True)
        if args.mode == "check":
            cluster.verify()
            cluster.report["status"] = "PASS"
        else:
            cluster.wait(
                lambda: any(node["connection"] == "ONLINE" for node in cluster.nodes()),
                "节点上线",
                30,
            )
            (cluster.directory / "services.json").write_text(
                json.dumps(
                    {
                        "started_at": stamp(),
                        "launcher_pid": os.getpid(),
                        "pids": {name: proc.pid for name, proc in cluster.processes.items()},
                        "resource_base_url": cluster.resource_url,
                        "task_base_url": cluster.task_url,
                    },
                    indent=2,
                )
                + "\n"
            )
            print(
                "真实本机 OCR 节点已启动；提交 Job 后由 P2 自动准备。Ctrl+C 停止。",
                flush=True,
            )
            while True:
                for name, process in cluster.processes.items():
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} 已退出；停止其余本次启动的进程")
                time.sleep(1)
        return 0
    except KeyboardInterrupt:
        if cluster:
            cluster.report["status"] = "STOPPED" if args.mode == "serve" else "INTERRUPTED"
        return 0 if args.mode == "serve" else 130
    except Exception as exc:
        if cluster:
            cluster.report.update(
                status="FAIL",
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
        print(f"联调失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if cluster:
            cluster.close()
            cluster.report["finished_at"] = stamp()
            report = cluster.directory / "report.json"
            report.write_text(json.dumps(cluster.report, ensure_ascii=False, indent=2) + "\n")
            print(f"{cluster.report['status']}: {report}", flush=True)
        if run_lock:
            run_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
