"""P4 node entry: reuse P1 Agent with a local file control channel, not terminal stdin."""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from control import save

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "P1"))
from node_agent.agent import Agent, lock_node_directory  # noqa: E402


class LoggedAgent(Agent):
    def __init__(self, server, name, status_file):
        super().__init__(server, name)
        self.display_name = name
        self.status_file = status_file

    def _internal(self, path, payload):
        response = super()._internal(path, payload)
        if path.endswith("/heartbeat") and response:
            try:
                save(self.status_file, {
                    "worker_id": self.worker_id,
                    "mode": response.get("mode"),
                    "received_at": time.time(),
                })
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
    args = parser.parse_args()
    agent = LoggedAgent(args.server, args.name, args.control_file.with_name("node-status.json"))
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


if __name__ == "__main__":
    sys.exit(main())
