import asyncio
import logging
import time

from splitters.ocr import TASK_TYPE, split_images

from task_service.contract import (
    EXECUTION_TERMINAL,
    JOB_TERMINAL,
    TASK_TERMINAL,
    APIError,
    canonical,
    error,
    now,
    uid,
)
from task_service.p1 import DependencyUnavailable, InvalidResult, P1Rejected
from task_service.prefetch import PrefetchPlanner
from task_service.scheduling import PROVIDERS, Scheduler, measurement

log = logging.getLogger(__name__)


def find_task(state, task_id):
    return next(t for t in state["job"]["tasks"] if t["task_id"] == task_id)


def candidate_action(node, job):
    """Plan 1.0 §3.4–3.5: an empty preparation slot does not require READY."""
    if node["connection"] != "ONLINE":
        return None, "节点离线（OFFLINE）"
    if node["mode"] != "ACTIVE":
        return None, f"节点暂停或排空中（{node['mode']}）"
    if node["cleanup_pending"]:
        return None, "节点等待清理完成"
    if node["active_preparation_id"]:
        return None, "节点模型准备中（PREPARING），等待原准备完成"
    if (
        node["reserved_slots"] < len(node["active_execution_ids"])
        or node["available_slots"] + node["reserved_slots"] > node["max_slots"]
    ):
        return None, "节点执行占槽快照不一致"
    if node["available_slots"] <= 0:
        return None, "节点占槽或尚无可用槽位"
    cap = next(
        (
            c
            for c in node["capabilities"]
            if all(c[k] == job[k] for k in ("task_type", "profile_id"))
        ),
        None,
    )
    if cap is None:
        return None, "节点未发布匹配 task_type/profile_id 的能力"
    state = cap["state"]
    if state in {"ERROR", "UNAVAILABLE"}:
        return None, f"节点能力 {state}：{cap['reason'] or '等待 P1 修复或配置兼容节点'}"
    if state == "PREPARING":
        return None, "节点模型准备中（PREPARING），等待原准备完成"
    if cap["profile_digest"] is not None and cap["profile_digest"] != job["profile_digest"]:
        return None, f"{state} 能力与 Job 的 profile 摘要不匹配，等待配置对齐"
    if state == "NOT_PREPARED":
        if node["active_execution_ids"] or node["reserved_slots"]:
            return None, "节点已有执行占槽，不能整体准备"
        if cap["verified"]:
            return None, "NOT_PREPARED 与 verified=true 矛盾，等待 P1 修复协议状态"
        return "prepare", None
    # READY requires reported evidence. P2 does not load or verify P1's model files.
    if cap["profile_digest"] is None:
        return None, "READY 缺少 profile 摘要，等待 P1 修复配置"
    if not cap["verified"]:
        return None, "READY 但 verified=false，等待 P1 完成核验"
    if not cap["calibration"] or not cap["calibration"]["passed"]:
        return None, "READY 缺少通过的自测，等待 P1 修复核验状态"
    if not cap["backend"] or not cap["runtime_provider"] or not cap["model_sha256"]:
        return None, "READY 缺少实际后端、provider 或模型 SHA 核验证据"
    if PROVIDERS.get(cap["backend"]) != cap["runtime_provider"]:
        return None, "READY 后端与实际 provider 不匹配"
    threads, budget = node.get("threads_per_slot", 0), node.get("cpu_thread_budget", 0)
    prefetch = node.get("prefetch_capacity", 0)
    # 预取仍仅单槽：该校验对所有后端一致（含 GPU）。
    if prefetch and (node["max_slots"] != 1 or threads < 1):
        return None, "节点 CPU 线程预算不足或未声明"
    # GPU 逻辑多路：不再要求单槽，也不走 CPU 多槽线程预算（显存预算已在 P1 注册准入）。
    # 其余（CPU 任意槽、GPU 单槽）保持原有线程预算校验不变。
    if not (cap["backend"] == "cuda" and node["max_slots"] > 1):
        if (node["max_slots"] > 1 and threads < 1) or (
            threads and threads * node["max_slots"] + prefetch > budget
        ):
            return None, "节点 CPU 线程预算不足或未声明"
        if node["hardware"].get("logical_cores") and budget > node["hardware"]["logical_cores"]:
            return None, "节点 CPU 预算超过逻辑核心数"
    return "execute", None


