"""
Discord Platform Adapter
Uses discord.py library for real-time bot communication via Discord Gateway WebSocket.

Requires:
- Bot token from Discord Developer Portal
- MESSAGE_CONTENT privileged intent enabled
- Bot invited to server with appropriate permissions

Behavior:
- Responds to all DMs
- Responds to @mentions in server channels
- Handles text, images, and file attachments
- Respects Discord rate limits (handled by discord.py)

API Key Note:
    The BotProfile.api_key_encrypted field stores the AI provider API key
    (OpenAI, Anthropic, etc.), same as WhatsApp. The Discord bot token must
    be provided via a 'platform_token' key in the config dict, or stored in
    BotProfile.platform_token_encrypted (requires DB migration). Until then,
    the adapter falls back to using api_key_encrypted as the Discord bot
    token, which means AI responses require hub-level AI agent configuration.
"""

import asyncio
import base64
import functools
import logging
import random
from datetime import datetime
from typing import Dict, Any, List, Optional

import discord
from discord import Intents, Message as DiscordMessage

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)
from app.platforms.message_handler import (
    get_db_session,
    find_or_create_conversation,
    save_user_message,
    save_assistant_message,
    update_conversation_stats,
    is_duplicate_message,
    has_response_after,
    build_ai_messages,
    save_media_file,
    analyze_media_with_ai,
    broadcast_user_message,
    broadcast_assistant_message,
    broadcast_typing,
    is_human_takeover_active,
    log_activity,
)

logger = logging.getLogger(__name__)


