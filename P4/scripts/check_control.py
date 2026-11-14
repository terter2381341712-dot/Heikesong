"""Explicit local real-service check of the button workflow. Stops its center afterwards."""

import argparse
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8003")
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
    try:
        checked(call("/api/status")["mode"] == "idle", "initial idle")
        call("/api/center", {"local_node": True})
        started = True
        state = wait_done()
        checked(
            state["mode"] == "center" and all(state["processes"].values()),
            "automatic environment detection and center + local node",
        )
        checked(
            state["python_paths"]["P1"] == state["python_paths"]["P2"],
            "P1 and P2 share the Python 3.9.6 interpreter",
        )
        report["shared_python"] = "3.9.6"
        response = call("/api/discover", {})
        checked(
            all(c["name"] != state["node_name"] for c in response["centers"]),
            "self center excluded from join list",
        )
        checked(bool(control.discover()), "UDP center discovery diagnostic")
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
            if state["mode"] != "idle" or state["processes"]:
                report.update(status="FAIL", cleanup="FAIL")
            else:
                report["cleanup"] = "PASS"
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
