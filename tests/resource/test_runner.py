"""真实 RapidOCR Runner 测试：需要 venv 已安装 rapidocr/onnxruntime。"""
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from resource_service.resultschema import validate_ocr_result

rapidocr = pytest.importorskip("rapidocr")

from runners.rapidocr_runner import RapidOCRRunner, combined_model_sha256  # noqa: E402


def make_text_image(path: Path, text="HELLO OCR 123", size=(480, 160)) -> Path:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=44)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((30, size[1] // 2 - 20), text, fill="black", font=font)
    img.save(path, format="PNG")
    return path


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    r = RapidOCRRunner()
    r.ensure_engine()  # 模块级预热一次
    return r


def test_combined_model_sha(runner):
    sha = combined_model_sha256()
    assert len(sha) == 64
    int(sha, 16)  # 合法 hex


def test_recognizes_text(runner, tmp_path):
    input_path = make_text_image(tmp_path / "in.png", "HELLO OCR 123")
    result = runner.execute(input_path, {}, {}, tmp_path)
    validate_ocr_result({**result, "input_artifact_id": "x1"}, "x1")
    assert result["image_width"] == 480
    assert result["image_height"] == 160
    assert result["text"].strip() == "HELLO OCR 123"
    assert len(result["blocks"]) >= 1
    block = result["blocks"][0]
    x0, y0, x1, y1 = block["bbox"]
    assert 0 <= x0 <= x1 <= 480
    assert 0 <= y0 <= y1 <= 160
    assert block["confidence"] is None or 0 <= block["confidence"] <= 1


def test_blank_image_returns_empty_result(runner, tmp_path):
    input_path = make_text_image(tmp_path / "blank.png", text="", size=(200, 100))
    img = Image.new("RGB", (200, 100), "white")
    img.save(input_path, format="PNG")
    result = runner.execute(input_path, {}, {}, tmp_path)
    validate_ocr_result({**result, "input_artifact_id": "x2"}, "x2")
    assert result["text"] == ""
    assert result["blocks"] == []


def test_bbox_clamped_in_bounds(runner, tmp_path):
    """小图上写大字：bbox 必须被裁剪到边界内。"""
    input_path = make_text_image(tmp_path / "tight.png", "WWWW", size=(120, 60))
    result = runner.execute(input_path, {}, {}, tmp_path)
    for block in result["blocks"]:
        x0, y0, x1, y1 = block["bbox"]
        assert 0 <= x0 <= x1 <= 120
        assert 0 <= y0 <= y1 <= 60
