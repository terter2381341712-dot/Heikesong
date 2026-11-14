"""P1 资源服务配置：全部来自环境变量，令牌不进源码。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONTRACT_VERSION = "0.4.0"

# 仓库根目录（profiles/ 与代码同仓库）
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bind_host: str
    port: int
    data_dir: Path
    demo_token: str
    task_token: str
    join_token: str
    profiles_dir: Path = REPO_ROOT / "profiles"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "resource.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


def load_config() -> Config:
    data_dir = Path(os.environ.get("RESOURCE_DATA_DIR", "./data/resource")).resolve()
    tokens = [os.environ.get(key, "").strip() for key in ("DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN")]
    if not all(tokens) or len(set(tokens)) != len(tokens):
        raise ValueError("DEMO_TOKEN、TASK_TOKEN、JOIN_TOKEN 必须非空且各不相同")
    return Config(
        bind_host=os.environ.get("RESOURCE_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("RESOURCE_PORT", "8001")),
        data_dir=data_dir,
        demo_token=tokens[0],
        task_token=tokens[1],
        join_token=tokens[2],
        profiles_dir=Path(
            os.environ.get("RESOURCE_PROFILES_DIR", str(REPO_ROOT / "profiles"))
        ).resolve(),
    )
