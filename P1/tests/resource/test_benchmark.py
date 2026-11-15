"""离线基线工具测试：真实跑通 JSONL 输出与退出码。"""
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from resource_service.resultschema import validate_ocr_result

pytest.importorskip("rapidocr")


def make_image(path: Path, text: str) -> None:
    img = Image.new("RGB", (480, 160), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=44)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((30, 60), text, fill="black", font=font)
    img.save(path, format="PNG")


def test_benchmark_end_to_end(tmp_path):
    inputs = []
    for i in range(2):
        f = tmp_path / f"page{i}.png"
        make_image(f, f"BASELINE {i} ABC")
        inputs.append(
            {
                "input_index": i,
                "input_artifact_id": f"a{uuid.uuid4().hex[:8]}",
                "local_path": str(f),
                "input_sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            }
        )
    manifest = tmp_path / "inputs.jsonl"
    manifest.write_text(
        "\n".join(json.dumps(i) for i in inputs), encoding="utf-8"
    )
    out_dir = tmp_path / "baseline-out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "node_agent.benchmark",
            "--manifest",
            str(manifest),
            "--profile",
            str(Path("profiles/ocr.default.v1.lock.json").resolve()),
            "--backend",
            "cpu",
            "--threads",
            "2",
            "--output-dir",
            str(out_dir),
            "--run-id",
            "test-run-1",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    records = [row for row in lines if row["record_type"] == "file"]
    summary = [row for row in lines if row["record_type"] == "summary"]
    assert len(records) == 2
    assert len(summary) == 1
    assert summary[0]["total"] == 2
    assert summary[0]["succeeded"] == 2
    assert summary[0]["failed"] == 0
    assert summary[0]["warmup_ms"] >= 0
    for r, source in zip(records, inputs):
        assert r["run_id"] == "test-run-1"
        assert r["task_type"] == "ocr.image.v1"
        assert len(r["profile_digest"]) == 64
        assert r["status"] == "SUCCEEDED"
        assert r["input_sha256"] == source["input_sha256"]
        assert r["backend"] == "cpu"
        result = Path(r["result_path"]).read_bytes()
        assert hashlib.sha256(result).hexdigest() == r["result_sha256"]
        validate_ocr_result(json.loads(result), source["input_artifact_id"])
    assert "backend=CPUExecutionProvider model_sha256=" in proc.stderr
    # 持久副本
    persisted = out_dir / "baseline-test-run-1.jsonl"
    assert persisted.exists()


def test_benchmark_sha_mismatch_fails(tmp_path):
    f = tmp_path / "bad.png"
    make_image(f, "WRONG SHA")
    manifest = tmp_path / "inputs.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "input_index": 0,
                "input_artifact_id": "aaaa",
                "local_path": str(f),
                "input_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "node_agent.benchmark",
            "--manifest",
            str(manifest),
            "--profile",
            str(Path("profiles/ocr.default.v1.lock.json").resolve()),
            "--backend",
            "cpu",
            "--output-dir",
            str(tmp_path / "out2"),
            "--run-id",
            "test-run-2",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 1  # 输入失败，但工具本身正常运行
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert lines[-1]["failed"] == 1
    assert lines[0]["status"] == "FAILED"
    assert lines[0]["error"]["code"] == "EXECUTION_ERROR"
