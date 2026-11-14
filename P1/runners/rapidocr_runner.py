"""RapidOCR 执行器：EXIF 纠向 → 检测/方向分类/识别 → 轴对齐 bbox → 规范 OCRResult。

模型与依赖版本冻结在 profiles/ocr.default.v1.lock.json；
引擎实例进程内复用（契约要求"模型会话必须复用"）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageOps

MODEL_NAMES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
}


def model_files() -> list[Path]:
    """RapidOCR 内置 ONNX 权重文件路径（按文件名排序，保证确定性）。"""
    import rapidocr

    models_dir = Path(rapidocr.__file__).parent / "models"
    return [models_dir / name for name in sorted(MODEL_NAMES.values())]


def combined_model_sha256() -> str:
    """检测+方向分类+识别三组权重的组合 SHA256（按文件名序拼接字节）。"""
    digest = hashlib.sha256()
    for path in model_files():
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _polygon_to_bbox(points: Any, width: int, height: int) -> list[float]:
    """四点多边形 → 轴对齐外接矩形 [x0,y0,x1,y1]，裁剪到图像边界内。"""
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    x0 = max(0.0, min(xs))
    y0 = max(0.0, min(ys))
    x1 = min(float(width), max(xs))
    y1 = min(float(height), max(ys))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [x0, y0, x1, y1]


def _reading_order_key(block: dict) -> tuple[float, float]:
    """阅读顺序：优先按上边缘 y0 分行（行内容差 12px），行内按 x0。"""
    return (round(block["bbox"][1] / 12.0), block["bbox"][0])


class RapidOCRRunner:
    task_type = "ocr.image.v1"

    def __init__(self, threads: int = 0) -> None:
        self._engine: Optional[Any] = None
        self._threads = threads

    def ensure_engine(self) -> Any:
        if self._engine is None:
            from rapidocr import RapidOCR

            paths = {path.name: path for path in model_files()}
            params = {
                f"{stage}.model_path": str(paths[name]) for stage, name in MODEL_NAMES.items()
            }
            if self._threads:
                params["EngineConfig.onnxruntime.intra_op_num_threads"] = self._threads
                params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
            self._engine = RapidOCR(params=params)
            self._engine._load_det_model()
            self._engine._load_cls_model()
            self._engine._load_rec_model()
        return self._engine

    def runtime_provider(self) -> str:
        engine = self.ensure_engine()
        providers = [
            stage.session.session.get_providers()
            for stage in (engine.text_det, engine.text_cls, engine.text_rec)
        ]
        if any(active != ["CPUExecutionProvider"] for active in providers):
            raise ValueError("当前 profile 要求三组模型均使用 CPUExecutionProvider")
        return providers[0][0]

    def execute(
        self,
        input_path: Path,
        media_info: dict,
        parameters: dict,
        workdir: Path,
    ) -> dict:
        engine = self.ensure_engine()
        with Image.open(input_path) as img:
            img = ImageOps.exif_transpose(img)  # EXIF 纠向后 OCR：坐标即纠正空间坐标
            rgb = img.convert("RGB")
        width, height = rgb.size
        array = np.asarray(rgb, dtype=np.uint8)

        output = engine(array)
        blocks: list[dict] = []
        if output is not None and getattr(output, "txts", None):
            boxes = output.boxes if output.boxes is not None else []
            for i, text in enumerate(output.txts):
                if i >= len(boxes):
                    break
                bbox = _polygon_to_bbox(boxes[i], width, height)
                score = output.scores[i] if i < len(output.scores) else None
                confidence = round(float(score), 6) if score is not None else None
                blocks.append({"text": str(text), "bbox": bbox, "confidence": confidence})
        blocks.sort(key=_reading_order_key)

        return {
            "schema_version": "1.0",
            "task_type": "ocr.image.v1",
            "input_artifact_id": "",  # 由 agent/服务端按执行记录填充
            "image_width": int(width),
            "image_height": int(height),
            "text": "\n".join(b["text"] for b in blocks),
            "blocks": blocks,
        }
