import asyncio
import logging
import time

from splitters.ocr import TASK_TYPE, split_images
from task_service.contract import (
    APIError, EXECUTION_TERMINAL, JOB_TERMINAL, TASK_TERMINAL, canonical, error, now, uid,
)
from task_service.p1 import DependencyUnavailable, InvalidResult, P1Rejected

log = logging.getLogger(__name__)


def find_task(state, task_id):
    return next(t for t in state["job"]["tasks"] if t["task_id"] == task_id)


class TaskService:
    def __init__(self, store, p1, settings, clock=time.time):
        self.store, self.p1, self.settings, self.clock = store, p1, settings, clock
        self.wake = asyncio.Event()
        self.tick_lock = asyncio.Lock()
        self.last_worker = ""

    async def catalog(self):
        catalog = await self.p1.catalog()
        catalog["task_types"] = [t for t in catalog["task_types"] if t["task_type"] == TASK_TYPE]
        return catalog

    async def create(self, payload):
        existing = self.store.replay(payload)
        if existing:
            return existing["job"], False
        if payload["task_type"] != TASK_TYPE:
            raise APIError(422, "UNSUPPORTED_TASK_TYPE", "本 P2 首版只实现 ocr.image.v1")
        catalog = await self.catalog()
        descriptor = next((d for d in catalog["task_types"] if d["task_type"] == TASK_TYPE), None)
        if descriptor is None:
            raise APIError(422, "UNSUPPORTED_TASK_TYPE", "P1 未发布此任务类型")
        if descriptor["configuration_state"] != "CONFIGURED" or not descriptor["profile_digest"]:
            raise APIError(503, "PROFILE_UNCONFIGURED", "P1 尚未发布可用 OCR profile", True)
        if any(payload[k] != descriptor[k] for k in ("profile_id", "profile_digest")):
            raise APIError(409, "PROFILE_MISMATCH", "请求与 P1 发布的 profile 不一致")
        if payload["parameters"]["language"] not in descriptor["supported_languages"]:
            raise APIError(422, "PARAMETER_UNSUPPORTED", "当前 profile 不支持此语言")
        artifacts = []
        for artifact_id in payload["input_artifact_ids"]:
            try:
                artifacts.append(await self.p1.artifact(artifact_id))
            except P1Rejected as exc:
                if exc.status == 404:
                    raise APIError(422, "INPUT_INVALID", "输入文件不存在", artifact_id=artifact_id) from exc
                raise DependencyUnavailable("P1 无法提供输入文件元信息") from exc
        job = {
            "job_id": uid(), "client_request_id": payload["client_request_id"],
            "task_type": TASK_TYPE, "profile_id": payload["profile_id"],
            "profile_digest": payload["profile_digest"], "parameters": payload["parameters"],
            "status": "QUEUED", "created_at": now(), "started_at": None, "finished_at": None,
            "cancel_requested": False, "dependency_status": "AVAILABLE", "counts": {},
            "tasks": split_images(artifacts, descriptor), "download_path": None,
        }
        state, created = self.store.create(payload, {
            "job": job, "requests": {}, "results": {}, "retry_at": {}, "cooldowns": {},
            "timeout_seconds": descriptor["default_timeout_seconds"],
        })
        self.wake.set()
        return state["job"], created

    def cancel(self, job_id):
        before = self.store.get(job_id)["job"]
        if before["status"] in JOB_TERMINAL:
            return before, 200

        def change(state):
            state["job"]["cancel_requested"] = True
            for task in state["job"]["tasks"]:
                if task["status"] in TASK_TERMINAL:
                    continue
                task["status"] = "CANCELLING" if task["current_execution_id"] else "CANCELLED"
                task["waiting_reason"] = "等待 P1 对账并确认取消" if task["current_execution_id"] else None
        job = self.store.edit(job_id, change)["job"]
        self.wake.set()
        return job, 200 if job["status"] in JOB_TERMINAL else 202

    def _task_edit(self, job_id, task_id, change):
        def apply(state):
            task = find_task(state, task_id)
            if task["status"] not in TASK_TERMINAL:
                change(state, task)
        return self.store.edit(job_id, apply)

    def _dependency(self, available, reason=None):
        for state in self.store.active():
            def change(s):
                s["job"]["dependency_status"] = "AVAILABLE" if available else "UNREACHABLE"
                if not available:
                    for t in s["job"]["tasks"]:
                        if t["status"] not in TASK_TERMINAL:
                            t["waiting_reason"] = reason or "P1 依赖不可达；保留原执行等待对账"
                            if not t["current_execution_id"] and not s["job"]["cancel_requested"]:
                                t["status"] = "WAITING_FOR_RESOURCES"
            self.store.edit(state["job"]["job_id"], change)

    def _record_execution(self, job_id, task_id, execution, result=None, result_error=None):
        def change(state, task):
            if task["current_execution_id"] != execution["execution_id"]:
                return
            summary_keys = ("execution_id", "attempt_no", "worker_id", "status", "accepted_at", "finished_at", "error")
            summary = {key: execution[key] for key in summary_keys}
            task["dispatches"] = [d for d in task["dispatches"] if d["execution_id"] != execution["execution_id"]] + [summary]
            if execution["accepted_at"]:
                task["attempts_used"] = max(task["attempts_used"], execution["attempt_no"])
            status = execution["status"]
            cancelled = state["job"]["cancel_requested"]
            if status == "SUCCEEDED":
                if result_error:
                    task.update(status="FAILED", error=error("RESULT_INVALID", result_error), waiting_reason=None)
                elif result is not None:
                    state["results"][task_id] = result
                    task.update(status="SUCCEEDED", result_artifact=execution["result_artifact"], error=None, waiting_reason=None)
                else:
                    task.update(status="RUNNING", waiting_reason="结果待取回", error=None)
            elif status in {"FAILED", "REJECTED"}:
                failure = execution["error"] or error("EXECUTION_ERROR", "P1 未提供失败原因")
                if status == "REJECTED":
                    state["cooldowns"][execution["worker_id"]] = self.clock() + self.settings.retry_seconds
                if cancelled:
                    task.update(status="CANCELLED", waiting_reason=None, error=failure)
                elif failure["retryable"] and task["attempts_used"] < 3:
                    task.update(status="WAITING_FOR_RESOURCES", current_execution_id=None,
                                waiting_reason=failure["message"], error=None)
                    state["retry_at"][task_id] = self.clock() + self.settings.retry_seconds
                else:
                    task.update(status="FAILED", waiting_reason=None, error=failure)
            elif status == "CANCELLED":
                task.update(status="CANCELLED", waiting_reason=None, error=execution["error"])
            else:
                task.update(status="CANCELLING" if cancelled else "RUNNING", error=None,
                            waiting_reason="等待 P1 确认取消" if cancelled else None)
        self._task_edit(job_id, task_id, change)

    def _check_execution(self, request, execution, input_sha):
        if any(execution.get(k) != v for k, v in request.items()):
            raise DependencyUnavailable("P1 执行记录与持久请求不一致；停止新尝试等待修复")
        if execution["status"] != "REJECTED" and (
                not execution["accepted_at"] or execution["input_sha256"] != input_sha):
            raise DependencyUnavailable("P1 已接纳执行缺少接纳时间或输入 SHA 不一致")

    async def _reconcile_task(self, job_id, task_id):
        state = self.store.get(job_id)
        task = find_task(state, task_id)
        execution_id = task["current_execution_id"]
        if not execution_id or task["status"] in TASK_TERMINAL:
            return
        request = state["requests"][execution_id]
        execution = await self.p1.reconcile("executions", request)
        self._check_execution(request, execution, task["input"]["sha256"])
        if self.store.get(job_id)["job"]["cancel_requested"] and execution["status"] not in EXECUTION_TERMINAL:
            execution = await self.p1.cancel(execution_id)
            self._check_execution(request, execution, task["input"]["sha256"])
        self._record_execution(job_id, task_id, execution)
        if execution["status"] == "SUCCEEDED":
            try:
                if not execution["result_artifact"]:
                    raise InvalidResult("SUCCEEDED 缺少结果 artifact")
                result = await self.p1.result(execution["result_artifact"])
                info = task["input"]["media_info"]
                if (result["input_artifact_id"] != task["input"]["artifact_id"]
                        or result["image_width"] != info["width"] or result["image_height"] != info["height"]):
                    raise InvalidResult("OCR 结果输入关联或原图尺寸不匹配")
                for block in result["blocks"]:
                    x0, y0, x1, y1 = block["bbox"]
                    if not (0 <= x0 <= x1 <= info["width"] and 0 <= y0 <= y1 <= info["height"]):
                        raise InvalidResult("OCR 文字框越界或坐标顺序无效")
                self._record_execution(job_id, task_id, execution, result=result)
            except InvalidResult as exc:
                self._record_execution(job_id, task_id, execution, result_error=str(exc))

    async def _sync_preparation(self, key, record):
        request = record["request"]
        result = await self.p1.reconcile("preparations", request)
        if any(result.get(k) != v for k, v in request.items()):
            raise DependencyUnavailable("P1 准备记录与原请求不一致")
        record.update(status=result["status"], error=result["error"], retry_at=self.clock() + self.settings.retry_seconds)
        self.store.save_preparation(key, record)

    async def _prepare(self, node, job):
        key = canonical([node["worker_id"], node["session_id"], job["profile_id"], job["profile_digest"]])
        previous = self.store.preparations().get(key)
        if previous:
            if previous["status"] == "FAILED":
                return "模型准备失败，等待 P1 修复或新会话"
            if previous["status"] == "REJECTED":
                if not previous["error"] or not previous["error"]["retryable"]:
                    return "模型准备被拒绝，等待 P1 修复"
                if self.clock() < previous["retry_at"]:
                    return "模型准备被拒绝，等待退避"
            elif previous["status"] != "READY":
                return "等待模型准备完成"
        request = {"preparation_id": uid(), "worker_id": node["worker_id"],
                   "worker_session_id": node["session_id"],
                   **{k: job[k] for k in ("task_type", "profile_id", "profile_digest")}}
        record = {"request": request, "status": "UNKNOWN", "error": None, "retry_at": 0}
        self.store.save_preparation(key, record)
        await self._sync_preparation(key, record)
        return "已申请模型准备，等待 READY"

    async def _dispatch(self, job_id, task_id, node):
        execution_id = uid()

        def change(state, task):
            if state["job"]["cancel_requested"] or task["current_execution_id"]:
                return
            job = state["job"]
            request = {"execution_id": execution_id, "job_id": job_id, "task_id": task_id,
                       "attempt_no": task["attempts_used"] + 1, "worker_id": node["worker_id"],
                       "worker_session_id": node["session_id"],
                       **{k: job[k] for k in ("task_type", "profile_id", "profile_digest", "parameters")},
                       "input_artifact_id": task["input"]["artifact_id"],
                       "timeout_seconds": state["timeout_seconds"]}
            state["requests"][execution_id] = request
            task.update(status="DISPATCHING", current_execution_id=execution_id,
                        waiting_reason="执行接纳结果待对账", error=None)
        state = self._task_edit(job_id, task_id, change)
        if find_task(state, task_id)["current_execution_id"] == execution_id:
            self.last_worker = node["worker_id"]
            await self._reconcile_task(job_id, task_id)

    async def tick(self):
        async with self.tick_lock:
            if not self.store.active():
                return True
            try:
                for key, prep in self.store.preparations().items():
                    if prep["status"] not in {"READY", "FAILED", "REJECTED"}:
                        await self._sync_preparation(key, prep)
                # Recovery always precedes any new allocation, including after restart.
                for state in self.store.active():
                    for task in state["job"]["tasks"]:
                        if task["current_execution_id"] and task["status"] not in TASK_TERMINAL:
                            await self._reconcile_task(state["job"]["job_id"], task["task_id"])
                if not self.store.active():
                    return True
                nodes = (await self.p1.resources())["resources"]
                self._dependency(True)
                busy = set()
                cooldowns = {}
                for state in self.store.active():
                    for task in state["job"]["tasks"]:
                        if task["current_execution_id"] and task["status"] not in TASK_TERMINAL:
                            busy.add(state["requests"][task["current_execution_id"]]["worker_id"])
                    for worker, deadline in state["cooldowns"].items():
                        cooldowns[worker] = max(cooldowns.get(worker, 0), deadline)
                for initial in self.store.active():
                    job_id = initial["job"]["job_id"]
                    for initial_task in initial["job"]["tasks"]:
                        state = self.store.get(job_id)
                        job, task_id = state["job"], initial_task["task_id"]
                        task = find_task(state, task_id)
                        if (job["cancel_requested"] or task["status"] in TASK_TERMINAL
                                or task["current_execution_id"] or self.clock() < state["retry_at"].get(task_id, 0)):
                            continue
                        reason = "没有匹配 profile 且空闲的 ONLINE/ACTIVE 节点"
                        candidates = sorted(nodes, key=lambda n: (n["worker_id"] <= self.last_worker, n["worker_id"]))
                        for node in candidates:
                            worker = node["worker_id"]
                            if (worker in busy or self.clock() < cooldowns.get(worker, 0)
                                    or node["connection"] != "ONLINE" or node["mode"] != "ACTIVE"
                                    or node["cleanup_pending"] or node["available_slots"] != 1
                                    or node["active_execution_ids"] or node["active_preparation_id"]):
                                continue
                            cap = next((c for c in node["capabilities"] if all(c[k] == job[k]
                                        for k in ("task_type", "profile_id", "profile_digest"))), None)
                            if not cap:
                                continue
                            if cap["state"] == "READY" and cap["verified"]:
                                busy.add(worker)
                                await self._dispatch(job_id, task_id, node)
                                break
                            if cap["state"] == "NOT_PREPARED":
                                busy.add(worker)
                                reason = await self._prepare(node, job)
                            elif cap["state"] in {"ERROR", "UNAVAILABLE"}:
                                reason = cap["reason"] or "节点 OCR 能力不可用，等待 P1 修复"
                        else:
                            def wait(s, t):
                                if not s["job"]["cancel_requested"]:
                                    t.update(status="WAITING_FOR_RESOURCES", waiting_reason=reason)
                            self._task_edit(job_id, task_id, wait)
                return True
            except (DependencyUnavailable, P1Rejected) as exc:
                self._dependency(False, str(exc) if isinstance(exc, DependencyUnavailable) else "P1 依赖接口异常")
                return False

    async def run(self):
        delay = self.settings.poll_seconds
        while True:
            self.wake.clear()
            try:
                healthy = await self.tick()
                delay = self.settings.poll_seconds if healthy else min(10, max(1, delay * 2))
            except Exception:
                # Do not log bodies, credentials or artifact contents. State remains durable.
                log.error("P2 scheduler internal error; pending requests retained", exc_info=False)
                self._dependency(False, "调度器异常，保留原请求等待恢复")
                delay = 10
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except TimeoutError:
                pass
