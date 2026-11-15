"""Check public interfaces and collect OCR evidence; no direct execution dispatch."""

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "P2"))
from task_service.contract import JOB_TERMINAL, SPEC, validate  # noqa: E402


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def table(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def static_check():
    directory = ROOT / "D题参赛执行包/contracts"
    canonical = (directory / "openapi.yaml").read_bytes()
    for component in ("P1", "P2"):
        require(
            (ROOT / component / "contracts/openapi.yaml").read_bytes() == canonical,
            f"{component} 契约字节不一致",
        )
    examples = json.loads((directory / "examples.json").read_text(encoding="utf-8"))
    mapping = json.loads((directory / "example-schema-map.json").read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator

    for schema in SPEC["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    for key, schema in mapping.items():
        validate(schema, examples[key])
    types = json.loads((directory / "task-types.json").read_text(encoding="utf-8"))
    require(types["contract_version"] == "0.4.0", "类型目录契约版本不一致")
    for descriptor in types["task_types"]:
        validate("TaskTypeDescriptor", descriptor)
    return {
        "mode": "STATIC",
        "status": "PASS",
        "examples_checked": len(mapping),
        "task_types_checked": len(types["task_types"]),
        "contract_sha256": hashlib.sha256(canonical).hexdigest(),
        "at": stamp(),
    }


class Probe:
    def __init__(self, config, output):
        self.config, self.output = config, output
        self.client = httpx.Client(timeout=60, trust_env=False)
        self.report = {
            "mode": "REAL_HTTP_OCR",
            "status": "RUNNING",
            "started_at": stamp(),
            "contract_version": "0.4.0",
            "checks": [],
            "physical_collaboration": "NOT_RUN",
            "quality": "NOT_RUN",
            "formal_performance": "NOT_RUN",
            "browser_e2e": "NOT_RUN",
        }
        dump(
            output / "config.json",
            {
                "resource_url": config["resource_url"],
                "task_url": config["task_url"],
                "contract_version": "0.4.0",
                "sample_interval_seconds": 3,
                "source_sha256": {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for folder in ("P1", "P2", "P4/scripts")
                    for p in (ROOT / folder).rglob("*.py")
                    if not any(
                        part.startswith(".") or part == "var" for part in p.relative_to(ROOT).parts
                    )
                },
            },
        )
        self.events_after = 0

    def call(self, service, method, suffix, schema=None, status=200, role="DEMO_TOKEN", **kw):
        prefix = "/api/resource/v1" if service == "resource" else "/api/task/v1"
        url = self.config[service + "_url"] + prefix + suffix
        headers = {"Authorization": "Bearer " + self.config["tokens"][role]} if role else {}
        response = self.client.request(method, url, headers=headers, **kw)
        record = {
            "at": stamp(),
            "service": service,
            "method": method,
            "path": prefix + suffix,
            "status": response.status_code,
            "request": kw.get("json"),
            "form": kw.get("data"),
        }
        if kw.get("files"):
            record["files"] = {
                key: {
                    "name": value[0],
                    "sha256": hashlib.sha256(value[1]).hexdigest(),
                    "size_bytes": len(value[1]),
                }
                for key, value in kw["files"].items()
            }
        if "json" in response.headers.get("content-type", ""):
            record["response"] = response.json()
        with (self.output / "http.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        require(
            response.status_code == status,
            f"{method} {prefix + suffix}: {response.status_code}, expected {status}",
        )
        return validate(schema, response.json()) if schema else response.content

    def passed(self, name):
        self.report["checks"].append({"name": name, "status": "PASS", "at": stamp()})
        print("PASS", name, flush=True)

    def sample(self):
        snapshot = self.call("resource", "GET", "/resources", "ResourceSnapshot")
        with (self.output / "telemetry.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"at": stamp(), "snapshot": snapshot}, ensure_ascii=False) + "\n"
            )
        while True:
            page = self.call("resource", "GET", f"/events?after={self.events_after}", "EventPage")
            with (self.output / "fault-events.jsonl").open("a", encoding="utf-8") as stream:
                for event in page["events"]:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            require(
                not page["has_more"] or page["next_after"] > self.events_after, "事件游标未前进"
            )
            self.events_after = page["next_after"]
            if not page["has_more"]:
                break
        return snapshot

    def verify(self, args):
        self.report["static"] = static_check()
        for service, expected in (("resource", "p1_resource"), ("task", "p2_task")):
            health = self.call(service, "GET", "/healthz", "Health", role=None)
            require(
                health["service"] == expected and health["contract_version"] == "0.4.0",
                "健康版本不一致",
            )
            self.call(service, "GET", "/task-types", "Error", role=None, status=401)
            invalid = self.config["tokens"].setdefault("INVALID_TOKEN", uuid.uuid4().hex)
            require(
                invalid not in [self.config["tokens"][k] for k in ("DEMO_TOKEN", "TASK_TOKEN")],
                "无效令牌冲突",
            )
            self.call(service, "GET", "/task-types", "Error", role="INVALID_TOKEN", status=401)
        self.call("resource", "POST", "/executions", "Error", status=403, json={})
        self.call("task", "GET", "/task-types", "Error", status=403, role="TASK_TOKEN")
        self.passed("G1 subset: health/version and 401/403 roles")
        catalogs = [self.call(s, "GET", "/task-types", "TypeCatalog") for s in ("resource", "task")]
        descriptor = next(t for t in catalogs[1]["task_types"] if t["task_type"] == "ocr.image.v1")
        require(descriptor["configuration_state"] == "CONFIGURED", "OCR 未配置")
        require(descriptor in catalogs[0]["task_types"], "两端 OCR 目录不一致")
        digest = hashlib.sha256(
            (args.profiles_dir / (descriptor["profile_id"] + ".lock.json")).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        require(descriptor["profile_digest"] == digest, "profile 与本地锁文件不一致")
        self.report["profile_digest"] = digest
        self.report["initial_resources"] = self.sample()
        if args.health_only:
            return
        paths = sorted(Path(args.inputs).iterdir())
        paths = [p for p in paths if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
        require(1 <= len(paths) <= 200, "需要 1–200 张不同字节的 JPG/PNG")
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        require(len(set(hashes)) == len(hashes), "输入含重复 SHA")
        artifacts, manifest = [], []
        user_start = time.monotonic()
        for index, path in enumerate(paths):
            raw = path.read_bytes()
            payload = {"upload_id": str(uuid.uuid4())}
            files = {"file": (path.name, raw, mimetypes.guess_type(path.name)[0])}
            artifact = self.call(
                "resource", "POST", "/artifacts", "Artifact", status=201, data=payload, files=files
            )
            require(
                artifact["sha256"] == hashes[index] and artifact["size_bytes"] == len(raw),
                "上传元信息/SHA 不一致",
            )
            require(
                self.call("resource", "GET", f"/artifacts/{artifact['artifact_id']}/content")
                == raw,
                "下载原字节不一致",
            )
            replay = self.call(
                "resource", "POST", "/artifacts", "Artifact", data=payload, files=files
            )
            require(replay["artifact_id"] == artifact["artifact_id"], "上传重放创建新文件")
            artifacts.append(artifact["artifact_id"])
            manifest.append(
                {
                    "input_index": index,
                    "input_artifact_id": artifact["artifact_id"],
                    "local_path": str(path.resolve()),
                    "input_sha256": hashes[index],
                }
            )
        with (self.output / "inputs.jsonl").open("w", encoding="utf-8") as stream:
            for item in manifest:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.passed("G1 subset: original-byte upload/download and upload replay")
        request = {key: descriptor[key] for key in ("task_type", "profile_id", "profile_digest")}
        request.update(
            client_request_id=str(uuid.uuid4()),
            input_artifact_ids=artifacts,
            parameters={"language": "auto"},
        )
        dump(self.output / "job-request.json", request)  # Keep ID even if response is lost.
        begin = time.monotonic()
        job = self.call("task", "POST", "/jobs", "Job", status=201, json=request)
        reference = self.output / "job-reference.json"
        temporary = reference.with_suffix(".tmp")
        dump(temporary, {"job_id": job["job_id"]})
        temporary.replace(reference)
        dump(self.output / "job.json", job)
        replay = self.call("task", "POST", "/jobs", "Job", json=request)
        require(replay["job_id"] == job["job_id"], "Job 重放创建新批次")
        conflict = dict(request, parameters={"language": "en"})
        self.call("task", "POST", "/jobs", "Error", status=409, json=conflict)
        self.passed("T02 subset: same request same Job; changed payload 409")
        deadline, next_sample = begin + args.timeout, 0
        while job["status"] not in JOB_TERMINAL:
            require(
                time.monotonic() < deadline,
                f"等待 Job 超时；保留原 Job {job['job_id']}，未自动取消或重新派发",
            )
            if time.monotonic() >= next_sample:
                self.sample()
                next_sample = time.monotonic() + 3
            time.sleep(1)
            job = self.call("task", "GET", f"/jobs/{job['job_id']}", "Job")
            dump(self.output / "job.json", job)
        elapsed = time.monotonic() - begin
        self.report.update(
            job_id=job["job_id"],
            observed_job_seconds=elapsed,
            observed_with_upload_seconds=time.monotonic() - user_start,
        )
        results = self.call("task", "GET", f"/jobs/{job['job_id']}/results", "JobResults")
        dump(self.output / "results.json", results)
        archive_raw = self.call("task", "GET", f"/jobs/{job['job_id']}/download")
        (self.output / "results.zip").write_bytes(archive_raw)
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
            require(json.loads(archive.read("results.json")) == results, "ZIP 与 results 不一致")
        table(
            self.output / "tasks.csv",
            [
                {
                    **t,
                    "run_id": self.output.name,
                    "job_id": job["job_id"],
                    "input_sha256": t["input"]["sha256"],
                    "profile_digest": digest,
                }
                for t in job["tasks"]
            ],
            [
                "run_id",
                "job_id",
                "task_id",
                "input_index",
                "input_sha256",
                "profile_digest",
                "status",
                "attempts_used",
                "current_execution_id",
            ],
        )
        executions, workers = [], set()
        for task in job["tasks"]:
            for dispatch in task["dispatches"]:
                execution = self.call(
                    "resource",
                    "GET",
                    f"/executions/{dispatch['execution_id']}",
                    "Execution",
                    role="TASK_TOKEN",
                )
                executions.append(execution)
            require(
                task["input"]["artifact_id"] == artifacts[task["input_index"]], "Task 输入顺序错误"
            )
            if task["status"] != "SUCCEEDED":
                continue
            execution = next(
                e for e in executions if e["execution_id"] == task["current_execution_id"]
            )
            require(
                execution["status"] == "SUCCEEDED"
                and execution["input_sha256"] == task["input"]["sha256"]
                and execution["profile_digest"] == digest,
                "Execution 输入/profile/状态关联错误",
            )
            artifact = execution["result_artifact"]
            raw = self.call(
                "resource",
                "GET",
                f"/artifacts/{artifact['artifact_id']}/content",
                role="TASK_TOKEN",
            )
            require(
                hashlib.sha256(raw).hexdigest() == artifact["sha256"]
                and len(raw) == artifact["size_bytes"],
                "结果字节校验失败",
            )
            result = validate("OCRResult", json.loads(raw))
            require(result["input_artifact_id"] == task["input"]["artifact_id"], "结果输入关联错误")
            item = results["items"][task["input_index"]]
            require(
                item["task_id"] == task["task_id"] and item["result"] == result,
                "P1/P2 结果不一致或顺序错误",
            )
            workers.add(execution["worker_id"])
        dump(self.output / "executions.json", executions)
        table(
            self.output / "executions.csv",
            [{**e, "run_id": self.output.name} for e in executions],
            [
                "run_id",
                "task_type",
                "profile_digest",
                "execution_id",
                "worker_id",
                "session_id",
                "status",
                "input_sha256",
                "accepted_at",
                "finished_at",
            ],
        )
        self.report["final_resources"] = self.sample()
        self.report["successful_workers"] = sorted(workers)
        table(
            self.output / "summary.csv",
            [
                {
                    "run_id": self.output.name,
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "images": len(paths),
                    "observed_job_seconds": elapsed,
                    "correct_images_per_second": None,
                    "aggregation_efficiency": None,
                }
            ],
            [
                "run_id",
                "job_id",
                "status",
                "images",
                "observed_job_seconds",
                "correct_images_per_second",
                "aggregation_efficiency",
            ],
        )
        require(job["status"] == "SUCCEEDED", "Job 含失败或取消；已保留结果与 ZIP")
        self.passed("G2/G3 API subset: OCR, input/execution/result links and ZIP")
        if args.min_workers:
            require(
                len(workers) >= args.min_workers,
                f"需要 {args.min_workers} 个节点贡献；实际 {len(workers)}，可增加不同图片后重试",
            )
            require(args.devices, "多节点进程不能证明物理多机；需要 --devices 人工设备映射")
            devices = json.loads(Path(args.devices).read_text(encoding="utf-8"))
            physical = {
                d["physical_device_id"]
                for d in devices
                if d["worker_id"] in workers and d.get("operator_attested") is True
            }
            require(len(physical) >= args.min_workers, "独立物理设备确认不足")
            self.report["physical_devices"] = devices
            self.report["physical_collaboration"] = "PASS_WITH_OPERATOR_ATTESTATION"
            self.passed("G4 API evidence with operator-attested physical devices")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--inputs")
    parser.add_argument("--out", required=True)
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--min-workers", type=int, default=0)
    parser.add_argument("--devices")
    parser.add_argument("--profiles-dir", type=Path, default=ROOT / "P1/profiles")
    args = parser.parse_args()
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    probe = None
    report = {"status": "FAIL", "mode": "STATIC" if args.static else "REAL_HTTP_OCR"}
    code = 1
    try:
        if args.static:
            report = static_check()
        else:
            require(
                args.config and (args.health_only or args.inputs),
                "需要 --config 及 --inputs（或 --health-only）",
            )
            probe = Probe(json.loads(Path(args.config).read_text(encoding="utf-8")), output)
            report = probe.report
            probe.verify(args)
        report["status"] = "PASS"
        code = 0
    except KeyboardInterrupt:
        report.update(status="FAIL", error="INTERRUPTED")
        code = 130
    except Exception as exc:
        # Do not print tokens, headers or arbitrary HTTP exception payloads.
        report.update(status="FAIL", error_type=type(exc).__name__)
        if isinstance(exc, ValueError):
            report["error"] = str(exc)
        print("FAIL", report.get("error", type(exc).__name__), file=sys.stderr)
    finally:
        report["finished_at"] = stamp()
        dump(output / "report.json", report)
        if probe:
            probe.client.close()
        telemetry = output / "telemetry.jsonl"
        if telemetry.exists():
            rows = []
            for line in telemetry.read_text(encoding="utf-8").splitlines():
                sample = json.loads(line)
                for node in sample["snapshot"]["resources"]:
                    rows.append(
                        {
                            "run_id": output.name,
                            "observed_at": sample["at"],
                            "worker_id": node["worker_id"],
                            "connection": node["connection"],
                            "mode": node["mode"],
                            "raw_node_json": json.dumps(node, ensure_ascii=False),
                        }
                    )
            table(
                output / "telemetry.csv",
                rows,
                ["run_id", "observed_at", "worker_id", "connection", "mode", "raw_node_json"],
            )
    return code


if __name__ == "__main__":
    sys.exit(main())
