"""Lemon Squeezy Billing integration."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .auth import get_current_user
from .config import settings
from .db import get_db
from .usage import get_tier_and_limit, _resolve_usage_key, get_api_key_created_at
from .userstore import User

logger = logging.getLogger("agenttrafficcop.billing")
router = APIRouter()


_TIER_FOR_EVENT = {
    "subscription_created":   "pro",
    "subscription_resumed":   "pro",
    "subscription_unpaused":  "pro",
    "subscription_cancelled": "free",
    "subscription_expired":   "free",
    "subscription_paused":    "free",
}


@router.get("/v1/billing/checkout")
async def get_checkout_url(current_user: Annotated[User, Depends(get_current_user)]):
    """Returns the Lemon Squeezy checkout URL with the user's ID prefilled."""
    if not settings.lemonsqueezy_checkout_url:
        raise HTTPException(status_code=500, detail="Billing not configured.")

    base_url = settings.lemonsqueezy_checkout_url
    separator = "&" if "?" in base_url else "?"
    
    # FIX 2: Append the success URL so the user is redirected back to the dashboard
    # with a ?upgraded=true flag, which triggers an immediate dashboard refresh.
    success_url = "https://agenttrafficcop.com/dashboard.html?upgraded=true"
    
    url = (
        f"{base_url}{separator}"
        f"checkout[custom][user_id]={current_user.id}&"
        f"checkout[success_url]={success_url}"
    )
    return {"checkout_url": url}


@router.post("/v1/billing/webhook")
async def lemonsqueezy_webhook(
    request: Request,
    x_signature: Annotated[str | None, Header()] = None,
):
    """Listen for Lemon Squeezy events and upgrade/downgrade the user."""
    if not settings.lemonsqueezy_webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured.")

    payload = await request.body()

    secret = settings.lemonsqueezy_webhook_secret.encode("utf-8")
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()

    if not x_signature or not hmac.compare_digest(digest, x_signature):
        logger.warning("lemonsqueezy_webhook_invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(payload)
        event_name = data["meta"]["event_name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid payload")

    logger.info("lemonsqueezy_webhook_received event=%s", event_name)

    new_tier = _TIER_FOR_EVENT.get(event_name)
    if new_tier is None:
        return {"status": "ignored", "event": event_name}

    meta = data.get("meta") or {}
    custom = meta.get("custom_data") or {}
    raw_user_id = custom.get("user_id")

    if not raw_user_id:
        logger.error("lemonsqueezy_webhook_missing_user_id event=%s", event_name)
        raise HTTPException(status_code=500, detail="Webhook missing user_id")

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=500, detail="Webhook user_id is not an integer.")

    with get_db() as conn:
        cursor = conn.cursor()
        
        # Update tier
        cursor.execute(
            "UPDATE api_keys SET tier = ? WHERE user_id = ? AND revoked_at IS NULL",
            (new_tier, user_id),
        )
        
        # FIX 1: If they downgrade to free, revoke all keys except the oldest one.
        # This prevents them from keeping 5 Pro keys and using them on the Free tier.
        if new_tier == "free":
            cursor.execute(
                "SELECT id FROM api_keys WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at ASC",
                (user_id,)
            )
            rows = cursor.fetchall()
            if len(rows) > 1:
                keep_id = rows[0]["id"]
                revoke_ids = [r["id"] for r in rows[1:]]
                placeholders = ",".join(["?"] * len(revoke_ids))
                cursor.execute(
                    f"UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                    revoke_ids
                )
                logger.info("revoked_extra_keys user_id=%s count=%d", user_id, len(revoke_ids))
                
        conn.commit()

    # Clear all caches so the new tier and key revocations take effect immediately
    try:
        get_tier_and_limit.cache_clear()
        _resolve_usage_key.cache_clear()
        get_api_key_created_at.cache_clear()
    except Exception:
        pass

    logger.info("user_tier_updated user_id=%s event=%s tier=%s", user_id, event_name, new_tier)
    return {"status": "success", "event": event_name}