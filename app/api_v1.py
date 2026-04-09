"""
ChatHub Public API v1
REST API for third-party integration and white-label usage.
Authentication via API keys (separate from user JWT).
"""

import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.database import get_db, User, BotProfile, Conversation, Message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Public API v1"])


async def get_api_user(
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate via API key header."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    # TODO: Lookup API key in dedicated table
    # For now, validate against user's auth token as a placeholder
    raise HTTPException(status_code=401, detail="API key authentication not yet configured. Use JWT auth instead.")


@router.get("/bots")
async def list_bots_api(user: User = Depends(get_api_user), db: Session = Depends(get_db)):
    """List all bots for the authenticated user."""
    bots = db.query(BotProfile).filter(BotProfile.user_id == user.id).all()
    return {
        "bots": [
            {
                "id": b.id,
                "name": b.name,
                "platform_type": b.platform_type,
                "is_running": b.is_running,
            }
            for b in bots
        ]
    }


@router.post("/bots/{bot_id}/send")
async def send_message_api(
    bot_id: int,
    body: dict,
    user: User = Depends(get_api_user),
    db: Session = Depends(get_db),
):
    """Send a message via a bot (API access)."""
    from app.platforms.send import send_message

    bot = db.query(BotProfile).filter(BotProfile.id == bot_id, BotProfile.user_id == user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    chat_id = body.get("chat_id")
    message = body.get("message")
    if not chat_id or not message:
        raise HTTPException(status_code=400, detail="chat_id and message required")

    success = await send_message(
        bot_profile_id=bot.id,
        chat_id=chat_id,
        chat_name=body.get("chat_name", chat_id),
        message=message,
        platform_type=bot.platform_type or "whatsapp",
    )

    return {"success": success, "bot_id": bot_id, "chat_id": chat_id}


@router.get("/bots/{bot_id}/conversations")
async def list_conversations_api(
    bot_id: int,
    limit: int = 50,
    user: User = Depends(get_api_user),
    db: Session = Depends(get_db),
):
    """List conversations for a bot (API access)."""
    bot = db.query(BotProfile).filter(BotProfile.id == bot_id, BotProfile.user_id == user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    convs = db.query(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
    ).order_by(Conversation.last_message_at.desc().nullslast()).limit(limit).all()

    return {
        "conversations": [
            {
                "id": c.id,
                "chat_id": c.chat_id,
                "chat_name": c.chat_name,
                "is_group": c.is_group,
                "message_count": c.message_count or 0,
                "last_message_at": c.last_message_at.isoformat() + "Z" if c.last_message_at else None,
            }
            for c in convs
        ]
    }
