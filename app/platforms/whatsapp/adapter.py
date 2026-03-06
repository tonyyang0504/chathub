"""
WhatsApp Platform Adapter
Delegates to the existing whatsapp_bot.py implementation.

This adapter wraps the existing WhatsApp bot functions without modifying them,
preserving all current functionality while conforming to the PlatformAdapter interface.
"""

import logging
from typing import Dict, Any, List

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)


class WhatsAppAdapter(PlatformAdapter):
    """WhatsApp adapter using Playwright browser automation.

    This is a thin wrapper around the existing whatsapp_bot.py functions.
    All actual logic remains in whatsapp_bot.py — this adapter just
    delegates to those functions through the PlatformAdapter interface.
    """

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.WHATSAPP

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=False,
            supports_read_receipts=True,
            supports_typing_indicator=False,
            supports_history_sync=True,
            supports_contacts_list=True,
            supports_groups_list=True,
            supports_profile_pic=True,
            auth_method=AuthMethod.QR_CODE,
            max_message_length=65536,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "video/mp4", "video/3gpp",
                "audio/mpeg", "audio/ogg", "audio/aac",
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ],
        )

    async def run(self, instance) -> None:
        """Start WhatsApp bot by delegating to existing run_whatsapp_bot()."""
        from app.bots.whatsapp_bot import run_whatsapp_bot
        await run_whatsapp_bot(instance)

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send message by delegating to existing send_whatsapp_message()."""
        from app.bots.whatsapp_bot import send_whatsapp_message
        return await send_whatsapp_message(bot_profile_id, chat_id, chat_name, message)

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send file by delegating to existing send_whatsapp_file()."""
        from app.bots.whatsapp_bot import send_whatsapp_file
        return await send_whatsapp_file(bot_profile_id, chat_id, file_path, caption, file_type, chat_name)

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up WhatsApp bot queues."""
        from app.bots.whatsapp_bot import cleanup_bot_queues
        cleanup_bot_queues(bot_profile_id)
