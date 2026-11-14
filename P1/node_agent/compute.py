"""Resident model process protocol and single-slot supervisor."""

import multiprocessing
import sys
import time
from datetime import datetime, timezone


def configure_thread_environment(threads):
    import os

    if threads:
        for variable in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            os.environ[variable] = str(threads)


def compute(connection, server, name, identity, options):
    threads = options.get("threads", 0)
    configure_thread_environment(threads)
    if threads:
        import cv2

        cv2.setNumThreads(threads)
    from node_agent.agent import Agent

    agent = Agent(server, name, prefetch=0, **options)
    agent.worker_id, agent.session_id, agent.node_secret = identity
    try:
        while True:
            kind, payload = connection.recv()
            evidence = error = None
            if kind == "load":
                try:
                    evidence = agent._prepare_local(payload)
                except Exception as exc:
                    error = {
                        "code": getattr(exc, "code", "MODEL_LOAD_FAILED"),
                        "message": str(exc),
                    }
            elif kind == "preparation":
                agent._do_preparation(payload)
            else:
                agent._do_execution(payload)
            connection.send(
                {
                    "prepared": agent._prepared[0] if agent._prepared else None,
                    "session_stale": agent._session_stale,
                    "evidence": evidence,
                    "error": error,
                }
            )
    except EOFError:
        pass
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


def close_runtime(agent, invalidate=True):
    runtime = getattr(agent, "_runtime", None)
    if runtime is not None:
        child, connection = runtime
        stop_process(child)  # Never acknowledge/reset a slot until this succeeds.
        connection.close()
        agent._runtime = None
    pipeline = getattr(agent, "_input_pipeline", None)
    if pipeline is not None:
        pipeline.close()  # Join both workers before runtime_stopped/ack releases anything.
        agent._input_pipeline = None
    agent._prepared = None
    if invalidate:
        agent._runtime_reset_pending = True


def _expired(payload):
    deadline = payload.get("hard_deadline_at")
    return bool(
        deadline
        and datetime.now(timezone.utc)
        >= datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    )


def supervise(agent, kind, payload):
    """Normal completion preserves the model; stopped processes require a new session."""
    runtime = getattr(agent, "_runtime", None)
    if runtime is None:
        configure_thread_environment(getattr(agent, "threads", 0))
        ctx = multiprocessing.get_context("spawn")
        parent, child_pipe = ctx.Pipe()
        options = {
            k: getattr(agent, k, default)
            for k, default in (
                ("backend", "cpu"),
                ("device_id", 0),
                ("threads", 0),
            )
        }
        child = ctx.Process(
            target=compute,
            args=(
                child_pipe,
                agent.server,
                agent.name,
                (agent.worker_id, agent.session_id, agent.node_secret),
                options,
            ),
        )
        child.start()
        child_pipe.close()
        runtime = agent._runtime = (child, parent)
    child, connection = runtime
    cached = None
    try:
        pipeline = getattr(agent, "_input_pipeline", None)
        if kind == "execution" and pipeline is not None:
            try:
                cached = pipeline.take(payload)
            except (OSError, ValueError, EOFError):
                pipeline.close()
            if cached:
                payload = {**payload, "_prefetched": cached}
        connection.send((kind, payload))
        while True:
            interrupted = (
                agent._sharing_disabled
                or agent._session_stale
                or getattr(agent, "_stop", False)
                or payload.get("execution_id") in agent._cancel_ids
                or time.monotonic()
                - getattr(agent, "_last_heartbeat", time.monotonic())
                >= 12
                or _expired(payload)
            )
            if interrupted:
                close_runtime(agent)
                if kind == "execution":
                    agent._ack_cancel(payload["execution_id"])
                return
            tick = getattr(agent, "_tick_input_pipeline", None)
            if tick and kind == "execution":
                tick()
            if connection.poll(0.05):
                response = connection.recv()
                if response["session_stale"]:
                    agent._session_stale = True
                    close_runtime(agent)
                else:
                    key = response["prepared"]
                    agent._prepared = (tuple(key), True) if key else None
                return
            if not child.is_alive():
                raise RuntimeError(f"模型常驻进程异常退出：{child.exitcode}")
    except (EOFError, BrokenPipeError, OSError, RuntimeError) as exc:
        close_runtime(agent)
        print(f"[agent] 模型进程已停止，等待中心重置：{exc}", file=sys.stderr)
    finally:
        if cached:
            from pathlib import Path
            Path(cached["path"]).unlink(missing_ok=True)
