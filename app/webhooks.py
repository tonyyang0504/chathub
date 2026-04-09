"""
External Webhook Triggers
Allows external services (Zapier, Shopify, CRM, etc.) to trigger ChatHub actions
via HTTP POST with API key authentication.

Endpoints:
    POST /api/webhooks/{webhook_key}/trigger — trigger an action
    GET  /api/webhooks/keys — list webhook keys for current user
    POST /api/webhooks/keys — create a new webhook key
    DELETE /api/webhooks/keys/{key_id} — delete a webhook key
"""

import json
import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db, User, BotProfile, Conversation
from app.auth.utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


# ============== Webhook Key Management ==============

@router.get("/api/webhooks/keys")
async def list_webhook_keys(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all webhook keys for the current user."""
    from app.database import WebhookKey
    keys = db.query(WebhookKey).filter(WebhookKey.user_id == current_user.id).all()
    return {
        "keys": [
            {
                "id": k.id,
                "name": k.name,
                "key": k.key,
                "bot_id": k.bot_profile_id,
                "created_at": k.created_at.isoformat() + "Z" if k.created_at else None,
                "last_used_at": k.last_used_at.isoformat() + "Z" if k.last_used_at else None,
                "use_count": k.use_count or 0,
            }
            for k in keys
        ]
    }


@router.post("/api/webhooks/keys")
async def create_webhook_key(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new webhook key tied to a bot."""
    from app.database import WebhookKey

    bot_id = body.get("bot_id")
    name = body.get("name", "Webhook Key")

    if bot_id:
        bot = db.query(BotProfile).filter(
            BotProfile.id == bot_id, BotProfile.user_id == current_user.id
        ).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")

    key = WebhookKey(
        user_id=current_user.id,
        bot_profile_id=bot_id,
        name=name,
        key=f"whk_{secrets.token_urlsafe(32)}",
    )
    db.add(key)
    db.commit()
    db.refresh(key)

    return {
        "id": key.id,
        "name": key.name,
        "key": key.key,
        "bot_id": key.bot_profile_id,
        "webhook_url": f"/api/webhooks/{key.key}/trigger",
    }


@router.delete("/api/webhooks/keys/{key_id}")
async def delete_webhook_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a webhook key."""
    from app.database import WebhookKey
    key = db.query(WebhookKey).filter(
        WebhookKey.id == key_id, WebhookKey.user_id == current_user.id
    ).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    db.delete(key)
    db.commit()
    return {"success": True}


# ============== Webhook Trigger ==============

@router.post("/api/webhooks/{webhook_key}/trigger")
async def trigger_webhook(
    webhook_key: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a ChatHub action via webhook. No auth header needed — key is in URL.

    Payload examples:
        {"action": "send_message", "chat_id": "12345", "message": "Hello!"}
        {"action": "send_message", "phone": "+1234567890", "message": "Hello!"}
        {"action": "analyze_contact", "contact_id": 123}
        {"action": "send_to_all", "message": "Broadcast message"}
    """
    from app.database import WebhookKey
    from app.platforms.send import send_message

    # Validate webhook key
    key_record = db.query(WebhookKey).filter(WebhookKey.key == webhook_key).first()
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid webhook key")

    # Update usage stats
    key_record.last_used_at = datetime.utcnow()
    key_record.use_count = (key_record.use_count or 0) + 1
    db.commit()

    # Parse payload
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = payload.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="Missing 'action' field")

    bot_id = key_record.bot_profile_id or payload.get("bot_id")
    if not bot_id:
        raise HTTPException(status_code=400, detail="No bot_id configured for this key or in payload")

    bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Execute action
    if action == "send_message":
        chat_id = payload.get("chat_id") or payload.get("phone")
        message = payload.get("message")
        if not chat_id or not message:
            raise HTTPException(status_code=400, detail="send_message requires 'chat_id' (or 'phone') and 'message'")

        # Find conversation by chat_id or phone
        chat_name = payload.get("chat_name", chat_id)
        conv = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            (Conversation.chat_id == chat_id) | (Conversation.phone == chat_id)
        ).first()
        if conv:
            chat_name = conv.chat_name or chat_name

        success = await send_message(
            bot_profile_id=bot_id,
            chat_id=chat_id,
            chat_name=chat_name,
            message=message,
            platform_type=bot.platform_type or "whatsapp",
        )
        return {"success": success, "action": action, "chat_id": chat_id}

    elif action == "send_to_all":
        message = payload.get("message")
        if not message:
            raise HTTPException(status_code=400, detail="send_to_all requires 'message'")

        # Get all private conversations for this bot
        convs = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            Conversation.is_group == False,
        ).all()

        sent = 0
        for conv in convs:
            try:
                success = await send_message(
                    bot_profile_id=bot_id,
                    chat_id=conv.chat_id,
                    chat_name=conv.chat_name or conv.chat_id,
                    message=message,
                    platform_type=bot.platform_type or "whatsapp",
                )
                if success:
                    sent += 1
            except Exception:
                pass

        return {"success": True, "action": action, "sent": sent, "total": len(convs)}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
