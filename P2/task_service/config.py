import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    resource_base_url: str
    task_token: str
    demo_token: str
    data_dir: Path
    poll_seconds: float = 1.0
    retry_seconds: float = 1.0
    scheduler_concurrency: int = 8
    scheduler_io_timeout_seconds: float = 5.0
    history_ttl_seconds: float = 3600.0
    exploration_interval: int = 8

    def __post_init__(self):
        url = urlsplit(self.resource_base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.path
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError("RESOURCE_BASE_URL 必须只有协议、主机和端口，无尾部斜杠")
        if not self.task_token or not self.demo_token or self.task_token == self.demo_token:
            raise ValueError("TASK_TOKEN 和 DEMO_TOKEN 必须非空且不同")
        if not self.data_dir.is_absolute():
            raise ValueError("TASK_DATA_DIR 必须是独立的绝对路径")
        if self.poll_seconds < 1 or self.retry_seconds < 1:
            raise ValueError("轮询与拒绝重试间隔至少 1 秒")
        if not 1 <= self.scheduler_concurrency <= 64 or self.exploration_interval < 2:
            raise ValueError("调度并发须为 1–64，探索间隔至少为 2")
        if any(
            not math.isfinite(v) or v <= 0
            for v in (self.scheduler_io_timeout_seconds, self.history_ttl_seconds)
        ):
            raise ValueError("调度超时与历史有效期必须为有限正数")

    @classmethod
    def from_env(cls):
        return cls(
            resource_base_url=os.environ["RESOURCE_BASE_URL"],
            task_token=os.environ["TASK_TOKEN"],
            demo_token=os.environ["DEMO_TOKEN"],
            data_dir=Path(os.environ["TASK_DATA_DIR"]),
            scheduler_concurrency=int(os.getenv("TASK_SCHEDULER_CONCURRENCY", "8")),
            scheduler_io_timeout_seconds=float(os.getenv("TASK_SCHEDULER_IO_TIMEOUT_SECONDS", "5")),
            history_ttl_seconds=float(os.getenv("TASK_SCHEDULER_HISTORY_TTL_SECONDS", "3600")),
            exploration_interval=int(os.getenv("TASK_SCHEDULER_EXPLORATION_INTERVAL", "8")),
        )
