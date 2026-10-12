"""Runtime configuration for AgentTrafficCop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

try:
    from dotenv import load_dotenv

    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
except ImportError:  # pragma: no cover
    pass


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_api_keys() -> Tuple[str, ...]:
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
    rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("ATC_RATE_LIMIT_PER_MINUTE", 120)
    )

    # --- Observability ---
    metrics_enabled: bool = field(
        default_factory=lambda: _env_bool("ATC_METRICS_ENABLED", True)
    )
    metrics_path: str = field(
        default_factory=lambda: _env_str("ATC_METRICS_PATH", "/metrics")
    )

    # --- Storage backend ---
    redis_url: Optional[str] = field(
        default_factory=lambda: _env_optional_str("ATC_REDIS_URL")
    )
    redis_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ATC_REDIS_TTL_SECONDS", 86_400)
    )
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
        return len(self.api_keys) > 0

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)


settings = Settings()