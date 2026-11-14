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

    def __post_init__(self):
        url = urlsplit(self.resource_base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.path
                or url.query or url.fragment or url.username or url.password):
            raise ValueError("RESOURCE_BASE_URL 必须只有协议、主机和端口，无尾部斜杠")
        if not self.task_token or not self.demo_token or self.task_token == self.demo_token:
            raise ValueError("TASK_TOKEN 和 DEMO_TOKEN 必须非空且不同")
        if not self.data_dir.is_absolute():
            raise ValueError("TASK_DATA_DIR 必须是独立的绝对路径")
        if self.poll_seconds < 1 or self.retry_seconds < 1:
            raise ValueError("轮询与拒绝重试间隔至少 1 秒")

    @classmethod
    def from_env(cls):
        return cls(
            resource_base_url=os.environ["RESOURCE_BASE_URL"],
            task_token=os.environ["TASK_TOKEN"], demo_token=os.environ["DEMO_TOKEN"],
            data_dir=Path(os.environ["TASK_DATA_DIR"]),
        )
