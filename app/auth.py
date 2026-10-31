"""Authentication routes: Register, Login, and Key Management."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field, field_validator

from .config import settings
from .db import get_db
from .middleware import _SlidingWindowLimiter, _client_key
from .schemas import UsageResponse
from .usage import (
    snapshot, 
    _resolve_usage_key, 
    get_tier_and_limit,
    get_api_key_created_at,
)
from .userstore import user_store, User

logger = logging.getLogger("agenttrafficcop.auth")
router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login")

# FIX: Dedicated rate limiter for auth routes to stop brute-force and spam.
_auth_limiter = _SlidingWindowLimiter(
    max_per_window=10,
    window_seconds=60,
    max_clients=10_000,
)


def _enforce_auth_rate_limit(request: Request) -> None:
    ip = _client_key(request)
    allowed, retry_after = _auth_limiter.check(ip)
    if not allowed:
        logger.warning(
            "auth_rate_limit_exceeded ip=%s path=%s",
            ip,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many authentication attempts. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class UserRegister(BaseModel):
    email: EmailStr
    # Field-level limit is char-based; the validator below enforces the
    # bcrypt *byte* limit, which is what actually matters.
    password: str = Field(..., min_length=8, max_length=72)

    # FIX: Enforce 72 UTF-8 bytes to prevent silent truncation.
    @field_validator("password")
    @classmethod
    def _bcrypt_byte_limit(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError(
                "password must be at most 72 bytes when UTF-8 encoded "
                "(bcrypt limitation). Use a shorter password or ASCII only."
            )
        return v


class Token(BaseModel):
    access_token: str
    token_type: str
    api_key: str | None = None


class UserProfile(BaseModel):
    id: int
    email: str
    tier: str
    api_key_fingerprint: str


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = user_store.get_user_by_email(email)
    if user is None:
        raise credentials_exception
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/v1/auth/register", response_model=Token, status_code=201)
async def register(request: Request, body: UserRegister):
    """Register a new user and generate their first free API key."""
    _enforce_auth_rate_limit(request)

    try:
        user = user_store.create_user(body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw_key = user_store.create_api_key(user.id, tier="free")

    # Clear caches so the new key is recognized immediately.
    _resolve_usage_key.cache_clear()
    get_tier_and_limit.cache_clear()
    get_api_key_created_at.cache_clear()

    access_token = create_access_token(data={"sub": user.email})
    logger.info("user_registered email=%s", user.email)
    return Token(access_token=access_token, token_type="bearer", api_key=raw_key)


@router.post("/v1/auth/login", response_model=Token)
async def login(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    """Login with email and password to get a JWT."""
    _enforce_auth_rate_limit(request)

    user = user_store.get_user_by_email(form_data.username)

    if not user or not user_store.verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": user.email})
    return Token(access_token=access_token, token_type="bearer")


@router.get("/v1/auth/me", response_model=UserProfile)
async def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    """Get the current user's profile and their active API key tier."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tier, fingerprint FROM api_keys WHERE user_id = ? AND revoked_at IS NULL LIMIT 1",
            (current_user.id,),
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No active API keys found.")

    return UserProfile(
        id=current_user.id,
        email=current_user.email,
        tier=row["tier"],
        api_key_fingerprint=row["fingerprint"],
    )


@router.get("/v1/auth/usage", response_model=UsageResponse)
async def get_dashboard_usage(current_user: Annotated[User, Depends(get_current_user)]):
    """Fetch usage for the dashboard using JWT auth."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT fingerprint FROM api_keys WHERE user_id = ? AND revoked_at IS NULL LIMIT 1",
            (current_user.id,),
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No active API keys found.")

    snap = snapshot(row["fingerprint"])
    return UsageResponse(**snap.to_dict())


@router.post("/v1/auth/keys", response_model=dict)
async def create_new_key(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Generate a new API key, enforcing tier limits."""
    _enforce_auth_rate_limit(request)

    tier = user_store.get_user_tier(current_user.id)
    max_keys = 1 if tier == "free" else (5 if tier == "pro" else 999999)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM api_keys WHERE user_id = ? AND revoked_at IS NULL",
            (current_user.id,),
        )
        row = cursor.fetchone()
        current_count = row["count"] if row else 0

    if current_count >= max_keys:
        raise HTTPException(
            status_code=400,
            detail=(
                f"API key limit reached for the {tier} tier "
                f"({max_keys} keys). Upgrade to generate more."
            ),
        )

    raw_key = user_store.create_api_key(current_user.id, tier=tier)

    _resolve_usage_key.cache_clear()
    get_tier_and_limit.cache_clear()
    get_api_key_created_at.cache_clear()

    return {"api_key": raw_key, "message": "Save this key. It will not be shown again."}


# FIX: New endpoint to revoke keys so users aren't locked out if they lose one.
@router.delete("/v1/auth/keys")
async def revoke_all_keys(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Revoke all active API keys so the user can generate a new one."""
    _enforce_auth_rate_limit(request)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = ? AND revoked_at IS NULL",
            (current_user.id,),
        )
        conn.commit()
        
    _resolve_usage_key.cache_clear()
    get_tier_and_limit.cache_clear()
    get_api_key_created_at.cache_clear()

    logger.info("user_revoked_keys user_id=%s", current_user.id)
    return {"status": "success", "message": "All API keys revoked. You can now generate a new one."}