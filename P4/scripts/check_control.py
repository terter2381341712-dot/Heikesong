"""Explicit local real-service check of the button workflow. Stops its center afterwards."""

import argparse
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8003")
    parser.add_argument("--address", help="Use 127.0.0.1 for a local-only check.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        raise ValueError("证据文件已存在，请使用新路径")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(args.url) as response:
        html = response.read().decode()
    session = json.loads(html.split("const session=", 1)[1].split(";", 1)[0])
    report = {
        "status": "FAIL",
        "mode": "REAL_SINGLE_MACHINE_BUTTON_HTTP",
        "checks": [],
        "physical_multinode": "NOT_RUN",
        "macos_live": "NOT_RUN",
    }

    def call(path, body=None):
        request = urllib.request.Request(
            args.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-P4-Session": session},
        )
        with opener.open(request, timeout=5) as response:
            return json.load(response)

    def wait_done(timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = call("/api/status")
            if not state["busy"]:
                return state
            time.sleep(0.5)
        raise ValueError("按钮操作超时")

    def checked(condition, name):
        if not condition:
            raise ValueError(name)
        report["checks"].append({"name": name, "status": "PASS"})
        print("PASS", name, flush=True)

    started = False
    workbench = None
    try:
        checked(call("/api/status")["mode"] == "idle", "initial idle")
        call("/api/center", {"local_node": True, "address": args.address})
        started = True
        state = wait_done()
        checked(
            state["mode"] == "center"
            and all(state["processes"].get(role) for role in ("resource", "task", "web", "node")),
            "automatic environment detection and center + workbench + local node",
        )
        versions = {
            component: tuple(
                json.loads(subprocess.check_output(
                    [path, "-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
                    timeout=5,
                ))
            )
            for component, path in state["python_paths"].items()
        }
        checked(
            versions["P1"] == (3, 9, 6) and versions["P2"] >= (3, 11),
            "P1 and P2 use their supported Python versions",
        )
        report["python_versions"] = versions
        workbench = state["workbench"]["local_url"]
        with opener.open(workbench + "config", timeout=5) as response:
            checked(json.load(response)["mock"] is False, "workbench is running in LIVE mode")
        for role in ("resource", "task"):
            with opener.open(workbench + f"api/{role}/v1/healthz", timeout=5) as response:
                checked(json.load(response)["contract_version"] == "0.2.0", f"workbench proxies {role}")
        token = call("/api/workbench-token", {})["token"]
        request = urllib.request.Request(
            workbench + "api/task/v1/task-types", headers={"Authorization": f"Bearer {token}"}
        )
        with opener.open(request, timeout=5) as response:
            checked(response.status == 200, "workbench accepts the local demo credential")
        response = call("/api/discover", {})
        checked(
            all(c["name"] != state["node_name"] for c in response["centers"]),
            "self center excluded from join list",
        )
        if state["address"] != "127.0.0.1":
            checked(not state["discovery_error"], state["discovery_error"] or "UDP listener bound")
            checked(
                any(center["address"] == f"{state['address']}:{state['control_port']}"
                    for center in control.discover()),
                "UDP discovery reports this center",
            )
            paired = call(
                "/api/pair",
                {
                    "protocol": control.PROTOCOL,
                    "fingerprint": control.fingerprint(),
                    "code": state["code"],
                },
            )
            checked(set(paired["tokens"]) == {"JOIN_TOKEN"}, "pairing returns only node credentials")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = call("/api/status")
            if any(
                n["name"] == state["node_name"] and n["connection"] == "ONLINE"
                for n in state.get("nodes", [])
            ):
                break
            time.sleep(0.5)
        call("/api/off", {})
        time.sleep(4)
        state = call("/api/status")
        checked(
            any(
                n["name"] == state["node_name"]
                and n["connection"] == "ONLINE"
                and n["mode"] == "PAUSED"
                for n in state.get("nodes", [])
            ),
            "sharing off confirmed by P1",
        )
        call("/api/on", {})
        time.sleep(4)
        state = call("/api/status")
        checked(
            any(
                n["name"] == state["node_name"]
                and n["connection"] == "ONLINE"
                and n["mode"] == "ACTIVE"
                for n in state.get("nodes", [])
            ),
            "sharing on confirmed by P1",
        )
        call("/api/test", {})
        state = wait_done()
        checked("验证通过" in state["message"], "button submits real OCR and verifies result / ZIP")
        job_id = state["ocr_test"]["job_id"]
        checked(state["ocr_test"]["state"] == "passed", "controller publishes the verifier state")
        for suffix in ("", "/results"):
            request = urllib.request.Request(
                workbench + f"api/task/v1/jobs/{job_id}{suffix}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with opener.open(request, timeout=5) as response:
                checked(json.load(response)["job_id"] == job_id, f"workbench reads test job{suffix}")
        report["test_job_id"] = job_id
        report["test_message"] = state["message"]
        request = urllib.request.Request(
            args.url + "/api/result.zip", headers={"X-P4-Session": session}
        )
        with opener.open(request, timeout=5) as response:
            raw = response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            checked(
                archive.testzip() is None and "results.json" in archive.namelist(),
                "button result ZIP download",
            )
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if started:
            call("/api/stop", {})
            state = wait_done()
            if state["mode"] != "idle" or state["processes"] != {"web": True}:
                report.update(status="FAIL", cleanup="FAIL")
            else:
                try:
                    if not workbench:
                        raise ValueError("Workbench URL was unavailable")
                    with opener.open(workbench, timeout=5) as response:
                        checked(response.status == 200, "workbench stays open after stopping services")
                    try:
                        opener.open(workbench + "api/task/v1/healthz", timeout=5).close()
                    except urllib.error.HTTPError as exc:
                        checked(exc.code == 503, "workbench reports the stopped backend")
                    else:
                        raise ValueError("Stopped backend still reports healthy")
                    report["cleanup"] = "PASS"
                except (OSError, ValueError) as exc:
                    report.update(status="FAIL", cleanup="FAIL", cleanup_error=str(exc))
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
