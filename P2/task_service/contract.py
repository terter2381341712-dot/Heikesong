"""Validate directly against the team's unmodified OpenAPI 3.1 schemas."""

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "contracts/openapi.yaml"
SPEC = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
TASK_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
JOB_TERMINAL = TASK_TERMINAL | {"PARTIAL_FAILED"}
EXECUTION_TERMINAL = TASK_TERMINAL | {"REJECTED"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def uid():
    return str(uuid4())


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@lru_cache(maxsize=None)
def validator(name):
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]},
        format_checker=FormatChecker(),
    )


def validate(name, value):
    validator(name).validate(value)
    return value


def error(code, message, retryable=False, **details):
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": uid(),
        "details": details,
    }


class APIError(Exception):
    def __init__(self, status, code, message, retryable=False, **details):
        super().__init__(message)
        self.status = status
        self.body = error(code, message, retryable, **details)


def validate_request(name, value):
    try:
        validate(name, value)
    except ValidationError as exc:
        field = ".".join(str(x) for x in exc.absolute_path)
        raise APIError(422, "INVALID_REQUEST", "请求不符合 0.4.0 契约", field=field) from exc
