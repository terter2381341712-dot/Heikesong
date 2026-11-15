"""RapidOCR 执行器：EXIF 纠向 → 检测/方向分类/识别 → 轴对齐 bbox → 规范 OCRResult。

模型与依赖版本冻结在 profiles/ocr.default.v1.lock.json；
引擎实例进程内复用（契约要求"模型会话必须复用"）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
from resource_service.backends import PROVIDERS, GPU_BACKENDS

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

    def __init__(self, threads: int = 0, backend: str = "cpu", device_id: int = 0) -> None:
        if backend not in PROVIDERS or device_id < 0:
            raise ValueError("无效的 OCR 后端或 GPU 编号")
        self._engine: Optional[Any] = None
        self._threads = threads
        self.backend = backend
        self.device_id = device_id
        self._acceleration_checked = False

    def ensure_engine(self) -> Any:
        if self._engine is None:
            from rapidocr import RapidOCR
            import onnxruntime as ort

            expected = PROVIDERS[self.backend]
            if expected not in ort.get_available_providers():
                raise ValueError(f"{expected} 不可用；不允许静默降级 CPU")

            paths = {path.name: path for path in model_files()}
            params = {
                f"{stage}.model_path": str(paths[name]) for stage, name in MODEL_NAMES.items()
            }
            params["EngineConfig.onnxruntime.use_cuda"] = self.backend == "cuda"
            params["EngineConfig.onnxruntime.cuda_ep_cfg.device_id"] = self.device_id
            params["EngineConfig.onnxruntime.use_dml"] = False
            params["EngineConfig.onnxruntime.use_coreml"] = False
            accelerator_sessions = {}
            if self.backend in {"directml", "coreml"}:
                # RapidOCR accepts explicit per-stage sessions. DirectML requires
                # sequential execution and disabled memory patterns.
                for stage, name in MODEL_NAMES.items():
                    options = ort.SessionOptions()
                    directory = Path(os.environ.get("NODE_AGENT_DATA_DIR", ".")) / "provider-evidence"
                    directory.mkdir(parents=True, exist_ok=True)
                    options.enable_profiling = True
                    options.profile_file_prefix = str(directory / stage)
                    if self._threads:
                        options.intra_op_num_threads = self._threads
                        options.inter_op_num_threads = 1
                    if self.backend == "directml":
                        options.enable_mem_pattern = False
                        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                        provider_options = {"device_id": self.device_id}
                    else:
                        # ORT 1.19.2 is the last Mac wheel supporting Python 3.9.
                        # CoreML's device choice is system-managed (GPU/ANE/CPU);
                        # do not pass newer options that this version ignores.
                        provider_options = {"flags": "COREML_FLAG_CREATE_MLPROGRAM"}
                    session = ort.InferenceSession(str(paths[name]), sess_options=options,
                        providers=[(expected, provider_options), "CPUExecutionProvider"])
                    session.disable_fallback()
                    accelerator_sessions[stage] = session
            if self._threads:
                params["EngineConfig.onnxruntime.intra_op_num_threads"] = self._threads
                params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
            self._engine = RapidOCR(params=params)
            self._engine._load_det_model()
            self._engine._load_cls_model()
            self._engine._load_rec_model()
            for stage, session in accelerator_sessions.items():
                target = {"Det": self._engine.text_det, "Cls": self._engine.text_cls,
                          "Rec": self._engine.text_rec}[stage]
                target.session.session = session
            self._check_providers()
        return self._engine

    def _check_providers(self) -> str:
        engine = self._engine
        expected = PROVIDERS[self.backend]
        sessions = [stage.session.session for stage in (engine.text_det, engine.text_cls, engine.text_rec)]
        providers = [
            session.get_providers() for session in sessions
        ]
        if any(not active or active[0] != expected for active in providers):
            raise ValueError(f"三组 OCR 模型必须以 {expected} 为首选实际 provider：{providers}")
        if self.backend in GPU_BACKENDS:
            for session in sessions:
                # Keep CPU graph partition fallback, but forbid retrying an entire failed
                # CUDA session on CPU while still advertising this node as CUDA-ready.
                session.disable_fallback()
                actual = session.get_provider_options().get(expected, {}).get("device_id")
                if self.backend == "cuda" and (actual is None or int(actual) != self.device_id):
                    raise ValueError("CUDA 会话实际设备编号与请求不一致")
        return expected

    def verify_acceleration(self):
        """Reject a nominal GPU route whose calibration only executed CPU nodes."""
        if self.backend not in {"directml", "coreml"} or self._acceleration_checked:
            return
        expected = PROVIDERS[self.backend]
        counts = {}
        for label, stage in (("det", self._engine.text_det), ("cls", self._engine.text_cls), ("rec", self._engine.text_rec)):
            path = Path(stage.session.session.end_profiling())
            events = json.loads(path.read_text(encoding="utf-8"))
            counts[label] = sum(e.get("cat") == "Node" and e.get("args", {}).get("provider") == expected for e in events)
        if not sum(counts.values()):
            raise ValueError(f"{expected} 自测未观察到加速后端算子执行，不能作为 GPU 接单：{counts}")
        print(f"[GPU] provider={expected} calibration_operator_counts={counts}; unsupported operators may execute on CPU", flush=True)
        self._acceleration_checked = True

    def runtime_provider(self) -> str:
        self.ensure_engine()
        return self._check_providers()

    def execute(
        self,
        input_path: Path,
        media_info: dict,
        parameters: dict,
        workdir: Path,
    ) -> dict:
        from node_agent.input_pipeline import decode_image
        return self.execute_array(decode_image(input_path, media_info), parameters, workdir)

    def execute_array(self, array, parameters: dict, workdir: Path) -> dict:
        engine = self.ensure_engine()
        if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3:
            raise ValueError("OCR 输入必须是 RGB uint8 数组")
        height, width = array.shape[:2]
        output = engine(array)
        self._check_providers()
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