class DiscordAdapter(PlatformAdapter):
    """Discord adapter using discord.py with Gateway WebSocket."""

    def __init__(self):
        self._clients: Dict[int, discord.Client] = {}

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.DISCORD

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=True,
            supports_typing_indicator=True,
            supports_read_receipts=False,
            supports_history_sync=True,
            supports_contacts_list=True,
            supports_groups_list=True,
            supports_profile_pic=True,
            auth_method=AuthMethod.API_TOKEN,
            max_message_length=2000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "video/mp4",
                "audio/mpeg", "audio/ogg",
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ],
        )

    async def run(self, instance) -> None:
        """Start Discord bot via Gateway WebSocket.

        Args:
            instance: BotInstance with config and callbacks
        """
        bot_profile_id = instance.bot_profile_id
        config = instance.config

        await instance.notify_status({"status": "starting", "message": "Initializing Discord bot..."})

        from app.auth.utils import decrypt_string

        # Get the Discord bot token and AI API key from config.
        # api_key_encrypted stores the AI provider key (same as WhatsApp).
        # platform_token stores the Discord bot token (required).
        if "api_key" in config:
            ai_api_key = config["api_key"]
        else:
            ai_api_key = decrypt_string(config.get("api_key_encrypted", ""))

        bot_token = config.get("platform_token")
        if not bot_token and config.get("platform_token_encrypted"):
            bot_token = decrypt_string(config["platform_token_encrypted"])

        if not bot_token:
            instance.error = "No Discord bot token configured. Set platform_token in bot config."
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": "No Discord bot token configured. Set platform_token in bot config."})
            return

        # Initialize AI provider
        from app.ai.factory import get_ai_provider
        try:
            ai_provider = get_ai_provider(
                config.get("ai_provider", "openai"),
                ai_api_key,
                model=config.get("model"),
            )
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: Failed to init AI provider: {e}")
            ai_provider = None

        # Set up intents
        intents = Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        client = discord.Client(intents=intents)
        self._clients[bot_profile_id] = client

        @client.event
        async def on_ready():
            logger.info(f"Bot {bot_profile_id}: Discord connected as {client.user}")
            instance.platform_connected = True
            await instance.notify_status({
                "status": "connected",
                "message": f"Connected as {client.user.name}#{client.user.discriminator}",
            })

        @client.event
        async def on_message(message: DiscordMessage):
            # Ignore own messages
            if message.author == client.user:
                return

            # Ignore other bots
            if message.author.bot:
                return

            is_dm = isinstance(message.channel, discord.DMChannel)

            # In servers, only respond to @mentions (unless respond_to_all_in_group is set)
            if not is_dm:
                if not config.get("group_chat_enabled", True):
                    return
                is_mentioned = client.user in message.mentions
                if not config.get("respond_to_all_in_group", False) and not is_mentioned:
                    return

            # Check AI response toggle
            if not instance.ai_response_enabled:
                return

            if not ai_provider:
                logger.debug(f"Bot {bot_profile_id}: No AI provider, skipping response")
                return

            await self._handle_message(
                message=message,
                client=client,
                instance=instance,
                config=config,
                ai_provider=ai_provider,
                is_dm=is_dm,
            )

        @client.event
        async def on_guild_join(guild):
            logger.info(f"Bot {bot_profile_id}: Joined server '{guild.name}' (id={guild.id})")

        await instance.notify_status({"status": "connecting", "message": "Connecting to Discord..."})

        try:
            await client.start(bot_token)
        except discord.LoginFailure:
            instance.error = "Invalid bot token"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": "Invalid bot token"})
        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: Discord task cancelled, closing client")
            await client.close()
            raise
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Discord error: {e}", exc_info=True)
            instance.error = str(e)
            instance.is_running = False
            await instance.notify_status({"error": str(e), "message": f"Discord error: {e}"})
        finally:
            if not client.is_closed():
                await client.close()
            self._clients.pop(bot_profile_id, None)
            instance.platform_connected = False

    async def _handle_message(
        self,
        message: DiscordMessage,
        client: discord.Client,
        instance,
        config: dict,
        ai_provider,
        is_dm: bool,
    ):
        """Process an incoming Discord message.

        All blocking calls (DB, AI, broadcast) are dispatched to a thread pool
        via run_in_executor to avoid blocking the Discord event loop.
        """
        bot_profile_id = instance.bot_profile_id
        loop = asyncio.get_running_loop()

        # Build chat identifiers
        if is_dm:
            chat_id = f"dm_{message.author.id}"
            chat_name = message.author.display_name
        else:
            chat_id = f"channel_{message.channel.id}"
            chat_name = f"#{message.channel.name}" if hasattr(message.channel, "name") else f"Channel {message.channel.id}"

        # Strip bot mention from message content
        content = message.content
        if client.user:
            content = content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

        sender_name = message.author.display_name
        sender_id = str(message.author.id)
        platform_message_id = str(message.id)
        profile_pic = str(message.author.display_avatar.url) if message.author.display_avatar else ""

        # Handle attachments
        media_info = None
        media_analysis = None
        if message.attachments:
            attachment = message.attachments[0]
            media_info = await self._download_attachment(attachment, bot_profile_id, chat_name)
            if media_info:
                file_type = media_info.get("file_type", "")
                if file_type.startswith("image/") or file_type == "application/pdf":
                    media_analysis = await loop.run_in_executor(
                        None,
                        functools.partial(
                            analyze_media_with_ai,
                            ai_provider,
                            media_info["local_file_path"],
                            file_type,
                            content,
                        ),
                    )

        # DB operations + broadcast (blocking) — run in thread pool
        def _db_save_and_check():
            """Save message to DB, check dedup/takeover, build AI messages.

            Returns (conversation_id, ai_messages) or (conversation_id, None)
            if AI response should be skipped.
            """
            with get_db_session() as db:
                conversation = find_or_create_conversation(
                    db,
                    bot_profile_id,
                    chat_id,
                    chat_name,
                    is_group=not is_dm,
                    display_name=chat_name,
                    profile_pic=profile_pic,
                )

                # Dedup check
                is_dup, existing = is_duplicate_message(
                    db,
                    conversation.id,
                    platform_message_id=platform_message_id,
                    content=content,
                )
                if is_dup and existing and has_response_after(db, conversation.id, existing.id):
                    logger.debug(f"Bot {bot_profile_id}: Duplicate message skipped (already responded)")
                    return conversation.id, None

                # Save user message
                user_msg = save_user_message(
                    db,
                    conversation.id,
                    content,
                    sender_name=sender_name,
                    sender_id=sender_id,
                    platform_message_id=platform_message_id,
                    timestamp=message.created_at.replace(tzinfo=None),
                    file_url=media_info["file_url"] if media_info else None,
                    file_type=media_info["file_type"] if media_info else None,
                    file_name=media_info["file_name"] if media_info else None,
                    file_size=media_info["file_size"] if media_info else None,
                    file_pages=media_info.get("file_pages") if media_info else None,
                    media_analysis=media_analysis,
                )
                update_conversation_stats(db, conversation.id)

                # Log activity for analytics feed
                from app.database import BotProfile as BotProfileModel
                bot = db.query(BotProfileModel).filter_by(id=bot_profile_id).first()
                if bot:
                    log_activity(
                        db,
                        bot.user_id,
                        "discord_message_received",
                        f"Message from {sender_name} in {chat_name}",
                    )

                db.commit()

                # Broadcast to WebSocket (safe — we're in a thread, not the event loop)
                broadcast_user_message(conversation.id, bot_profile_id, user_msg, conversation)

                # Check human takeover
                if is_human_takeover_active(db, conversation.id):
                    logger.info(f"Bot {bot_profile_id}: Human takeover active for {chat_name}, skipping AI")
                    return conversation.id, None

                broadcast_typing(conversation.id, True, "Bot")

                # Build AI messages
                system_prompt = config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db,
                    conversation.id,
                    system_prompt,
                    max_history=config.get("max_history", 20),
                    is_group=not is_dm,
                    current_message=user_msg,
                )

                return conversation.id, ai_messages

        conversation_id, ai_messages = await loop.run_in_executor(None, _db_save_and_check)

        if ai_messages is None:
            return

        # Generate AI response (blocking) — run in thread pool
        def _generate_ai_response():
            return ai_provider.chat_completion(
                messages=ai_messages,
                max_tokens=config.get("max_tokens", 1000),
                temperature=config.get("temperature", 0.7),
            )

        try:
            response = await loop.run_in_executor(None, _generate_ai_response)
            ai_text = response.content
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: AI error: {e}", exc_info=True)
            await loop.run_in_executor(
                None, functools.partial(broadcast_typing, conversation_id, False)
            )
            return

        if not ai_text:
            await loop.run_in_executor(
                None, functools.partial(broadcast_typing, conversation_id, False)
            )
            return

        # Apply response delay
        delay_min = config.get("response_delay_min", 0)
        delay_max = config.get("response_delay_max", 0)
        if delay_min or delay_max:
            delay = random.uniform(delay_min, delay_max)
            await asyncio.sleep(delay)

        # Send response to Discord
        try:
            async with message.channel.typing():
                await asyncio.sleep(0.5)

            chunks = self._split_message(ai_text, 2000)
            for chunk in chunks:
                await message.channel.send(chunk)
        except discord.Forbidden:
            logger.warning(f"Bot {bot_profile_id}: No permission to send in {chat_name}")
            return
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send Discord message: {e}")
            return

        # Save assistant message to DB + broadcast (blocking) — run in thread pool
        def _db_save_response():
            with get_db_session() as db:
                conv = find_or_create_conversation(db, bot_profile_id, chat_id, chat_name, is_group=not is_dm)
                assistant_msg = save_assistant_message(
                    db,
                    conv.id,
                    ai_text,
                    sender_name=client.user.display_name if client.user else "Bot",
                    sender_id=str(client.user.id) if client.user else "",
                )
                update_conversation_stats(db, conv.id)

                from app.database import BotProfile as BotProfileModel
                bot = db.query(BotProfileModel).filter_by(id=bot_profile_id).first()
                if bot:
                    log_activity(
                        db,
                        bot.user_id,
                        "discord_message_sent",
                        f"AI response sent in {chat_name}",
                    )

                db.commit()

                broadcast_typing(conv.id, False)
                broadcast_assistant_message(conv.id, assistant_msg)

        await loop.run_in_executor(None, _db_save_response)

    async def _download_attachment(
        self,
        attachment: discord.Attachment,
        bot_profile_id: int,
        chat_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Download a Discord attachment and save it locally."""
        try:
            data = await attachment.read()
            b64_data = base64.b64encode(data).decode("utf-8")
            content_type = attachment.content_type or "application/octet-stream"

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                functools.partial(
                    save_media_file,
                    base64_data=b64_data,
                    media_type=content_type,
                    bot_profile_id=bot_profile_id,
                    chat_name=chat_name,
                    direction="received",
                    original_filename=attachment.filename,
                ),
            )
            return result
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to download attachment: {e}")
            return None

    def _split_message(self, text: str, max_length: int = 2000) -> List[str]:
        """Split a message into chunks that fit Discord's character limit."""
        if len(text) <= max_length:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Try to split at a newline
            split_at = text.rfind("\n", 0, max_length)
            if split_at == -1:
                split_at = text.rfind(" ", 0, max_length)
            if split_at == -1:
                split_at = max_length

            chunk = text[:split_at]
            if chunk.strip():
                chunks.append(chunk)
            text = text[split_at:].lstrip("\n ")

        return chunks

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message to a Discord channel or DM."""
        client = self._clients.get(bot_profile_id)
        if not client or client.is_closed():
            logger.warning(f"Bot {bot_profile_id}: Discord client not connected")
            return False

        try:
            channel = await self._resolve_channel(client, chat_id)
            if not channel:
                logger.warning(f"Bot {bot_profile_id}: Could not resolve channel {chat_id}")
                return False

            chunks = self._split_message(message, 2000)
            for chunk in chunks:
                await channel.send(chunk)
            return True

        except discord.Forbidden:
            logger.warning(f"Bot {bot_profile_id}: No permission to send to {chat_id}")
            return False
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send message: {e}")
            return False

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send a file to a Discord channel or DM."""
        client = self._clients.get(bot_profile_id)
        if not client or client.is_closed():
            logger.warning(f"Bot {bot_profile_id}: Discord client not connected")
            return False

        try:
            channel = await self._resolve_channel(client, chat_id)
            if not channel:
                logger.warning(f"Bot {bot_profile_id}: Could not resolve channel {chat_id}")
                return False

            file = discord.File(file_path)
            await channel.send(content=caption or None, file=file)
            return True

        except discord.Forbidden:
            logger.warning(f"Bot {bot_profile_id}: No permission to send file to {chat_id}")
            return False
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send file: {e}")
            return False

    async def _resolve_channel(self, client: discord.Client, chat_id: str):
        """Resolve a chat_id to a Discord channel or DM channel.

        chat_id format:
        - "dm_{user_id}" for DMs
        - "channel_{channel_id}" for server channels
        """
        try:
            if chat_id.startswith("dm_"):
                user_id = int(chat_id.removeprefix("dm_"))
                user = await client.fetch_user(user_id)
                if user:
                    return await user.create_dm()
            elif chat_id.startswith("channel_"):
                channel_id = int(chat_id.removeprefix("channel_"))
                return client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f"Failed to resolve channel {chat_id}: {e}")
        return None

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up Discord client resources."""
        client = self._clients.pop(bot_profile_id, None)
        if client and not client.is_closed():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.close())
            except RuntimeError:
                # No running event loop — run close synchronously in a new loop
                try:
                    asyncio.run(client.close())
                except Exception:
                    logger.warning(f"Bot {bot_profile_id}: Failed to close Discord client during cleanup")

    def get_contacts(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Get list of users the bot can see (from mutual servers)."""
        client = self._clients.get(bot_profile_id)
        if not client:
            return []

        contacts = []
        seen_ids = set()
        for guild in client.guilds:
            for member in guild.members:
                if member.bot or member.id in seen_ids:
                    continue
                seen_ids.add(member.id)
                contacts.append({
                    "chat_id": f"dm_{member.id}",
                    "name": member.display_name,
                    "phone": str(member.id),
                    "profile_pic": str(member.display_avatar.url) if member.display_avatar else "",
                })
        return contacts

    def get_groups(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Get list of servers and channels the bot is in."""
        client = self._clients.get(bot_profile_id)
        if not client:
            return []

        groups = []
        for guild in client.guilds:
            for channel in guild.text_channels:
                groups.append({
                    "chat_id": f"channel_{channel.id}",
                    "name": f"{guild.name} / #{channel.name}",
                    "member_count": guild.member_count,
                })
        return groups
