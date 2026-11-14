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
from pathlib import Path
from typing import Optional

import httpx

from .hardware import detect_hardware
from .telemetry import sample


class Agent:
    HEARTBEAT_INTERVAL = 3.0
    CLAIM_INTERVAL = 1.0

    def __init__(self, server: str, name: str) -> None:
        self.server = server.rstrip("/")
        self.name = name
        self.join_token = os.environ.get("JOIN_TOKEN", "")
        if not self.join_token:
            print("[agent] 缺少 JOIN_TOKEN 环境变量", file=sys.stderr)
            sys.exit(2)
        self.data_dir = Path(os.environ.get("NODE_AGENT_DATA_DIR", "./data/agent")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.worker_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.node_secret: Optional[str] = None
        self._stop = False
        self._session_stale = False
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
                        "hardware": detect_hardware(),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                with self._state_lock:
                    self.worker_id = data["worker_id"]
                    self.session_id = data["session_id"]
                    self.node_secret = data["node_secret"]
                    self._session_stale = False
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

    def _internal(self, path: str, payload: dict) -> Optional[dict]:
        try:
            resp = self._client.post(
                f"{self.server}{path}", json=payload, timeout=10.0
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
        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)
        self.register()
        # 心跳独立续租，停止确认由主线程在计算返回后发送。
        hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb_thread.start()
        try:
            while not self._stop:
                if self._session_stale:
                    # 仅在主线程空闲时重新注册，旧推理尚未结束时不释放槽位。
                    self.register()
                else:
                    self._try_claim_and_work()
                time.sleep(self.CLAIM_INTERVAL)
        finally:
            hb_thread.join(timeout=2.0)

    def _handle_sigint(self, signum, frame) -> None:
        print("[agent] 收到退出信号：完成当前任务后停止", file=sys.stderr)
        self._stop = True

    def _heartbeat_loop(self) -> None:
        while not self._stop:
            try:
                self._heartbeat()
            except Exception as e:
                print(f"[agent] 心跳异常：{e}", file=sys.stderr)
            time.sleep(self.HEARTBEAT_INTERVAL)

    def _heartbeat(self) -> None:
        if not self.worker_id or not self.session_id:
            return
        self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/heartbeat",
            {**self._auth_payload(), "telemetry": sample()},
        )

    def _try_claim_and_work(self) -> None:
        data = self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/claim",
            self._auth_payload(),
        )
        if not data:
            return
        # 认领、运行和确认停止均由主线程串行执行。
        for execution_id in data.get("stop_ack_required", []):
            self._ack_cancel(execution_id)
        prep = data.get("preparation")
        if prep:
            self._do_preparation(prep)
            return
        execution = data.get("execution")
        if execution:
            self._do_execution(execution)

    # ---------- 模型准备 ----------

    def _prep_status(self, prep_id: str, payload: dict) -> None:
        self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/preparations/{prep_id}/status",
            {**self._auth_payload(), **payload},
        )

    def _do_preparation(self, prep: dict) -> None:
        """模型准备：加载执行器并对固定样本真实试跑；通过才上报 READY。"""
        from runners.base import load_runner

        prep_id = prep["preparation_id"]
        task_type = prep["task_type"]
        self._prep_status(prep_id, {"status": "LOADING", "progress": 0.2})
        try:
            runner = load_runner(task_type)
            runner.ensure_engine()
        except Exception as e:
            self._prep_status(
                prep_id,
                {
                    "status": "FAILED",
                    "error_code": "MODEL_LOAD_FAILED",
                    "error_message": f"执行器加载失败：{e}",
                    "error_retryable": True,
                },
            )
            return

        self._prep_status(prep_id, {"status": "VERIFYING", "progress": 0.6})
        try:
            calibration = self._self_test(task_type, runner)
        except Exception as e:
            self._prep_status(
                prep_id,
                {
                    "status": "FAILED",
                    "error_code": "MODEL_LOAD_FAILED",
                    "error_message": f"自测失败：{e}",
                    "error_retryable": True,
                },
            )
            return

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # F10：自测未通过不得上报 READY
        if not calibration["passed"]:
            self._prep_status(
                prep_id,
                {
                    "status": "FAILED",
                    "error_code": "MODEL_LOAD_FAILED",
                    "error_message": (
                        f"自测未通过：{calibration['sample_count']} 个样本中"
                        f"仅 {calibration['non_empty']} 个产生识别结果"
                    ),
                    "error_retryable": True,
                    "calibration": {
                        "sample_count": calibration["sample_count"],
                        "median_compute_ms": calibration["median_compute_ms"],
                        "passed": False,
                        "finished_at": now,
                    },
                },
            )
            return
        if task_type == "ocr.image.v1":
            from runners.rapidocr_runner import combined_model_sha256

            model_sha = combined_model_sha256()
            provider = "CPUExecutionProvider"
        else:
            model_sha = None
            provider = None
        self._prep_status(
            prep_id,
            {
                "status": "READY",
                "progress": 1.0,
                "backend": "cpu",
                "runtime_provider": provider,
                "model_sha256": model_sha,
                "model_cached": True,
                "calibration": {
                    "sample_count": calibration["sample_count"],
                    "median_compute_ms": calibration["median_compute_ms"],
                    "passed": calibration["passed"],
                    "finished_at": now,
                },
            },
        )

    def _self_test(self, task_type: str, runner) -> dict:
        """固定合成样本真实试跑：3 张样本，输出耗时中位数；OCR 要求至少一张非空。"""
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
            samples.append(path)

        durations, non_empty = [], 0
        for path in samples:
            t0 = time.monotonic()
            result = runner.execute(path, {"width": None, "height": None}, {}, path.parent)
            durations.append((time.monotonic() - t0) * 1000)
            if task_type == "ocr.image.v1" and result.get("blocks"):
                non_empty += 1
        passed = non_empty >= 1 if task_type == "ocr.image.v1" else True
        return {
            "sample_count": len(samples),
            "non_empty": non_empty,
            "median_compute_ms": round(statistics.median(durations), 3),
            "passed": passed,
        }

    # ---------- 单次执行 ----------

    def _do_execution(self, execution: dict) -> None:
        from runners.base import load_runner

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
            t0 = time.monotonic()
            input_path = self._download_input(execution)
            timings["download_ms"] = (time.monotonic() - t0) * 1000

            t0 = time.monotonic()
            runner = load_runner(execution["task_type"])
            media_info = execution["input_media_info"]
            result = runner.execute(
                Path(input_path), media_info, execution.get("parameters", {}), workdir
            )
            timings["inference_ms"] = (time.monotonic() - t0) * 1000

            result["input_artifact_id"] = execution["input_artifact_id"]

            t0 = time.monotonic()
            out_file = workdir / "result.json"
            out_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            timings["write_output_ms"] = (time.monotonic() - t0) * 1000
            timings["postprocess_ms"] = 0.0
            timings["total_worker_ms"] = sum(v for k, v in timings.items() if k != "total_worker_ms")

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
            self._internal(
                f"/api/resource/internal/v1/nodes/{self.worker_id}/executions/{execution_id}/complete",
                {
                    **self._auth_payload(),
                    "outcome": "FAILED",
                    "error_code": "EXECUTION_ERROR",
                    "error_message": "节点执行失败，详见节点日志",
                    "error_retryable": True,
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
            raise ValueError("下载文件 SHA-256 与执行记录不符")
        target = self.data_dir / "work" / execution["execution_id"] / "input.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.content)
        return str(target)

    def _ack_cancel(self, execution_id: str) -> None:
        self._internal(
            f"/api/resource/internal/v1/nodes/{self.worker_id}/executions/{execution_id}/cancel_ack",
            self._auth_payload(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="节点程序：接入 P1 资源服务")
    parser.add_argument("--server", default=os.environ.get("RESOURCE_BASE_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--name", default=socket.gethostname()[:80])
    args = parser.parse_args()
    Agent(server=args.server, name=args.name).run()


if __name__ == "__main__":
    main()
