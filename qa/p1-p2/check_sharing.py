"""Real single-machine, two process nodes: local off, confirmed cancel, redistribution."""

import json
import subprocess
import uuid

from run_local import ROOT, Cluster, python_for


def main():
    directory = ROOT / "qa/p1-p2/var" / ("sharing-" + uuid.uuid4().hex[:8])
    cluster = Cluster(directory, 0, 0)
    nodes = []
    try:
        cluster.start("resource")
        cluster.start("task")
        for name in ("A", "B"):
            env = cluster.child_environment("node")
            env["NODE_AGENT_DATA_DIR"] = str(directory / name)
            log = (directory / (name + ".log")).open("w")
            node = subprocess.Popen(
                [
                    str(python_for("P1")),
                    "-m",
                    "node_agent",
                    "--server",
                    cluster.resource_url,
                    "--name",
                    name,
                    "--interactive",
                ],
                cwd=ROOT / "P1",
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            nodes.append((node, log))
        nodes[0][0].stdin.write("on\n")
        nodes[0][0].stdin.flush()
        cluster.wait(lambda: len(cluster.nodes()) == 1, "A online", 30)
        image_dir = directory / "inputs"
        image_dir.mkdir()
        generator = """from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import sys
for i,text in enumerate(('HELLO OCR 123','APPROVED 456')):
 im=Image.new('RGB',(640,220),'white')
 ImageDraw.Draw(im).text((30,70),text,font=ImageFont.load_default(size=44),fill='black')
 im.save(Path(sys.argv[1])/f'{i}.png')
"""
        subprocess.run(
            [str(python_for("P1")), "-c", generator, str(image_dir)], check=True, timeout=20
        )
        paths = sorted(image_dir.glob("*.png"))
        artifacts = []
        for path in paths:
            a = cluster.request(
                "resource",
                "POST",
                "/artifacts",
                "Artifact",
                status=201,
                data={"upload_id": str(uuid.uuid4())},
                files={"file": (path.name, path.read_bytes(), "image/png")},
            )
            artifacts.append(a["artifact_id"])
        descriptor = cluster.request("task", "GET", "/task-types", "TypeCatalog")["task_types"][0]
        job, _ = cluster.create_job(descriptor, artifacts)

        def running_second():
            j = cluster.request("task", "GET", "/jobs/" + job["job_id"], "Job", record=False)
            return (
                j
                if j["tasks"][0]["status"] == "SUCCEEDED" and j["tasks"][1]["current_execution_id"]
                else None
            )

        checkpoint = cluster.wait(running_second, "first saved, second dispatched")
        saved = checkpoint["tasks"][0]
        nodes[0][0].stdin.write("off\n")
        nodes[0][0].stdin.flush()

        def stopped():
            j = cluster.request("task", "GET", "/jobs/" + job["job_id"], "Job", record=False)
            return j if j["tasks"][1]["status"] == "WAITING_FOR_RESOURCES" else None

        waiting = cluster.wait(stopped, "A stopped and second task requeued", 30)
        assert waiting["tasks"][0] == saved
        nodes[1][0].stdin.write("on\n")
        nodes[1][0].stdin.flush()

        def finished():
            j = cluster.request("task", "GET", "/jobs/" + job["job_id"], "Job", record=False)
            return (
                j
                if j["status"] in {"SUCCEEDED", "FAILED", "PARTIALLY_SUCCEEDED", "CANCELLED"}
                else None
            )

        final = cluster.wait(finished, "migration completed", 120)
        assert final["status"] == "SUCCEEDED", final
        assert final["tasks"][0] == saved
        assert final["tasks"][1]["attempts_used"] == 2
        assert final["tasks"][1]["dispatches"][0]["status"] == "CANCELLED"
        assert (
            final["tasks"][0]["dispatches"][0]["worker_id"]
            != final["tasks"][1]["dispatches"][-1]["worker_id"]
        )
        result = cluster.request("task", "GET", "/jobs/" + job["job_id"] + "/results", "JobResults")
        assert [i["result"]["text"].strip() for i in result["items"]] == [
            "HELLO OCR 123",
            "APPROVED 456",
        ]
        cluster.download(job["job_id"], result)
        cluster.report.update(status="PASS", checkpoint=checkpoint, waiting=waiting, final=final)
        print("PASS real stop/requeue: preserved first result, second completed on B", flush=True)
    except BaseException as exc:
        cluster.report.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        for node, log in nodes:
            node.terminate()
            try:
                node.wait(8)
            except subprocess.TimeoutExpired:
                node.kill()
                node.wait()
            log.close()
        cluster.close()
        (directory / "report.json").write_text(
            json.dumps(cluster.report, ensure_ascii=False, indent=2)
        )
        print(directory)


if __name__ == "__main__":
    main()
