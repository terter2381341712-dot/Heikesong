"""P1 资源服务配置：全部来自环境变量，令牌不进源码。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONTRACT_VERSION = "0.2.0"


@dataclass(frozen=True)
class Config:
    bind_host: str
    port: int
    data_dir: Path
    demo_token: str
    task_token: str
    join_token: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "resource.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


def load_config() -> Config:
    data_dir = Path(os.environ.get("RESOURCE_DATA_DIR", "./data/resource")).resolve()
    return Config(
        bind_host=os.environ.get("RESOURCE_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("RESOURCE_PORT", "8001")),
        data_dir=data_dir,
        demo_token=os.environ.get("DEMO_TOKEN", ""),
        task_token=os.environ.get("TASK_TOKEN", ""),
        join_token=os.environ.get("JOIN_TOKEN", ""),
    )
