import asyncio
import os

import uvicorn

if __name__ == "__main__":
    loop = os.environ.get("UVICORN_LOOP", "auto")
    if loop == "asyncio:SelectorEventLoop" and not hasattr(uvicorn.Config, "get_loop_factory"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = "asyncio"
    uvicorn.run(
        "task_service.app:create_app",
        factory=True,
        host=os.environ.get("TASK_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("TASK_PORT", "8002")),
        loop=loop,
        workers=1,
    )
