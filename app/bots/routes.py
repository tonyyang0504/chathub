"""
Bot Management Routes
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

logger = logging.getLogger(__name__)

# Get base directory for session storage (consistent with whatsapp_bot.py)
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # python_bot directory
SESSIONS_DIR = BASE_DIR / "data" / "sessions"

from app.config import settings
from app.database import get_db, User, BotProfile, Conversation, Message
from app.auth.utils import get_current_user, encrypt_string, decrypt_string, get_websocket_user
from app.bots.schemas import (
    BotProfileCreate,
    BotProfileUpdate,
    BotProfileResponse,
    BotStatusResponse,
    BotListResponse
)
from app.bots.manager import bot_manager

router = APIRouter(tags=["Bots"])


# ============== Helper Functions ==============

def get_bot_profile(
    bot_id: int,
    user: User,
    db: Session
) -> BotProfile:
    """Get bot profile and verify ownership."""
    bot = db.query(BotProfile).filter(
        BotProfile.id == bot_id,
        BotProfile.user_id == user.id
    ).first()

    if not bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bot profile not found"
        )

    return bot


def mask_api_key(encrypted_key: str) -> Optional[str]:
    """
    Decrypt and mask API key for display.
    Shows first 8 and last 4 characters: sk-proj-...gasA
    """
    if not encrypted_key:
        return None
    try:
        decrypted = decrypt_string(encrypted_key)
        if not decrypted or len(decrypted) < 12:
            return None
        # Show first 8 chars and last 4 chars
        return f"{decrypted[:8]}...{decrypted[-4:]}"
    except Exception:
        return None


def bot_to_response(bot: BotProfile, db: Session) -> BotProfileResponse:
    """Convert BotProfile to response schema with counts."""
    # Get conversation count
    conv_count = db.query(func.count(Conversation.id)).filter(
        Conversation.bot_profile_id == bot.id
    ).scalar() or 0

    # Get message count
    msg_count = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id == bot.id
    ).scalar() or 0

    return BotProfileResponse(
        id=bot.id,
        name=bot.name,
        platform_type=bot.platform_type or "whatsapp",
        ai_provider=bot.ai_provider or "openai",
        api_key_masked=mask_api_key(bot.api_key_encrypted),
        model=bot.model,
        system_prompt=bot.system_prompt or "",
        temperature=bot.temperature if bot.temperature is not None else 0.7,
        max_tokens=bot.max_tokens if bot.max_tokens is not None else 1000,
        top_p=bot.top_p if bot.top_p is not None else 1.0,
        frequency_penalty=bot.frequency_penalty if bot.frequency_penalty is not None else 0.0,
        presence_penalty=bot.presence_penalty if bot.presence_penalty is not None else 0.0,
        max_history=bot.max_history,
        response_delay_min=bot.response_delay_min,
        response_delay_max=bot.response_delay_max,
        group_chat_enabled=bot.group_chat_enabled,
        respond_to_all_in_group=bot.respond_to_all_in_group,
        ending_detection_enabled=bot.ending_detection_enabled if bot.ending_detection_enabled is not None else False,
        headless=bot.headless if bot.headless is not None else False,
        # Proxy Settings
        proxy_enabled=bot.proxy_enabled if bot.proxy_enabled is not None else False,
        proxy_url=bot.proxy_url,
        is_active=bot.is_active,
        is_running=bot.is_running,
        whatsapp_connected=bot.whatsapp_connected,
        last_active=bot.last_active,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
        conversation_count=conv_count,
        message_count=msg_count,
        # WhatsApp Account Info
        whatsapp_phone=bot.whatsapp_phone,
        whatsapp_name=bot.whatsapp_name,
        whatsapp_push_name=bot.whatsapp_push_name,
        whatsapp_profile_pic=bot.whatsapp_profile_pic,
        whatsapp_about=bot.whatsapp_about,
        whatsapp_account_type=bot.whatsapp_account_type or "personal"
    )


# ============== Provider Info ==============

@router.get("/providers/info")
async def get_providers_info():
    """Return available AI providers with their models and capabilities."""
    from app.ai.factory import get_available_providers, DEFAULT_MODELS
    from app.ai.providers.openai_provider import OpenAIProvider
    from app.ai.providers.anthropic_provider import AnthropicProvider
    from app.ai.providers.google_provider import GoogleProvider
    from app.ai.providers.deepseek_provider import DeepSeekProvider
    from app.ai.providers.qwen_provider import QwenProvider

    provider_classes = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "google": GoogleProvider,
        "deepseek": DeepSeekProvider,
        "qwen": QwenProvider,
    }

    providers = {}
    for name, cls in provider_classes.items():
        providers[name] = {
            "models": getattr(cls, 'MODELS', []),
            "default_model": DEFAULT_MODELS.get(name),
            "supports_tools": cls.supports_tools,
            "supports_vision": cls.supports_vision,
        }

    return {"providers": providers}


# ============== CRUD Routes ==============

@router.get("", response_model=BotListResponse)
async def list_bots(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all bot profiles for current user."""
    bots = db.query(BotProfile).filter(
        BotProfile.user_id == current_user.id
    ).order_by(BotProfile.created_at.desc()).all()

    return BotListResponse(
        bots=[bot_to_response(bot, db) for bot in bots],
        total=len(bots)
    )


