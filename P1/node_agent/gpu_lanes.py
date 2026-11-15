"""GPU 逻辑多槽：一个常驻子进程承载一份权重 + N 个推理路（线程）。

与 CPU 多槽（pool.py，进程/槽）对外协议完全一致，仅 slot 语义由"OS 进程"
变为"逻辑路"：

- 单子进程只 load_locked_runner 一次（一份权重），N 路共享。
- 整机准备与 CPU 多槽同语义：模型加载并自测通过后上报整机 READY，服务端
  一次性把本节点全部槽置 ready=1；无需（也无法）在准备期间逐槽上报 ready
  （准备期间能力仍为 PREPARING，slots/{slot_id}/ready 会被 409）。
- 取消/隔离到"路"级：等待线程 done 后父进程上报 slots/{slot_id}/stopped 原子结束该路任务并
  轮换代次；模型仍常驻，下一轮用同一份自测证据自动重报该路 ready，该路晚到
  结果被 P1 以 STALE_EXECUTION 拒收。
- 10 秒内未收到 done 时回收整个共享子进程，并通过 runtime_stopped 隔离所有路；
  其他路会一起中断。取消请求确认本身不代表计算已经停止。
- 共享子进程崩溃视为整机故障：检测到后立即走 runtime_stopped 兜底（撤销会话
  与全部能力），由外层循环重新注册并重新准备。
- 每路在线程内执行，共享一个引擎锁（方案 B 的 B2：共享 session + 锁串行提交）。

本模块只服务于 backend=="cuda" 且 slots>1 的节点；CPU 多槽仍走 pool.py，互不影响。
"""

import multiprocessing
import signal
import threading
import time
from dataclasses import dataclass

from . import compute


@dataclass
class Lane:
    slot_id: int
    generation: str
    ready: bool = False
    execution_id: str = ""
    retry_at: float = 0.0
    cancel_started: float = 0.0
    compute_finished: bool = False


