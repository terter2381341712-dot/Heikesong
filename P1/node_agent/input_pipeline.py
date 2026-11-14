"""Bounded, disposable CPU input worker. No execution lease or model lives here."""

import hashlib
import multiprocessing
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 10 * 1024**2
MAX_PIXELS = 20_000_000
KEYS = ("job_id", "task_id", "input_artifact_id", "input_sha256", "profile_digest")


def decode_image(path, media):
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("输入超过像素限制")
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        width, height = rgb.size
        if any(
            media.get(k) not in (None, v)
            for k, v in (("width", width), ("height", height))
        ):
            raise ValueError("图片纠向尺寸与元信息不一致")
        return np.asarray(rgb, dtype=np.uint8)


def prepare_input(client, server, worker_id, secret, hint, directory):
    """Network/decode work is killable by the parent, including a stalled response."""
    import numpy as np
    from resource_service.media import probe_image

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    begun = time.time()
    start = time.monotonic()
    content = bytearray()
    with client.stream(
        "GET",
        f"{server}/api/resource/internal/v1/nodes/{worker_id}/artifacts/{hint['input_artifact_id']}/content",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=5,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(64 * 1024):
            if len(content) + len(chunk) > MAX_BYTES or time.monotonic() - start > 15:
                raise ValueError("预取下载超过大小或时间限制")
            content.extend(chunk)
    if (
        len(content) != hint["input_size_bytes"]
        or hashlib.sha256(content).hexdigest() != hint["input_sha256"]
    ):
        raise ValueError("预取内容与输入摘要不一致")
    probe_image(content, hint["input_content_type"])
    source = directory / "input.bin"
    source.write_bytes(content)
    del content
    download_ms = (time.monotonic() - start) * 1000
    start = time.monotonic()
    array = decode_image(source, hint["input_media_info"])
    target = directory / "decoded.npy"
    with target.open("wb") as output:
        np.save(output, array, allow_pickle=False)
    source.unlink()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "path": str(target),
        "sha256": digest,
        "download_ms": download_ms,
        "decode_ms": (time.monotonic() - start) * 1000,
        "started_wall": begun,
        "finished_wall": time.time(),
    }


def input_worker(pipe, server, worker_id, secret):
    from .compute import configure_thread_environment

    configure_thread_environment(1)
    import httpx

    with httpx.Client() as client:
        try:
            while True:
                hint, directory = pipe.recv()
                try:
                    result = prepare_input(
                        client, server, worker_id, secret, hint, directory
                    )
                except Exception:
                    result = None  # Speculation failure cannot fail an execution.
                pipe.send(result)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            pipe.close()


def load_decoded(cached, media):
    import numpy as np

    path = Path(cached["path"])
    if path.stat().st_size > MAX_PIXELS * 3 + 1024:
        raise ValueError("解码缓存过大")
    if hashlib.sha256(path.read_bytes()).hexdigest() != cached["sha256"]:
        raise ValueError("解码缓存摘要错误")
    array = np.load(path, allow_pickle=False)
    if (
        array.dtype != np.uint8
        or array.ndim != 3
        or array.shape[2] != 3
        or array.shape[0] * array.shape[1] > MAX_PIXELS
        or array.shape[:2] != (media["height"], media["width"])
    ):
        raise ValueError("解码缓存格式错误")
    return array


def same_input(a, b):
    return bool(a and b and all(a.get(k) == b.get(k) for k in KEYS))


def expired(hint):
    return datetime.now(timezone.utc) >= datetime.fromisoformat(
        hint["expires_at"].replace("Z", "+00:00")
    )


class InputPipeline:
    def __init__(self, agent):
        self.agent = agent
        self.directory = agent.data_dir / "prefetch"
        shutil.rmtree(
            self.directory, ignore_errors=True
        )  # Owned by the agent-directory lock.
        self.process = self.pipe = self.hint = self.result = None
        self.busy = False
        self.started = self.polled = 0.0
        self.consumed = None

    def close(self):
        from .compute import stop_process

        if self.process is not None:
            stop_process(self.process)
            self.pipe.close()
            self.process = self.pipe = None
        self.hint = self.result = None
        self.busy = False
        shutil.rmtree(self.directory, ignore_errors=True)

    def tick(self, force=False):
        agent = self.agent
        if agent._sharing_disabled or agent._session_stale or agent._stop:
            self.close()
            return
        if self.busy:
            if self.pipe.poll():
                try:
                    self.result = self.pipe.recv()
                except (EOFError, OSError):
                    self.result = None
                self.busy = False
                if self.result:
                    print(
                        f"[agent] prefetch_ready task_id={self.hint['task_id']} "
                        f"started={self.result['started_wall']:.6f} "
                        f"finished={self.result['finished_wall']:.6f}",
                        flush=True,
                    )
            elif not self.process.is_alive() or time.monotonic() - self.started > 20:
                self.close()
        if self.hint and expired(self.hint):
            self.close()
        if not force and time.monotonic() - self.polled < 0.25:
            return
        self.polled = time.monotonic()
        snapshot = agent._internal(
            f"/api/resource/internal/v1/nodes/{agent.worker_id}/prefetch",
            agent._auth_payload(),
            timeout=0.5,
        )
        if snapshot is None:
            return  # Locally enforced TTL still bounds an unreachable control plane.
        hint = snapshot["hint"]
        if not snapshot["enabled"]:
            self.close()
            return
        # A newer hint can arrive between acceptance and claim. Preserve owned input.
        owned = (
            self.hint
            and [self.hint["job_id"], self.hint["task_id"]] in snapshot["owned_tasks"]
        )
        if self.hint and (same_input(self.hint, hint) or owned):
            if same_input(self.hint, hint):
                self.hint = hint
            return
        if self.hint:
            self.close()  # Future-task cancellation kills only this CPU worker.
        if not hint or same_input(hint, self.consumed):
            return
        if self.process is None:
            from .compute import configure_thread_environment

            configure_thread_environment(1)
            ctx = multiprocessing.get_context("spawn")
            self.pipe, child = ctx.Pipe()
            self.process = ctx.Process(
                target=input_worker,
                args=(child, agent.server, agent.worker_id, agent.node_secret),
            )
            self.process.start()
            child.close()
        self.hint, self.result = hint, None
        self.started, self.busy = time.monotonic(), True
        # A killed parent can leave a worker finishing its current download. Unique
        # request directories prevent it from overwriting a restarted agent's input.
        self.pipe.send((hint, str(self.directory / uuid.uuid4().hex)))
        print(
            f"[agent] prefetch_started task_id={hint['task_id']} pid={self.process.pid}",
            flush=True,
        )

    def take(self, execution):
        self.tick(force=True)
        if not same_input(self.hint, execution):
            return None
        self.consumed = self.hint
        if self.busy or not self.result:
            self.close()  # No second download competing with the execution fallback.
            return None
        result = self.result
        destination = (
            self.agent.data_dir
            / "work"
            / execution["execution_id"]
            / (uuid.uuid4().hex + ".npy")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = Path(result["path"])
        source.replace(destination)
        try:
            source.parent.rmdir()
        except OSError:
            pass
        result["path"] = str(destination)
        self.hint = self.result = None
        print(
            f"[agent] prefetch_hit execution_id={execution['execution_id']}", flush=True
        )
        return result
