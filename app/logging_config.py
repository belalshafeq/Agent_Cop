"""Structured JSON logging for AgentTrafficCop.

Installs a single JSON formatter on the root logger. Every log record
becomes one line of parseable JSON with a stable schema:
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict

from .config import settings


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON."""

    # FIX: Force UTC time. The original used local time but appended "Z",
    # which made logs lie about their timezone (e.g., Karachi time at 10am
    # would show as 10:00Z instead of 05:00Z).
    converter = time.gmtime

    # Fields provided by the logging module that we don't want to
    # duplicate as "extra" attributes.
    _RESERVED = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "asctime",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach any extra fields passed via logger.<level>(..., extra={...}).
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # cheap check: is it serializable?
                base[key] = value
            except (TypeError, ValueError):
                base[key] = repr(value)

        # Include exception traceback if present.
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(base, default=str, ensure_ascii=False)


def configure_logging() -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    root = logging.getLogger()

    # Already configured? Do nothing.
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    # Remove any pre-existing handlers (e.g. from a previous basicConfig)
    # so we don't double-log.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())