class TaskService:
    def __init__(self, store, p1, settings, clock=time.time):
        self.store, self.p1, self.settings, self.clock = store, p1, settings, clock
        self.wake = asyncio.Event()
        self.tick_lock = asyncio.Lock()
        self.last_worker = ""
        self.scheduler = Scheduler(store, settings, clock)
        self.io_slots = asyncio.Semaphore(settings.scheduler_concurrency)
        self.healthy_workers = False
        self.prefetch = PrefetchPlanner(self)

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
        if descriptor["result_schema"] != "OCRResult":
            raise DependencyUnavailable("P1 OCR 目录声明了不匹配的结果结构")
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
                    raise APIError(
                        422, "INPUT_INVALID", "输入文件不存在", artifact_id=artifact_id
                    ) from exc
                raise DependencyUnavailable("P1 无法提供输入文件元信息") from exc
        job = {
            "job_id": uid(),
            "client_request_id": payload["client_request_id"],
            "task_type": TASK_TYPE,
            "profile_id": payload["profile_id"],
            "profile_digest": payload["profile_digest"],
            "parameters": payload["parameters"],
            "status": "QUEUED",
            "created_at": now(),
            "started_at": None,
            "finished_at": None,
            "cancel_requested": False,
            "dependency_status": "AVAILABLE",
            "counts": {},
            "tasks": split_images(artifacts, descriptor),
            "download_path": None,
        }
        state, created = self.store.create(
            payload,
            {
                "job": job,
                "requests": {},
                "results": {},
                "retry_at": {},
                "cooldowns": {},
                "timeout_seconds": descriptor["default_timeout_seconds"],
            },
        )
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
                task["waiting_reason"] = (
                    "等待 P1 对账并确认取消" if task["current_execution_id"] else None
                )

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
            state["job"]["dependency_status"] = "AVAILABLE"
            summary_keys = (
                "execution_id",
                "attempt_no",
                "worker_id",
                "status",
                "accepted_at",
                "finished_at",
                "error",
            )
            summary = {key: execution[key] for key in summary_keys}
            task["dispatches"] = [
                d for d in task["dispatches"] if d["execution_id"] != execution["execution_id"]
            ] + [summary]
            if execution["accepted_at"]:
                task["attempts_used"] = max(task["attempts_used"], execution["attempt_no"])
            status = execution["status"]
            cancelled = state["job"]["cancel_requested"]
            if status == "SUCCEEDED":
                if result_error:
                    task.update(
                        status="FAILED",
                        error=error("RESULT_INVALID", result_error),
                        waiting_reason=None,
                    )
                elif result is not None:
                    if not cancelled:
                        decision = self.store.scheduling_decision(execution["execution_id"])
                        sample = measurement(execution, decision)
                        age = (
                            self.clock() - decision.get("decided_at", float("-inf"))
                            if decision
                            else float("inf")
                        )
                        if sample and 0 <= age <= self.settings.history_ttl_seconds:
                            self.store.record_scheduling_sample(
                                execution["execution_id"], self.clock(), sample
                            )
                    state["results"][task_id] = result
                    task.update(
                        status="SUCCEEDED",
                        result_artifact=execution["result_artifact"],
                        error=None,
                        waiting_reason=None,
                    )
                else:
                    task.update(status="RUNNING", waiting_reason="结果待取回", error=None)
            elif status in {"FAILED", "REJECTED"}:
                failure = execution["error"] or error("EXECUTION_ERROR", "P1 未提供失败原因")
                if status == "REJECTED":
                    state["cooldowns"][execution["worker_id"]] = (
                        self.clock() + self.settings.retry_seconds
                    )
                if cancelled:
                    task.update(status="CANCELLED", waiting_reason=None, error=failure)
                elif failure["retryable"] and task["attempts_used"] < 3:
                    task.update(
                        status="WAITING_FOR_RESOURCES",
                        current_execution_id=None,
                        waiting_reason=failure["message"],
                        error=None,
                    )
                    state["retry_at"][task_id] = self.clock() + self.settings.retry_seconds
                else:
                    task.update(status="FAILED", waiting_reason=None, error=failure)
            elif status == "CANCELLED":
                failure = execution["error"]
                withdrawn = (
                    failure
                    and failure["code"] == "NODE_UNAVAILABLE"
                    and failure["retryable"]
                    and failure["details"].get("execution_id") == execution["execution_id"]
                )
                if withdrawn and not cancelled:
                    if task["attempts_used"] < 3:
                        task.update(
                            status="WAITING_FOR_RESOURCES",
                            current_execution_id=None,
                            waiting_reason=failure["message"],
                            error=None,
                        )
                        state["retry_at"][task_id] = self.clock() + self.settings.retry_seconds
                    else:
                        task.update(status="FAILED", waiting_reason=None, error=failure)
                else:
                    task.update(status="CANCELLED", waiting_reason=None, error=failure)
            else:
                task.update(
                    status="CANCELLING" if cancelled else "RUNNING",
                    error=None,
                    waiting_reason="等待 P1 确认取消" if cancelled else None,
                )

        self._task_edit(job_id, task_id, change)

    def _check_execution(self, request, execution, input_sha):
        if any(execution.get(k) != v for k, v in request.items()):
            raise DependencyUnavailable("P1 执行记录与持久请求不一致；停止新尝试等待修复")
        if execution["status"] != "REJECTED" and (
            not execution["accepted_at"] or execution["input_sha256"] != input_sha
        ):
            raise DependencyUnavailable("P1 已接纳执行缺少接纳时间或输入 SHA 不一致")

    async def _reconcile_task(self, job_id, task_id):
        state = self.store.get(job_id)
        task = find_task(state, task_id)
        execution_id = task["current_execution_id"]
        if not execution_id or task["status"] in TASK_TERMINAL:
            return
        request = state["requests"][execution_id]
        execution = await self._request(lambda: self.p1.reconcile("executions", request))
        self._check_execution(request, execution, task["input"]["sha256"])
        if (
            self.store.get(job_id)["job"]["cancel_requested"]
            and execution["status"] not in EXECUTION_TERMINAL
        ):
            execution = await self._request(lambda: self.p1.cancel(execution_id))
            self._check_execution(request, execution, task["input"]["sha256"])
        self._record_execution(job_id, task_id, execution)
        if execution["status"] == "SUCCEEDED":
            try:
                if not execution["result_artifact"]:
                    raise InvalidResult("SUCCEEDED 缺少结果 artifact")
                result = await self._request(lambda: self.p1.result(execution["result_artifact"]))
                info = task["input"]["media_info"]
                if (
                    result["input_artifact_id"] != task["input"]["artifact_id"]
                    or result["image_width"] != info["width"]
                    or result["image_height"] != info["height"]
                ):
                    raise InvalidResult("OCR 结果输入关联或原图尺寸不匹配")
                for block in result["blocks"]:
                    x0, y0, x1, y1 = block["bbox"]
                    if not (0 <= x0 <= x1 <= info["width"] and 0 <= y0 <= y1 <= info["height"]):
                        raise InvalidResult("OCR 文字框越界或坐标顺序无效")
                self._record_execution(job_id, task_id, execution, result=result)
            except InvalidResult as exc:
                self._record_execution(job_id, task_id, execution, result_error=str(exc))
            except DependencyUnavailable:
                # An unavailable result file must not starve unrelated tasks. The
                # original successful execution remains current and is fetched again.
                def pending(s, t):
                    t["waiting_reason"] = "结果待取回：P1 文件暂不可读，保留原执行继续读取"

                self._task_edit(job_id, task_id, pending)

    async def _sync_preparation(self, key, record):
        request = record["request"]
        result = await self._request(lambda: self.p1.reconcile("preparations", request))
        if any(result.get(k) != v for k, v in request.items()):
            raise DependencyUnavailable("P1 准备记录与原请求不一致")
        record.update(
            status=result["status"],
            error=result["error"],
            retry_at=self.clock() + self.settings.retry_seconds,
        )
        self.store.save_preparation(key, record)

    def _preparation_wait(self, record):
        failure = record["error"]
        detail = f"{failure['code']}：{failure['message']}" if failure else "P1 未提供错误详情"
        if record["status"] == "FAILED":
            return f"模型准备失败（{detail}），等待 P1 修复或新会话"
        if record["status"] == "REJECTED":
            if not failure or not failure["retryable"]:
                return f"模型准备被拒绝（{detail}），等待 P1 修复"
            if self.clock() < record["retry_at"]:
                return f"模型准备被拒绝（{detail}），等待退避"
        elif record["status"] != "READY":
            return "等待模型准备完成"
        return None

    async def _prepare(self, node, job):
        key = canonical(
            [node["worker_id"], node["session_id"], job["profile_id"], job["profile_digest"]]
        )
        previous = self.store.preparations().get(key)
        if previous and not (
            previous["status"] == "FAILED"
            and previous.get("error")
            and previous["error"]["code"] == "NODE_UNAVAILABLE"
            and previous["error"]["retryable"]
            and node["mode"] == "ACTIVE"
        ):
            reason = self._preparation_wait(previous)
            if reason:
                return reason
        request = {
            "preparation_id": uid(),
            "worker_id": node["worker_id"],
            "worker_session_id": node["session_id"],
            **{k: job[k] for k in ("task_type", "profile_id", "profile_digest")},
        }
        record = {"request": request, "status": "UNKNOWN", "error": None, "retry_at": 0}
        self.store.save_preparation(key, record)
        await self._sync_preparation(key, record)
        return self._preparation_wait(record) or "模型准备完成，等待资源 READY"

    async def _dispatch(self, job_id, task_id, node, decision):
        execution_id = uid()

        def change(state, task):
            if state["job"]["cancel_requested"] or task["current_execution_id"]:
                return
            job = state["job"]
            request = {
                "execution_id": execution_id,
                "job_id": job_id,
                "task_id": task_id,
                "attempt_no": task["attempts_used"] + 1,
                "worker_id": node["worker_id"],
                "worker_session_id": node["session_id"],
                **{k: job[k] for k in ("task_type", "profile_id", "profile_digest", "parameters")},
                "input_artifact_id": task["input"]["artifact_id"],
                "timeout_seconds": state["timeout_seconds"],
            }
            state["requests"][execution_id] = request
            self.store.record_scheduling_decision(execution_id, decision)
            task.update(
                status="DISPATCHING",
                current_execution_id=execution_id,
                waiting_reason="执行接纳结果待对账",
                error=None,
            )

        state = self._task_edit(job_id, task_id, change)
        if find_task(state, task_id)["current_execution_id"] == execution_id:
            self.last_worker = node["worker_id"]
            await self._io(lambda: self._reconcile_task(job_id, task_id))

    async def _io(self, operation):
        async with self.io_slots:
            return await operation()

    async def _request(self, operation):
        try:
            return await asyncio.wait_for(
                operation(), timeout=self.settings.scheduler_io_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise DependencyUnavailable("P1 对账或结果下载超时，保留原执行") from exc

    async def _recover_work(self):
        """Reconcile independent executions concurrently; an uncertain worker is fenced."""
        work = []
        for key, prep in self.store.preparations().items():
            if prep["status"] not in {"READY", "FAILED", "REJECTED"}:
                work.append(
                    (
                        prep["request"]["worker_id"],
                        lambda k=key, p=prep: self._sync_preparation(k, p),
                    )
                )
        for state in self.store.active():
            for task in state["job"]["tasks"]:
                eid = task["current_execution_id"]
                if eid and task["status"] not in TASK_TERMINAL:
                    work.append(
                        (
                            state["requests"][eid]["worker_id"],
                            lambda j=state["job"]["job_id"], t=task["task_id"]: (
                                self._reconcile_task(j, t)
                            ),
                        )
                    )

        async def recover(worker, operation):
            try:
                await self._io(operation)
            except (DependencyUnavailable, P1Rejected):
                return worker
            return None

        tasks = [asyncio.create_task(recover(*item)) for item in work]
        try:
            return {worker for worker in await asyncio.gather(*tasks) if worker}
        finally:
            # Cancellation/shutdown may not leave network tasks mutating a closed Store.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self):
        async with self.tick_lock:
            self.healthy_workers = False
            await self.prefetch.revoke_invalid()
            if not self.store.active():
                return True
            try:
                # Recovery always precedes any new allocation, including after restart.
                uncertain = await self._recover_work()
                if not self.store.active():
                    return not uncertain
                nodes = (await self._request(self.p1.resources))["resources"]
                self._dependency(True)
                busy = {}
                pending = {}
                cooldowns = {}
                for state in self.store.active():
                    for task in state["job"]["tasks"]:
                        if task["current_execution_id"] and task["status"] not in TASK_TERMINAL:
                            worker = state["requests"][task["current_execution_id"]]["worker_id"]
                            eid = task["current_execution_id"]
                            summary = next(
                                (d for d in task["dispatches"] if d["execution_id"] == eid), None
                            )
                            if summary is None or summary["status"] not in EXECUTION_TERMINAL:
                                pending.setdefault(worker, set()).add(eid)
                    for worker, deadline in state["cooldowns"].items():
                        cooldowns[worker] = max(cooldowns.get(worker, 0), deadline)
                remaining = {
                    n["worker_id"]: max(
                        0,
                        min(n["available_slots"], n["max_slots"] - n["reserved_slots"])
                        - len(pending.get(n["worker_id"], set()) - set(n["active_execution_ids"])),
                    )
                    for n in nodes
                }
                for initial in self.store.active():
                    job_id = initial["job"]["job_id"]
                    for initial_task in initial["job"]["tasks"]:
                        state = self.store.get(job_id)
                        job, task_id = state["job"], initial_task["task_id"]
                        task = find_task(state, task_id)
                        if (
                            job["cancel_requested"]
                            or task["status"] in TASK_TERMINAL
                            or task["current_execution_id"]
                            or self.clock() < state["retry_at"].get(task_id, 0)
                        ):
                            continue
                        reasons = []
                        ready, cold = [], []
                        candidates = sorted(
                            nodes,
                            key=lambda n: (n["worker_id"] <= self.last_worker, n["worker_id"]),
                        )
                        for node in candidates:
                            worker = node["worker_id"]
                            if worker in uncertain:
                                reasons.append("节点执行或准备尚未对账，暂不接新任务")
                                continue
                            if worker in busy:
                                reasons.append(busy[worker])
                                continue
                            if self.clock() < cooldowns.get(worker, 0):
                                reasons.append("节点拒绝后退避中")
                                continue
                            action, reason = candidate_action(node, job)
                            if action and remaining[worker] <= 0:
                                reasons.append("节点占槽或接纳结果待对账")
                                continue
                            if action == "execute":
                                ready.append(
                                    (
                                        node,
                                        self.scheduler.estimate(node, job, task, remaining[worker]),
                                    )
                                )
                            elif action == "prepare":
                                cold.append(node)
                            elif reason:
                                reasons.append(reason)
                        if ready:
                            node, decision = self.scheduler.choose(ready, self.last_worker)
                            worker = node["worker_id"]
                            remaining[worker] -= 1
                            try:
                                await self._dispatch(job_id, task_id, node, decision)
                            except (DependencyUnavailable, P1Rejected):
                                uncertain.add(worker)
                            # A rejection may have established a cooldown during this tick.
                            cooldowns.update(
                                {
                                    w: max(cooldowns.get(w, 0), deadline)
                                    for w, deadline in self.store.get(job_id)["cooldowns"].items()
                                }
                            )
                            # Periodically prepare one cold node too: a steady stream of small
                            # Jobs must not leave a newly joined GPU forever unmeasured.
                            if (
                                cold
                                and self.settings.scheduler_policy == "resource_aware"
                                and self.store.scheduling_sequence()
                                % self.settings.exploration_interval
                                == 0
                            ):
                                target = min(
                                    cold,
                                    key=lambda n: (
                                        not bool(n["hardware"].get("gpu_name")),
                                        n["worker_id"],
                                    ),
                                )
                                try:
                                    busy[target["worker_id"]] = await self._prepare(target, job)
                                except (DependencyUnavailable, P1Rejected):
                                    uncertain.add(target["worker_id"])
                                    busy[target["worker_id"]] = "模型准备接纳结果待对账"
                        else:
                            # Ready slots run immediately; cold nodes are prepared when demand exceeds them.
                            for node in cold:
                                worker = node["worker_id"]
                                try:
                                    reason = await self._prepare(node, job)
                                except (DependencyUnavailable, P1Rejected):
                                    uncertain.add(worker)
                                    reason = "模型准备接纳结果待对账"
                                busy[worker] = reason
                                reasons.append(reason)
                            reason = (
                                "；".join(dict.fromkeys(reasons)) or "P1 尚未发布资源：没有资源节点"
                            )

                            def wait(s, t):
                                if not s["job"]["cancel_requested"]:
                                    t.update(status="WAITING_FOR_RESOURCES", waiting_reason=reason)

                            self._task_edit(job_id, task_id, wait)
                if uncertain:
                    # Preserve progress on healthy workers, while reporting affected Jobs as degraded.
                    for state in self.store.active():
                        if (
                            any(r["worker_id"] in uncertain for r in state["requests"].values())
                            or not state["requests"]
                        ):
                            self.store.edit(
                                state["job"]["job_id"],
                                lambda s: s["job"].update(dependency_status="UNREACHABLE"),
                            )
                await self.prefetch.sync(nodes, remaining, uncertain, cooldowns)
                self.healthy_workers = any(
                    n["worker_id"] not in uncertain
                    and n["connection"] == "ONLINE"
                    and n["mode"] == "ACTIVE"
                    for n in nodes
                )
                return not uncertain
            except (DependencyUnavailable, P1Rejected) as exc:
                self._dependency(
                    False, str(exc) if isinstance(exc, DependencyUnavailable) else "P1 依赖接口异常"
                )
                return False

    async def run(self):
        failures = 0
        while True:
            self.wake.clear()
            try:
                healthy = await self.tick()
                delay = (
                    self.settings.poll_seconds
                    if healthy or self.healthy_workers
                    else min(10, 2**failures)
                )
                failures = 0 if healthy else min(4, failures + 1)
            except Exception:
                # Do not log bodies, credentials or artifact contents. State remains durable.
                log.error("P2 scheduler internal error; pending requests retained", exc_info=False)
                self._dependency(False, "调度器异常，保留原请求等待恢复")
                delay = 10
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
