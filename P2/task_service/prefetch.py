"""Durable, revision-fenced lookahead. Speculation never changes task ownership."""

import asyncio

from task_service.contract import TASK_TERMINAL
from task_service.p1 import DependencyUnavailable, P1Rejected


class PrefetchPlanner:
    def __init__(self, service):
        self.service = service
        self.cleared = {}

    async def publish(self, worker, session, hint, revision=0):
        store = self.service.store
        old = store.prefetches().get(worker)
        if (
            old
            and old["worker_session_id"] == session
            and old["hint"] == hint
            and old["revision"] >= revision
        ):
            request = old
        else:
            request = {
                "worker_session_id": session,
                "hint": hint,
                "revision": max(revision, old["revision"] if old else 0) + 1,
            }
            store.save_prefetch(worker, request)  # Fence even a POST whose response is lost.
        key = (worker, session, request["revision"])
        if hint is None and self.cleared.get(worker) == key:
            return
        try:
            async with self.service.io_slots:
                await asyncio.wait_for(self.service.p1.prefetch(worker, request), timeout=1)
            if hint is None:
                self.cleared[worker] = key
        except P1Rejected as exc:
            if hint is None and exc.body.get("code") == "SESSION_CHANGED":
                self.cleared[worker] = key
        except (DependencyUnavailable, asyncio.TimeoutError):
            pass  # TTL and replay cover failure without blocking normal allocation.

    async def revoke_invalid(self):
        active = {s["job"]["job_id"]: s for s in self.service.store.active()}
        calls = []
        for worker, request in self.service.store.prefetches().items():
            hint = request["hint"]
            state = active.get(hint["job_id"]) if hint else None
            if hint and (
                not state
                or state["job"]["cancel_requested"]
                or not any(
                    t["task_id"] == hint["task_id"] and t["status"] not in TASK_TERMINAL
                    for t in state["job"]["tasks"]
                )
            ):
                calls.append(self.publish(worker, request["worker_session_id"], None))
            elif hint is None:
                # Clears must be replayed after timeout/restart, including with no active jobs.
                calls.append(self.publish(worker, request["worker_session_id"], None))
        await asyncio.gather(*calls)

    async def sync(self, nodes, remaining, uncertain, cooldowns):
        from task_service.service import candidate_action

        calls = []
        for node in nodes:
            worker = node["worker_id"]
            old = self.service.store.prefetches().get(worker)
            if not node.get("prefetch_capacity") and not old:
                continue
            hint = None
            if (
                node.get("prefetch_capacity") == 1
                and node["max_slots"] == 1
                and remaining[worker] == 0
                and worker not in uncertain
                and self.service.clock() >= cooldowns.get(worker, 0)
            ):
                probe = node | {
                    "available_slots": 1,
                    "reserved_slots": 0,
                    "active_execution_ids": [],
                }
                for state in self.service.store.active():
                    job = state["job"]
                    if job["cancel_requested"] or candidate_action(probe, job)[0] != "execute":
                        continue
                    for task in job["tasks"]:
                        if (
                            task["status"] not in TASK_TERMINAL
                            and not task["current_execution_id"]
                            and self.service.clock() >= state["retry_at"].get(task["task_id"], 0)
                        ):
                            hint = {
                                k: job[k]
                                for k in ("job_id", "task_type", "profile_id", "profile_digest")
                            }
                            hint.update(
                                task_id=task["task_id"],
                                input_artifact_id=task["input"]["artifact_id"],
                            )
                            break
                    if hint:
                        break
            if hint or old:
                calls.append(
                    self.publish(worker, node["session_id"], hint, node.get("prefetch_revision", 0))
                )
        await asyncio.gather(*calls)
