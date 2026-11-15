"""CPU resident slots. Only a joined process may acknowledge release of its slot."""

import multiprocessing
import signal
import sys
import threading
import time
from dataclasses import dataclass

from . import compute


@dataclass
class Slot:
    slot_id: int
    generation: str
    process: object = None
    pipe: object = None
    kind: str = ""
    payload: object = None
    evidence: object = None
    ready: bool = False
    resetting: bool = False
    retry_at: float = 0

    def stop(self):
        if self.process is not None:
            compute.stop_process(self.process)
            self.pipe.close()
        self.process = self.pipe = None
        self.kind, self.payload, self.evidence, self.ready = "", None, None, False


class Pool:
    def __init__(self, agent):
        self.agent = agent
        self.slots = [Slot(m["slot_id"], m["generation"]) for m in agent._slot_metadata]
        self.profile = self.preparation = self.preparation_error = None
        self.preparation_started = 0
        self.next_claim = 0

    def running_ids(self):
        # Heartbeat snapshots may race a stop; P1 only renews still-RUNNING leases.
        result = []
        for slot in self.slots:
            process, kind, payload = slot.process, slot.kind, slot.payload
            if (
                process is not None
                and process.is_alive()
                and kind == "execution"
                and payload
            ):
                result.append(payload["execution_id"])
        return result

    def stop(self):
        for slot in self.slots:
            slot.stop()

    def call(self, suffix, extra=None):
        a = self.agent
        return a._internal(
            f"/api/resource/internal/v1/nodes/{a.worker_id}/{suffix}",
            {**a._auth_payload(), **(extra or {})},
        )

    def submit(self, slot, kind, payload):
        a = self.agent
        if slot.process is None:
            compute.configure_thread_environment(a.threads)
            ctx = multiprocessing.get_context("spawn")
            parent, child = ctx.Pipe()
            process = ctx.Process(
                target=compute.compute,
                args=(
                    child,
                    a.server,
                    a.name,
                    (a.worker_id, a.session_id, a.node_secret),
                    {"backend": "cpu", "threads": a.threads, "slot_id": slot.slot_id},
                ),
            )
            process.start()
            child.close()
            slot.process, slot.pipe = process, parent
        slot.kind, slot.payload = kind, payload
        slot.pipe.send((kind, payload))

    def reset_slot(self, slot):
        # Preserve generation until the server confirms; retrying cannot kill its replacement.
        slot.stop()
        slot.resetting = True

    def begin_preparation(self, preparation):
        # P1 reserves the entire node for preparation. Retire old models first.
        self.stop()
        self.profile = self.preparation = preparation
        self.preparation_error = None
        self.preparation_started = time.monotonic()
        for slot in self.slots:
            slot.resetting = False
            self.submit(slot, "load", self.profile)

    def poll(self, slot):
        a = self.agent
        remote = next(
            (
                m
                for m in a._slot_metadata
                if m["slot_id"] == slot.slot_id and m["generation"] == slot.generation
            ),
            {},
        )
        interrupted = (
            remote.get("blocked")
            or remote.get("execution_id") in a._cancel_ids
            or (
                slot.payload
                and (
                    slot.payload.get("execution_id") in a._cancel_ids
                    or compute._expired(slot.payload)
                )
            )
        )
        if interrupted and not slot.resetting:
            self.reset_slot(slot)
        if slot.process is not None:
            try:
                if slot.pipe.poll():
                    response = slot.pipe.recv()
                    kind = slot.kind
                    slot.kind, slot.payload = "", None
                    if response.get("session_stale"):
                        a._session_stale = True
                        return
                    if kind == "load":
                        if response.get("error"):
                            if self.preparation:
                                self.preparation_error = response["error"]
                            self.reset_slot(slot)
                            slot.retry_at = time.monotonic() + 5
                        else:
                            slot.evidence = response["evidence"]
                if slot.process is not None and not slot.process.is_alive():
                    raise RuntimeError("计算进程退出")
            except (EOFError, OSError, RuntimeError) as exc:
                print(f"[agent] slot={slot.slot_id} 已停止：{exc}", file=sys.stderr)
                if self.preparation:
                    self.preparation_error = {
                        "code": "MODEL_LOAD_FAILED",
                        "message": str(exc),
                    }
                self.reset_slot(slot)
        if self.preparation or time.monotonic() < slot.retry_at:
            return
        if slot.resetting:
            reply = self.call(
                f"slots/{slot.slot_id}/stopped", {"generation": slot.generation}
            )
            slot.retry_at = time.monotonic() + 0.5
            if reply is None:
                return
            slot.generation = reply["generation"]
            slot.resetting = False
        if self.profile and not slot.ready and not slot.kind:
            if slot.evidence is None:
                self.submit(slot, "load", self.profile)
            else:
                reply = self.call(
                    f"slots/{slot.slot_id}/ready",
                    {
                        "generation": slot.generation,
                        **{
                            k: self.profile[k]
                            for k in ("task_type", "profile_id", "profile_digest")
                        },
                        **slot.evidence,
                    },
                )
                slot.ready = reply is not None
                slot.retry_at = time.monotonic() + 0.5

    def tick(self):
        a = self.agent
        for slot in self.slots:
            self.poll(slot)
        if a._session_stale or a._sharing_disabled:
            return
        now = time.monotonic()
        if self.preparation:
            if now - self.preparation_started >= 900:
                self.stop()
                a._runtime_reset_pending = True
                return
            if now < self.next_claim:
                return
            self.next_claim = now + 0.5
            error = self.preparation_error
            if error:
                self.stop()
                result = a._prep_status(
                    self.preparation["preparation_id"],
                    {
                        "status": "FAILED",
                        "error_code": error["code"],
                        "error_message": error["message"],
                        "error_retryable": False,
                    },
                )
                if result is not None:
                    self.preparation = self.profile = None
                return
            if all(s.evidence for s in self.slots):
                # Each slot passed the same frozen model; use the slowest median for estimates.
                evidence = max(
                    (s.evidence for s in self.slots),
                    key=lambda e: e["calibration"]["median_compute_ms"],
                )
                result = a._prep_status(
                    self.preparation["preparation_id"],
                    {
                        "status": "READY",
                        "progress": 1,
                        "model_cached": True,
                        **evidence,
                    },
                )
                if result is not None:
                    if result["status"] == "READY":
                        for slot in self.slots:
                            slot.ready = True
                    else:
                        self.stop()
                        self.profile = None
                    self.preparation = None
            return
        if now < self.next_claim:
            return
        self.next_claim = now + 0.2
        # A generation-free claim only fetches preparation, including a lost claim reply.
        # Do this even when every slot is cold after a failed recovery.
        reply = self.call("claim")
        if reply and reply.get("preparation"):
            self.begin_preparation(reply["preparation"])
            return
        if reply is None or self.profile is None:
            return
        for slot in self.slots:
            if not slot.ready or slot.kind or a._sharing_disabled or a._session_stale:
                continue
            reply = self.call(
                "claim", {"slot_id": slot.slot_id, "generation": slot.generation}
            )
            if reply is None:
                # Claim may have committed before a lost response; fence just this slot.
                self.reset_slot(slot)
            elif reply.get("preparation"):
                self.begin_preparation(reply["preparation"])
                return
            elif reply.get("execution"):
                try:
                    self.submit(slot, "execution", reply["execution"])
                except (OSError, EOFError):
                    self.reset_slot(slot)


