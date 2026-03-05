"""
LINE Webhook Routes
Receives LINE webhook events and routes them to the LineAdapter.
"""

import json
import logging

from fastapi import APIRouter, Request, Response, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/line/webhook/{bot_id}")
async def line_webhook(bot_id: int, request: Request):
    """Receive LINE webhook events for a specific bot.

    LINE sends POST requests with:
    - Header: X-Line-Signature (HMAC-SHA256 of body with channel secret)
    - Body: JSON with 'destination' and 'events' array

    The webhook URL to configure in LINE Developer Console:
        https://your-domain.com/api/line/webhook/{bot_id}
    """
    from app.platforms.registry import platform_registry
    from app.platforms.base import PlatformType

    # Get LINE adapter
    if not platform_registry.is_registered(PlatformType.LINE):
        raise HTTPException(status_code=404, detail="LINE platform not registered")

    adapter = platform_registry.get_adapter(PlatformType.LINE)

    # Check if bot is active
    if not adapter.is_bot_active(bot_id):
        logger.warning(f"LINE webhook for inactive bot {bot_id}")
        raise HTTPException(status_code=404, detail="Bot not active")

    # Read raw body for signature validation
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature header")

    # Validate signature
    if not adapter.validate_signature(bot_id, body, signature):
        logger.warning(f"LINE webhook signature validation failed for bot {bot_id}")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Parse events from the already-read body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    events = data.get("events", [])

    # Empty events array = LINE webhook URL verification
    if not events:
        return Response(status_code=200)

    # Process events asynchronously
    await adapter.handle_webhook(bot_id, events)

    return Response(status_code=200)
