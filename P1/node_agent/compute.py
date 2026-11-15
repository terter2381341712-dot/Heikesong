"""Spawned computation owns the model; the supervisor can terminate it and join."""

import multiprocessing
import time


def compute(connection, server, name, identity, kind, payload, prepared):
    import os
    from pathlib import Path

    from runners.profile import load_locked_runner

    from node_agent.agent import Agent

    agent = Agent(server, name)
    agent.worker_id, agent.session_id, agent.node_secret = identity
    try:
        if kind == "preparation":
            agent._do_preparation(payload)
            connection.send(agent._prepared is not None)
        else:
            key = (payload["task_type"], payload["profile_id"], payload["profile_digest"])
            if prepared == key:
                root = Path(
                    os.environ.get(
                        "NODE_PROFILES_DIR", Path(__file__).resolve().parents[1] / "profiles"
                    )
                )
                runner, _, _ = load_locked_runner(
                    (root / (key[1] + ".lock.json")).read_bytes(), *key, backend=agent.backend
                )
                agent._prepared = (key, runner)
            agent._do_execution(payload)
            connection.send(True)
    finally:
        agent._client.close()
        connection.close()


def stop_process(process):
    if process.is_alive():
        process.terminate()
        process.join(1)
    if process.is_alive():
        process.kill()
    process.join(5)
    if process.is_alive():
        raise RuntimeError("模型进程未确认停止")


def supervise(agent, kind, payload):
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    child = ctx.Process(
        target=compute,
        args=(
            sender,
            agent.server,
            agent.name,
            (agent.worker_id, agent.session_id, agent.node_secret),
            kind,
            payload,
            agent._prepared[0] if agent._prepared else None,
        ),
    )
    child.start()
    sender.close()
    cancelled = False
    try:
        while child.is_alive():
            if (
                agent._sharing_disabled
                or agent._session_stale
                or payload.get("execution_id") in agent._cancel_ids
                or time.monotonic() - getattr(agent, "_last_heartbeat", time.monotonic()) >= 12
            ):
                cancelled = True
                break
            child.join(0.05)
        stop_process(child)
        if cancelled:
            agent._prepared = None
            if kind == "execution":
                agent._ack_cancel(payload["execution_id"])
        elif child.exitcode != 0:
            raise RuntimeError("模型子进程异常退出")
        elif kind == "preparation" and receiver.poll() and receiver.recv():
            key = tuple(payload[k] for k in ("task_type", "profile_id", "profile_digest"))
            agent._prepared = (key, True)
    finally:
        stop_process(child)
        receiver.close()
