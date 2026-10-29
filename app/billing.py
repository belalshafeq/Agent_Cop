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
from .usage import get_tier_and_limit
from .userstore import User

logger = logging.getLogger("agenttrafficcop.billing")
router = APIRouter()


# FIX (C9): recognized events → target tier. Unknown events are acked,
# not silently dropped.
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

    url = (
        f"{settings.lemonsqueezy_checkout_url}"
        f"?checkout[custom][user_id]={current_user.id}"
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
        logger.info("lemonsqueezy_webhook_ignored event=%s", event_name)
        return {"status": "ignored", "event": event_name}

    # FIX (C9): safe .get chain + STRICT 500 on missing user_id.
    # Returning 200 hides the failure — the customer pays and stays on
    # free tier forever because nobody notices. 500 forces Lemon Squeezy
    # to retry AND makes the failure visible in any log-based alerting.
    #
    # If this fires repeatedly, the cause is almost always: the custom
    # field `checkout[custom][user_id]` hasn't been configured in the
    # Lemon Squeezy checkout dashboard.
    meta = data.get("meta") or {}
    custom = meta.get("custom_data") or {}
    raw_user_id = custom.get("user_id")

    if not raw_user_id:
        logger.error(
            "lemonsqueezy_webhook_missing_user_id event=%s — "
            "verify checkout[custom][user_id] is configured in the "
            "Lemon Squeezy dashboard",
            event_name,
        )
        raise HTTPException(
            status_code=500,
            detail="Webhook missing user_id — cannot process subscription event.",
        )

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        logger.error(
            "lemonsqueezy_webhook_bad_user_id event=%s raw=%r",
            event_name, raw_user_id,
        )
        raise HTTPException(status_code=500, detail="Webhook user_id is not an integer.")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_keys SET tier = ? WHERE user_id = ? AND revoked_at IS NULL",
            (new_tier, user_id),
        )
        conn.commit()

    # FIX: clear the LRU cache. Without this, a customer who was already
    # seen by get_tier_and_limit() stays cached on "free" until the
    # process restarts.
    try:
        get_tier_and_limit.cache_clear()
    except Exception:
        pass

    logger.info(
        "user_tier_updated user_id=%s event=%s tier=%s",
        user_id, event_name, new_tier,
    )
    return {"status": "success", "event": event_name}