class LanePool:
    def __init__(self, agent):
        self.agent = agent
        self.lanes = [Lane(m["slot_id"], m["generation"]) for m in agent._slot_metadata]
        self.profile = None
        self.preparation = None
        self.preparation_error = None
        self.preparation_started = 0.0
        self._evidence = None
        self.next_claim = 0.0
        self.child = None
        self.pipe = None

    # ---------- 子进程 ----------

    def _ensure_child(self):
        if self.child is None:
            a = self.agent
            compute.configure_thread_environment(a.threads)
            ctx = multiprocessing.get_context("spawn")
            parent, child_pipe = ctx.Pipe()
            options = {"backend": "cuda", "device_id": a.device_id, "threads": a.threads}
            child = ctx.Process(
                target=compute.compute_lanes,
                args=(
                    child_pipe,
                    a.server,
                    a.name,
                    (a.worker_id, a.session_id, a.node_secret),
                    options,
                    len(self.lanes),
                ),
            )
            child.start()
            child_pipe.close()
            self.child, self.pipe = child, parent

    def _send(self, kind, payload):
        """向子进程发送消息；发送失败（子进程已死）时走整机兜底，绝不让异常击穿 tick。"""
        if self.pipe is None:
            self.stop()
            self.agent._runtime_reset_pending = True
            return False
        try:
            self.pipe.send((kind, payload))
            return True
        except (BrokenPipeError, OSError, ValueError):
            self.stop()
            self.agent._runtime_reset_pending = True
            return False

    def _drain(self):
        """读取子进程回包：load_evidence / accepted / done / stopped。"""
        if self.pipe is None:
            return
        while self.pipe.poll():
            try:
                kind, payload = self.pipe.recv()
            except (EOFError, OSError):
                return
            if kind == "load_evidence":
                if payload.get("session_stale"):
                    self.agent._session_stale = True
                elif payload.get("error"):
                    self.preparation_error = payload["error"]
                else:
                    self._evidence = payload.get("evidence")
            elif kind == "done":
                if payload.get("session_stale"):
                    self.agent._session_stale = True
                lane = self._lane(payload.get("slot_id"))
                if lane is not None and lane.execution_id == payload.get("execution_id"):
                    lane.compute_finished = True
                    if not lane.cancel_started and lane.execution_id not in self.agent._cancel_ids:
                        lane.execution_id = ""
            # accepted / stopped 无需额外处理

    def _lane(self, slot_id):
        return next((l for l in self.lanes if l.slot_id == slot_id), None)

    def stop(self):
        for lane in self.lanes:
            lane.execution_id = ""
            lane.cancel_started = 0.0
            lane.compute_finished = False
        if self.child is not None:
            if self.pipe is not None:
                try:
                    self.pipe.send(("stop", None))
                except (BrokenPipeError, OSError, ValueError):
                    pass
            compute.stop_process(self.child)
        if self.pipe is not None:
            self.pipe.close()
        self.child = self.pipe = None
        self._evidence = None

    # ---------- 远端调用（与 pool.call 相同） ----------

    def call(self, suffix, extra=None):
        a = self.agent
        return a._internal(
            f"/api/resource/internal/v1/nodes/{a.worker_id}/{suffix}",
            {**a._auth_payload(), **(extra or {})},
        )

    def running_ids(self):
        return [lane.execution_id for lane in self.lanes if lane.execution_id]

    # ---------- 准备 ----------

    def begin_preparation(self, preparation):
        self.stop()
        self.profile = self.preparation = preparation
        self.preparation_error = None
        self.preparation_started = time.monotonic()
        self._evidence = None
        for lane in self.lanes:
            lane.ready = False
            lane.execution_id = ""
        self._ensure_child()
        self._send("load", preparation)

    def _report_ready(self, lane):
        """上报单路 ready（模型仍常驻，只需同一份自测证据）。

        仅在整机能力已 READY 后有效：准备期间的调用会被服务端 409（能力
        PREPARING），整机 READY 由 preparation_status 一次性置全部槽 ready=1。
        """
        if not self._evidence or not self.profile:
            return False
        if time.monotonic() < lane.retry_at:
            return lane.ready
        reply = self.call(
            f"slots/{lane.slot_id}/ready",
            {
                "generation": lane.generation,
                **{k: self.profile[k] for k in ("task_type", "profile_id", "profile_digest")},
                **self._evidence,
            },
        )
        lane.ready = reply is not None
        lane.retry_at = time.monotonic() + 0.5
        return lane.ready

    # ---------- 执行与取消 ----------

    def submit(self, lane, execution):
        lane.execution_id = execution["execution_id"]
        lane.compute_finished = False
        lane.cancel_started = 0.0
        self._ensure_child()
        if not self._send("execution", execution):
            lane.execution_id = ""

    def _report_stopped(self, lane):
        reply = self.call(f"slots/{lane.slot_id}/stopped", {"generation": lane.generation})
        lane.retry_at = time.monotonic() + 0.5
        if reply is None:
            return
        lane.generation = reply["generation"]
        lane.ready = False
        lane.execution_id = ""
        lane.cancel_started = 0.0
        lane.compute_finished = False
        # 模型仍常驻：由 tick 的恢复循环用同一份证据重新上报 ready。

    def _cancel_lane(self, lane):
        self._send(
            "stop_lane", {"slot_id": lane.slot_id, "execution_id": lane.execution_id}
        )

    # ---------- tick ----------

    def tick(self):
        a = self.agent
        if self.child is not None and not self.child.is_alive():
            # 共享子进程崩溃：所有路同时失效，走整机兜底（外层循环 → runtime_stopped）。
            self.stop()
            a._runtime_reset_pending = True
            return
        self._drain()
        if a._session_stale or a._sharing_disabled:
            return
        now = time.monotonic()

        if self.preparation:
            if now - self.preparation_started >= 900:
                self.stop()
                a._runtime_reset_pending = True
                return
            if self.preparation_error:
                result = a._prep_status(
                    self.preparation["preparation_id"],
                    {
                        "status": "FAILED",
                        "error_code": self.preparation_error["code"],
                        "error_message": self.preparation_error["message"],
                        "error_retryable": False,
                    },
                )
                if result is not None:
                    self.preparation = self.profile = None
                return
            if self._evidence is not None:
                # 共享模型已加载并通过自测：上报整机 READY。服务端会把本节点全部
                # 槽置 ready=1（与 CPU 多槽一致），本地同步。准备期间不逐槽上报
                # ready（能力仍 PREPARING，必然 409）。
                result = a._prep_status(
                    self.preparation["preparation_id"],
                    {"status": "READY", "progress": 1, "model_cached": True, **self._evidence},
                )
                if result is not None:
                    if result["status"] == "READY":
                        for lane in self.lanes:
                            lane.ready = True
                    else:
                        self.stop()
                        self.profile = None
                    self.preparation = None
            return

        # 执行阶段：先恢复未就绪但模型常驻的路，再处理取消，最后接新单。
        if self._evidence is not None and self.profile is not None:
            for lane in self.lanes:
                if not lane.ready:
                    self._report_ready(lane)
        for lane in self.lanes:
            if lane.execution_id and (lane.cancel_started or lane.execution_id in a._cancel_ids):
                if not lane.cancel_started:
                    lane.cancel_started = now
                    self._cancel_lane(lane)
                # A thread cannot be killed independently. A stop request is not
                # evidence of exit; do not release the slot until its done reply.
                if lane.compute_finished:
                    if now >= lane.retry_at:
                        self._report_stopped(lane)
                elif now - lane.cancel_started >= 10:
                    # Bound cancellation by joining the shared process, then let
                    # runtime_stopped fence ALL lanes before any are reused.
                    self.stop()
                    a._runtime_reset_pending = True
                    return

        if now < self.next_claim:
            return
        self.next_claim = now + 0.2

        reply = self.call("claim")
        if reply and reply.get("preparation"):
            self.begin_preparation(reply["preparation"])
            return
        if reply is None or self.profile is None:
            return
        for lane in self.lanes:
            if not lane.ready or lane.execution_id:
                continue
            if now < lane.retry_at:
                continue
            reply = self.call("claim", {"slot_id": lane.slot_id, "generation": lane.generation})
            if reply is None:
                self._report_stopped(lane)
            elif reply.get("preparation"):
                self.begin_preparation(reply["preparation"])
                return
            elif reply.get("execution"):
                self.submit(lane, reply["execution"])


def run_gpu_lanes(agent):
    signal.signal(signal.SIGINT, agent._handle_sigint)
    signal.signal(signal.SIGTERM, agent._handle_sigint)
    while agent._sharing_disabled and not agent._stop:
        time.sleep(0.1)
    if agent._stop:
        return
    agent.register()
    agent._pool = pool = LanePool(agent)
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
                agent._pool = pool = LanePool(agent)
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
