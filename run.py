"""Local development entrypoint for AgentTrafficCop.

Run:  python run.py

Reads ATC_HOST, ATC_PORT, ATC_LOG_LEVEL from the environment (or .env).
For production, do not use this file — invoke uvicorn directly with
your process manager (systemd, gunicorn+uvicorn workers, Docker, etc.).
"""

from __future__ import annotations

import uvicorn

from app.config import settings


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
        # We configure JSON logging ourselves in app/logging_config.py,
        # so tell uvicorn not to override it.
        log_config=None,
        access_log=False,
    )