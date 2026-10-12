"""Append-only audit log for AgentTrafficCop.

Records governance events (trips, resets, auth failures, schema
failures, rate limits) to a JSON Lines file. Each line is a complete
JSON object with a stable schema.

Design:
  * Append-only. Lines are never modified after being written.
  * Thread-safe. A single Lock guards all writes.
  * Fail-soft. Write failures are logged but never propagate.
  * Bounded. Only governance events are logged, not every approval.

File format:
    {"ts":"2026-10-12T10:22:26.123Z","event":"trip","error_code":"...",...}
    {"ts":"2026-10-12T10:22:31.456Z","event":"reset","session_id":"...",...}

Reading the log:
    grep '"event":"trip"' audit.log
    jq -c 'select(.error_code == "REPEATED_PAYLOAD_LOOP")' audit.log
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import settings

logger = logging.getLogger("agenttrafficcop.audit")


class AuditLog:
    """Append-only JSON Lines writer for governance events.

    One instance is created per process. It holds an open file handle
    in append mode. All writes go through a single Lock.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._lock = threading.Lock()
        self._file = None
        self._path: Optional[Path] = None

        if not enabled:
            logger.info("audit_log_disabled")
            return

        if path is None:
            logger.info("audit_log_disabled reason=no_path_configured")
            self._enabled = False
            return

        try:
            self._path = Path(path).expanduser().resolve()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Open in append mode, line-buffered.
            self._file = open(self._path, "a", encoding="utf-8", buffering=1)
            logger.info("audit_log_opened path=%s", self._path)
        except Exception as e:
            logger.error(
                "audit_log_open_failed path=%s error=%s enabled=false",
                path,
                e,
            )
            self._enabled = False
            self._file = None
            self._path = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def close(self) -> None:
        """Close the underlying file, if open."""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    # --- Event emitters ----------------------------------------------------

    def log_trip(
        self,
        *,
        session_id: str,
        error_code: str,
        step: int,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        api_key_fingerprint: Optional[str] = None,
    ) -> None:
        """Record a circuit breaker trip."""
        self._write({
            "event": "trip",
            "error_code": error_code,
            "session_id": session_id,
            "step": step,
            "sender": sender,
            "recipient": recipient,
            "diagnostics": diagnostics or {},
            "api_key_fingerprint": api_key_fingerprint,
        })

    def log_reset(
        self,
        *,
        session_id: str,
        removed: bool,
        api_key_fingerprint: Optional[str] = None,
    ) -> None:
        """Record a session reset."""
        self._write({
            "event": "reset",
            "session_id": session_id,
            "removed": removed,
            "api_key_fingerprint": api_key_fingerprint,
        })

    def log_auth_failed(
        self,
        *,
        reason: str,
        api_key_fingerprint: Optional[str] = None,
    ) -> None:
        """Record an authentication failure."""
        self._write({
            "event": "auth_failed",
            "reason": reason,
            "api_key_fingerprint": api_key_fingerprint,
        })

    def log_schema_failed(
        self,
        *,
        session_id: Optional[str],
        error_count: int,
    ) -> None:
        """Record a schema validation failure."""
        self._write({
            "event": "schema_failed",
            "session_id": session_id,
            "error_count": error_count,
        })

    def log_rate_limited(
        self,
        *,
        path: str,
        retry_after: int,
    ) -> None:
        """Record a rate limit rejection."""
        self._write({
            "event": "rate_limited",
            "path": path,
            "retry_after_seconds": retry_after,
        })

    # --- Internal ----------------------------------------------------------

    def _write(self, payload: Dict[str, Any]) -> None:
        """Write one event as a single JSON line.

        Never raises. If the write fails, logs the failure and returns.
        """
        if not self._enabled or self._file is None:
            return

        # Timestamp + schema enforcement.
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            **payload,
        }

        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            logger.warning("audit_log_json_error error=%s", e)
            return

        with self._lock:
            if self._file is None:
                return
            try:
                self._file.write(line + "\n")
            except Exception as e:
                logger.error("audit_log_write_failed error=%s", e)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


audit = AuditLog(
    path=settings.audit_log_path if settings.audit_log_enabled else None,
    enabled=settings.audit_log_enabled,
)