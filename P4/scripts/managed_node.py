"""P4 node entry: reuse P1 Agent with a local file control channel, not terminal stdin."""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Windows embedded Python and isolated mode do not add the script directory.
sys.path.insert(0, str(ROOT / "P4/scripts"))
from control import save  # noqa: E402
import compute_config

sys.path.insert(0, str(ROOT / "P1"))
from node_agent.agent import Agent, lock_node_directory  # noqa: E402


class LoggedAgent(Agent):
    def __init__(self, server, name, status_file, **options):
        super().__init__(server, name, **options)
        self.display_name = name
        self.status_file = status_file

    def _internal(self, path, payload, timeout=10.0):
        response = super()._internal(path, payload, timeout=timeout)
        if path.endswith("/heartbeat") and response:
            try:
                save(
                    self.status_file,
                    {
                        "worker_id": self.worker_id,
                        "mode": response.get("mode"),
                        "received_at": time.time(),
                    },
                )
            except OSError:
                # The next heartbeat can refresh the panel snapshot.
                pass
        return response

    def _do_execution(self, execution):
        print(
            f"{time.strftime('%H:%M:%S')} [设备 {self.display_name}] 开始计算 execution={execution['execution_id']} input={execution.get('input_artifact_id', '')} sha256={execution.get('input_sha256', '')[:12]}",
            flush=True,
        )
        try:
            return super()._do_execution(execution)
        finally:
            print(
                f"{time.strftime('%H:%M:%S')} [设备 {self.display_name}] 执行结束 execution={execution['execution_id']}，成功或失败状态见相邻 agent 日志。",
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--control-file", type=Path, required=True)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--status-file", type=Path)
    for key, value in compute_config.DEFAULTS["node"].items():
        if key != "python":
            parser.add_argument("--" + key.replace("_", "-"), type=str if key == "backend" else int, default=value)
    args = parser.parse_args()
    if args.plan_file:
        return run_devices(args)
    options = {k: getattr(args, k) for k in compute_config.DEFAULTS["node"] if k != "python"}
    agent = LoggedAgent(args.server, args.name, args.status_file or args.control_file.with_name("node-status.json"), **options)
    agent._sharing_disabled = True
    finished = threading.Event()

    def controls():
        previous = None
        while not finished.wait(0.2):
            try:
                body = json.loads(args.control_file.read_text(encoding="utf-8"))
                revision = body.get("revision")
                if revision != previous and isinstance(body.get("enabled"), bool):
                    agent.set_sharing(body["enabled"])
                    previous = revision
            except (OSError, ValueError):
                pass

    with lock_node_directory(agent.data_dir):
        thread = threading.Thread(target=controls, daemon=True)
        thread.start()
        try:
            agent.run()
        finally:
            finished.set()
            thread.join(timeout=2)
    return 0


def run_devices(args):
    """One private child process/data directory per CPU/GPU; one shared switch."""
    plan = json.loads(args.plan_file.read_text(encoding="utf-8"))
    children = []
    failed = set()
    stopped = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stopped.set())
    try:
        for worker in plan["workers"]:
            key = worker["key"]
            status = args.control_file.with_name(f"node-status-{key}.json")
            status.unlink(missing_ok=True)
            env = dict(os.environ)
            env.update(NODE_AGENT_DATA_DIR=str(Path(env.get("NODE_AGENT_DATA_DIR", "data/agent")) / key),
                       P4_PHYSICAL_DEVICE_ID=plan["physical_id"],
                       P4_ADAPTER_NAME=worker.get("adapter", {}).get("name", ""),
                       P4_ADAPTER_MEMORY=str(worker.get("adapter", {}).get("dedicated_bytes", "")),
                       P4_ADAPTER_LUID=worker.get("adapter", {}).get("luid", ""),
                       P4_EXECUTION_BACKEND=worker["options"]["backend"])
            argv = [worker["python"], str(Path(__file__).absolute()), "--server", args.server,
                    f"--name={args.name[:55]}-{key}", "--control-file", str(args.control_file),
                    "--status-file", str(status)]
            argv += [item for name, value in worker["options"].items() if value is not None
                     for item in ("--" + name.replace("_", "-"), str(value))]
            import managed_runtime
            child = subprocess.Popen(argv, env=managed_runtime.process_environment(worker["python"], env))
            children.append((key, child, status))
        while not stopped.wait(0.2):
            nodes = []
            for key, child, path in children:
                if child.poll() is not None:
                    if key not in failed:
                        print(f"[compute] {key} exited code={child.returncode}; other devices continue. Check this node log.", flush=True)
                        failed.add(key)
                    continue
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if time.time() - state["received_at"] <= 12:
                        nodes.append({**state, "device": key})
                except (OSError, ValueError, KeyError):
                    pass
            if nodes:
                save(args.control_file.with_name("node-status.json"), {
                    "worker_id": nodes[0]["worker_id"], "worker_ids": [n["worker_id"] for n in nodes],
                    "mode": "UNKNOWN" if failed or len(nodes) != len(children) else
                            "ACTIVE" if all(n["mode"] == "ACTIVE" for n in nodes) else
                            "PAUSED" if all(n["mode"] == "PAUSED" for n in nodes) else "DRAINING",
                    "received_at": min(n["received_at"] for n in nodes), "devices": nodes,
                    "failed_devices": sorted(failed),
                })
            if not any(child.poll() is None for _, child, _ in children):
                return 1
    finally:
        for _, child, _ in children:
            if child.poll() is None:
                child.terminate()
        for _, child, _ in children:
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
