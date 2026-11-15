"""Real CPU/HTTP evidence for learned ranking and persisted scheduling history."""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing

from run_local import ROOT, Cluster, python_for


def main():
    os.environ["TASK_SCHEDULER_POLICY"] = "resource_aware"
    directory = ROOT / "qa/p1-p2/var" / ("resource-scheduling-" + uuid.uuid4().hex[:8])
    cluster = Cluster(directory, 0, 0)
    cluster.profiles_dir = ROOT / "P1/profiles/heterogeneous"
    processes = []

    def start_node(name, slots, threads):
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
                "--backend",
                "cpu",
                "--slots",
                str(slots),
                "--threads",
                str(threads),
                "--cpu-budget",
                str(slots * threads),
            ],
            cwd=ROOT / "P1",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        processes.append((p, log))

    def evidence():
        db = directory / "task/tasks.sqlite3"
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            decisions = [
                dict(sequence=seq, execution_id=eid, **json.loads(raw))
                for seq, eid, raw in conn.execute(
                    "SELECT sequence,execution_id,decision FROM scheduling_decisions ORDER BY sequence"
                )
            ]
            samples = [
                dict(execution_id=eid, recorded_at=at, **json.loads(raw))
                for eid, at, raw in conn.execute(
                    "SELECT execution_id,recorded_at,measurement FROM scheduling_samples ORDER BY recorded_at"
                )
            ]
        return decisions, samples

    def idle():
        nodes = cluster.nodes()
        return (
            nodes
            if len(nodes) == 2
            and all(
                n["available_slots"] == n["max_slots"]
                and any(c["state"] == "READY" for c in n["capabilities"])
                for n in nodes
            )
            else None
        )

    def finish(job):
        def terminal():
            j = cluster.request(
                "task", "GET", "/jobs/" + job["job_id"], "Job", record=False
            )
            if j["status"] in {"FAILED", "PARTIAL_FAILED", "CANCELLED"}:
                raise AssertionError(j)
            return j if j["status"] == "SUCCEEDED" else None

        return cluster.wait(terminal, "real resource-aware OCR completion", 120)

    try:
        cluster.start("resource")
        cluster.start("task")
        start_node("cpu-two-slots", 2, 1)
        start_node("cpu-one-slot", 1, 2)
        cluster.wait(lambda: len(cluster.nodes()) == 2, "two CPU nodes online", 30)
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
for i in range(6):
 im=Image.new('RGB',(640,220),'white')
 ImageDraw.Draw(im).text((30,70),f'RESOURCE TEST {i} 123',font=ImageFont.load_default(size=40),fill='black')
 im.save(root/f'{i}.png')
im=Image.new('RGB',(1600,1000),'white')
for y in range(20,970,50):
 ImageDraw.Draw(im).text((30,y),'RESOURCE SCHEDULING LARGE 123',font=ImageFont.load_default(size=36),fill='black')
im.save(root/'large.png')
""",
                str(directory),
            ],
            check=True,
            timeout=30,
        )
        artifacts = []
        for filename in [f"{i}.png" for i in range(6)] + ["large.png"]:
            artifacts.append(
                cluster.request(
                    "resource",
                    "POST",
                    "/artifacts",
                    "Artifact",
                    status=201,
                    data={"upload_id": str(uuid.uuid4())},
                    files={
                        "file": (
                            filename,
                            (directory / filename).read_bytes(),
                            "image/png",
                        )
                    },
                )["artifact_id"]
            )
        descriptor = cluster.request("task", "GET", "/task-types", "TypeCatalog")[
            "task_types"
        ][0]
        first, _ = cluster.create_job(descriptor, artifacts[:3])
        finish(first)
        cluster.wait(idle, "all models prepared", 60)
        warm, _ = cluster.create_job(descriptor, artifacts[:6])
        warmed = finish(warm)
        cluster.results(warmed, [f"RESOURCE TEST {i} 123" for i in range(6)])
        nodes = cluster.wait(idle, "warm nodes idle", 30)
        used = {t["dispatches"][-1]["worker_id"] for t in warmed["tasks"]}
        assert used == {n["worker_id"] for n in nodes}
        before_decisions, before_samples = evidence()
        assert len(before_samples) == 9
        assert all(
            s["compute_ms"] > 0 and s["total_ms"] >= s["compute_ms"]
            for s in before_samples
        )
        cluster.stop("task")
        cluster.start("task")
        assert evidence() == (before_decisions, before_samples)
        probes = []
        for i in range(3):
            cluster.wait(idle, "free slots before ranking probe", 30)
            job, _ = cluster.create_job(descriptor, [artifacts[i]])
            final = finish(job)
            cluster.results(final, [f"RESOURCE TEST {i} 123"])
            eid = final["tasks"][0]["current_execution_id"]
            d = next(d for d in evidence()[0] if d["execution_id"] == eid)
            assert d["source"] == "execution_history" and len(d["candidates"]) == 2
            assert not d["exploration"]
            assert d["score_ms"] == min(c["score_ms"] for c in d["candidates"])
            probes.append(d)
        large, _ = cluster.create_job(descriptor, [artifacts[-1]])
        large_result = finish(large)
        large_decision = next(
            d
            for d in evidence()[0]
            if d["execution_id"] == large_result["tasks"][0]["current_execution_id"]
        )
        assert (
            large_decision["bucket"] == 2 and large_decision["source"] == "calibration"
        )
        decisions, observed = evidence()
        assert len(observed) == 13 and len({s["execution_id"] for s in observed}) == 13
        cluster.report.update(
            status="PASS",
            physical_machines=1,
            windows_gpu="NOT_RUN",
            nodes=nodes,
            warm_job=warmed,
            restart_history_preserved=True,
            probes=probes,
            new_size_bucket=large_decision,
            scheduling_decisions=decisions,
            observations=observed,
            source_sha256={
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in (
                    "P2/task_service/scheduling.py",
                    "P2/task_service/service.py",
                    "P2/task_service/store.py",
                    "P2/task_service/config.py",
                )
            },
        )
        print(
            "PASS: real CPU history, both nodes used, restart persistence, measured ranking, size isolation"
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
        cluster.report["finished_at_epoch"] = time.time()
        (directory / "report.json").write_text(
            json.dumps(cluster.report, ensure_ascii=False, indent=2) + "\n"
        )
        print(directory)


if __name__ == "__main__":
    main()