@router.post("/check-and-recover")
async def check_and_recover_bots(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Check all user's bots and recover any that should be running but aren't."""
    # Get all bots for this user that are marked as running
    bots = db.query(BotProfile).filter(
        BotProfile.user_id == current_user.id,
        BotProfile.is_running == True
    ).all()

    recovered = []
    already_running = []
    failed = []

    for bot in bots:
        try:
            # Check if bot needs recovery
            if bot_manager.needs_recovery(bot.id, bot.is_running):
                logger.info(f"Bot {bot.id} ({bot.name}) needs recovery")

                # Build config for recovery
                api_key = decrypt_string(bot.api_key_encrypted) if bot.api_key_encrypted else None

                if not api_key:
                    logger.warning(f"Bot {bot.id}: No API key, marking as stopped")
                    bot.is_running = False
                    db.commit()
                    failed.append({"id": bot.id, "name": bot.name, "reason": "No API key"})
                    continue

                config = {
                    "bot_profile_id": bot.id,
                    "ai_provider": bot.ai_provider or "openai",
                    "api_key": api_key,
                    "model": bot.model or "gpt-4o-mini",
                    "system_prompt": bot.system_prompt,
                    "temperature": bot.temperature if bot.temperature is not None else 0.7,
                    "max_tokens": bot.max_tokens if bot.max_tokens is not None else 1000,
                    "top_p": bot.top_p if bot.top_p is not None else 1.0,
                    "frequency_penalty": bot.frequency_penalty if bot.frequency_penalty is not None else 0.0,
                    "presence_penalty": bot.presence_penalty if bot.presence_penalty is not None else 0.0,
                    "max_history": bot.max_history or 20,
                    "response_delay_min": bot.response_delay_min or 3,
                    "response_delay_max": bot.response_delay_max or 8,
                    "group_chat_enabled": bot.group_chat_enabled if bot.group_chat_enabled is not None else True,
                    "headless": bot.headless if bot.headless is not None else False,
                    "browser_timezone": bot.browser_timezone or 'UTC',
                }

                # Add proxy settings if enabled
                if bot.proxy_enabled and bot.proxy_url:
                    config["proxy_enabled"] = True
                    config["proxy_url"] = bot.proxy_url
                    if bot.proxy_username:
                        config["proxy_username"] = decrypt_string(bot.proxy_username)
                    if bot.proxy_password:
                        config["proxy_password"] = decrypt_string(bot.proxy_password)

                # Recover the bot
                await bot_manager.recover_bot(bot.id, config)
                recovered.append({"id": bot.id, "name": bot.name})

            else:
                # Bot is already running fine
                already_running.append({"id": bot.id, "name": bot.name})

        except Exception as e:
            logger.error(f"Error recovering bot {bot.id}: {e}")
            failed.append({"id": bot.id, "name": bot.name, "reason": str(e)})

    return {
        "recovered": recovered,
        "already_running": already_running,
        "failed": failed,
        "message": f"Recovered {len(recovered)} bot(s), {len(already_running)} already running, {len(failed)} failed"
    }


@router.get("/{bot_id}/health")
async def get_bot_health(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get health status of a specific bot."""
    bot = get_bot_profile(bot_id, current_user, db)

    health = bot_manager.get_health_status(bot_id)
    health["db_is_running"] = bot.is_running
    health["db_whatsapp_connected"] = bot.whatsapp_connected

    return health


@router.post("", response_model=BotProfileResponse)
async def create_bot(
    bot_data: BotProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new bot profile."""
    # Encrypt API key
    encrypted_key = encrypt_string(bot_data.api_key)

    # Encrypt proxy credentials if provided
    encrypted_proxy_username = encrypt_string(bot_data.proxy_username) if bot_data.proxy_username else None
    encrypted_proxy_password = encrypt_string(bot_data.proxy_password) if bot_data.proxy_password else None

    bot = BotProfile(
        user_id=current_user.id,
        name=bot_data.name,
        platform_type=bot_data.platform_type,
        ai_provider=bot_data.ai_provider,
        api_key_encrypted=encrypted_key,
        model=bot_data.model,
        system_prompt=bot_data.system_prompt,
        temperature=bot_data.temperature,
        max_tokens=bot_data.max_tokens,
        top_p=bot_data.top_p,
        frequency_penalty=bot_data.frequency_penalty,
        presence_penalty=bot_data.presence_penalty,
        max_history=bot_data.max_history,
        response_delay_min=bot_data.response_delay_min,
        response_delay_max=bot_data.response_delay_max,
        group_chat_enabled=bot_data.group_chat_enabled,
        respond_to_all_in_group=bot_data.respond_to_all_in_group,
        ending_detection_enabled=bot_data.ending_detection_enabled,
        headless=bot_data.headless,
        # Proxy settings
        proxy_enabled=bot_data.proxy_enabled,
        proxy_url=bot_data.proxy_url,
        proxy_username=encrypted_proxy_username,
        proxy_password=encrypted_proxy_password,
        is_active=True
    )

    db.add(bot)
    db.commit()
    db.refresh(bot)

    return bot_to_response(bot, db)


@router.get("/{bot_id}", response_model=BotProfileResponse)
async def get_bot(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a specific bot profile."""
    bot = get_bot_profile(bot_id, current_user, db)
    return bot_to_response(bot, db)


@router.put("/{bot_id}", response_model=BotProfileResponse)
async def update_bot(
    bot_id: int,
    bot_data: BotProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a bot profile."""
    bot = get_bot_profile(bot_id, current_user, db)

    # Update fields if provided
    if bot_data.name is not None:
        bot.name = bot_data.name
    if bot_data.platform_type is not None:
        bot.platform_type = bot_data.platform_type
    if bot_data.ai_provider is not None:
        bot.ai_provider = bot_data.ai_provider
    if bot_data.api_key is not None:
        bot.api_key_encrypted = encrypt_string(bot_data.api_key)
    if bot_data.model is not None:
        bot.model = bot_data.model
    if bot_data.system_prompt is not None:
        bot.system_prompt = bot_data.system_prompt
    if bot_data.temperature is not None:
        bot.temperature = bot_data.temperature
    if bot_data.max_tokens is not None:
        bot.max_tokens = bot_data.max_tokens
    if bot_data.top_p is not None:
        bot.top_p = bot_data.top_p
    if bot_data.frequency_penalty is not None:
        bot.frequency_penalty = bot_data.frequency_penalty
    if bot_data.presence_penalty is not None:
        bot.presence_penalty = bot_data.presence_penalty
    if bot_data.max_history is not None:
        bot.max_history = bot_data.max_history
    if bot_data.response_delay_min is not None:
        bot.response_delay_min = bot_data.response_delay_min
    if bot_data.response_delay_max is not None:
        bot.response_delay_max = bot_data.response_delay_max
    if bot_data.group_chat_enabled is not None:
        bot.group_chat_enabled = bot_data.group_chat_enabled
    if bot_data.respond_to_all_in_group is not None:
        bot.respond_to_all_in_group = bot_data.respond_to_all_in_group
    if bot_data.ending_detection_enabled is not None:
        bot.ending_detection_enabled = bot_data.ending_detection_enabled
    if bot_data.headless is not None:
        bot.headless = bot_data.headless
    # Proxy settings
    if bot_data.proxy_enabled is not None:
        bot.proxy_enabled = bot_data.proxy_enabled
    if bot_data.proxy_url is not None:
        bot.proxy_url = bot_data.proxy_url
    if bot_data.proxy_username is not None:
        bot.proxy_username = encrypt_string(bot_data.proxy_username) if bot_data.proxy_username else None
    if bot_data.proxy_password is not None:
        bot.proxy_password = encrypt_string(bot_data.proxy_password) if bot_data.proxy_password else None

    db.commit()
    db.refresh(bot)

    return bot_to_response(bot, db)


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a bot profile."""
    bot = get_bot_profile(bot_id, current_user, db)

    # Stop bot if running
    if bot.is_running:
        await bot_manager.stop_bot(bot_id)

    db.delete(bot)
    db.commit()

    return {"message": "Bot deleted successfully"}


# ============== Bot Control Routes ==============

@router.post("/{bot_id}/start")
async def start_bot(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start a bot instance."""
    logger.info(f"HTTP POST /api/bots/{bot_id}/start received")
    try:
        bot = get_bot_profile(bot_id, current_user, db)
        logger.info(f"Bot {bot_id} found, is_running={bot.is_running}")

        if bot.is_running:
            logger.warning(f"Bot {bot_id} is already running")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bot is already running"
            )

        # Check if this bot's WhatsApp account is already used by another running bot
        if bot.whatsapp_phone:
            existing_bot = db.query(BotProfile).filter(
                BotProfile.whatsapp_phone == bot.whatsapp_phone,
                BotProfile.id != bot_id,
                BotProfile.is_running == True
            ).first()

            if existing_bot:
                logger.warning(f"WhatsApp phone {bot.whatsapp_phone} is already used by bot {existing_bot.id}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"This WhatsApp account is already in use by another bot: {existing_bot.name}"
                )
        else:
            # Bot has never connected to WhatsApp - ensure fresh session
            # Clear any existing session data to prevent inheriting another bot's session
            session_path = str(SESSIONS_DIR / f"bot_{bot_id}")
            if os.path.exists(session_path):
                try:
                    shutil.rmtree(session_path)
                    logger.info(f"Cleared session directory for new bot {bot_id} to ensure fresh QR code")
                except Exception as e:
                    logger.warning(f"Failed to clear session directory for bot {bot_id}: {e}")

        # Prepare config
        config = {
            "platform_type": bot.platform_type or "whatsapp",
            "ai_provider": bot.ai_provider or "openai",
            "api_key_encrypted": bot.api_key_encrypted,
            "model": bot.model,
            "system_prompt": bot.system_prompt,
            "temperature": bot.temperature if bot.temperature is not None else 0.7,
            "max_tokens": bot.max_tokens if bot.max_tokens is not None else 1000,
            "top_p": bot.top_p if bot.top_p is not None else 1.0,
            "frequency_penalty": bot.frequency_penalty if bot.frequency_penalty is not None else 0.0,
            "presence_penalty": bot.presence_penalty if bot.presence_penalty is not None else 0.0,
            "max_history": bot.max_history,
            "response_delay_min": bot.response_delay_min,
            "response_delay_max": bot.response_delay_max,
            "group_chat_enabled": bot.group_chat_enabled,
            "respond_to_all_in_group": bot.respond_to_all_in_group,
            "ending_detection_enabled": bot.ending_detection_enabled if bot.ending_detection_enabled is not None else False,
            "headless": bot.headless if bot.headless is not None else False,
            "browser_timezone": bot.browser_timezone or 'UTC',
            # Proxy settings
            "proxy_enabled": bot.proxy_enabled if bot.proxy_enabled is not None else False,
            "proxy_url": bot.proxy_url,
            "proxy_username": bot.proxy_username,  # Encrypted
            "proxy_password": bot.proxy_password   # Encrypted
        }

        # Check if bot has existing session (doesn't need QR scan)
        session_path = SESSIONS_DIR / f"bot_{bot_id}"
        has_session = session_path.exists() and any(session_path.iterdir()) if session_path.exists() else False
        needs_qr_scan = not has_session or not bot.whatsapp_phone
        logger.info(f"Bot {bot_id}: has_session={has_session}, whatsapp_phone={bot.whatsapp_phone}, needs_qr_scan={needs_qr_scan}")

        # Start bot
        logger.info(f"Starting bot {bot_id}...")
        instance = await bot_manager.start_bot(bot_id, config)

        # Clear any cached QR code from previous sessions to show fresh placeholder
        instance.qr_code = None
        logger.info(f"Cleared cached QR code for bot {bot_id}")

        # Update database
        bot.is_running = True
        db.commit()
        logger.info(f"Bot {bot_id} started successfully, returning HTTP 200")

        return {
            "message": "Bot started",
            "status": bot_manager.get_status(bot_id),
            "needs_qr_scan": needs_qr_scan
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting bot {bot_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start bot: {str(e)}"
        )


@router.post("/{bot_id}/stop")
async def stop_bot(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Stop a bot instance."""
    bot = get_bot_profile(bot_id, current_user, db)

    if not bot.is_running:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bot is not running"
        )

    # Stop bot
    await bot_manager.stop_bot(bot_id)

    # Update database
    bot.is_running = False
    bot.whatsapp_connected = False
    db.commit()

    return {"message": "Bot stopped"}


@router.post("/{bot_id}/disconnect")
async def disconnect_whatsapp(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Disconnect WhatsApp from a bot and clear the session.
    This allows the bot to show a fresh QR code on next start.
    """
    bot = get_bot_profile(bot_id, current_user, db)

    # Stop bot if running
    if bot.is_running:
        await bot_manager.stop_bot(bot_id)
        bot.is_running = False
        # Wait for browser to fully close and release file locks
        import asyncio
        await asyncio.sleep(2)

    # Clear WhatsApp account info
    bot.whatsapp_connected = False
    bot.whatsapp_phone = None
    bot.whatsapp_name = None
    bot.whatsapp_push_name = None
    bot.whatsapp_profile_pic = None
    bot.whatsapp_about = None
    bot.whatsapp_account_type = None

    # Clear conversations and messages for this bot
    conversations = db.query(Conversation).filter(Conversation.bot_profile_id == bot_id).all()
    message_count = 0
    for conv in conversations:
        msg_deleted = db.query(Message).filter(Message.conversation_id == conv.id).delete()
        message_count += msg_deleted
    conv_deleted = db.query(Conversation).filter(Conversation.bot_profile_id == bot_id).delete()
    logger.info(f"Cleared {conv_deleted} conversations and {message_count} messages for bot {bot_id}")

    # Clear session directory to force new QR code (with retry)
    session_path = str(SESSIONS_DIR / f"bot_{bot_id}")
    session_cleared = False
    if os.path.exists(session_path):
        # Try multiple times with delays (browser may take time to release locks)
        import asyncio
        for attempt in range(3):
            try:
                shutil.rmtree(session_path)
                logger.info(f"Cleared session directory for bot {bot_id}: {session_path}")
                session_cleared = True
                break
            except Exception as e:
                logger.warning(f"Attempt {attempt+1}/3: Failed to clear session for bot {bot_id}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1)  # Wait before retry

        if not session_cleared:
            # Session couldn't be cleared, but continue anyway - it will be cleared on next start
            logger.warning(f"Could not clear session for bot {bot_id}, will be cleared on next start")

    db.commit()

    return {"message": "WhatsApp disconnected. The bot will show a QR code on next start."}


@router.get("/{bot_id}/status")
async def get_bot_status(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get bot status including runtime toggle states."""
    bot = get_bot_profile(bot_id, current_user, db)

    response = {
        "id": bot.id,
        "name": bot.name,
        "is_running": bot.is_running,
        "whatsapp_connected": bot.whatsapp_connected,
        "last_active": bot.last_active.isoformat() if bot.last_active else None,
    }

    # Include runtime toggle state from in-memory instance
    instance = bot_manager.get_instance(bot_id)
    if instance:
        response["ai_response_enabled"] = instance.ai_response_enabled
        response["history_sync_active"] = instance.history_sync_active
        response["history_sync_progress"] = instance.history_sync_progress
    else:
        response["ai_response_enabled"] = False
        response["history_sync_active"] = False
        response["history_sync_progress"] = {"total": 0, "completed": 0, "current_chat": "", "status": "idle"}

    return response


@router.post("/{bot_id}/toggle-ai")
async def toggle_ai_response(
    bot_id: int,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Toggle AI auto-response on/off for a running bot."""
    bot = get_bot_profile(bot_id, current_user, db)

    if not bot.is_running:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bot is not running"
        )

    instance = bot_manager.get_instance(bot_id)
    if not instance or not instance.whatsapp_connected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bot is not connected to WhatsApp"
        )

    enabled = body.get("enabled", False)
    instance.ai_response_enabled = bool(enabled)
    logger.info(f"Bot {bot_id}: AI response toggled to {instance.ai_response_enabled}")

    return {"success": True, "ai_response_enabled": instance.ai_response_enabled}


@router.post("/{bot_id}/sync-history")
async def start_history_sync(
    bot_id: int,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start a one-time history sync for all conversations."""
    bot = get_bot_profile(bot_id, current_user, db)

    if not bot.is_running:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bot is not running"
        )

    instance = bot_manager.get_instance(bot_id)
    if not instance or not instance.whatsapp_connected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bot is not connected to WhatsApp"
        )

    if instance.history_sync_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="History sync is already running"
        )

    sync_all = body.get("sync_all", False)
    if sync_all:
        # Use -1 to indicate sync all messages
        message_count = -1
    else:
        message_count = body.get("message_count", 50)
        message_count = max(10, min(9999, int(message_count)))

    instance.history_sync_count = message_count
    instance.history_sync_requested = True
    instance.history_sync_stop_requested = False  # Reset stop flag for new sync
    log_msg = "all messages" if sync_all else f"count={message_count}"
    logger.info(f"Bot {bot_id}: History sync requested with {log_msg}")

    return {"success": True, "message": "History sync started"}


@router.get("/{bot_id}/sync-history/status")
async def get_history_sync_status(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get the current history sync progress."""
    bot = get_bot_profile(bot_id, current_user, db)

    instance = bot_manager.get_instance(bot_id)
    if not instance:
        return {"total": 0, "completed": 0, "current_chat": "", "status": "idle"}

    return instance.history_sync_progress


@router.post("/{bot_id}/sync-history/stop")
async def stop_history_sync(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Stop an ongoing history sync."""
    bot = get_bot_profile(bot_id, current_user, db)

    instance = bot_manager.get_instance(bot_id)
    if not instance:
        return {"success": False, "message": "Bot instance not found"}

    if not instance.history_sync_active:
        return {"success": False, "message": "No active sync to stop"}

    instance.history_sync_stop_requested = True
    logger.info(f"Bot {bot_id}: History sync stop requested")

    return {"success": True, "message": "Sync stop requested"}


@router.get("/{bot_id}/analytics/daily")
async def get_bot_daily_analytics(
    bot_id: int,
    days: int = 7,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get daily analytics for a specific bot."""
    bot = get_bot_profile(bot_id, current_user, db)

    # Daily stats - latest date first
    daily_stats = []
    for i in range(days):
        date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        next_date = date + timedelta(days=1)

        sent = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            Message.role == "assistant",
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        received = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            Message.role == "user",
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        # Active chats
        active_chats = db.query(func.count(func.distinct(Conversation.id))).join(Message).filter(
            Conversation.bot_profile_id == bot_id,
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        # New conversations
        new_convs = db.query(func.count(Conversation.id)).filter(
            Conversation.bot_profile_id == bot_id,
            Conversation.created_at >= date,
            Conversation.created_at < next_date
        ).scalar() or 0

        daily_stats.append({
            "date": date.strftime("%Y-%m-%d"),
            "messages_sent": sent,
            "messages_received": received,
            "active_chats": active_chats,
            "new_conversations": new_convs
        })

    # Hourly stats (for today)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    hourly_stats = []
    for hour in range(24):
        hour_start = today + timedelta(hours=hour)
        hour_end = hour_start + timedelta(hours=1)

        count = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            Message.timestamp >= hour_start,
            Message.timestamp < hour_end
        ).scalar() or 0
        hourly_stats.append(count)

    # Message types
    total_received = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
        Message.role == "user"
    ).scalar() or 0

    total_sent = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
        Message.role == "assistant"
    ).scalar() or 0

    return {
        "daily_stats": daily_stats,
        "hourly_stats": hourly_stats,
        "message_types": {"received": total_received, "sent": total_sent}
    }


# ============== WebSocket for QR Code ==============

@router.websocket("/{bot_id}/qr")
async def websocket_qr(
    websocket: WebSocket,
    bot_id: int
):
    """WebSocket endpoint for QR code streaming."""
    logger.info(f"WebSocket connection attempt for bot {bot_id}")

    # Authenticate the WebSocket connection before accepting
    db = next(get_db())
    try:
        # We need to accept first to read cookies, but we'll close immediately if auth fails
        await websocket.accept()
        logger.info(f"WebSocket accepted for bot {bot_id}")

        user = await get_websocket_user(websocket, db)
        if not user:
            logger.warning(f"WebSocket QR: Authentication failed for bot {bot_id}")
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=4001, reason="Authentication required")
            return

        # Verify bot ownership
        bot = db.query(BotProfile).filter(
            BotProfile.id == bot_id,
            BotProfile.user_id == user.id
        ).first()

        if not bot:
            logger.warning(f"WebSocket QR: Bot {bot_id} not found for user {user.id}")
            await websocket.send_json({"error": "Bot not found"})
            await websocket.close(code=4004, reason="Bot not found")
            return

        instance = None
        on_qr = None
        on_status = None

        # Wait a moment for bot instance to be created
        logger.info(f"Waiting for bot instance {bot_id}...")
        for i in range(10):
            instance = bot_manager.get_instance(bot_id)
            if instance:
                logger.info(f"Found bot instance {bot_id} after {i+1} attempts")
                break
            await asyncio.sleep(0.5)

        if not instance:
            logger.error(f"Bot instance {bot_id} not found after 10 attempts")
            await websocket.send_json({"error": "Bot not found or not started. Please try starting the bot again."})
            return

        try:
            # Add callback for QR updates
            async def on_qr(qr_code: str):
                try:
                    logger.info(f"on_qr callback triggered for bot {bot_id}, sending to WebSocket")
                    await websocket.send_json({"qr_code": qr_code})
                    logger.info(f"QR code sent via WebSocket for bot {bot_id}")
                except Exception as e:
                    logger.error(f"QR send error for bot {bot_id}: {e}")

            async def on_status(status: dict):
                try:
                    logger.info(f"on_status callback triggered for bot {bot_id}: {status.get('message', status)}")
                    await websocket.send_json({"status": status})
                except Exception as e:
                    logger.error(f"Status send error for bot {bot_id}: {e}")

            instance.add_qr_callback(on_qr)
            instance.add_status_callback(on_status)
            logger.info(f"WebSocket callbacks registered for bot {bot_id}. QR callbacks: {len(instance.qr_callbacks)}, Status callbacks: {len(instance.status_callbacks)}")

            # Send initial status
            await websocket.send_json({
                "status": {
                    "is_running": instance.is_running,
                    "whatsapp_connected": instance.whatsapp_connected,
                    "message": "Connecting to bot..."
                }
            })
            logger.info(f"Initial status sent for bot {bot_id}")

            # Send last status if available
            if instance.last_status:
                logger.info(f"Sending last status for bot {bot_id}: {instance.last_status}")
                await websocket.send_json({"status": instance.last_status})

            # Send current QR if available (this catches QR codes detected before WebSocket connected)
            if instance.qr_code:
                logger.info(f"Sending existing QR code to WebSocket for bot {bot_id}, length: {len(instance.qr_code)}")
                await websocket.send_json({"qr_code": instance.qr_code})

            # Keep connection alive
            logger.info(f"WebSocket entering message loop for bot {bot_id}")
            while True:
                try:
                    data = await websocket.receive_text()
                    if data == "ping":
                        await websocket.send_text("pong")
                except WebSocketDisconnect:
                    logger.info(f"WebSocket disconnected for bot {bot_id}")
                    break

        except Exception as e:
            logger.error(f"WebSocket error for bot {bot_id}: {e}", exc_info=True)
            try:
                await websocket.send_json({"error": str(e)})
            except:
                pass
        finally:
            logger.info(f"WebSocket cleanup for bot {bot_id}")
            if instance:
                if on_qr:
                    try:
                        instance.remove_qr_callback(on_qr)
                        logger.info(f"Removed QR callback for bot {bot_id}")
                    except:
                        pass
                if on_status:
                    try:
                        instance.remove_status_callback(on_status)
                        logger.info(f"Removed status callback for bot {bot_id}")
                    except:
                        pass
            try:
                await websocket.close()
            except:
                pass
    finally:
        db.close()


# ============== Bot Contacts and Groups for Schedule Content ==============
# NOTE: Static routes (/all/contacts, /all/groups) MUST be defined BEFORE
# dynamic routes (/{bot_id}/contacts, /{bot_id}/groups) to avoid route conflicts

@router.get("/all/contacts")
async def get_all_bots_contacts(
    bot_ids: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get contacts from user's bots. Optionally filter by specific bot IDs (comma-separated)."""
    # Get all user's bots
    user_bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    user_bot_ids = [b[0] for b in user_bot_ids]

    if not user_bot_ids:
        return {"contacts": [], "total": 0}

    # If specific bot_ids provided, filter to only those (that user owns)
    if bot_ids:
        try:
            requested_ids = [int(bid.strip()) for bid in bot_ids.split(',') if bid.strip()]
            # Only include bot IDs that the user actually owns
            filter_bot_ids = [bid for bid in requested_ids if bid in user_bot_ids]
        except ValueError:
            filter_bot_ids = user_bot_ids
    else:
        filter_bot_ids = user_bot_ids

    if not filter_bot_ids:
        return {"contacts": [], "total": 0}

    # Get all non-group conversations from selected bots
    conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(filter_bot_ids),
        Conversation.is_group == False,
        Conversation.phone.isnot(None),
        Conversation.phone != ""
    ).all()

    contacts = []
    seen_phones = set()
    for conv in conversations:
        if conv.phone not in seen_phones:
            seen_phones.add(conv.phone)
            contacts.append({
                "phone": conv.phone,
                "display_name": conv.chat_name or conv.phone,
                "profile_pic": conv.profile_pic,
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                "bot_id": conv.bot_profile_id
            })

    # Sort by last_message_at (most recent first)
    contacts.sort(key=lambda x: x["last_message_at"] or "", reverse=True)

    return {"contacts": contacts, "total": len(contacts)}


@router.get("/all/groups")
async def get_all_bots_groups(
    bot_ids: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get groups from user's bots. Optionally filter by specific bot IDs (comma-separated)."""
    # Get all user's bots
    user_bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    user_bot_ids = [b[0] for b in user_bot_ids]

    if not user_bot_ids:
        return {"groups": [], "total": 0}

    # If specific bot_ids provided, filter to only those (that user owns)
    if bot_ids:
        try:
            requested_ids = [int(bid.strip()) for bid in bot_ids.split(',') if bid.strip()]
            # Only include bot IDs that the user actually owns
            filter_bot_ids = [bid for bid in requested_ids if bid in user_bot_ids]
        except ValueError:
            filter_bot_ids = user_bot_ids
    else:
        filter_bot_ids = user_bot_ids

    if not filter_bot_ids:
        return {"groups": [], "total": 0}

    # Get all group conversations from selected bots
    conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(filter_bot_ids),
        Conversation.is_group == True,
        Conversation.chat_id.isnot(None)
    ).all()

    groups = []
    seen_ids = set()
    for conv in conversations:
        if conv.chat_id not in seen_ids:
            seen_ids.add(conv.chat_id)
            groups.append({
                "chat_id": conv.chat_id,
                "name": conv.chat_name or conv.chat_id,
                "profile_pic": conv.profile_pic,
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                "bot_id": conv.bot_profile_id
            })

    # Sort by last_message_at (most recent first)
    groups.sort(key=lambda x: x["last_message_at"] or "", reverse=True)

    return {"groups": groups, "total": len(groups)}


# Dynamic routes with bot_id parameter (must come AFTER static /all/* routes)

@router.get("/{bot_id}/contacts")
async def get_bot_contacts(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all contacts (non-group conversations) for a bot."""
    bot = get_bot_profile(bot_id, current_user, db)

    # Get all non-group conversations for this bot
    conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
        Conversation.is_group == False,
        Conversation.phone.isnot(None),
        Conversation.phone != ""
    ).all()

    contacts = []
    seen_phones = set()
    for conv in conversations:
        if conv.phone not in seen_phones:
            seen_phones.add(conv.phone)
            contacts.append({
                "phone": conv.phone,
                "display_name": conv.chat_name or conv.phone,
                "profile_pic": conv.profile_pic,
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None
            })

    # Sort by last_message_at (most recent first)
    contacts.sort(key=lambda x: x["last_message_at"] or "", reverse=True)

    return {"contacts": contacts, "total": len(contacts)}


@router.get("/{bot_id}/groups")
async def get_bot_groups(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all groups for a bot."""
    bot = get_bot_profile(bot_id, current_user, db)

    # Get all group conversations for this bot
    conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
        Conversation.is_group == True,
        Conversation.chat_id.isnot(None)
    ).all()

    groups = []
    seen_ids = set()
    for conv in conversations:
        if conv.chat_id not in seen_ids:
            seen_ids.add(conv.chat_id)
            groups.append({
                "chat_id": conv.chat_id,
                "name": conv.chat_name or conv.chat_id,
                "profile_pic": conv.profile_pic,
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None
            })

    # Sort by last_message_at (most recent first)
    groups.sort(key=lambda x: x["last_message_at"] or "", reverse=True)

    return {"groups": groups, "total": len(groups)}
