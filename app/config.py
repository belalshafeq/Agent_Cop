"""Runtime configuration for AgentTrafficCop.

All settings are read once at import time from environment variables
(with safe defaults). Values are frozen in an immutable dataclass so
they cannot be accidentally mutated at runtime.

The `.env` file (if present) is loaded via python-dotenv. Secrets are
NEVER hardcoded here and NEVER logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

# Load .env if it exists. This is a no-op if the file is missing,
# so it is safe to call in any environment (dev, Codespaces, prod).
try:
    from dotenv import load_dotenv

    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
except ImportError:  # pragma: no cover - dotenv is a hard dependency
    pass


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read an integer env var. Invalid values fall back to *default*."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    return value


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _env_optional_str(name: str) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def _env_api_keys() -> Tuple[str, ...]:
    """Parse the comma-separated ATC_API_KEYS env var.

    Returns a tuple of non-empty, stripped keys. If no valid keys are
    configured, returns an empty tuple — which means the API is
    effectively LOCKED (all requests will be rejected). This is a
    fail-closed design: no keys means no access.
    """
    raw = os.getenv("ATC_API_KEYS", "")
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",")]
    return tuple(p for p in parts if p)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for AgentTrafficCop."""

    # --- Server ---
    host: str = field(default_factory=lambda: _env_str("ATC_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("ATC_PORT", 8000))
    environment: str = field(
        default_factory=lambda: _env_str("ATC_ENV", "development")
    )
    log_level: str = field(
        default_factory=lambda: _env_str("ATC_LOG_LEVEL", "INFO").upper()
    )

    # --- Security ---
    api_keys: Tuple[str, ...] = field(default_factory=_env_api_keys)
    max_body_bytes: int = field(
        default_factory=lambda: _env_int("ATC_MAX_BODY_BYTES", 1_048_576)
    )

    # --- Storage backend ---
    # If set, the session store uses Redis. If unset, it uses in-memory.
    redis_url: Optional[str] = field(
        default_factory=lambda: _env_optional_str("ATC_REDIS_URL")
    )
    # TTL for Redis sessions, in seconds. 0 = no expiry.
    redis_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ATC_REDIS_TTL_SECONDS", 86_400)
    )
    # Key prefix so multiple deployments can share one Redis DB.
    redis_key_prefix: str = field(
        default_factory=lambda: _env_str("ATC_REDIS_KEY_PREFIX", "atc:")
    )

    # --- Governance thresholds ---
    max_steps: int = field(
        default_factory=lambda: _env_int("ATC_MAX_STEPS", 25)
    )
    max_repeated_hashes: int = field(
        default_factory=lambda: _env_int("ATC_MAX_REPEATED_HASHES", 3)
    )
    max_cycle_length: int = field(
        default_factory=lambda: _env_int("ATC_MAX_CYCLE_LENGTH", 2)
    )
    max_cycle_repeats: int = field(
        default_factory=lambda: _env_int("ATC_MAX_CYCLE_REPEATS", 3)
    )
    history_window: int = field(
        default_factory=lambda: _env_int("ATC_HISTORY_WINDOW", 16)
    )
    max_sessions: int = field(
        default_factory=lambda: _env_int("ATC_MAX_SESSIONS", 10_000)
    )

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def auth_enabled(self) -> bool:
        """Auth is active iff at least one API key is configured."""
        return len(self.api_keys) > 0

    @property
    def redis_enabled(self) -> bool:
        """True iff a Redis URL was provided."""
        return bool(self.redis_url)


# Module-level singleton. Import this everywhere.
settings = Settings()