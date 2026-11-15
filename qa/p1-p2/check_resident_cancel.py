"""Real CPU: cancel an actively running OCR, then reprepare for a new Job."""

import json
import re
import subprocess
import uuid

from run_local import ROOT, Cluster, python_for


def main():
    directory = ROOT / "qa/p1-p2/var" / ("resident-cancel-" + uuid.uuid4().hex[:8])
    cluster = Cluster(directory, 0, 0)
    cluster.profiles_dir = ROOT / "P1/profiles/heterogeneous"
    try:
        for name in ("resource", "task", "node"):
            cluster.start(name)
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
im=Image.new('RGB',(3000,3000),'white')
draw=ImageDraw.Draw(im)
font=ImageFont.load_default(size=38)
for y in range(20,2950,50):
 draw.text((20,y),'RESIDENT CANCELLATION TEST 123 ' * 3,font=font,fill='black')
im.save(root/'large.png')
im=Image.new('RGB',(640,220),'white')
ImageDraw.Draw(im).text((30,70),'HELLO OCR 123',font=ImageFont.load_default(size=44),fill='black')
im.save(root/'small.png')
""",
                str(directory),
            ],
            check=True,
            timeout=30,
        )
        ids = []
        for name in ("large.png", "small.png"):
            ids.append(
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
        job, _ = cluster.create_job(descriptor, ids[:1])

        def running():
            current = cluster.request(
                "task", "GET", "/jobs/" + job["job_id"], "Job", record=False
            )
            eid = current["tasks"][0]["current_execution_id"]
            if eid:
                execution = cluster.request(
                    "resource",
                    "GET",
                    "/executions/" + eid,
                    "Execution",
                    role="TASK_TOKEN",
                    record=False,
                )
                if execution["status"] == "RUNNING":
                    return execution
            return None

        execution = cluster.wait(running, "real OCR RUNNING", 120)
        cluster.request(
            "task", "POST", "/jobs/" + job["job_id"] + "/cancel", "Job", status=202
        )

        def cancelled():
            j = cluster.request(
                "task", "GET", "/jobs/" + job["job_id"], "Job", record=False
            )
            return j if j["status"] == "CANCELLED" else None

        cancelled_job = cluster.wait(cancelled, "cancel confirmation", 30)
        task = cancelled_job["tasks"][0]
        assert task["status"] == "CANCELLED" and len(task["dispatches"]) == 1
        assert task["result_artifact"] is None
        replacement, _ = cluster.create_job(descriptor, ids[1:])
        final = cluster.finish_job(replacement)
        cluster.results(final, ["HELLO OCR 123"])
        log = (directory / "node.log").read_text()
        loads = re.findall(r"model_loaded pid=(\d+)", log)
        assert len(loads) == 2 and loads[0] != loads[1], loads
        cluster.report.update(
            status="PASS",
            running_execution=execution,
            cancelled_job=cancelled_job,
            replacement=final,
            model_processes=loads,
        )
        print(
            "PASS real running cancellation: no redistribution; new Job uses a newly prepared process"
        )
    except BaseException as exc:
        cluster.report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        cluster.close()
        (directory / "report.json").write_text(
            json.dumps(cluster.report, ensure_ascii=False, indent=2) + "\n"
        )
        print(directory)


if __name__ == "__main__":
    main()
