"""
Platform-agnostic message sending.

Provides send_message() and send_file() that route through the correct
platform adapter based on bot configuration. External code can use these
instead of importing platform-specific functions directly.

For backward compatibility, direct imports of send_whatsapp_message()
and send_whatsapp_file() from app.bots.whatsapp_bot continue to work.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def send_message(
    bot_profile_id: int,
    chat_id: str,
    chat_name: str,
    message: str,
    platform_type: str = None,
) -> bool:
    """Send a text message through the correct platform adapter.

    If platform_type is not provided, looks up the bot's platform from DB.
    Falls back to 'whatsapp' if not found.

    Args:
        bot_profile_id: Bot database ID
        chat_id: Platform-specific chat identifier
        chat_name: Display name of the chat
        message: Text to send
        platform_type: Optional platform override (avoids DB lookup)

    Returns:
        True if sent successfully
    """
    pt = platform_type or _get_bot_platform(bot_profile_id)
    adapter = _get_adapter(pt)
    return await adapter.send_message(bot_profile_id, chat_id, chat_name, message)


async def send_file(
    bot_profile_id: int,
    chat_id: str,
    file_path: str,
    caption: str = "",
    file_type: str = "",
    chat_name: str = "",
    platform_type: str = None,
) -> bool:
    """Send a file through the correct platform adapter.

    Args:
        bot_profile_id: Bot database ID
        chat_id: Platform-specific chat identifier
        file_path: Absolute path to file
        caption: Optional caption
        file_type: MIME type
        chat_name: Display name of the chat
        platform_type: Optional platform override

    Returns:
        True if sent successfully
    """
    pt = platform_type or _get_bot_platform(bot_profile_id)
    adapter = _get_adapter(pt)
    return await adapter.send_file(bot_profile_id, chat_id, file_path, caption, file_type, chat_name)


def _get_bot_platform(bot_profile_id: int) -> str:
    """Look up the platform type for a bot from the database.

    Returns 'whatsapp' as default if not found or on error.
    """
    try:
        from app.database import SessionLocal, BotProfile
        db = SessionLocal()
        try:
            bot = db.query(BotProfile.platform_type).filter(
                BotProfile.id == bot_profile_id
            ).first()
            return (bot.platform_type if bot and bot.platform_type else "whatsapp")
        finally:
            db.close()
    except Exception:
        return "whatsapp"


def _get_adapter(platform_type: str):
    """Get adapter from registry."""
    from app.platforms.base import PlatformType
    from app.platforms.registry import platform_registry
    return platform_registry.get_adapter(PlatformType(platform_type))
