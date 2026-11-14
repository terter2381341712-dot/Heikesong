"""P4 node entry: reuse P1 Agent with a local file control channel, not terminal stdin."""

import argparse
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "P1"))
from node_agent.agent import Agent, lock_node_directory  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--control-file", type=Path, required=True)
    args = parser.parse_args()
    agent = Agent(args.server, args.name)
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
