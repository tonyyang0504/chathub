"""
Telegram Platform Adapter
Uses python-telegram-bot library with long polling for receiving messages.

Auth method: API_TOKEN (bot token from @BotFather)
The bot token is stored encrypted in bot.api_key_encrypted alongside the AI API key,
using the format: "ai_api_key|||telegram_bot_token"

If the encrypted value does NOT contain "|||", the entire value is treated as the
AI API key and the bot cannot start (no Telegram token).
"""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Dict

from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)

# Store active Telegram applications keyed by bot_profile_id
_active_apps: Dict[int, Application] = {}


def _parse_credentials(encrypted_value: str) -> tuple:
    """Parse the encrypted credential string into (ai_api_key, telegram_token).

    Format: "ai_api_key|||telegram_bot_token"
    If no separator, returns (encrypted_value, None).
    """
    if "|||" in encrypted_value:
        parts = encrypted_value.split("|||", 1)
        return parts[0].strip(), parts[1].strip()
    return encrypted_value, None


class TelegramAdapter(PlatformAdapter):
    """Telegram adapter using Bot API with long polling.

    Uses python-telegram-bot library for receiving and sending messages.
    Leverages shared message_handler.py for all DB/AI/WebSocket operations.
    """

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.TELEGRAM

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=True,
            supports_read_receipts=False,
            supports_typing_indicator=True,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=False,
            supports_profile_pic=True,
            auth_method=AuthMethod.API_TOKEN,
            max_message_length=4096,
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
        """Start Telegram bot polling loop.

        Args:
            instance: BotInstance with config, callbacks, and outbound queue
        """
        from app.auth.utils import decrypt_string

        config = instance.config
        bot_profile_id = instance.bot_profile_id

        # Extract credentials
        encrypted_key = config.get("api_key_encrypted", "")
        decrypted = decrypt_string(encrypted_key) if encrypted_key else ""
        ai_api_key, telegram_token = _parse_credentials(decrypted)

        if not telegram_token:
            error_msg = (
                "No Telegram bot token found. Store credentials as "
                "'ai_api_key|||telegram_bot_token' in the API key field."
            )
            logger.error(f"Bot {bot_profile_id}: {error_msg}")
            instance.error = error_msg
            instance.is_running = False
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return

        # Verify the token works
        try:
            bot = Bot(token=telegram_token)
            bot_info = await bot.get_me()
            logger.info(
                f"Bot {bot_profile_id}: Telegram connected as @{bot_info.username} "
                f"(id={bot_info.id})"
            )
        except Exception as e:
            error_msg = f"Invalid Telegram bot token: {e}"
            logger.error(f"Bot {bot_profile_id}: {error_msg}")
            instance.error = error_msg
            instance.is_running = False
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return

        # Store AI config for use in handlers
        handler_context = _HandlerContext(
            bot_profile_id=bot_profile_id,
            instance=instance,
            ai_api_key=ai_api_key,
            config=config,
        )

        # Build the Application
        app = (
            Application.builder()
            .token(telegram_token)
            .build()
        )

        # Register handlers
        app.add_handler(CommandHandler("start", handler_context.handle_start))
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handler_context.handle_message,
            )
        )
        app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO | filters.VOICE,
                handler_context.handle_media,
            )
        )

        _active_apps[bot_profile_id] = app

        # Notify connected
        instance.whatsapp_connected = True  # reused field means "platform connected"
        await instance.notify_status({
            "message": f"Telegram bot @{bot_info.username} connected",
            "connected": True,
            "platform": "telegram",
            "bot_username": bot_info.username,
        })

        try:
            # Initialize and start polling
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            logger.info(f"Bot {bot_profile_id}: Telegram polling started")

            # Keep alive + process outbound queue
            while instance.is_running and not instance.stopped_by_user:
                # Process outbound messages
                if instance.has_outbound_messages():
                    outbound = instance.get_outbound_messages()
                    for msg in outbound:
                        try:
                            chunks = _split_message(msg["message"], 4096)
                            for chunk in chunks:
                                await app.bot.send_message(
                                    chat_id=msg["chat_id"],
                                    text=chunk,
                                )
                        except Exception as e:
                            logger.error(
                                f"Bot {bot_profile_id}: Failed to send outbound: {e}"
                            )
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: Telegram task cancelled")
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Telegram error: {e}", exc_info=True)
            instance.error = str(e)
            await instance.notify_status({"error": str(e), "message": f"Telegram error: {e}"})
        finally:
            # Shutdown
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception as e:
                logger.warning(f"Bot {bot_profile_id}: Error during shutdown: {e}")

            _active_apps.pop(bot_profile_id, None)
            instance.whatsapp_connected = False
            instance.is_running = False
            logger.info(f"Bot {bot_profile_id}: Telegram adapter stopped")

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via Telegram."""
        app = _active_apps.get(bot_profile_id)
        if not app:
            logger.warning(f"Bot {bot_profile_id}: No active Telegram app for sending")
            return False

        try:
            # Split long messages (Telegram limit: 4096 chars)
            chunks = _split_message(message, 4096)
            for chunk in chunks:
                await app.bot.send_message(chat_id=int(chat_id), text=chunk)
            return True
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
        """Send a file via Telegram."""
        app = _active_apps.get(bot_profile_id)
        if not app:
            logger.warning(f"Bot {bot_profile_id}: No active Telegram app for sending")
            return False

        try:
            path = Path(file_path)
            if not path.exists():
                logger.error(f"Bot {bot_profile_id}: File not found: {file_path}")
                return False

            cid = int(chat_id)

            with open(path, "rb") as f:
                if file_type.startswith("image/"):
                    await app.bot.send_photo(
                        chat_id=cid, photo=f, caption=caption or None,
                    )
                elif file_type.startswith("video/"):
                    await app.bot.send_video(
                        chat_id=cid, video=f, caption=caption or None,
                    )
                elif file_type.startswith("audio/"):
                    await app.bot.send_audio(
                        chat_id=cid, audio=f, caption=caption or None,
                    )
                else:
                    await app.bot.send_document(
                        chat_id=cid, document=f, caption=caption or None,
                    )
            return True
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send file: {e}")
            return False

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up Telegram resources."""
        app = _active_apps.pop(bot_profile_id, None)
        if app:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._async_cleanup(app, bot_profile_id))
            except RuntimeError:
                # No running loop — run synchronously in a new loop
                try:
                    asyncio.run(self._async_cleanup(app, bot_profile_id))
                except Exception as e:
                    logger.warning(f"Bot {bot_profile_id}: cleanup error: {e}")

    @staticmethod
    async def _async_cleanup(app: Application, bot_profile_id: int) -> None:
        """Perform async shutdown of the Telegram application."""
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: error during async cleanup: {e}")


