"""Authentication routes: Register, Login, and Key Management."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr

from .config import settings
from .db import get_db
from .userstore import user_store, User

logger = logging.getLogger("agenttrafficcop.auth")
router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class UserRegister(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str
    api_key: str | None = None  # Only returned on registration


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
async def register(body: UserRegister):
    """Register a new user and generate their first free API key."""
    try:
        user = user_store.create_user(body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Generate their first API key
    raw_key = user_store.create_api_key(user.id, tier="free")
    
    # Create JWT
    access_token = create_access_token(data={"sub": user.email})
    
    return Token(access_token=access_token, token_type="bearer", api_key=raw_key)


@router.post("/v1/auth/login", response_model=Token)
async def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    """Login with email and password to get a JWT."""
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
            (current_user.id,)
        )
        row = cursor.fetchone()
        
    if not row:
        raise HTTPException(status_code=404, detail="No active API keys found.")
        
    return UserProfile(
        id=current_user.id,
        email=current_user.email,
        tier=row["tier"],
        api_key_fingerprint=row["fingerprint"]
    )


@router.post("/v1/auth/keys", response_model=dict)
async def create_new_key(current_user: Annotated[User, Depends(get_current_user)]):
    """Generate a new API key (e.g., for a new environment)."""
    raw_key = user_store.create_api_key(current_user.id, tier="free")
    return {"api_key": raw_key, "message": "Save this key. It will not be shown again."}