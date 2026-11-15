"""Read the aggregate OCR results published by the operator on the center."""
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, FiniteFloat, ValidationError

from .catalog import BACKEND_PROVIDERS
from .models import HEX64_PAT


class DeviceResult(BaseModel):
    baseline_images_per_second: FiniteFloat = Field(ge=0)
    pool_images_per_second: Optional[FiniteFloat] = Field(default=None, ge=0)


class PoolResult(BaseModel):
    images_per_second: FiniteFloat = Field(ge=0)
    efficiency_percent: Optional[FiniteFloat] = Field(ge=0)
    speedup: Optional[FiniteFloat] = Field(ge=0)


class Report(BaseModel):
    backend: Literal["cpu", "cuda"]
    profile_digest: str = Field(pattern=HEX64_PAT)
    measured_at: datetime
    devices: dict[str, DeviceResult]
    pool: Optional[PoolResult] = None


def published_results(directory: Path, profile_digest: Optional[str]) -> dict:
    results = {}
    for backend in BACKEND_PROVIDERS:
        path = directory / f"ocr-performance-{backend}.json"
        try:
            report = Report.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            continue
        if report.backend == backend and report.profile_digest == profile_digest:
            results[backend] = report.model_dump(mode="json")
    return results
