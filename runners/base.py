"""执行器接口与注册表：每种 task_type 一个 Runner 实现。

Runner 负责读取输入 → 预处理 → 推理 → 后处理 → 返回符合 result schema 的 dict。
模型选择未定稿前，profile 通过锁文件绑定 Runner 与后端；当前仅提供 DevStubRunner
用于联调流水线，**不得用于正式性能数据**。
"""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional


class Runner(ABC):
    task_type: str = ""

    @abstractmethod
    def execute(
        self,
        input_path: Path,
        media_info: dict,
        parameters: dict,
        workdir: Path,
    ) -> dict:
        """返回符合 OpenAPI result schema 的 dict（不含上传）。"""


_REGISTRY: dict[str, str] = {
    # task_type -> "module:ClassName"（延迟导入，避免未安装依赖时崩溃）
    "ocr.image.v1": "runners.devstub:DevStubRunner",
    "asr.file.v1": "runners.devstub:DevStubRunner",
}


def set_runner(task_type: str, target: str) -> None:
    _REGISTRY[task_type] = target


def load_runner(task_type: str) -> Runner:
    target = _REGISTRY.get(task_type)
    if target is None:
        raise RuntimeError(f"没有注册 {task_type} 的执行器")
    module_name, class_name = target.split(":", 1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls()
