"""节点程序主循环：注册 → 心跳(3s) → 认领 → 执行 → 提交；DRAINING/SIGINT 优雅退出。

用法：
    python -m node_agent --server http://192.168.x.x:8001 --name M4-MacBook

JOIN_TOKEN 从环境变量读取；日志不打印令牌。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx

from resource_service.config import REPO_ROOT
from resource_service.media import probe_image
from resource_service.resultschema import validate_ocr_result
from resource_service.timeutil import iso_now
from runners.profile import ProfileError, load_locked_runner

from .hardware import detect_hardware
from .telemetry import sample


@contextmanager
def lock_node_directory(data_dir: Path):
    """同一节点目录只允许一个 Agent 运行，进程退出由系统释放锁。"""
    with (data_dir / "agent.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Agent:
    HEARTBEAT_INTERVAL = 3.0
    CLAIM_INTERVAL = 1.0

    def __init__(self, server: str, name: str, backend: str = "cpu",
                 device_id: int = 0, threads: int = 0, slots: int = 1,
                 cpu_budget: Optional[int] = None, slot_id: Optional[int] = None,
                 prefetch: Optional[int] = None) -> None:
        if backend not in {"cpu", "cuda"} or device_id < 0 or threads < 0:
            raise ValueError("无效的 backend/device_id/threads")
        cores = os.cpu_count() or 1
        budget = cores if cpu_budget is None else cpu_budget
        if not 1 <= slots <= 64 or not 1 <= budget <= cores or (backend == "cuda" and slots != 1):
            raise ValueError("槽位/CPU 预算无效；GPU 只允许一槽")
        prefetch = int(backend == "cuda") if prefetch is None else prefetch
        if prefetch not in (0, 1) or (prefetch and slots != 1):
            raise ValueError("预取只支持单槽，容量为 0 或 1")
        if prefetch:
            threads = threads or min(2, budget - 1)
            if threads < 1 or threads + 1 > budget:
                raise ValueError("预取需要额外一个 CPU 线程预算")
        if slots > 1 or cpu_budget is not None:
            threads = threads or budget // slots
            if threads < 1:
                raise ValueError("CPU 预算不足以为每槽分配至少一个线程")
        if slots * threads > budget:
            raise ValueError("槽位数 × 每槽线程数超过 CPU 预算")
        self.server = server.rstrip("/")
        self.name = name
        self.backend, self.device_id, self.threads = backend, device_id, threads
        self.slots, self.cpu_budget = slots, budget if threads else 0
        self._pool = None
        self._slot_metadata = []
        self.prefetch_capacity = prefetch
        self._input_pipeline = None
        self._runtime = None
        self._runtime_reset_pending = False
        self.join_token = os.environ.get("JOIN_TOKEN", "")
        if not self.join_token:
            print("[agent] 缺少 JOIN_TOKEN 环境变量", file=sys.stderr)
            sys.exit(2)
        self.data_dir = Path(os.environ.get("NODE_AGENT_DATA_DIR", "./data/agent")).resolve()
        if slot_id is not None:
            self.data_dir = self.data_dir / "slots" / str(slot_id)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.worker_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.node_secret: Optional[str] = None
        self._prepared = None
        self._sharing_disabled = False
        self._sharing_request = None
        self._last_heartbeat = time.monotonic()
        self._cancel_ids = set()
        self._stop = False
        self._session_stale = False
        self._confirm_idle = False
        self._state_lock = threading.Lock()
        self._client = httpx.Client(timeout=10.0)

    # ---------- 注册与状态 ----------

    def _install_file(self) -> Path:
        return self.data_dir / f"install-{self.name}.json"

    def _load_worker_id(self) -> Optional[str]:
        f = self._install_file()
        if f.exists():
            try:
                return json.loads(f.read_text()).get("worker_id")
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _save_worker_id(self, worker_id: str) -> None:
        self._install_file().write_text(json.dumps({"worker_id": worker_id}))

    def register(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                resp = self._client.post(
                    f"{self.server}/api/resource/internal/v1/nodes/register",
                    headers={"Authorization": f"Bearer {self.join_token}"},
                    json={
                        "worker_id": self._load_worker_id(),
                        "name": self.name,
                        "hardware": detect_hardware(self.device_id),
                        "prefetch_capacity": self.prefetch_capacity,
                        "max_slots": self.slots,
                        "threads_per_slot": self.threads,
                        "cpu_thread_budget": self.cpu_budget,
                        "backend": self.backend,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                with self._state_lock:
                    self.worker_id = data["worker_id"]
                    self.session_id = data["session_id"]
                    self.node_secret = data["node_secret"]
                    self._session_stale = False
                    self._confirm_idle = data["confirm_idle"]
                    self._slot_metadata = data.get("slots", [])
                self._prepared = None
                self._last_heartbeat = time.monotonic()
                self._save_worker_id(self.worker_id)
                print(f"[agent] 注册成功 worker_id={self.worker_id}", file=sys.stderr)
                return
            except (httpx.HTTPError, ValueError, KeyError) as e:
                print(f"[agent] 注册失败，{backoff:.0f}s 后重试：{e}", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _auth_payload(self) -> dict:
        with self._state_lock:
            return {"session_id": self.session_id, "node_secret": self.node_secret}

    def _internal(self, path: str, payload: dict, timeout: float = 10.0) -> Optional[dict]:
        try:
            resp = self._client.post(
                f"{self.server}{path}", json=payload, timeout=timeout
            )
            if resp.status_code == 409:
                detail = resp.json()
                if detail.get("code") == "SESSION_CHANGED":
                    with self._state_lock:
                        if payload["session_id"] == self.session_id:
                            self._session_stale = True
                return None
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"[agent] 请求失败 {path}: {e}", file=sys.stderr)
            return None

    # ---------- 主循环 ----------

    def run(self) -> None:
        if self.slots > 1:
            from .pool import run_pool
            return run_pool(self)
        from .compute import close_runtime
        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)
        while self._sharing_disabled and not self._stop:
            time.sleep(.1)
        if self._stop:
            return
        self.register()
        # 心跳独立续租，停止确认由主线程在计算返回后发送。
        hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb_thread.start()
        try:
            while not self._stop:
                if self._runtime is not None and (
                    not self._runtime[0].is_alive() or self._sharing_disabled
                    or self._session_stale or time.monotonic() - self._last_heartbeat >= 12
                ):
                    close_runtime(self)
                if self._session_stale:
                    # 仅在主线程空闲时重新注册，旧推理尚未结束时不释放槽位。
                    close_runtime(self, invalidate=False)
                    self._runtime_reset_pending = False
                    self.register()
                elif self._runtime_reset_pending:
                    self._reset_runtime()
                elif self._confirm_idle:
                    self._confirm_idle = self._internal(
                        f"/api/resource/internal/v1/nodes/{self.worker_id}/confirm_idle",
                        self._auth_payload(),
                    ) is None
                elif self._sharing_disabled:
                    # No compute child is alive here: the supervisor joins before returning.
                    self._try_claim_and_work()
                    self._internal(f"/api/resource/internal/v1/nodes/{self.worker_id}/confirm_idle", self._auth_payload())
                else:
                    self._tick_input_pipeline()
                    self._try_claim_and_work()
                time.sleep(.1 if self.prefetch_capacity else self.CLAIM_INTERVAL)
        finally:
            close_runtime(self)
            self._reset_runtime()
            self._stop = True
            hb_thread.join(timeout=2.0)
            self._client.close()

    def _handle_sigint(self, signum, frame) -> None:
        print("[agent] 收到退出信号：停止计算并释放会话", file=sys.stderr)
        self._stop = True

    def _reset_runtime(self) -> None:
        """Only call after joining the resident process. Retries fence the old session."""
        if self._runtime is not None or (self._input_pipeline and self._input_pipeline.process is not None):
            raise RuntimeError("计算进程未停止，不能重置运行时")
        response = self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/runtime_stopped",
            self._auth_payload(),
        )
        if response is not None or self._session_stale:
            self._runtime_reset_pending = False
            self._session_stale = True

    def _heartbeat_loop(self) -> None:
        while not self._stop:
            try:
                self._heartbeat()
            except Exception as e:
                print(f"[agent] 心跳异常：{e}", file=sys.stderr)
            time.sleep(self.HEARTBEAT_INTERVAL)

    def set_sharing(self, enabled: bool) -> bool:
        # Local stop is immediate even when the centre is unreachable.
        if not enabled:
            self._sharing_disabled = True
        self._sharing_request = enabled
        if not self.worker_id:
            self._sharing_disabled = not enabled
            return True
        response = self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/sharing",
            {**self._auth_payload(), "enabled": enabled},
        )
        if response:
            self._sharing_request = None
            if enabled:
                self._sharing_disabled = False
            return True
        return False

    def _heartbeat(self) -> None:
        if not self.worker_id or not self.session_id:
            return
        if self._sharing_request is not None:
            self.set_sharing(self._sharing_request)
        payload = {**self._auth_payload(), "telemetry": sample(self.device_id)}
        if self._pool is not None:
            payload["running_execution_ids"] = self._pool.running_ids()
        response = self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/heartbeat",
            payload,
        )
        if response:
            with self._state_lock:
                # A response from the previous session must not replace new slot generations.
                if payload["session_id"] != self.session_id:
                    return
                self._last_heartbeat = time.monotonic()
                disabled = self._sharing_request is False or not response.get("sharing_enabled", True)
                self._sharing_disabled = disabled
                self._cancel_ids = set(response.get("cancel_requested", []))
                self._slot_metadata = response.get("slots", self._slot_metadata)


    def _tick_input_pipeline(self):
        if self.prefetch_capacity and self._prepared:
            from .input_pipeline import InputPipeline
            if self._input_pipeline is None:
                self._input_pipeline = InputPipeline(self)
            try:
                self._input_pipeline.tick()
            except (OSError, ValueError, EOFError):
                self._input_pipeline.close()

    def _try_claim_and_work(self) -> None:
        data = self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/claim",
            self._auth_payload(),
        )
        if not data:
            return
        # 认领、运行和确认停止均由主线程串行执行。
        for execution_id in data.get("stop_ack_required", []):
            from .compute import close_runtime
            close_runtime(self)
            self._ack_cancel(execution_id)
        if data.get("stop_ack_required"):
            return
        prep = data.get("preparation")
        if prep:
            from .compute import supervise
            supervise(self, "preparation", prep)
            return
        execution = data.get("execution")
        if execution:
            from .compute import supervise
            supervise(self, "execution", execution)

    # ---------- 模型准备 ----------

    def _prep_status(self, prep_id: str, payload: dict) -> None:
        return self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/preparations/{prep_id}/status",
            {**self._auth_payload(), **payload},
        )

    def _prepare_local(self, prep: dict) -> dict:
        """Load/self-test one resident process, without publishing whole-node READY."""
        key = tuple(prep[k] for k in ("task_type", "profile_id", "profile_digest"))
        root = Path(os.environ.get("NODE_PROFILES_DIR", REPO_ROOT / "profiles"))
        started = time.monotonic()
        runner, model_sha, provider = load_locked_runner(
            (root / f"{key[1]}.lock.json").read_bytes(), *key,
            backend=self.backend, threads=self.threads, device_id=self.device_id,
        )
        calibration = self._self_test(key[0], runner)
        if not calibration["passed"]:
            raise ProfileError("MODEL_LOAD_FAILED", "OCR 校准样本未达到要求")
        self._prepared = (key, runner)
        print(f"[agent] model_loaded pid={os.getpid()} backend={self.backend} "
              f"threads={self.threads} device_id={self.device_id} provider={provider} "
              f"load_ms={(time.monotonic()-started)*1000:.3f}", flush=True)
        return {"backend": self.backend, "runtime_provider": provider, "model_sha256": model_sha,
                "calibration": {k: calibration[k] for k in ("sample_count", "median_compute_ms", "passed")}
                    | {"finished_at": iso_now()}}

    def _do_preparation(self, prep: dict) -> None:
        """模型准备：加载执行器并对固定样本真实试跑；通过才上报 READY。"""
        prep_id = prep["preparation_id"]
        profile_key = (prep["task_type"], prep["profile_id"], prep["profile_digest"])
        profiles_dir = Path(os.environ.get("NODE_PROFILES_DIR", REPO_ROOT / "profiles"))
        self._prepared = None
        self._prep_status(prep_id, {"status": "LOADING", "progress": 0.2})
        try:
            raw = (profiles_dir / f"{prep['profile_id']}.lock.json").read_bytes()
            started = time.monotonic()
            runner, model_sha, provider = load_locked_runner(
                raw, *profile_key, backend=self.backend, threads=self.threads, device_id=self.device_id,
            )
            print(f"[agent] model_loaded pid={os.getpid()} backend={self.backend} "
                  f"device_id={self.device_id} provider={provider} "
                  f"load_ms={(time.monotonic() - started) * 1000:.3f}", flush=True)
            self._prep_status(prep_id, {"status": "VERIFYING", "progress": 0.6})
            calibration = self._self_test(prep["task_type"], runner)
            if not calibration["passed"]:
                raise ProfileError("MODEL_LOAD_FAILED", "OCR 校准样本未达到要求")
        except Exception as exc:
            print(f"[agent] 准备失败：{exc}", file=sys.stderr)
            code = exc.code if isinstance(exc, ProfileError) else "MODEL_LOAD_FAILED"
            self._prep_status(prep_id, {
                "status": "FAILED",
                "error_code": code,
                "error_message": str(exc) if isinstance(exc, ProfileError) else "节点无法加载或自测模型，详见节点日志",
                "error_retryable": False,
            })
            return

        # 先保留已核验的 runner，READY 回包丢失时仍可履行已接纳的执行。
        self._prepared = (profile_key, runner)
        self._prep_status(prep_id, {
            "status": "READY",
            "progress": 1.0,
            "backend": self.backend,
            "runtime_provider": provider,
            "model_sha256": model_sha,
            "model_cached": True,
            "calibration": {
                "sample_count": calibration["sample_count"],
                "median_compute_ms": calibration["median_compute_ms"],
                "passed": True,
                "finished_at": iso_now(),
            },
        })

    def _self_test(self, task_type: str, runner) -> dict:
        """固定合成样本真实试跑：3 张样本，逐张核对文本及结果结构。"""
        import statistics

        from PIL import Image, ImageDraw, ImageFont

        samples = []
        for i, (size, label) in enumerate(
            [((480, 160), "HELLO OCR TEST 123"), ((640, 240), "APPROVED 456"), ((360, 200), "SAMPLE 789")]
        ):
            img = Image.new("RGB", size, "white")
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.load_default(size=44)
            except TypeError:
                font = ImageFont.load_default()
            draw.text((30, size[1] // 2 - 20), label, fill="black", font=font)
            path = self.data_dir / "calibration" / f"sample-{i}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path, format="PNG")
            samples.append((path, label))

        durations, non_empty, matches = [], 0, 0
        for path, label in samples:
            t0 = time.monotonic()
            result = runner.execute(path, {"width": None, "height": None}, {}, path.parent)
            durations.append((time.monotonic() - t0) * 1000)
            if task_type == "ocr.image.v1" and result.get("blocks"):
                non_empty += 1
                result["input_artifact_id"] = "calibration"
                validate_ocr_result(result, "calibration")
                if " ".join(result["text"].upper().split()) == label:
                    matches += 1
        passed = matches == len(samples)
        return {
            "sample_count": len(samples),
            "non_empty": non_empty,
            "median_compute_ms": round(statistics.median(durations), 3),
            "passed": passed,
        }

    # ---------- 单次执行 ----------

    def _do_execution(self, execution: dict) -> None:
        execution_id = execution["execution_id"]
        timings: dict[str, float] = {
            "download_ms": 0.0,
            "inference_ms": 0.0,
            "postprocess_ms": 0.0,
            "write_output_ms": 0.0,
            "total_worker_ms": 0.0,
        }
        workdir = self.data_dir / "work" / execution_id
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            from .input_pipeline import decode_image, load_decoded
            key = (execution["task_type"], execution["profile_id"], execution["profile_digest"])
            if self._prepared is None or self._prepared[0] != key:
                raise ProfileError("PROFILE_MISMATCH", "节点缺少本会话核验过的 profile")
            runner = self._prepared[1]
            array = None
            cached = execution.get("_prefetched")
            t0 = time.monotonic()
            if cached:
                try:
                    array = load_decoded(cached, execution["input_media_info"])
                    timings.update(prefetch_hit=True, prefetch_download_ms=cached["download_ms"],
                                   prefetch_decode_ms=cached["decode_ms"])
                except (OSError, ValueError):
                    pass  # Local speculative cache never replaces validation of the input.
                finally:
                    Path(cached["path"]).unlink(missing_ok=True)
            timings["decode_ms"] = (time.monotonic() - t0) * 1000
            if array is None:
                t0 = time.monotonic()
                input_path = self._download_input(execution)
                timings["download_ms"] = (time.monotonic() - t0) * 1000
                t0 = time.monotonic()
                try:
                    array = decode_image(input_path, execution["input_media_info"])
                except (OSError, ValueError) as exc:
                    raise ProfileError("INPUT_INVALID", "输入文件无法解码") from exc
                timings["decode_ms"] += (time.monotonic() - t0) * 1000
            print(f"[agent] execute pid={os.getpid()} execution_id={execution_id} "
                  f"backend={self.backend} device_id={self.device_id} started={time.time():.6f}", flush=True)
            t0 = time.monotonic()
            result = runner.execute_array(array, execution.get("parameters", {}), workdir)
            timings["inference_ms"] = (time.monotonic() - t0) * 1000
            print(f"[agent] inference_finished execution_id={execution_id} finished={time.time():.6f}", flush=True)

            result["input_artifact_id"] = execution["input_artifact_id"]

            t0 = time.monotonic()
            out_file = workdir / "result.json"
            out_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            timings["write_output_ms"] = (time.monotonic() - t0) * 1000
            timings["postprocess_ms"] = 0.0
            timings["total_worker_ms"] = sum(timings[k] for k in ("download_ms", "decode_ms", "inference_ms", "postprocess_ms", "write_output_ms"))

            data = self._internal(
                f"/api/resource/internal/v1/nodes/{self.worker_id}/executions/{execution_id}/complete",
                {
                    **self._auth_payload(),
                    "outcome": "SUCCEEDED",
                    "result": result,
                    "timings": timings,
                },
            )
            if data is not None:
                print(
                    f"[agent] 执行 {execution_id} 完成 status={data.get('status')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[agent] 执行 {execution_id} 失败：{e}", file=sys.stderr)
            code = e.code if isinstance(e, ProfileError) else "EXECUTION_ERROR"
            self._internal(
                f"/api/resource/internal/v1/nodes/{self.worker_id}/executions/{execution_id}/complete",
                {
                    **self._auth_payload(),
                    "outcome": "FAILED",
                    "error_code": code,
                    "error_message": "节点执行失败，详见节点日志",
                    "error_retryable": code == "EXECUTION_ERROR",
                },
            )

    def _download_input(self, execution: dict) -> str:
        path = execution.get("input_download_path")
        if not path:
            raise RuntimeError("执行缺少输入下载路径")
        resp = self._client.get(
            f"{self.server}/api/resource/internal/v1/nodes/{self.worker_id}/artifacts/"
            f"{execution['input_artifact_id']}/content",
            headers={"Authorization": f"Bearer {self.node_secret}"},
            timeout=60.0,
        )
        resp.raise_for_status()
        if hashlib.sha256(resp.content).hexdigest() != execution["input_sha256"]:
            raise ProfileError("INPUT_INVALID", "下载文件 SHA-256 与执行记录不符")
        try:
            probe_image(resp.content, execution["input_content_type"], decode=False)
        except ValueError as exc:
            raise ProfileError("INPUT_INVALID", "输入文件无法解码") from exc
        target = self.data_dir / "work" / execution["execution_id"] / "input.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.content)
        return str(target)

    def _ack_cancel(self, execution_id: str) -> None:
        # One atomic confirmation both finishes cancellation and withdraws READY.
        # Separate cancel_ack + reset would allow an intervening new acceptance
        # against a model process that has already been terminated.
        self._reset_runtime()


def main() -> None:
    parser = argparse.ArgumentParser(description="节点程序：接入 P1 资源服务")
    parser.add_argument("--server", default=os.environ.get("RESOURCE_BASE_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--name", default=socket.gethostname()[:80])
    parser.add_argument("--interactive", action="store_true", help="默认不注册；输入 on/off 开关共享")
    parser.add_argument("--backend", choices=["cpu", "cuda"], default="cpu",
                        help="显式选择执行后端；CUDA 不可用时失败，不静默降级")
    parser.add_argument("--device-id", type=int, default=0, help="CUDA 设备编号")
    parser.add_argument("--threads", type=int, default=0, help="CPU 推理线程预算；0 使用运行时默认")
    parser.add_argument("--slots", type=int, default=1, help="CPU 常驻执行槽数；GPU 仅允许 1")
    parser.add_argument("--prefetch", type=int, choices=[0, 1], default=None,
                        help="输入预取；CUDA 默认 1，CPU 默认 0；仅单槽，额外占 1 CPU 线程预算")
    parser.add_argument("--cpu-budget", type=int, help="节点 CPU 总线程预算，不得超过逻辑核心数")
    args = parser.parse_args()
    if args.device_id < 0 or args.threads < 0:
        parser.error("device-id 和 threads 不能为负数")
    agent = Agent(server=args.server, name=args.name, backend=args.backend,
                  device_id=args.device_id, threads=args.threads, slots=args.slots, cpu_budget=args.cpu_budget, prefetch=args.prefetch)
    try:
        with lock_node_directory(agent.data_dir):
            if args.interactive:
                agent._sharing_disabled = True
                def controls():
                    for line in sys.stdin:
                        if line.strip() in {"on", "off"}:
                            agent.set_sharing(line.strip() == "on")
                threading.Thread(target=controls, daemon=True).start()
            agent.run()
    except BlockingIOError:
        parser.exit(2, "节点数据目录已被另一个 Agent 占用\n")


if __name__ == "__main__":
    main()