def run_pool(agent):
    signal.signal(signal.SIGINT, agent._handle_sigint)
    signal.signal(signal.SIGTERM, agent._handle_sigint)
    while agent._sharing_disabled and not agent._stop:
        time.sleep(0.1)
    if agent._stop:
        return
    agent.register()
    agent._pool = pool = Pool(agent)
    heartbeat = threading.Thread(target=agent._heartbeat_loop, daemon=True)
    heartbeat.start()
    paused_clean = False
    try:
        while not agent._stop:
            unhealthy = time.monotonic() - agent._last_heartbeat >= 12
            if agent._session_stale:
                pool.stop()
                agent._runtime_reset_pending = False
                agent.register()
                agent._pool = pool = Pool(agent)
            elif agent._runtime_reset_pending:
                pool.stop()
                agent._reset_runtime()
            elif unhealthy or (agent._sharing_disabled and not paused_clean):
                pool.stop()
                agent._runtime_reset_pending = True
                paused_clean = agent._sharing_disabled
                agent._reset_runtime()
            elif agent._confirm_idle:
                agent._confirm_idle = pool.call("confirm_idle") is None
            elif agent._sharing_disabled:
                pass
            else:
                paused_clean = False
                pool.tick()
            time.sleep(0.05)
    finally:
        pool.stop()
        agent._reset_runtime()
        agent._stop = True
        heartbeat.join(timeout=2)
        agent._client.close()