def _split_message(text: str, max_length: int = 4096) -> list:
    """Split a message into chunks that fit within Telegram's limit."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Try to split at last newline within limit
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1 or split_at < max_length // 2:
            # Try space
            split_at = text.rfind(" ", 0, max_length)
        if split_at == -1 or split_at < max_length // 2:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:]
        if text.startswith("\n"):
            text = text[1:]
    return chunks


class _HandlerContext:
    """Holds per-bot context for Telegram update handlers."""

    def __init__(
        self,
        bot_profile_id: int,
        instance,
        ai_api_key: str,
        config: dict,
    ):
        self.bot_profile_id = bot_profile_id
        self.instance = instance
        self.ai_api_key = ai_api_key
        self.config = config

    def _get_ai_provider(self):
        """Create an AI provider instance from config."""
        from app.ai.factory import get_ai_provider

        return get_ai_provider(
            provider_name=self.config.get("ai_provider", "openai"),
            api_key=self.ai_api_key,
            model=self.config.get("model"),
        )

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        await update.message.reply_text(
            "Hello! I'm an AI assistant. Send me a message and I'll respond."
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming text messages."""
        if not update.message or not update.message.text:
            return

        from app.platforms.message_handler import (
            get_db_session,
            find_or_create_conversation,
            save_user_message,
            save_assistant_message,
            update_conversation_stats,
            is_duplicate_message,
            has_response_after,
            build_ai_messages,
            is_human_takeover_active,
            broadcast_user_message,
            broadcast_assistant_message,
            broadcast_typing,
        )

        msg = update.message
        chat_id = str(msg.chat_id)
        is_group = msg.chat.type in ("group", "supergroup")

        # Determine chat name and sender info
        if is_group:
            chat_name = msg.chat.title or f"Group {chat_id}"
        else:
            chat_name = _get_user_display_name(msg.from_user) or f"Chat {chat_id}"

        sender_name = _get_user_display_name(msg.from_user)
        sender_id = str(msg.from_user.id) if msg.from_user else ""

        # Skip if group chats disabled
        if is_group and not self.config.get("group_chat_enabled", False):
            return

        # In groups, only respond if bot is mentioned or replied to
        if is_group and not self.config.get("respond_to_all_in_group", False):
            bot_user = await context.bot.get_me()
            mentioned = f"@{bot_user.username}" in (msg.text or "")
            replied_to_bot = (
                msg.reply_to_message
                and msg.reply_to_message.from_user
                and msg.reply_to_message.from_user.id == bot_user.id
            )
            if not mentioned and not replied_to_bot:
                return

        text = msg.text.strip()
        platform_message_id = str(msg.message_id)

        with get_db_session() as db:
            # Find or create conversation
            conversation = find_or_create_conversation(
                db,
                self.bot_profile_id,
                chat_id,
                chat_name,
                is_group=is_group,
                display_name=chat_name,
            )

            # Dedup check
            is_dup, existing = is_duplicate_message(
                db,
                conversation.id,
                platform_message_id=platform_message_id,
                content=text,
            )
            if is_dup and existing:
                if has_response_after(db, conversation.id, existing.id):
                    # Already have a response for this message — skip entirely
                    db.commit()
                    return
                # Duplicate message but no response yet — use existing msg
                # for AI generation without saving again
                db.commit()
                user_msg = existing
            else:
                # Save new user message
                user_msg = save_user_message(
                    db,
                    conversation.id,
                    text,
                    sender_name=sender_name,
                    sender_id=sender_id,
                    platform_message_id=platform_message_id,
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

            # Broadcast via WebSocket
            broadcast_user_message(
                conversation.id,
                self.bot_profile_id,
                user_msg,
                conversation,
            )

            # Check if AI responses are enabled
            if not self.instance.ai_response_enabled:
                return

            # Check human takeover
            if is_human_takeover_active(db, conversation.id):
                return

            # Send typing indicator
            broadcast_typing(conversation.id, True, "Bot")
            try:
                await context.bot.send_chat_action(
                    chat_id=int(chat_id), action="typing"
                )
            except Exception:
                pass

            # Response delay
            delay_min = self.config.get("response_delay_min", 0) or 0
            delay_max = self.config.get("response_delay_max", 0) or 0
            if delay_min > 0 or delay_max > 0:
                import random
                delay = random.uniform(delay_min, max(delay_min, delay_max))
                await asyncio.sleep(delay)

            # Build AI messages and get response
            try:
                system_prompt = self.config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db,
                    conversation.id,
                    system_prompt,
                    max_history=self.config.get("max_history", 20) or 20,
                    is_group=is_group,
                    current_message=user_msg,
                )

                provider = self._get_ai_provider()
                response = await asyncio.to_thread(
                    provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=self.config.get("max_tokens", 1000),
                    temperature=self.config.get("temperature", 0.7),
                )
                ai_text = response.content

                if not ai_text:
                    return

                # Save assistant message
                assistant_msg = save_assistant_message(
                    db,
                    conversation.id,
                    ai_text,
                    sender_name="AI Agent",
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

                # Broadcast assistant message via WebSocket
                broadcast_assistant_message(conversation.id, assistant_msg)

                # Send via Telegram (split if needed)
                chunks = _split_message(ai_text, 4096)
                for chunk in chunks:
                    await msg.reply_text(chunk)

            except Exception as e:
                logger.error(
                    f"Bot {self.bot_profile_id}: AI response error: {e}",
                    exc_info=True,
                )
            finally:
                broadcast_typing(conversation.id, False, "Bot")

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming media messages (photos, documents, audio, video)."""
        if not update.message:
            return

        from app.platforms.message_handler import (
            get_db_session,
            find_or_create_conversation,
            save_user_message,
            save_assistant_message,
            update_conversation_stats,
            build_ai_messages,
            is_human_takeover_active,
            broadcast_user_message,
            broadcast_assistant_message,
            broadcast_typing,
            save_media_file,
            analyze_media_with_ai,
        )

        msg = update.message
        chat_id = str(msg.chat_id)
        is_group = msg.chat.type in ("group", "supergroup")

        if is_group and not self.config.get("group_chat_enabled", False):
            return

        # In groups, check mention/reply
        if is_group and not self.config.get("respond_to_all_in_group", False):
            bot_user = await context.bot.get_me()
            caption_text = msg.caption or ""
            mentioned = f"@{bot_user.username}" in caption_text
            replied_to_bot = (
                msg.reply_to_message
                and msg.reply_to_message.from_user
                and msg.reply_to_message.from_user.id == bot_user.id
            )
            if not mentioned and not replied_to_bot:
                return

        chat_name = (
            (msg.chat.title or f"Group {chat_id}")
            if is_group
            else (_get_user_display_name(msg.from_user) or f"Chat {chat_id}")
        )
        sender_name = _get_user_display_name(msg.from_user)
        sender_id = str(msg.from_user.id) if msg.from_user else ""
        caption = msg.caption or ""

        # Determine file to download
        file_obj = None
        file_type = "application/octet-stream"
        original_filename = None

        if msg.photo:
            # Get highest resolution photo
            photo = msg.photo[-1]
            file_obj = await context.bot.get_file(photo.file_id)
            file_type = "image/jpeg"
            original_filename = f"photo_{photo.file_unique_id}.jpg"
        elif msg.document:
            file_obj = await context.bot.get_file(msg.document.file_id)
            file_type = msg.document.mime_type or "application/octet-stream"
            original_filename = msg.document.file_name
        elif msg.audio:
            file_obj = await context.bot.get_file(msg.audio.file_id)
            file_type = msg.audio.mime_type or "audio/mpeg"
            original_filename = msg.audio.file_name or f"audio_{msg.audio.file_unique_id}.mp3"
        elif msg.video:
            file_obj = await context.bot.get_file(msg.video.file_id)
            file_type = msg.video.mime_type or "video/mp4"
            original_filename = msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
        elif msg.voice:
            file_obj = await context.bot.get_file(msg.voice.file_id)
            file_type = msg.voice.mime_type or "audio/ogg"
            original_filename = f"voice_{msg.voice.file_unique_id}.ogg"

        if not file_obj:
            return

        # Download file to temp location and then save via shared handler
        try:
            file_bytes = await file_obj.download_as_bytearray()
            b64_data = base64.b64encode(file_bytes).decode("utf-8")

            media_info = save_media_file(
                base64_data=b64_data,
                media_type=file_type,
                bot_profile_id=self.bot_profile_id,
                chat_name=chat_name,
                direction="received",
                original_filename=original_filename,
            )
        except Exception as e:
            logger.error(f"Bot {self.bot_profile_id}: Failed to download media: {e}")
            media_info = None

        # Analyze media with AI if available
        media_analysis = None
        if media_info and self.instance.ai_response_enabled:
            try:
                provider = self._get_ai_provider()
                media_analysis = analyze_media_with_ai(
                    provider,
                    media_info["local_file_path"],
                    file_type,
                    caption,
                )
            except Exception as e:
                logger.error(f"Bot {self.bot_profile_id}: Media analysis error: {e}")

        with get_db_session() as db:
            conversation = find_or_create_conversation(
                db,
                self.bot_profile_id,
                chat_id,
                chat_name,
                is_group=is_group,
                display_name=chat_name,
            )

            # Build content text
            content = caption
            if not content and media_analysis:
                content = f"[Media: {file_type}]"
            elif not content:
                content = f"[Media: {file_type}]"

            user_msg = save_user_message(
                db,
                conversation.id,
                content,
                sender_name=sender_name,
                sender_id=sender_id,
                platform_message_id=str(msg.message_id),
                file_url=media_info["file_url"] if media_info else None,
                file_type=file_type,
                file_name=original_filename,
                file_size=media_info["file_size"] if media_info else None,
                file_pages=media_info.get("file_pages") if media_info else None,
                media_analysis=media_analysis,
            )
            update_conversation_stats(db, conversation.id)
            db.commit()

            broadcast_user_message(
                conversation.id,
                self.bot_profile_id,
                user_msg,
                conversation,
            )

            # Generate AI response if enabled
            if not self.instance.ai_response_enabled:
                return
            if is_human_takeover_active(db, conversation.id):
                return

            broadcast_typing(conversation.id, True, "Bot")
            try:
                await context.bot.send_chat_action(
                    chat_id=int(chat_id), action="typing"
                )
            except Exception:
                pass

            try:
                system_prompt = self.config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db,
                    conversation.id,
                    system_prompt,
                    max_history=self.config.get("max_history", 20) or 20,
                    is_group=is_group,
                    current_message=user_msg,
                )

                provider = self._get_ai_provider()
                response = await asyncio.to_thread(
                    provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=self.config.get("max_tokens", 1000),
                    temperature=self.config.get("temperature", 0.7),
                )
                ai_text = response.content

                if not ai_text:
                    return

                assistant_msg = save_assistant_message(
                    db,
                    conversation.id,
                    ai_text,
                    sender_name="AI Agent",
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

                broadcast_assistant_message(conversation.id, assistant_msg)

                chunks = _split_message(ai_text, 4096)
                for chunk in chunks:
                    await msg.reply_text(chunk)

            except Exception as e:
                logger.error(
                    f"Bot {self.bot_profile_id}: AI response error (media): {e}",
                    exc_info=True,
                )
            finally:
                broadcast_typing(conversation.id, False, "Bot")


def _get_user_display_name(user) -> str:
    """Build a display name from a Telegram User object."""
    if not user:
        return ""
    parts = []
    if user.first_name:
        parts.append(user.first_name)
    if user.last_name:
        parts.append(user.last_name)
    return " ".join(parts) if parts else (user.username or "")
