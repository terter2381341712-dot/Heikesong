"""Real CPU opt-in exercises the GPU input path without claiming CUDA performance."""

import hashlib
import json
import re
import subprocess
import time
import uuid

from run_local import ROOT, Cluster, python_for


def main():
    directory = ROOT / "qa/p1-p2/var" / ("input-prefetch-" + uuid.uuid4().hex[:8])
    cluster = Cluster(directory, 0, 0)
    cluster.profiles_dir = ROOT / "P1/profiles/heterogeneous"
    processes = []
    cluster.report["runtime_sha256"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory_name in (
            "P1/node_agent",
            "P1/resource_service",
            "P1/runners",
            "P2/task_service",
        )
        for path in sorted((ROOT / directory_name).glob("*.py"))
    }
    cluster.report["contract_sha256"] = hashlib.sha256(
        (ROOT / "P1/contracts/openapi.yaml").read_bytes()
    ).hexdigest()

    def node(name, prefetch):
        env = cluster.child_environment("node")
        env["NODE_AGENT_DATA_DIR"] = str(directory / name)
        log = (directory / (name + ".log")).open("w")
        process = subprocess.Popen(
            [
                str(python_for("P1")),
                "-B",
                "-m",
                "node_agent",
                "--server",
                cluster.resource_url,
                "--name",
                name,
                "--interactive",
                "--threads",
                "1",
                "--cpu-budget",
                "2",
                "--prefetch",
                str(prefetch),
            ],
            cwd=ROOT / "P1",
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append((process, log))
        return process

    def switch(process, value):
        process.stdin.write(value + "\n")
        process.stdin.flush()

    def state(job):
        return cluster.request(
            "task", "GET", "/jobs/" + job["job_id"], "Job", record=False
        )

    def finish(job, expected="SUCCEEDED"):
        final = cluster.wait(
            lambda: (
                j
                if (j := state(job))["status"]
                in {"SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}
                else None
            ),
            "terminal job",
            180,
        )
        assert final["status"] == expected, final
        return final

    def active(job):
        for task in state(job)["tasks"]:
            if task["status"] == "RUNNING":
                execution = cluster.request(
                    "resource",
                    "GET",
                    "/executions/" + task["current_execution_id"],
                    "Execution",
                    role="TASK_TOKEN",
                    record=False,
                )
                if execution["status"] == "RUNNING":
                    return execution
        return None

    def log_text():
        return (directory / "A.log").read_text()

    try:
        cluster.start("resource")
        cluster.start("task")
        a, b = node("A", 1), node("B", 0)
        switch(a, "on")
        cluster.wait(lambda: len(cluster.nodes()) == 1, "A registered", 30)
        subprocess.run(
            [
                str(python_for("P1")),
                "-B",
                "-c",
                """
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import sys
root=Path(sys.argv[1])
for i in range(4):
 im=Image.new('RGB',(3000,3000),'white')
 draw=ImageDraw.Draw(im)
 for y in range(30,2900,70):
  draw.text((30,y),f'PREFETCH SAMPLE {i} HELLO OCR 123',font=ImageFont.load_default(size=45),fill='black')
 im.save(root/f'{i}.png')
im=Image.new('RGB',(480,160),'white')
ImageDraw.Draw(im).text((30,60),'PREFETCH SMALL 123',font=ImageFont.load_default(size=36),fill='black')
im.save(root/'small.png')
""",
                str(directory),
            ],
            check=True,
            timeout=30,
        )
        artifacts = []
        for name in ["0.png", "1.png", "2.png", "3.png", "small.png"]:
            artifacts.append(
                cluster.request(
                    "resource",
                    "POST",
                    "/artifacts",
                    "Artifact",
                    status=201,
                    data={"upload_id": str(uuid.uuid4())},
                    files={
                        "file": (name, (directory / name).read_bytes(), "image/png")
                    },
                )["artifact_id"]
            )
        descriptor = cluster.request("task", "GET", "/task-types", "TypeCatalog")[
            "task_types"
        ][0]
        warm, _ = cluster.create_job(descriptor, [artifacts[-1]])
        finish(warm)
        batch, _ = cluster.create_job(descriptor, artifacts[:4])
        begun = time.monotonic()
        completed = finish(batch)
        elapsed = time.monotonic() - begun
        executions, results = [], []
        for i, task in enumerate(completed["tasks"]):
            execution = cluster.request(
                "resource",
                "GET",
                "/executions/" + task["dispatches"][-1]["execution_id"],
                "Execution",
                role="TASK_TOKEN",
            )
            executions.append(execution)
            raw = cluster.client.get(
                cluster.resource_url + task["result_artifact"]["download_path"],
                headers={"Authorization": "Bearer " + cluster.tokens["TASK_TOKEN"]},
            )
            raw.raise_for_status()
            result = raw.json()
            assert f"PREFETCH SAMPLE {i}" in result["text"]
            results.append(result)
        hits = sum(e["timings"].get("prefetch_hit", False) for e in executions)
        assert hits >= 2, executions
        log = log_text()
        starts = dict(
            re.findall(r"execute pid=\d+ execution_id=(\S+).*started=([\d.]+)", log)
        )
        ends = dict(
            re.findall(r"inference_finished execution_id=(\S+) finished=([\d.]+)", log)
        )
        preps = re.findall(
            r"prefetch_ready task_id=(\S+) started=([\d.]+) finished=([\d.]+)", log
        )
        overlap = [
            (task, eid)
            for task, start, end in preps
            for eid, begin in starts.items()
            if eid in ends
            and max(float(start), float(begin)) < min(float(end), float(ends[eid]))
        ]
        assert overlap, (starts, ends, preps)
        assert len(re.findall(r"model_loaded pid=", log)) == 1
        cluster.report.update(
            batch=completed,
            executions=executions,
            hits=hits,
            overlap=overlap,
            cpu_batch_wall_seconds=elapsed,
            resident_loads=1,
        )

        cancelling, _ = cluster.create_job(descriptor, artifacts[:3])
        cluster.wait(
            lambda: active(cancelling), "active input pipeline before cancel", 30
        )
        cluster.request(
            "task", "POST", f"/jobs/{cancelling['job_id']}/cancel", "Job", status=202
        )
        cancelled = finish(cancelling, "CANCELLED")
        assert all(t["attempts_used"] == 0 for t in cancelled["tasks"][1:])
        cluster.wait(
            lambda: not list((directory / "A/prefetch").glob("*")),
            "cancel cleans cache",
            20,
        )

        migrating, _ = cluster.create_job(descriptor, [artifacts[-1], *artifacts[:3]])

        def partly_done():
            j = state(migrating)
            return (
                j
                if j["tasks"][0]["status"] == "SUCCEEDED" and active(migrating)
                else None
            )

        before = cluster.wait(partly_done, "one complete and one active before off", 90)
        switch(a, "off")
        cluster.wait(lambda: cluster.nodes()[0]["mode"] == "PAUSED", "A paused", 20)
        switch(b, "on")
        migrated = finish(migrating)
        assert (
            migrated["tasks"][0]["result_artifact"]
            == before["tasks"][0]["result_artifact"]
        )
        assert migrated["tasks"][0]["attempts_used"] == 1
        assert migrated["tasks"][1]["attempts_used"] == 2
        assert all(t["attempts_used"] == 1 for t in migrated["tasks"][2:])
        assert (
            migrated["tasks"][1]["dispatches"][0]["error"]["code"] == "NODE_UNAVAILABLE"
        )
        cluster.wait(
            lambda: not list((directory / "A/prefetch").glob("*")),
            "off cleans cache",
            20,
        )
        # Compare real decoded-cache and uncached execution results on the same images.
        for index, task in enumerate(migrated["tasks"][1:]):
            raw = cluster.client.get(
                cluster.resource_url + task["result_artifact"]["download_path"],
                headers={"Authorization": "Bearer " + cluster.tokens["TASK_TOKEN"]},
            )
            raw.raise_for_status()
            assert raw.json() == results[index]
        cluster.report.update(
            status="PASS",
            cancellation=cancelled,
            migration_before=before,
            migration_after=migrated,
            decoded_vs_normal_results_equal=True,
            windows_gpu="NOT_RUN",
            physical_machines=1,
        )
        print(
            f"PASS: {hits} cache hits, real CPU decode/inference overlap, cancellation and migration"
        )
    except BaseException as exc:
        cluster.report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        for process, log in processes:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
        cluster.close()
        (directory / "report.json").write_text(
            json.dumps(cluster.report, ensure_ascii=False, indent=2) + "\n"
        )
        print(directory)


if __name__ == "__main__":
    main()
