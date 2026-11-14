"""请求模型：全部 extra=forbid（请求正文不允许未声明字段）。"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

UUID_PAT = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
WORKER_PAT = r"^[A-Za-z0-9_-]{1,80}$"
ARTIFACT_PAT = r"^[A-Za-z0-9_-]{1,80}$"
PROFILE_PAT = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
HEX64_PAT = r"^[a-f0-9]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class HeartbeatRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    telemetry: Optional[TelemetryModel] = None
    capabilities: list[CapabilityModel] = Field(default_factory=list)


class ClaimRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)


class PreparationStatusRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
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


class ExecutionCompleteRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)
    outcome: Literal["SUCCEEDED", "FAILED"]
    result: Optional[dict[str, Any]] = None
    timings: Optional[dict[str, float]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_retryable: Optional[bool] = None


class CancelAckRequest(_Strict):
    session_id: str = Field(pattern=UUID_PAT)


def as_uuid(value: str) -> str:
    return str(uuid.UUID(value))
