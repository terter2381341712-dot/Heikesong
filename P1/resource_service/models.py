"""请求模型：全部 extra=forbid（请求正文不允许未声明字段）。"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

UUID_PAT = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
WORKER_PAT = r"^[A-Za-z0-9_-]{1,80}$"
ARTIFACT_PAT = r"^[A-Za-z0-9_-]{1,80}$"
PROFILE_PAT = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
HEX64_PAT = r"^[a-f0-9]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 节点内部凭证：与公开 session_id 分离的秘密（F07）
NodeSecret = str  # 校验在 Field 中完成


def _secret_field() -> Any:
    return Field(min_length=8, max_length=128)


class TaskParameters(_Strict):
    language: Literal["auto", "zh", "en"]


class ModeRequest(_Strict):
    mode: Literal["ACTIVE", "DRAINING"]


class HardwareModel(_Strict):
    os: str = Field(min_length=1)
    arch: str = Field(min_length=1)
    cpu_name: Optional[str] = None
    logical_cores: Optional[int] = Field(default=None, ge=1)
    ram_total_bytes: Optional[int] = Field(default=None, ge=1)
    ram_available_bytes: Optional[int] = Field(default=None, ge=0)
    gpu_name: Optional[str] = None
    gpu_vram_bytes: Optional[int] = Field(default=None, ge=0)
    gpu_info_source: Literal["detected", "unavailable"]
    manual_labels: dict[str, str] = Field(default_factory=dict)
    detection_errors: list[str] = Field(default_factory=list)


class TelemetryModel(_Strict):
    sampled_at: str = Field(min_length=1)
    cpu_percent: Optional[float] = Field(default=None, ge=0, le=100)
    ram_used_bytes: Optional[int] = Field(default=None, ge=0)
    gpu_percent: Optional[float] = Field(default=None, ge=0, le=100)
    gpu_memory_used_bytes: Optional[int] = Field(default=None, ge=0)
    gpu_metric_source: Literal["nvidia-smi", "unavailable"] = "unavailable"


class CalibrationModel(_Strict):
    sample_count: int = Field(ge=1)
    median_compute_ms: float = Field(gt=0)
    passed: bool
    finished_at: str = Field(min_length=1)


class CapabilityModel(_Strict):
    task_type: Literal["ocr.image.v1", "asr.file.v1"]
    profile_id: str = Field(pattern=PROFILE_PAT)
    profile_digest: Optional[str] = Field(default=None, pattern=HEX64_PAT)
    state: Literal["UNAVAILABLE", "NOT_PREPARED", "PREPARING", "READY", "ERROR"]
    model_cached: bool
    backend: Optional[Literal["cpu", "cuda"]] = None
    runtime_provider: Optional[str] = None
    model_sha256: Optional[str] = Field(default=None, pattern=HEX64_PAT)
    verified: bool
    calibration: Optional[CalibrationModel] = None
    reason: Optional[str] = None


class RegisterRequest(_Strict):
    worker_id: Optional[str] = Field(default=None, pattern=WORKER_PAT)
    name: str = Field(min_length=1, max_length=80)
    hardware: HardwareModel
    capabilities: list[CapabilityModel] = Field(default_factory=list)
    max_slots: int = Field(default=1, ge=1, le=64)
    threads_per_slot: int = Field(default=0, ge=0)
    cpu_thread_budget: int = Field(default=0, ge=0)
    backend: Optional[Literal["cpu", "cuda"]] = None

    @model_validator(mode="after")
    def validate_budget(self):
        if self.backend == "cuda" and self.max_slots != 1:
            raise ValueError("GPU 必须单卡单槽")
        if self.max_slots > 1 and (self.backend != "cpu" or self.threads_per_slot < 1):
            raise ValueError("CPU 多槽必须声明后端及每槽线程数")
        if self.threads_per_slot and self.max_slots * self.threads_per_slot > self.cpu_thread_budget:
            raise ValueError("槽位线程总量超过 CPU 预算")
        if self.hardware.logical_cores and self.cpu_thread_budget > self.hardware.logical_cores:
            raise ValueError("CPU 预算超过检测核心数")
        return self


class HeartbeatRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    node_secret: str = _secret_field()
    telemetry: Optional[TelemetryModel] = None
    capabilities: list[CapabilityModel] = Field(default_factory=list)
    running_execution_ids: Optional[list[str]] = Field(default=None, max_length=64)


class ClaimRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    node_secret: str = _secret_field()
    slot_id: Optional[int] = Field(default=None, ge=0, le=63)
    generation: Optional[str] = Field(default=None, pattern=UUID_PAT)


class SlotReadyRequest(ClaimRequest):
    task_type: Literal["ocr.image.v1"]
    profile_id: str = Field(pattern=PROFILE_PAT)
    profile_digest: str = Field(pattern=HEX64_PAT)
    backend: Literal["cpu"]
    runtime_provider: str
    model_sha256: str = Field(pattern=HEX64_PAT)
    calibration: CalibrationModel


class PreparationStatusRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    node_secret: str = _secret_field()
    status: Literal["DOWNLOADING", "LOADING", "VERIFYING", "READY", "FAILED"]
    progress: Optional[float] = Field(default=None, ge=0, le=1)
    backend: Optional[Literal["cpu", "cuda"]] = None
    runtime_provider: Optional[str] = None
    model_sha256: Optional[str] = Field(default=None, pattern=HEX64_PAT)
    model_cached: Optional[bool] = None
    calibration: Optional[CalibrationModel] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_retryable: Optional[bool] = None


class PreparationRequest(_Strict):
    preparation_id: str = Field(pattern=UUID_PAT)
    worker_id: str = Field(pattern=WORKER_PAT)
    worker_session_id: str = Field(pattern=UUID_PAT)
    task_type: Literal["ocr.image.v1", "asr.file.v1"]
    profile_id: str = Field(pattern=PROFILE_PAT)
    profile_digest: str = Field(pattern=HEX64_PAT)


class ExecutionRequest(_Strict):
    execution_id: str = Field(pattern=UUID_PAT)
    job_id: str = Field(pattern=UUID_PAT)
    task_id: str = Field(pattern=UUID_PAT)
    attempt_no: int = Field(ge=1, le=3)
    worker_id: str = Field(pattern=WORKER_PAT)
    worker_session_id: str = Field(pattern=UUID_PAT)
    task_type: Literal["ocr.image.v1", "asr.file.v1"]
    profile_id: str = Field(pattern=PROFILE_PAT)
    profile_digest: str = Field(pattern=HEX64_PAT)
    input_artifact_id: str = Field(pattern=ARTIFACT_PAT)
    parameters: TaskParameters
    timeout_seconds: int = Field(ge=10, le=600)


class ExecutionTimings(_Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    download_ms: float = Field(ge=0)
    inference_ms: float = Field(ge=0)
    postprocess_ms: float = Field(ge=0)
    write_output_ms: float = Field(ge=0)
    total_worker_ms: float = Field(ge=0)


class ExecutionCompleteRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    node_secret: str = _secret_field()
    outcome: Literal["SUCCEEDED", "FAILED"]
    result: Optional[dict[str, Any]] = None
    timings: Optional[ExecutionTimings] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_retryable: Optional[bool] = None


class CancelAckRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    node_secret: str = _secret_field()


def as_uuid(value: str) -> str:
    return str(uuid.UUID(value))


class SharingRequest(ClaimRequest):
    enabled: bool
