"""Runtime configuration for AgentTrafficCop."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("agenttrafficcop.config")

try:
    from dotenv import load_dotenv

    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
except ImportError:  # pragma: no cover
    pass


# FIX (C3): explicit list of environments treated as non-production.
# Anything NOT in this set is treated as production — fail closed. The
# old check matched {"production", "prod"} only, so `ATC_ENV=staging`
# would silently bypass the JWT safety check and ship the placeholder
# secret, making every JWT forgeable.
_DEV_ENVS = frozenset({"development", "dev", "test", "testing", "local"})

# FIX (#5): widen the list of known-bad JWT secrets. The old check only
# matched the literal "super-secret-change-me-in-prod". Anyone "fixing"
# the placeholder by changing it to another guessable value shipped
# prod with a weak signing key.
_INSECURE_JWT_SECRETS = frozenset({
    "",
    "super-secret-change-me-in-prod",
    "CHANGE-ME-GENERATE-WITH-secrets-token_urlsafe-32",
    "change-me",
    "changeme",
    "your-secret-here",
    "secret",
})


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
    raw = os.getenv("ATC_API_KEYS", "")
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _env_key_list(name: str) -> Tuple[str, ...]:
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

    # --- Security ---
    api_keys: Tuple[str, ...] = field(default_factory=_env_api_keys)
    cors_origins: Optional[str] = field(
        default_factory=lambda: _env_optional_str("ATC_CORS_ORIGINS")
    )

    # FIX (C5): only trust X-Forwarded-For when explicitly enabled.
    # Default is False because an attacker with a direct connection can
    # spoof the header and bypass rate limiting.
    trust_proxy: bool = field(
        default_factory=lambda: _env_bool("ATC_TRUST_PROXY", False)
    )

    # FIX (M1): admin API keys — required to access /v1/admin/* and
    # /v1/sessions/*. If empty, falls back to allowing any `team`-tier
    # key with a warning at startup.
    admin_api_keys: Tuple[str, ...] = field(
        default_factory=lambda: _env_key_list("ATC_ADMIN_API_KEYS")
    )

    # --- Auth / JWT ---
    jwt_secret_key: str = field(
        default_factory=lambda: _env_str(
            "ATC_JWT_SECRET", "super-secret-change-me-in-prod"
        )
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

    # --- Lemon Squeezy Billing ---
    lemonsqueezy_checkout_url: Optional[str] = field(
        default_factory=lambda: _env_optional_str("LEMONSQUEEZY_CHECKOUT_URL")
    )
    lemonsqueezy_webhook_secret: Optional[str] = field(
        default_factory=lambda: _env_optional_str("LEMONSQUEEZY_WEBHOOK_SECRET")
    )

    # --- Error Tracking ---
    sentry_dsn: Optional[str] = field(
        default_factory=lambda: _env_optional_str("SENTRY_DSN")
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

    # --- Tier message limits (per calendar month; 0 = unlimited) ---
    tier_free_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_FREE_LIMIT", 10_000, minimum=0)
    )
    tier_pro_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_PRO_LIMIT", 250_000, minimum=0)
    )
    tier_team_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_TEAM_LIMIT", 2_000_000, minimum=0)
    )

    # --- Tier agent limits ---
    tier_free_agents: int = field(
        default_factory=lambda: _env_int("ATC_TIER_FREE_AGENTS", 5, minimum=0)
    )
    tier_pro_agents: int = field(
        default_factory=lambda: _env_int("ATC_TIER_PRO_AGENTS", 25, minimum=0)
    )
    tier_team_agents: int = field(
        default_factory=lambda: _env_int("ATC_TIER_TEAM_AGENTS", 100, minimum=0)
    )

    # --- Tier rate limits (requests/minute) ---
    tier_free_rate_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_FREE_RATE_LIMIT", 60, minimum=1)
    )
    tier_pro_rate_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_PRO_RATE_LIMIT", 120, minimum=1)
    )
    tier_team_rate_limit: int = field(
        default_factory=lambda: _env_int("ATC_TIER_TEAM_RATE_LIMIT", 300, minimum=1)
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
        # FIX (C3): fail closed. Any env not in _DEV_ENVS is production.
        return self.environment.lower() not in _DEV_ENVS

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
        return self.free_api_keys + self.pro_api_keys + self.team_api_keys

    def tier_for_key(self, key: str) -> str:
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
        }.get(tier, self.tier_free_limit)

    def agent_limit_for_tier(self, tier: str) -> int:
        return {
            "free": self.tier_free_agents,
            "pro": self.tier_pro_agents,
            "team": self.tier_team_agents,
        }.get(tier, self.tier_free_agents)

    def rate_limit_for_tier(self, tier: str) -> int:
        return {
            "free": self.tier_free_rate_limit,
            "pro": self.tier_pro_rate_limit,
            "team": self.tier_team_rate_limit,
        }.get(tier, self.rate_limit_per_minute)


settings = Settings()


# --- Production safety checks ---
if settings.is_production:
    if settings.jwt_secret_key in _INSECURE_JWT_SECRETS:
        raise RuntimeError(
            "FATAL: ATC_ENV indicates production "
            f"(ATC_ENV={settings.environment!r}) but ATC_JWT_SECRET is "
            "a known placeholder. Generate a real secret and set it:\n"
            "    python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "Then set ATC_JWT_SECRET to the output and restart."
        )
elif settings.jwt_secret_key in _INSECURE_JWT_SECRETS:
    logger.warning(
        "insecure_jwt_secret env=%s — set ATC_JWT_SECRET before "
        "deploying to production",
        settings.environment,
    )

# FIX (M1): warn if admin API keys aren't configured. Admin endpoints
# will fall back to team-tier auth, which is less strict than a dedicated
# list but at least prevents free-tier users from accessing them.
if not settings.admin_api_keys and settings.auth_enabled:
    logger.warning(
        "admin_api_keys_not_configured — admin endpoints will accept any "
        "team-tier key. Set ATC_ADMIN_API_KEYS to lock them down."
    )