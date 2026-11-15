"""Real CPU two-slot OCR: overlap, reuse, isolated cancellation and off/migration."""

import json
import re
import subprocess
import uuid

from run_local import ROOT, Cluster, python_for


def main():
    directory = ROOT / "qa/p1-p2/var" / ("multislot-" + uuid.uuid4().hex[:8])
    cluster = Cluster(directory, 0, 0)
    cluster.profiles_dir = ROOT / "P1/profiles/heterogeneous"
    processes = []

    def node(name, slots):
        env = cluster.child_environment("node")
        env["NODE_AGENT_DATA_DIR"] = str(directory / name)
        log = (directory / (name + ".log")).open("w")
        p = subprocess.Popen(
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
                "--slots",
                str(slots),
                "--threads",
                "2",
                "--cpu-budget",
                str(slots * 2),
            ],
            cwd=ROOT / "P1",
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append((p, log))
        return p

    def switch(p, value):
        p.stdin.write(value + "\n")
        p.stdin.flush()

    def job_state(job):
        return cluster.request(
            "task", "GET", "/jobs/" + job["job_id"], "Job", record=False
        )

    def running(job):
        j = job_state(job)
        active = []
        for t in j["tasks"]:
            eid = t["current_execution_id"]
            # DISPATCHING is durable before POST; P1 may legitimately return 404 then.
            if eid and t["status"] in {"RUNNING", "CANCELLING"}:
                e = cluster.request(
                    "resource",
                    "GET",
                    "/executions/" + eid,
                    "Execution",
                    role="TASK_TOKEN",
                    record=False,
                )
                if e["status"] == "RUNNING":
                    active.append(e)
        return active

    def loads(name="A"):
        return re.findall(
            r"model_loaded pid=(\d+)", (directory / (name + ".log")).read_text()
        )

    def process_for(eid):
        matches = re.findall(
            r"execute pid=(\d+) execution_id=" + eid, (directory / "A.log").read_text()
        )
        return matches[-1] if matches else None

    try:
        cluster.start("resource")
        cluster.start("task")
        a, b = node("A", 2), node("B", 1)
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
for i in range(2):
 im=Image.new('RGB',(3000,3000),'white')
 draw=ImageDraw.Draw(im)
 for y in range(20,2950,50):
  draw.text((20,y),f'MULTISLOT CPU TEST {i} 123 ' * 3,font=ImageFont.load_default(size=38),fill='black')
 im.save(root/f'large{i}.png')
im=Image.new('RGB',(640,220),'white')
ImageDraw.Draw(im).text((30,70),'HELLO OCR 123',font=ImageFont.load_default(size=44),fill='black')
im.save(root/'small.png')
""",
                str(directory),
            ],
            check=True,
            timeout=30,
        )
        artifacts = []
        for name in ("large0.png", "large1.png", "small.png"):
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
        cancel_job, _ = cluster.create_job(descriptor, artifacts[:1])
        keep_job, _ = cluster.create_job(descriptor, artifacts[1:2])

        def both():
            x, y = running(cancel_job), running(keep_job)
            if (
                len(x) == len(y) == 1
                and process_for(x[0]["execution_id"])
                and process_for(y[0]["execution_id"])
            ):
                return x[0], y[0]

        x, y = cluster.wait(both, "two real CPU processes executing concurrently", 120)
        pids = [process_for(e["execution_id"]) for e in (x, y)]
        assert len(set(pids)) == 2 and x["slot_id"] != y["slot_id"]
        snapshot = cluster.nodes()[0]
        assert snapshot["reserved_slots"] == 2 and snapshot["available_slots"] == 0
        cluster.request(
            "task",
            "POST",
            "/jobs/" + cancel_job["job_id"] + "/cancel",
            "Job",
            status=202,
        )
        cancelled = cluster.wait(
            lambda: (
                j if (j := job_state(cancel_job))["status"] == "CANCELLED" else None
            ),
            "isolated cancellation",
            40,
        )
        kept = cluster.finish_job(keep_job)
        assert kept["status"] == "SUCCEEDED" and kept["tasks"][0]["attempts_used"] == 1
        assert len(cancelled["tasks"][0]["dispatches"]) == 1
        cluster.wait(
            lambda: cluster.nodes()[0]["available_slots"] == 2,
            "cancelled slot reloaded",
            60,
        )
        assert len(loads()) == 3 and loads().count(pids[1]) == 1, loads()
        reuse, _ = cluster.create_job(descriptor, [artifacts[2], artifacts[0]])
        reused = cluster.finish_job(reuse)
        assert reused["status"] == "SUCCEEDED" and len(loads()) == 3
        cluster.report.update(
            overlap=[x, y],
            overlap_pids=pids,
            resource_snapshot=snapshot,
            cancellation=cancelled,
            unaffected_job=kept,
            resident_load_pids=loads(),
            reused_job=reused,
        )

        migrating, _ = cluster.create_job(descriptor, [artifacts[2], *artifacts[:2]])

        def partly_done():
            j = job_state(migrating)
            active = running(migrating)
            return (
                j
                if j["tasks"][0]["status"] == "SUCCEEDED" and len(active) == 2
                else None
            )

        before = cluster.wait(
            partly_done, "one success plus two running slots before off", 90
        )
        switch(a, "off")
        cluster.wait(lambda: cluster.nodes()[0]["mode"] == "PAUSED", "A paused", 20)
        switch(b, "on")
        final = cluster.wait(
            lambda: (
                j
                if (j := job_state(migrating))["status"]
                in {"SUCCEEDED", "FAILED", "PARTIAL_FAILED", "CANCELLED"}
                else None
            ),
            "remaining tasks migrated to B",
            180,
        )
        assert final["status"] == "SUCCEEDED"
        assert final["tasks"][0]["attempts_used"] == 1
        assert (
            final["tasks"][0]["result_artifact"]
            == before["tasks"][0]["result_artifact"]
        )
        for task in final["tasks"][1:]:
            assert task["attempts_used"] == 2
            assert task["dispatches"][0]["error"]["code"] == "NODE_UNAVAILABLE"
            assert (
                task["dispatches"][-1]["worker_id"]
                != task["dispatches"][0]["worker_id"]
            )
        cluster.report.update(
            status="PASS",
            migration_before=before,
            migration_after=final,
            windows_gpu="NOT_RUN",
            physical_machines=1,
        )
        print(
            "PASS: two real CPU slots, model reuse, isolated cancel, all-slot off and migration"
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
