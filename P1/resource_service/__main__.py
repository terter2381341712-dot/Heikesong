"""启动入口：python -m resource_service"""
from __future__ import annotations

import asyncio
import os

import uvicorn

from .config import load_config
from .service import create_app


def main() -> None:
    try:
        cfg = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    app = create_app(cfg)
    loop = os.environ.get("UVICORN_LOOP", "auto")
    if loop == "asyncio:SelectorEventLoop" and not hasattr(uvicorn.Config, "get_loop_factory"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = "asyncio"
    uvicorn.run(
        app, host=cfg.bind_host, port=cfg.port, log_level="info",
        loop=loop,
    )


if __name__ == "__main__":
    main()
