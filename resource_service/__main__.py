"""启动入口：python -m resource_service"""
from __future__ import annotations

import uvicorn

from .config import load_config
from .service import create_app


def main() -> None:
    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.bind_host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
