"""Exercise stopped main data copies with new services, then restore the old snapshot.

Source must be an isolated, stopped run_local.py data directory, never a live deployment.
No credentials or databases are copied into the public evidence directory.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def file_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def probe(tree, directory, evidence):
    # Separate interpreter imports exactly one revision of run_local/contract.
    code = r'''
import sys, json, sqlite3, hashlib, io, zipfile
from pathlib import Path
tree, directory, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(tree / "qa/p1-p2"))
from run_local import Cluster
with sqlite3.connect(directory / "task/tasks.sqlite3") as db:
    states = [json.loads(r[0]) for r in db.execute("select state from jobs")]
cluster = Cluster(directory, 0, 0)
checks=[]
try:
    cluster.start("resource"); cluster.start("task")
    for state in states:
        job = state["job"]
        observed = cluster.request("task", "GET", "/jobs/" + job["job_id"], "Job", record=False)
        assert observed["tasks"] == job["tasks"] and observed["status"] == job["status"]
        results = cluster.request("task", "GET", "/jobs/" + job["job_id"] + "/results", "JobResults", record=False)
        raw = cluster.request("task", "GET", "/jobs/" + job["job_id"] + "/download", None, record=False)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            assert json.loads(z.read("results.json")) == results
        for task in job["tasks"]:
            a = task["input"]
            data = cluster.request("resource", "GET", "/artifacts/"+a["artifact_id"]+"/content", None, record=False)
            assert hashlib.sha256(data).hexdigest() == a["sha256"]
        checks.append({"job_id":job["job_id"], "tasks":len(job["tasks"]), "status":"PASS"})
    assert checks, "No historical jobs in snapshot"
finally:
    cluster.close()
output.write_text(json.dumps({"status":"PASS", "jobs":checks}, indent=2))
'''
    result = subprocess.run([sys.executable, "-c", code, str(tree), str(directory), str(evidence)],
                            capture_output=True, timeout=120)
    if result.returncode:
        # Exception detail can contain URLs/config; keep it private.
        (directory / "probe-error.log").write_bytes(result.stdout + result.stderr)
        raise ValueError("data-copy API probe failed; private probe-error.log has details")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--main-tree", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--private-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    a.private_dir.mkdir(parents=True, exist_ok=False)
    report = {"status":"FAIL", "scope":"LOCAL_STOPPED_MAIN_COPY", "physical_upgrade":"NOT_RUN"}
    try:
        source = a.source.resolve()
        if not (source / "resource/resource.sqlite3").is_file() or not (source / "task/tasks.sqlite3").is_file():
            raise ValueError("run_local baseline data is required")
        if a.private_dir.resolve().is_relative_to(source):
            raise ValueError("copy destination must be outside source")
        before = file_hashes(source)
        snapshot = a.private_dir / "snapshot"
        shutil.copytree(source, snapshot)
        upgrade = a.private_dir / "upgrade"
        shutil.copytree(snapshot, upgrade)
        probe(ROOT, upgrade, a.out / "upgrade-first.json")
        probe(ROOT, upgrade, a.out / "upgrade-restart.json")
        rollback = a.private_dir / "rollback"
        shutil.copytree(snapshot, rollback)
        probe(a.main_tree.resolve(), rollback, a.out / "rollback-main.json")
        assert before == file_hashes(source) == file_hashes(snapshot)
        report.update(status="PASS", original_unchanged=True, snapshot_unchanged=True,
                      checks=["new API reads old jobs/results/originals/ZIP", "second startup migration idempotent",
                              "restored old code + data reads historical results"],
                      note="No production data used; full Windows/two-device upgrade and new-job rollback remain NOT_RUN")
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    (a.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(report["status"], a.out / "report.json")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
