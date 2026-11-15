"""Execution backends retain provider identity; all accelerators belong to GPU."""

PROVIDERS = {
    "cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider",
    "directml": "DmlExecutionProvider", "coreml": "CoreMLExecutionProvider",
}
GPU_BACKENDS = frozenset(PROVIDERS) - {"cpu"}
RUNTIME_PACKAGES = {"cpu": "onnxruntime", "cuda": "onnxruntime-gpu",
                    "directml": "onnxruntime-directml", "coreml": "onnxruntime"}
