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
from .userstore import User

logger = logging.getLogger("agenttrafficcop.billing")
router = APIRouter()


@router.get("/v1/billing/checkout")
async def get_checkout_url(current_user: Annotated[User, Depends(get_current_user)]):
    """Returns the Lemon Squeezy checkout URL with the user's ID prefilled."""
    if not settings.lemonsqueezy_checkout_url:
        raise HTTPException(status_code=500, detail="Billing not configured.")

    url = f"{settings.lemonsqueezy_checkout_url}?checkout[custom][user_id]={current_user.id}"
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
    except (json.JSONDecodeError, KeyError):
        raise HTTPException(status_code=400, detail="Invalid payload")

    logger.info("lemonsqueezy_webhook_received event=%s", event_name)

    if event_name == "subscription_created":
        user_id = data["meta"]["custom_data"].get("user_id")
        if user_id:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE api_keys SET tier = 'pro' WHERE user_id = ? AND revoked_at IS NULL",
                    (user_id,)
                )
                conn.commit()
            logger.info("user_upgraded_to_pro user_id=%s", user_id)

    elif event_name == "subscription_cancelled":
        user_id = data["meta"]["custom_data"].get("user_id")
        if user_id:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE api_keys SET tier = 'free' WHERE user_id = ? AND revoked_at IS NULL",
                    (user_id,)
                )
                conn.commit()
            logger.info("user_downgraded_to_free user_id=%s", user_id)

    return {"status": "success"}