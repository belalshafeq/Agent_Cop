"""User management: creation, lookup, and password verification."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from passlib.context import CryptContext

from .db import get_db

logger = logging.getLogger("agenttrafficcop.userstore")

# Password hashing configuration
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@dataclass(frozen=True)
class User:
    id: int
    email: str
    password_hash: str


class UserStore:
    """Data access object for users."""

    def create_user(self, email: str, password: str) -> User:
        """Create a new user. Raises ValueError if email exists."""
        email = email.lower().strip()
        password_hash = pwd_context.hash(password)
        
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                    (email, password_hash)
                )
                conn.commit()
                user_id = cursor.lastrowid
                
            logger.info("user_created email=%s", email)
            return User(id=user_id, email=email, password_hash=password_hash)
            
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                raise ValueError("Email already registered") from e
            raise

    def get_user_by_email(self, email: str) -> Optional[User]:
        """Fetch a user by email. Returns None if not found."""
        email = email.lower().strip()
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, email, password_hash FROM users WHERE email = ?",
                (email,)
            )
            row = cursor.fetchone()
            
        if row is None:
            return None
            
        return User(
            id=row["id"],
            email=row["email"],
            password_hash=row["password_hash"]
        )

    def get_user_tier(self, user_id: int) -> str:
        """Fetch the tier of a user's active key."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tier FROM api_keys WHERE user_id = ? AND revoked_at IS NULL LIMIT 1",
                (user_id,)
            )
            row = cursor.fetchone()
            return row["tier"] if row else "free"

    def create_api_key(self, user_id: int, tier: str = "free") -> str:
        """Generate a new API key, save the hash to DB, return raw key exactly once."""
        import secrets
        import hashlib
        
        raw_key = f"atc_live_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        fingerprint = key_hash[:8]
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO api_keys (key_hash, fingerprint, user_id, tier) 
                VALUES (?, ?, ?, ?)
                """,
                (key_hash, fingerprint, user_id, tier)
            )
            conn.commit()
            
        logger.info("api_key_created user_id=%s tier=%s fingerprint=%s", user_id, tier, fingerprint)
        return raw_key

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a password against a hash."""
        # FIX: If `hashed_password` is malformed (someone inserted a bad row,
        # DB migration glitch), passlib raises ValueError → 500 on login.
        # A malformed user record should fail auth cleanly, not crash.
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except (ValueError, TypeError):
            logger.warning("password_verify_bad_hash")
            return False


# Singleton instance
user_store = UserStore()