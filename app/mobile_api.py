"""
Mobile API
REST endpoints for the ChatHub mobile companion app (React Native).
Provides push notification tokens, conversation sync, and voice input.
"""

import logging
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db, User, BotProfile, Conversation, Message
from app.auth.utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mobile", tags=["Mobile API"])


@router.post("/register-device")
async def register_device(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a mobile device for push notifications."""
    # TODO: Store device token (FCM for Android, APNS for iOS)
    device_token = body.get("device_token")
    platform = body.get("platform", "ios")  # ios or android

    if not device_token:
        raise HTTPException(status_code=400, detail="Missing device_token")

    logger.info(f"User {current_user.id}: Registered {platform} device")
    return {"success": True, "message": "Device registered"}


@router.get("/conversations")
async def get_mobile_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get conversations for mobile app (lightweight format)."""
    bots = db.query(BotProfile).filter(BotProfile.user_id == current_user.id).all()
    bot_ids = [b.id for b in bots]

    convs = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
    ).order_by(Conversation.last_message_at.desc().nullslast()).limit(50).all()

    return {
        "conversations": [
            {
                "id": c.id,
                "chat_name": c.chat_name or c.chat_id,
                "platform_type": next((b.platform_type for b in bots if b.id == c.bot_profile_id), "whatsapp"),
                "is_group": c.is_group,
                "message_count": c.message_count or 0,
                "last_message_at": c.last_message_at.isoformat() + "Z" if c.last_message_at else None,
                "dm_approved": getattr(c, 'dm_approved', True),
            }
            for c in convs
        ]
    }


@router.get("/conversations/{conv_id}/messages")
async def get_mobile_messages(
    conv_id: int,
    limit: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get messages for a conversation (mobile format)."""
    # Verify ownership
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    bot = db.query(BotProfile).filter(
        BotProfile.id == conv.bot_profile_id,
        BotProfile.user_id == current_user.id,
    ).first()
    if not bot:
        raise HTTPException(status_code=403, detail="Not authorized")

    messages = db.query(Message).filter(
        Message.conversation_id == conv_id,
    ).order_by(Message.timestamp.desc()).limit(limit).all()

    return {
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "sender_name": m.sender_name,
                "timestamp": m.timestamp.isoformat() + "Z" if m.timestamp else None,
            }
            for m in reversed(messages)
        ]
    }
