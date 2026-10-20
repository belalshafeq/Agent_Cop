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


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
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
    """Legacy ATC_API_KEYS — treated as `team` tier."""
    raw = os.getenv("ATC_API_KEYS", "")
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _env_key_list(name: str) -> Tuple[str, ...]:
    """Comma-separated key list for tier-specific assignment."""
    raw = os.getenv(name, "")
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for AgentTrafficCop."""

    # --- Server ---
    host: str = field(default_factory=lambda: _env_str("ATC_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("ATC_PORT", 8000, minimum=1))
    environment: str = field(
        default_factory=lambda: _env_str("ATC_ENV", "development")
    )
    log_level: str = field(
        default_factory=lambda: _env_str("ATC_LOG_LEVEL", "INFO").upper()
    )

    # --- Security (legacy list — treated as team tier) ---
    api_keys: Tuple[str, ...] = field(default_factory=_env_api_keys)
    # Optional comma-separated list of additional CORS origins.
    cors_origins: Optional[str] = field(
        default_factory=lambda: _env_optional_str("ATC_CORS_ORIGINS")
    )

    # --- Auth / JWT ---
    jwt_secret_key: str = field(
        default_factory=lambda: _env_str("ATC_JWT_SECRET", "super-secret-change-me-in-prod")
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days

    # --- Lemon Squeezy Billing ---
    lemonsqueezy_checkout_url: Optional[str] = field(
        default_factory=lambda: _env_optional_str("LEMONSQUEEZY_CHECKOUT_URL")
    )
    lemonsqueezy_webhook_secret: Optional[str] = field(
        default_factory=lambda: _env_optional_str("LEMONSQUEEZY_WEBHOOK_SECRET")
    )

    # --- Tier assignments ---
    free_api_keys: Tuple[str, ...] = field(
        default_factory=lambda: _env_key_list("ATC_FREE_API_KEYS")
    )
    pro_api_keys: Tuple[str, ...] = field(
        default_factory=lambda: _env_key_list("ATC_PRO_API_KEYS")
    )
    team_api_keys: Tuple[str, ...] = field(
        default_factory=lambda: _env_key_list("ATC_TEAM_API_KEYS")
    )
    enterprise_api_keys: Tuple[str, ...] = field(
        default_factory=lambda: _env_key_list("ATC_ENTERPRISE_API_KEYS")
    )

    # --- Tier message limits (per calendar month) ---
    tier_free_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_FREE_LIMIT", 10_000, minimum=0)
    )
    tier_pro_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_PRO_LIMIT", 250_000, minimum=0)
    )
    tier_team_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_TEAM_LIMIT", 2_000_000, minimum=0)
    )
    tier_enterprise_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_ENTERPRISE_LIMIT", 0, minimum=0)
    )

    # --- Usage metering ---
    usage_enforcement_enabled: bool = field(
        default_factory=lambda: _env_bool("ATC_USAGE_ENFORCEMENT_ENABLED", True)
    )
    usage_key_prefix: str = field(
        default_factory=lambda: _env_str("ATC_USAGE_KEY_PREFIX", "atc:usage:")
    )

    # --- Request limits ---
    max_body_bytes: int = field(
        default_factory=lambda: _env_int("ATC_MAX_BODY_BYTES", 1_048_576, minimum=1)
    )
    rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("ATC_RATE_LIMIT_PER_MINUTE", 120, minimum=1)
    )

    # --- Observability ---
    metrics_enabled: bool = field(
        default_factory=lambda: _env_bool("ATC_METRICS_ENABLED", True)
    )
    metrics_path: str = field(
        default_factory=lambda: _env_str("ATC_METRICS_PATH", "/metrics")
    )

    # --- Audit log ---
    audit_log_enabled: bool = field(
        default_factory=lambda: _env_bool("ATC_AUDIT_LOG_ENABLED", False)
    )
    audit_log_path: Optional[str] = field(
        default_factory=lambda: _env_optional_str("ATC_AUDIT_LOG_PATH")
    )

    # --- Storage backend ---
    redis_url: Optional[str] = field(
        default_factory=lambda: _env_optional_str("ATC_REDIS_URL")
    )
    redis_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ATC_REDIS_TTL_SECONDS", 86_400, minimum=1)
    )
    redis_key_prefix: str = field(
        default_factory=lambda: _env_str("ATC_REDIS_KEY_PREFIX", "atc:")
    )
    session_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ATC_SESSION_TTL_SECONDS", 0, minimum=0)
    )

    # --- Governance thresholds ---
    max_steps: int = field(
        default_factory=lambda: _env_int("ATC_MAX_STEPS", 25, minimum=1)
    )
    max_repeated_hashes: int = field(
        default_factory=lambda: _env_int("ATC_MAX_REPEATED_HASHES", 3, minimum=1)
    )
    max_cycle_length: int = field(
        default_factory=lambda: _env_int("ATC_MAX_CYCLE_LENGTH", 2, minimum=1)
    )
    max_cycle_repeats: int = field(
        default_factory=lambda: _env_int("ATC_MAX_CYCLE_REPEATS", 3, minimum=1)
    )
    history_window: int = field(
        default_factory=lambda: _env_int("ATC_HISTORY_WINDOW", 16, minimum=1)
    )
    max_sessions: int = field(
        default_factory=lambda: _env_int("ATC_MAX_SESSIONS", 10_000, minimum=1)
    )

    # --- Derived ---

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def auth_enabled(self) -> bool:
        return len(self.api_keys) > 0 or len(self._all_tier_keys()) > 0

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    @property
    def audit_enabled(self) -> bool:
        return self.audit_log_enabled and bool(self.audit_log_path)

    @property
    def ttl_enabled(self) -> bool:
        return self.session_ttl_seconds > 0

    def _all_tier_keys(self) -> Tuple[str, ...]:
        return (
            self.free_api_keys
            + self.pro_api_keys
            + self.team_api_keys
            + self.enterprise_api_keys
        )

    def tier_for_key(self, key: str) -> str:
        if key in self.enterprise_api_keys:
            return "enterprise"
        if key in self.team_api_keys or key in self.api_keys:
            return "team"
        if key in self.pro_api_keys:
            return "pro"
        if key in self.free_api_keys:
            return "free"
        return "free"

    def limit_for_tier(self, tier: str) -> int:
        return {
            "free": self.tier_free_limit,
            "pro": self.tier_pro_limit,
            "team": self.tier_team_limit,
            "enterprise": self.tier_enterprise_limit,
        }.get(tier, self.tier_free_limit)


settings = Settings()