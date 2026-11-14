"""Work-conserving CPU/CUDA ranking from validated, node-specific observations.

No busy-node queue, GPU speed multiplier or CPU-utilization threshold is used.
The estimates are heuristics; admission and cancellation still belong to P1.
"""

import hashlib
import math

from task_service.contract import canonical

PROVIDERS = {"cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider"}


def capability(node, job):
    return next(
        (
            c
            for c in node["capabilities"]
            if all(c[k] == job[k] for k in ("task_type", "profile_id"))
        ),
        None,
    )


def features(task):
    info = task["input"]["media_info"]
    pixels = info["width"] * info["height"]
    bucket = next(
        (i for i, limit in enumerate((250_000, 1_000_000, 4_000_000)) if pixels <= limit), 3
    )
    return {"pixels": pixels, "input_bytes": task["input"]["size_bytes"], "bucket": bucket}


def resource_key(node, job):
    cap = capability(node, job)
    identity = {
        "worker_id": node["worker_id"],
        "profile_digest": job["profile_digest"],
        "task_type": job["task_type"],
        "profile_id": job["profile_id"],
        "parameters": job["parameters"],
        **{k: cap[k] for k in ("backend", "runtime_provider", "model_sha256")},
        **{
            k: node.get(k, 0)
            for k in ("max_slots", "threads_per_slot", "cpu_thread_budget", "prefetch_capacity")
        },
        "hardware": {
            k: node["hardware"].get(k)
            for k in ("os", "arch", "cpu_name", "logical_cores", "gpu_name", "gpu_vram_bytes")
        },
    }
    return hashlib.sha256(canonical(identity).encode()).hexdigest()


def measurement(execution, decision):
    """Reject missing, nonfinite, inconsistent or impossible timing observations."""
    timings = execution.get("timings")
    if execution["status"] != "SUCCEEDED" or not decision or not timings:
        return None
    fields = ("download_ms", "inference_ms", "postprocess_ms", "write_output_ms", "total_worker_ms")
    values = [timings.get(k) for k in fields]
    if any(
        isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0
        for v in values
    ):
        return None
    download, compute, post, write, total = values
    if compute <= 0 or total <= 0 or total > execution["timeout_seconds"] * 1000:
        return None
    if download + compute + post + write > total + 1:
        return None
    return {k: decision[k] for k in ("resource_key", "bucket", "pixels", "input_bytes")} | {
        "compute_ms": compute,
        "download_ms": download,
        "overhead_ms": max(0, total - compute - download),
        "total_ms": total,
    }


class Scheduler:
    def __init__(self, store, settings, clock):
        self.store, self.settings, self.clock = store, settings, clock

    def estimate(self, node, job, task, remaining):
        cap = capability(node, job)
        target = features(task)
        key = resource_key(node, job)
        observed = self.store.scheduling_samples(
            key,
            target["bucket"],
            self.clock() - self.settings.history_ttl_seconds,
            self.clock(),
        )
        if observed:
            predicted = None
            # Oldest to newest, so the last executions have the greatest weight.
            for sample in reversed(observed):
                compute_scale = min(2, max(0.5, math.sqrt(target["pixels"] / sample["pixels"])))
                byte_scale = min(
                    4, max(0.25, target["input_bytes"] / max(1, sample["input_bytes"]))
                )
                value = (
                    sample["compute_ms"] * compute_scale
                    + sample["download_ms"] * byte_scale
                    + sample["overhead_ms"]
                )
                predicted = value if predicted is None else 0.3 * value + 0.7 * predicted
            source = "execution_history"
        else:
            # The fixed calibration uses ~0.15 MP images. Transfer prior: 10 MiB/s + 5 ms.
            # Both are explicit fallback heuristics, not benchmark claims.
            predicted = (
                cap["calibration"]["median_compute_ms"]
                * max(1, math.sqrt(target["pixels"] / 150_000))
                + target["input_bytes"] / (10 * 1024**2) * 1000
                + 5
            )
            source = "calibration"
        occupancy = (node["max_slots"] - remaining) / node["max_slots"]
        return {
            "policy": "resource-aware-v1",
            "worker_id": node["worker_id"],
            "backend": cap["backend"],
            "resource_key": key,
            **target,
            "source": source,
            "samples": len(observed),
            "last_sample_at": observed[0]["recorded_at"] if observed else None,
            "predicted_ms": round(max(1, predicted), 3),
            "score_ms": round(max(1, predicted) * (1 + 0.25 * occupancy), 3),
            "remaining_slots": remaining,
            "exploration": False,
        }

    def choose(self, candidates, last_worker):
        """Candidates already passed safety and remaining-capacity checks."""
        sequence = self.store.scheduling_sequence() + 1

        def rotation(item):
            return (item[0]["worker_id"] <= last_worker, item[0]["worker_id"])

        explore = len(candidates) > 1 and sequence % self.settings.exploration_interval == 0
        if explore:
            selected = min(
                candidates,
                key=lambda c: (c[1]["samples"], c[1]["last_sample_at"] or 0, *rotation(c)),
            )
        else:
            selected = min(candidates, key=lambda c: (c[1]["score_ms"], *rotation(c)))
        node, estimate = selected
        fields = (
            "worker_id",
            "backend",
            "source",
            "samples",
            "score_ms",
            "predicted_ms",
            "remaining_slots",
        )
        return node, {
            **estimate,
            "exploration": explore,
            "decided_at": self.clock(),
            "candidates": [{k: e[k] for k in fields} for _, e in candidates],
        }
