import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "task_service.app:create_app",
        factory=True,
        host=os.environ.get("TASK_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("TASK_PORT", "8002")),
        workers=1,
    )
