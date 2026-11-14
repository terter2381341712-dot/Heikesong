#!/usr/bin/env python3
"""P1 自检：运行本机进程，检查 R02–R08、R10、R11、X01 和 R09 忙时拒绝。

用法（仓库根目录）：
    .venv/bin/python scripts/verify_p1.py
    .venv/bin/python scripts/verify_p1.py --keep-data   # 保留临时数据目录便于排查

说明：
- 会真实启动 resource_service 与 node_agent 子进程，使用临时数据目录与随机端口。
- R01（第二台电脑互通）无法在本机自证，标记为 MANUAL 并给出操作步骤。
- R11 使用模拟节点验证协议（契约允许：模拟节点仅用于协议检查）。
- 不修改仓库文件，不写入 data/。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
LOCK = REPO / "profiles" / "ocr.default.v1.lock.json"

SNAPSHOTS: dict = {}
RESULTS: list[tuple[str, str, str]] = []  # (case, status, evidence)
REQUIRED_CASES = {
    "R01a", "R02", "R03", "R04", "R05", "E2E", "R06", "R07", "R08",
    "R09", "R10", "R11", "L13", "X01", "L14",
}


def record(case: str, ok: Optional[bool], evidence: str) -> None:
    status = "PASS" if ok is True else ("FAIL" if ok is False else "NOT_RUN")
    RESULTS.append((case, status, evidence))
    print(f"  [{status}] {case}: {evidence}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_png(text: str = "HELLO P1 2026", size=(640, 220)) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=52)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((40, size[1] // 2 - 26), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Server:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: Optional[subprocess.Popen] = None
        self.log = data_dir.parent / f"server-{self.port}.log"

    def start(self) -> None:
        env = {
            **os.environ,
            "RESOURCE_DATA_DIR": str(self.data_dir),
            "RESOURCE_PORT": str(self.port),
            "RESOURCE_BIND_HOST": "127.0.0.1",
            "RESOURCE_PROFILES_DIR": str(REPO / "profiles"),
            "DEMO_TOKEN": "demo-t",
            "TASK_TOKEN": "task-t",
            "JOIN_TOKEN": "join-t",
        }
        self._logf = open(self.log, "w")
        self.proc = subprocess.Popen(
            [PY, "-m", "resource_service"],
            cwd=str(REPO), env=env, stdout=self._logf, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                if self.proc.poll() is not None:
                    break
                if httpx.get(f"{self.base}/api/resource/v1/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"服务未启动，日志见 {self.log}")

    def stop(self, hard: bool = False) -> None:
        if self.proc is None:
            return
        if hard:
            self.proc.kill()
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None
        try:
            self._logf.close()
        except Exception:
            pass


class Agent:
    def __init__(self, server: Server, data_dir: Path, name: str = "verify-node") -> None:
        self.server = server
        self.data_dir = data_dir
        self.name = name
        self.proc: Optional[subprocess.Popen] = None
        self.log = data_dir.parent / f"agent-{name}.log"

    def start(self) -> None:
        env = {
            **os.environ,
            "JOIN_TOKEN": "join-t",
            "NODE_AGENT_DATA_DIR": str(self.data_dir),
        }
        self._logf = open(self.log, "w")
        self.proc = subprocess.Popen(
            [PY, "-m", "node_agent", "--server", self.server.base, "--name", self.name],
            cwd=str(REPO), env=env, stdout=self._logf, stderr=subprocess.STDOUT,
        )

    def wait_online(self, timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            nodes = api_nodes(self.server)
            for n in nodes:
                if n["name"] == self.name and n["connection"] == "ONLINE" and not n["cleanup_pending"]:
                    return n
            time.sleep(0.3)
        raise RuntimeError(f"节点未上线，日志见 {self.log}")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None
        try:
            self._logf.close()
        except Exception:
            pass


DEMO = {"Authorization": "Bearer demo-t"}
TASK = {"Authorization": "Bearer task-t"}
JOIN = {"Authorization": "Bearer join-t"}


def api_nodes(server: Server) -> list[dict]:
    r = httpx.get(f"{server.base}/api/resource/v1/resources", headers=DEMO, timeout=10)
    r.raise_for_status()
    return r.json()["resources"]


def profile_digest() -> str:
    return hashlib.sha256(LOCK.read_bytes()).hexdigest()


def upload(server: Server, content: Optional[bytes] = None, ctype: str = "image/png",
           name: str = "page.png", upload_id: Optional[str] = None) -> httpx.Response:
    return httpx.post(
        f"{server.base}/api/resource/v1/artifacts",
        headers=DEMO,
        data={"upload_id": upload_id or str(uuid.uuid4())},
        files={"file": (name, content if content is not None else make_png(), ctype)},
        timeout=30,
    )


def prepare(server: Server, node: dict, digest: str) -> httpx.Response:
    return httpx.post(
        f"{server.base}/api/resource/v1/preparations",
        headers=TASK,
        json={
            "preparation_id": str(uuid.uuid4()),
            "worker_id": node["worker_id"],
            "worker_session_id": node["session_id"],
            "task_type": "ocr.image.v1",
            "profile_id": "ocr.default.v1",
            "profile_digest": digest,
        },
        timeout=30,
    )


def wait_prep(server: Server, prep_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{server.base}/api/resource/v1/preparations/{prep_id}", headers=TASK, timeout=10)
        if r.status_code == 200 and r.json()["status"] in ("READY", "FAILED", "REJECTED"):
            return r.json()
        time.sleep(0.5)
    raise RuntimeError("准备超时")


def exec_body(node: dict, artifact_id: str, digest: str, **kw) -> dict:
    body = {
        "execution_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "attempt_no": 1,
        "worker_id": node["worker_id"],
        "worker_session_id": node["session_id"],
        "task_type": "ocr.image.v1",
        "profile_id": "ocr.default.v1",
        "profile_digest": digest,
        "input_artifact_id": artifact_id,
        "parameters": {"language": "auto"},
        "timeout_seconds": 120,
    }
    body.update(kw)
    return body


def post_exec(server: Server, body: dict) -> httpx.Response:
    return httpx.post(f"{server.base}/api/resource/v1/executions", headers=TASK, json=body, timeout=30)


def wait_exec(server: Server, exec_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{server.base}/api/resource/v1/executions/{exec_id}", headers=TASK, timeout=10)
        if r.status_code == 200 and r.json()["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"):
            return r.json()
        time.sleep(0.3)
    raise RuntimeError("执行超时")


# --------------------------------------------------------------------------
# 各项验收
# --------------------------------------------------------------------------

def case_r02(server: Server, node: dict) -> None:
    hw = node["hardware"]
    caps = node["capabilities"]
    ok = (
        bool(hw["os"]) and bool(hw["arch"]) and hw["cpu_name"] is not None
        and any(c["task_type"] == "ocr.image.v1" for c in caps)
    )
    record("R02 硬件/能力/采样", ok,
           f"os={hw['os']!r} arch={hw['arch']!r} cpu={hw['cpu_name']!r} "
           f"caps={[c['state'] for c in caps]} telemetry={'有' if node['telemetry'] else '暂无(首心跳前)'}")


def case_r03(server: Server) -> None:
    base = f"{server.base}/api/resource/v1"
    no_token = httpx.get(f"{base}/resources", timeout=10).status_code
    bad_token = httpx.get(f"{base}/resources", headers={"Authorization": "Bearer nope"}, timeout=10).status_code
    demo_exec = httpx.post(f"{base}/executions", headers=DEMO, json={}, timeout=10).status_code
    demo_prep = httpx.post(f"{base}/preparations", headers=DEMO, json={}, timeout=10).status_code
    ok = no_token == 401 and bad_token == 401 and demo_exec == 403 and demo_prep == 403
    record("R03 令牌与越权", ok,
           f"无令牌={no_token} 错令牌={bad_token} DEMO下发执行={demo_exec} DEMO准备={demo_prep}")


def case_r04(server: Server) -> Optional[dict]:
    content = make_png("UPLOAD TEST")
    uid = str(uuid.uuid4())
    first = upload(server, content, upload_id=uid)
    replay = upload(server, content, upload_id=uid)
    conflict = upload(server, make_png("DIFFERENT"), upload_id=uid)
    unsupported = upload(server, b"hello", ctype="text/plain", name="a.txt")
    corrupt = upload(server, b"not-a-png", name="bad.png")
    too_large = upload(server, b"x" * (11 * 1024 * 1024), name="big.png")
    ok = (
        first.status_code == 201 and replay.status_code == 200
        and first.json()["artifact_id"] == replay.json()["artifact_id"]
        and conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        and unsupported.status_code == 415
        and corrupt.status_code == 422
        and too_large.status_code == 413
    )
    record("R04 上传校验与幂等", ok,
           f"首次={first.status_code} 重放={replay.status_code} 改内容={conflict.status_code} "
           f"415={unsupported.status_code} 422={corrupt.status_code} 413={too_large.status_code}")
    # SHA 一致性（R01 的本机部分）
    if first.status_code == 201:
        art = first.json()
        got = httpx.get(f"{server.base}{art['download_path']}", headers=DEMO, timeout=30).content
        sha_ok = hashlib.sha256(got).hexdigest() == art["sha256"] == hashlib.sha256(content).hexdigest()
        record("R01a 上传下载 SHA 一致（本机）", sha_ok, f"sha256={art['sha256'][:16]}…")
        return art
    return None


def case_r05(server: Server, node: dict) -> Optional[dict]:
    digest = profile_digest()
    # 未配置的 digest
    bad = httpx.post(
        f"{server.base}/api/resource/v1/preparations",
        headers=TASK,
        json={
            "preparation_id": str(uuid.uuid4()),
            "worker_id": node["worker_id"],
            "worker_session_id": node["session_id"],
            "task_type": "ocr.image.v1",
            "profile_id": "ocr.default.v1",
            "profile_digest": "0" * 64,
        },
        timeout=20,
    )
    unconfigured_ok = bad.status_code == 503 and bad.json()["code"] == "PROFILE_UNCONFIGURED"

    prep = prepare(server, node, digest)
    if prep.status_code != 202:
        record("R05 模型准备", False, f"准备受理失败 {prep.status_code} {prep.text[:120]}")
        return None
    preparation_id = prep.json()["preparation_id"]
    SNAPSHOTS["accepted_preparation"] = prep.json()
    resources = api_nodes(server)
    for resource in resources:
        if resource["active_preparation_id"] == preparation_id:
            SNAPSHOTS["preparing_resource"] = resource
            break
    final = wait_prep(server, preparation_id)
    ready_ok = final["status"] == "READY"
    SNAPSHOTS["ready_preparation"] = final
    nodes = api_nodes(server)
    cap = next(
        (c for c in nodes[0]["capabilities"] if c["task_type"] == "ocr.image.v1"), {}
    )
    detail_ok = bool(cap.get("model_sha256")) and cap.get("verified") is True and bool(cap.get("calibration"))
    ok = unconfigured_ok and ready_ok and detail_ok
    record("R05 模型准备", ok,
           f"未配置={bad.status_code}/{bad.json().get('code')} 准备={final['status']} "
           f"provider={cap.get('runtime_provider')} sha={(cap.get('model_sha256') or '')[:12]}… "
           f"校准中位={cap.get('calibration', {}).get('median_compute_ms') if cap.get('calibration') else None}ms")
    return {"digest": digest, "ready": ready_ok}


def case_full_execution(server: Server, node: dict, digest: str, text: str = "VERIFY P1 2026") -> Optional[dict]:
    art = upload(server, make_png(text)).json()
    body = exec_body(node, art["artifact_id"], digest)
    r = post_exec(server, body)
    if r.status_code != 201:
        record("E2E 单图执行", False, f"受理失败 {r.status_code} {r.text[:120]}")
        return None
    final = wait_exec(server, body["execution_id"])
    SNAPSHOTS["executed"] = final
    if final["status"] != "SUCCEEDED":
        record("E2E 单图执行", False, f"终态 {final['status']} {final.get('error')}")
        return None
    download = httpx.get(f"{server.base}{final['result_artifact']['download_path']}", headers=TASK, timeout=30)
    download.raise_for_status()
    result = download.json()
    SNAPSHOTS["input_artifact"] = art
    SNAPSHOTS["ocr_result"] = result
    assert hashlib.sha256(download.content).hexdigest() == final["result_artifact"]["sha256"]
    text_ok = text.lower() in result["text"].lower().replace("\n", " ")
    media_ok = isinstance(final["result_artifact"]["media_info"], dict)
    timings = final.get("timings") or {}
    record("E2E 单图执行", text_ok and media_ok,
           f"识别={result['text']!r} blocks={len(result['blocks'])} "
           f"worker={timings.get('total_worker_ms', 0):.0f}ms media_info={'对象' if media_ok else '非对象'}")
    return {"execution": final, "body": body, "result": result}


def case_r06(server: Server, node: dict, digest: str) -> None:
    art = upload(server).json()
    b1 = exec_body(node, art["artifact_id"], digest)
    b2 = exec_body(node, art["artifact_id"], digest)
    out: list[httpx.Response] = []

    def fire(b: dict) -> None:
        out.append(post_exec(server, b))

    t1 = threading.Thread(target=fire, args=(b1,))
    t2 = threading.Thread(target=fire, args=(b2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    codes = sorted(r.status_code for r in out)
    accepted = [r for r in out if r.status_code == 201]
    rejected = [r for r in out if r.status_code == 409]
    busy = rejected and rejected[0].json().get("code") == "RESOURCE_BUSY"
    ok = len(accepted) == 1 and len(rejected) == 1 and bool(busy)
    record("R06 并发抢槽", ok, f"状态码={codes} 拒绝码={rejected[0].json().get('code') if rejected else '无'}")
    # 清理：取消占用中的执行
    if accepted:
        eid = accepted[0].json()["execution_id"]
        httpx.post(f"{server.base}/api/resource/v1/executions/{eid}/cancel", headers=TASK, timeout=10)
        wait_exec(server, eid, timeout=60)


def case_r07(server: Server, node: dict, digest: str) -> None:
    art = upload(server).json()
    body = exec_body(node, art["artifact_id"], digest)
    first = post_exec(server, body)
    replay = post_exec(server, body)
    changed = post_exec(server, dict(body, timeout_seconds=60))
    # 被拒 ID 不会变接受
    rejected_body = exec_body(node, "nonexistent0", digest)
    rej1 = post_exec(server, rejected_body)
    rej2 = post_exec(server, rejected_body)
    rejected_id = rejected_body["execution_id"]
    SNAPSHOTS["rejected_execution"] = httpx.get(
        f"{server.base}/api/resource/v1/executions/{rejected_id}", headers=TASK, timeout=10
    ).raise_for_status().json()
    ok = (
        first.status_code == 201 and replay.status_code == 200
        and changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
        and rej1.status_code == 409 and rej2.status_code == 409
        and rej1.json()["code"] == rej2.json()["code"]
    )
    record("R07 重放与冲突", ok,
           f"首次={first.status_code} 重放={replay.status_code} 改载荷={changed.json().get('code')} "
           f"被拒重放={rej1.json().get('code')}/{rej2.json().get('code')}")
    if first.status_code == 201:
        eid = body["execution_id"]
        httpx.post(f"{server.base}/api/resource/v1/executions/{eid}/cancel", headers=TASK, timeout=10)
        wait_exec(server, eid, timeout=60)


def case_r08(server: Server, node: dict, digest: str) -> None:
    art = upload(server).json()
    body = exec_body(node, art["artifact_id"], digest)
    assert post_exec(server, body).status_code == 201
    eid = body["execution_id"]
    cancel = httpx.post(f"{server.base}/api/resource/v1/executions/{eid}/cancel", headers=TASK, timeout=10)
    # 等节点主线程确认停止
    deadline = time.time() + 40
    status = None
    while time.time() < deadline:
        cur = httpx.get(f"{server.base}/api/resource/v1/executions/{eid}", headers=TASK, timeout=10).json()
        status = cur["status"]
        if status in ("CANCELLED", "SUCCEEDED", "FAILED"):
            break
        time.sleep(0.5)
    # 晚到提交必须被拒
    late = httpx.post(
        f"{server.base}/api/resource/internal/v1/nodes/{node['worker_id']}/executions/{eid}/complete",
        json={
            "session_id": node["session_id"],
            "node_secret": node.get("_secret", ""),
            "outcome": "SUCCEEDED",
            "result": {
                "schema_version": "1.0", "task_type": "ocr.image.v1",
                "input_artifact_id": art["artifact_id"],
                "image_width": 640, "image_height": 220, "text": "late", "blocks": [],
            },
        },
        timeout=10,
    )
    after = httpx.get(f"{server.base}/api/resource/v1/executions/{eid}", headers=TASK, timeout=10).json()
    SNAPSHOTS["cancelled_execution"] = after
    ok = (
        cancel.status_code == 202 and status == "CANCELLED"
        and late.status_code == 409 and late.json()["code"] == "STALE_EXECUTION"
        and after["status"] == status and after["result_artifact"] is None
    )
    record("R08 取消与晚到提交", ok,
           f"cancel={cancel.status_code} 终态={status} 晚到提交={late.status_code}/"
           f"{late.json().get('code') if late.status_code == 409 else ''} 终态未被改写={after['status'] == status}")


def case_r09(server: Server, node: dict, digest: str) -> None:
    art = upload(server).json()
    body = exec_body(node, art["artifact_id"], digest)
    assert post_exec(server, body).status_code == 201
    busy_prep = prepare(server, node, digest)
    ok = busy_prep.status_code == 409 and busy_prep.json()["code"] == "RESOURCE_BUSY"
    record("R09 忙时不换模型", ok, f"占用中再准备={busy_prep.status_code}/{busy_prep.json().get('code')}")
    httpx.post(f"{server.base}/api/resource/v1/executions/{body['execution_id']}/cancel", headers=TASK, timeout=10)
    wait_exec(server, body["execution_id"], timeout=60)


def case_r10(server: Server, node: dict, digest: str) -> None:
    wid = node["worker_id"]
    art = upload(server).json()
    before = post_exec(server, exec_body(node, art["artifact_id"], digest))
    assert before.status_code == 201, before.text
    mode = httpx.post(
        f"{server.base}/api/resource/v1/resources/{wid}/mode",
        headers=DEMO, json={"mode": "DRAINING"}, timeout=10,
    )
    blocked = post_exec(server, exec_body(node, art["artifact_id"], digest))
    drained = wait_exec(server, before.json()["execution_id"], timeout=20)
    # 空闲节点应在扫描后进入 PAUSED
    deadline = time.time() + 15
    state = None
    while time.time() < deadline:
        n = next(x for x in api_nodes(server) if x["worker_id"] == wid)
        state = n["mode"]
        if state == "PAUSED":
            break
        time.sleep(0.5)
    resumed = httpx.post(
        f"{server.base}/api/resource/v1/resources/{wid}/mode",
        headers=DEMO, json={"mode": "ACTIVE"}, timeout=10,
    )
    after = post_exec(server, exec_body(node, art["artifact_id"], digest))
    final = wait_exec(server, after.json()["execution_id"], timeout=20) if after.status_code == 201 else {}
    ok = (
        mode.status_code == 200 and blocked.status_code == 409
        and blocked.json()["code"] in ("NODE_UNAVAILABLE", "RESOURCE_BUSY")
        and drained["status"] == "SUCCEEDED"
        and state == "PAUSED" and resumed.status_code == 200 and final.get("status") == "SUCCEEDED"
    )
    record("R10 DRAINING/ACTIVE", ok,
           f"DRAINING={mode.json().get('mode')} 新执行被拒={blocked.json().get('code')} "
           f"原执行={drained['status']} 空闲后={state} 恢复后执行={final.get('status')}")


def case_r11(server: Server, digest: str) -> None:
    """模拟节点：注册→领取→停止心跳，验证 18 秒内离线与执行终止。"""
    reg = httpx.post(
        f"{server.base}/api/resource/internal/v1/nodes/register",
        headers=JOIN,
        json={
            "worker_id": "verify-mock-node",
            "name": "verify-mock-node",
            "hardware": {
                "os": "protocol-check", "arch": "x86_64", "cpu_name": "mock",
                "logical_cores": 1, "ram_total_bytes": 1, "ram_available_bytes": None,
                "gpu_name": None, "gpu_vram_bytes": None, "gpu_info_source": "unavailable",
                "manual_labels": {}, "detection_errors": [],
            },
        },
        timeout=10,
    )
    if reg.status_code != 200:
        record("R11 失联检测", False, f"模拟节点注册失败 {reg.status_code}")
        return
    info = reg.json()
    node = {"worker_id": info["worker_id"], "session_id": info["session_id"],
            "node_secret": info["node_secret"]}
    prefix = f"{server.base}/api/resource/internal/v1/nodes/{node['worker_id']}"
    auth = {k: node[k] for k in ("session_id", "node_secret")}
    prep = prepare(server, node, digest).raise_for_status().json()
    httpx.post(prefix + "/claim", json=auth, timeout=10).raise_for_status()
    ready = httpx.post(f"{prefix}/preparations/{prep['preparation_id']}/status", json={
        **auth,
        "status": "READY", "backend": "cpu", "runtime_provider": "CPUExecutionProvider",
        "model_sha256": json.loads(LOCK.read_bytes())["model_sha256"],
        "calibration": {"sample_count": 3, "median_compute_ms": 100, "passed": True,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }, timeout=10).raise_for_status().json()
    assert ready["status"] == "READY"
    art = upload(server).json()
    body = exec_body(node, art["artifact_id"], digest)
    accepted = post_exec(server, body)
    if accepted.status_code != 201:
        record("R11 失联检测", False, f"受理失败 {accepted.status_code} {accepted.text[:100]}")
        return
    claim = httpx.post(
        f"{server.base}/api/resource/internal/v1/nodes/{info['worker_id']}/claim",
        json={"session_id": info["session_id"], "node_secret": info["node_secret"]}, timeout=10,
    )
    claimed = claim.status_code == 200 and claim.json().get("execution") is not None
    httpx.post(prefix + "/heartbeat", json=auth, timeout=10).raise_for_status()
    last_beat = time.monotonic()
    deadline = time.time() + 25
    offline_after = None
    exec_status = None
    while time.time() < deadline:
        n = next((x for x in api_nodes(server) if x["worker_id"] == info["worker_id"]), None)
        e = httpx.get(f"{server.base}/api/resource/v1/executions/{body['execution_id']}",
                      headers=TASK, timeout=10).json()
        exec_status = e["status"]
        if n and n["connection"] == "OFFLINE" and offline_after is None:
            offline_after = time.monotonic() - last_beat
        if offline_after is not None and exec_status == "FAILED":
            break
        time.sleep(0.5)
    if offline_after is not None:
        SNAPSHOTS["mock_offline_resource"] = n
        SNAPSHOTS["mock_failed_execution"] = e
        replaced = httpx.post(
            f"{server.base}/api/resource/internal/v1/nodes/register", headers=JOIN,
            json={"worker_id": node["worker_id"], "name": n["name"], "hardware": n["hardware"]},
            timeout=10,
        ).raise_for_status().json()
        fresh = next(x for x in api_nodes(server) if x["worker_id"] == node["worker_id"])
        blocked = fresh["cleanup_pending"] and fresh["available_slots"] == 0
        httpx.post(prefix + "/confirm_idle", json={
            k: replaced[k] for k in ("session_id", "node_secret")
        }, timeout=10).raise_for_status()
        released = next(x for x in api_nodes(server) if x["worker_id"] == node["worker_id"])
        record("L13 清理屏障（协议模拟）", blocked and released["available_slots"] == 1,
               f"重新注册后仍阻塞={blocked} 确认静止后槽位={released['available_slots']}")
    ok = claimed and offline_after is not None and offline_after <= 18 and exec_status == "FAILED"
    record("R11 失联检测", ok,
           f"模拟节点领取={claimed} 离线耗时={offline_after!r}s(阈值18) 执行终态={exec_status}")


def case_x01(data_dir: Path, digest: str) -> None:
    """重启恢复：成功结果保留、活动执行确定失败、旧会话失效。"""
    server = Server(data_dir)
    agent = Agent(server, data_dir.parent / "agent-x01", "x01-node")
    try:
        server.start()
        agent.start()
        node = agent.wait_online()
        prep = prepare(server, node, digest)
        if prep.status_code != 202:
            record("X01 重启恢复", False, f"准备失败 {prep.status_code}")
            return
        wait_prep(server, prep.json()["preparation_id"])

        # 1) 一个成功执行
        art = upload(server).json()
        body = exec_body(node, art["artifact_id"], digest)
        post_exec(server, body)
        done = wait_exec(server, body["execution_id"])
        success_artifact = done["result_artifact"]["artifact_id"]

        # 停止轮询后，创建可确定留在 ACCEPTED 的记录。
        agent.stop()
        art2 = upload(server).json()
        body2 = exec_body(node, art2["artifact_id"], digest)
        active = post_exec(server, body2)
        if active.status_code != 201:
            record("X01 重启恢复", False, f"活动执行受理失败 {active.status_code}")
            return
        active_id = body2["execution_id"]
        server.stop(hard=True)  # 模拟崩溃

        # 3) 同数据目录重启
        server2 = Server(data_dir)
        server2.start()
        try:
            kept = httpx.get(f"{server2.base}/api/resource/v1/executions/{body['execution_id']}",
                             headers=TASK, timeout=10).json()
            kept_ok = kept["status"] == "SUCCEEDED"
            content = httpx.get(
                f"{server2.base}/api/resource/v1/artifacts/{success_artifact}/content",
                headers=TASK, timeout=30,
            )
            file_ok = content.status_code == 200 and content.content
            dead = httpx.get(f"{server2.base}/api/resource/v1/executions/{active_id}",
                             headers=TASK, timeout=10).json()
            dead_ok = dead["status"] == "FAILED" and (dead.get("error") or {}).get("code") == "RESOURCE_SERVICE_RESTARTED"
            old_session = httpx.post(
                f"{server2.base}/api/resource/internal/v1/nodes/{node['worker_id']}/heartbeat",
                json={"session_id": node["session_id"], "node_secret": "stale-secret-value"},
                timeout=10,
            )
            session_ok = old_session.status_code == 409
            events = httpx.get(f"{server2.base}/api/resource/v1/events", headers=DEMO, timeout=10).json()
            restarted_evt = any(e["type"] == "service.restarted" for e in events["events"])
            ok = kept_ok and bool(file_ok) and dead_ok and session_ok and restarted_evt
            record("X01 重启恢复", ok,
                   f"成功结果保留={kept_ok} 结果文件可取={bool(file_ok)} "
                   f"活动执行={dead['status']}/{(dead.get('error') or {}).get('code')} "
                   f"旧会话被拒={old_session.status_code} restarted事件={restarted_evt}")
            agent.server = server2
            agent.start()
            rejoined = agent.wait_online()
            reprepare = prepare(server2, rejoined, digest).raise_for_status().json()
            ready = wait_prep(server2, reprepare["preparation_id"])
            record("L14 新会话重准备（P1）",
                   rejoined["session_id"] != node["session_id"] and ready["status"] == "READY",
                   f"同一节点={rejoined['worker_id'] == node['worker_id']} 准备={ready['status']}")
        finally:
            agent.stop()
            server2.stop()
    finally:
        agent.stop()
        server.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--evidence", type=Path, help="写入去凭证的 JSON 证据")
    args = parser.parse_args()

    if not LOCK.exists():
        print(f"缺少 profile 锁文件：{LOCK}", file=sys.stderr)
        return 2

    RESULTS.clear()
    SNAPSHOTS.clear()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = Path(tempfile.mkdtemp(prefix="p1-verify-"))
    print(f"临时数据目录：{tmp}\n")
    digest = profile_digest()
    print(f"profile_digest = {digest}\n")

    server = Server(tmp / "data")
    agent = Agent(server, tmp / "agent", "verify-node")
    rc = 0
    try:
        print("启动 resource_service …")
        server.start()
        print("启动 node_agent …")
        agent.start()
        node = agent.wait_online()
        SNAPSHOTS["not_prepared_resource"] = dict(node)
        # 补上 node_secret（公开快照不含，测试 R08 晚到提交用）
        node["_secret"] = _read_agent_secret(tmp / "agent")
        print(f"节点上线：{node['worker_id']} ({node['hardware']['cpu_name']})\n")

        print("== 契约与鉴权 ==")
        case_r02(server, node)
        case_r03(server)
        case_r04(server)

        print("\n== 模型准备与真实执行 ==")
        prep = case_r05(server, node)
        if prep and prep["ready"]:
            node = next(n for n in api_nodes(server) if n["worker_id"] == node["worker_id"])
            SNAPSHOTS["ready_resource"] = dict(node)
            node["_secret"] = _read_agent_secret(tmp / "agent")
            case_full_execution(server, node, digest)

        print("\n== 并发与幂等 ==")
        case_r06(server, node, digest)
        case_r07(server, node, digest)
        case_r08(server, node, digest)
        case_r09(server, node, digest)

        print("\n== 节点模式与失联 ==")
        case_r10(server, node, digest)
        case_r11(server, digest)

        print("\n== R01 跨机互通（需第二台物理机）==")
        record("R01 第二台电脑互通", None, "按 docs/P1-启动与验收.md 在两台物理机复验。")

        print("\n== 重启恢复 ==")
        agent.stop()
        server.stop()
        case_x01(tmp / "data-x01", digest)
    except Exception as e:
        print(f"\n自检中断：{e}", file=sys.stderr)
        rc = 2
    finally:
        agent.stop()
        server.stop()

    completed_cases = {case.split()[0] for case, _, _ in RESULTS}
    for skipped in sorted(REQUIRED_CASES - completed_cases):
        record(skipped, None, "本机必测用例未执行")
        rc = 2

    print("\n" + "=" * 78)
    print(f"{'用例':<26} {'结果':<8} 证据")
    print("-" * 78)
    for case, status, ev in RESULTS:
        print(f"{case:<26} {status:<8} {ev[:120]}")
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    not_run = sum(1 for _, s, _ in RESULTS if s == "NOT_RUN")
    print("-" * 78)
    print(f"PASS {passed} / FAIL {failed} / NOT_RUN {not_run}")
    if args.keep_data:
        print(f"数据保留在 {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence = {
            "plan_version": "1.0", "contract_version": "0.3.0",
            "p1_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
            "p2_commit": None, "mode": "P1_LOCAL_ONLY", "profile_digest": digest,
            "started_at": started_at, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": [sys.executable, *sys.argv],
            "cases": [{"case": case, "status": status, "observed": observed}
                      for case, status, observed in RESULTS],
            "public_snapshots": SNAPSHOTS,
        }
        args.evidence.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return rc or int(failed > 0)


def _read_agent_secret(agent_dir: Path) -> str:
    """从 agent 日志中取不到 secret（不落盘），改用重注册路径不可行；
    这里通过服务端数据库读取（仅本机自检使用）。"""
    db = agent_dir.parent / "data" / "resource.sqlite3"
    import sqlite3

    try:
        conn = sqlite3.connect(str(db))
        row = conn.execute("SELECT node_secret FROM nodes ORDER BY registered_at LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
