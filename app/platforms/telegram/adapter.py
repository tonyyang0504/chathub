"""
Telegram Platform Adapter
Uses Telethon (Telegram Client API) to login as a real user account.

Auth flow: Phone number → SMS code → optional 2FA password
Session persists in data/sessions/bot_{id}/telegram.session

This is NOT a @BotFather bot — it connects as a real Telegram account,
just like WhatsApp connects via browser automation.
"""

import asyncio
import base64
import io
import json
import logging
import random
from pathlib import Path
from typing import Dict, Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
    AuthKeyUnregisteredError,
)
from telethon.tl.types import User as TelegramUser, Chat, Channel

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)

# Active Telethon clients keyed by bot_profile_id
_active_clients: Dict[int, TelegramClient] = {}

# Session directory
def _session_path(bot_profile_id: int) -> Path:
    base = Path(__file__).resolve().parent.parent.parent / "data" / "sessions" / f"bot_{bot_profile_id}"
    base.mkdir(parents=True, exist_ok=True)
    return base / "telegram.session"


def _get_user_display_name(user) -> str:
    """Build display name from Telethon User entity."""
    if not user:
        return ""
    if isinstance(user, dict):
        parts = [user.get("first_name", ""), user.get("last_name", "")]
        return " ".join(p for p in parts if p) or user.get("username", "")
    parts = []
    if getattr(user, "first_name", None):
        parts.append(user.first_name)
    if getattr(user, "last_name", None):
        parts.append(user.last_name)
    return " ".join(parts) if parts else (getattr(user, "username", None) or "")


class TelegramAdapter(PlatformAdapter):
    """Telegram adapter using Telethon Client API (real account login)."""

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
            supports_read_receipts=True,
            supports_typing_indicator=True,
            supports_history_sync=True,
            supports_contacts_list=True,
            supports_groups_list=True,
            supports_profile_pic=True,
            auth_method=AuthMethod.PHONE_CODE,
            max_message_length=4096,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "video/mp4",
                "audio/mpeg", "audio/ogg",
                "application/pdf",
            ],
        )

    async def run(self, instance) -> None:
        """Start Telegram client and enter message loop."""
        from app.config import settings
        from app.auth.utils import decrypt_string

        config = instance.config
        bot_profile_id = instance.bot_profile_id

        # Telegram API credentials: per-bot from platform_config, fallback to .env
        api_id = config.get("telegram_api_id") or settings.TELEGRAM_API_ID
        api_hash = config.get("telegram_api_hash") or settings.TELEGRAM_API_HASH

        if api_id:
            try:
                api_id = int(api_id)
            except (ValueError, TypeError):
                api_id = 0

        # Get AI API key for responses
        encrypted_key = config.get("api_key_encrypted", "")
        ai_api_key = decrypt_string(encrypted_key) if encrypted_key else ""

        # Load existing session
        session_file = _session_path(bot_profile_id)
        session_string = ""
        if session_file.exists():
            try:
                session_string = session_file.read_text().strip()
                logger.info(f"Bot {bot_profile_id}: Loading existing Telegram session")
            except Exception:
                session_string = ""

        # If no API credentials and no existing session, ask user for everything
        if (not api_id or not api_hash) and not session_string:
            logger.info(f"Bot {bot_profile_id}: No Telegram credentials — requesting via modal")
            creds = await self._request_credentials(instance, bot_profile_id)
            if not creds:
                instance.is_running = False
                return
            api_id = int(creds["api_id"])
            api_hash = creds["api_hash"]
            phone = creds["phone"]

            # Save credentials to DB for future reconnects
            self._save_credentials_to_db(bot_profile_id, api_id, api_hash)

            # Create client and authenticate with phone
            session = StringSession()
            client = TelegramClient(session, api_id, api_hash)
            try:
                await client.connect()
                authorized = await self._authenticate_with_phone(client, instance, bot_profile_id, phone)
                if not authorized:
                    await client.disconnect()
                    instance.is_running = False
                    return
            except Exception as e:
                logger.error(f"Bot {bot_profile_id}: Auth failed: {e}", exc_info=True)
                await instance.notify_status({"error": str(e), "message": f"Authentication failed: {e}"})
                try:
                    await client.disconnect()
                except Exception:
                    pass
                instance.is_running = False
                return
        else:
            # Have credentials — create client and connect
            if not api_id or not api_hash:
                error_msg = "Telegram API credentials missing. Delete and recreate the bot to re-enter them."
                instance.error = error_msg
                instance.is_running = False
                await instance.notify_status({"error": error_msg, "message": error_msg})
                return

            session = StringSession(session_string) if session_string else StringSession()
            client = TelegramClient(session, api_id, api_hash)

            try:
                await client.connect()

                if not await client.is_user_authorized():
                    # Session expired or first time with saved credentials — need phone
                    logger.info(f"Bot {bot_profile_id}: Telegram auth required")
                    creds = await self._request_credentials(instance, bot_profile_id, has_api_creds=True)
                    if not creds:
                        await client.disconnect()
                        instance.is_running = False
                        return
                    phone = creds["phone"]
                    authorized = await self._authenticate_with_phone(client, instance, bot_profile_id, phone)
                    if not authorized:
                        await client.disconnect()
                        instance.is_running = False
                        return
            except Exception as e:
                logger.error(f"Bot {bot_profile_id}: Connection failed: {e}", exc_info=True)
                await instance.notify_status({"error": str(e), "message": f"Connection failed: {e}"})
                try:
                    await client.disconnect()
                except Exception:
                    pass
                instance.is_running = False
                return

        # === Connected — save session, register handlers, run loop ===
        try:
            session_file.write_text(client.session.save())
            logger.info(f"Bot {bot_profile_id}: Telegram session saved")

            me = await client.get_me()
            display_name = _get_user_display_name(me)
            phone = getattr(me, "phone", "") or ""

            logger.info(f"Bot {bot_profile_id}: Telegram connected as {display_name} (+{phone})")

            _active_clients[bot_profile_id] = client

            await self._save_account_info(bot_profile_id, me)

            instance.whatsapp_connected = True
            await instance.notify_status({
                "message": f"Telegram connected: {display_name}",
                "connected": True,
                "platform": "telegram",
                "status": "running",
                "account_info": {
                    "phone": phone,
                    "name": display_name,
                    "username": getattr(me, "username", "") or "",
                },
            })

            handler_ctx = _HandlerContext(
                bot_profile_id=bot_profile_id,
                instance=instance,
                ai_api_key=ai_api_key,
                config=config,
                client=client,
                my_id=me.id,
            )

            @client.on(events.NewMessage(incoming=True))
            async def on_new_message(event):
                await handler_ctx.handle_message(event)

            logger.info(f"Bot {bot_profile_id}: Telegram listening for messages")

            while instance.is_running and not instance.stopped_by_user:
                # Check for history sync request
                if getattr(instance, 'history_sync_requested', False):
                    instance.history_sync_requested = False
                    instance.history_sync_active = True
                    try:
                        await self._sync_history(client, instance, bot_profile_id)
                    except Exception as e:
                        logger.error(f"Bot {bot_profile_id}: History sync error: {e}", exc_info=True)
                    finally:
                        instance.history_sync_active = False

                # Process outbound messages
                if instance.has_outbound_messages():
                    outbound = instance.get_outbound_messages()
                    for msg in outbound:
                        try:
                            await client.send_message(int(msg["chat_id"]), msg["message"])
                        except Exception as e:
                            logger.error(f"Bot {bot_profile_id}: Failed to send outbound: {e}")
                await asyncio.sleep(1)

        except AuthKeyUnregisteredError:
            logger.warning(f"Bot {bot_profile_id}: Session expired, clearing")
            session_file.unlink(missing_ok=True)
            instance.error = "Telegram session expired. Please restart to re-authenticate."
            await instance.notify_status({"error": instance.error, "message": instance.error})
        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: Telegram task cancelled")
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Telegram error: {e}", exc_info=True)
            instance.error = str(e)
            await instance.notify_status({"error": str(e), "message": f"Telegram error: {e}"})
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            _active_clients.pop(bot_profile_id, None)
            instance.whatsapp_connected = False
            instance.is_running = False
            logger.info(f"Bot {bot_profile_id}: Telegram adapter stopped")

    async def _request_credentials(self, instance, bot_profile_id: int, has_api_creds: bool = False) -> Optional[dict]:
        """Ask user for Telegram credentials via the connection modal.

        If has_api_creds=True, only ask for phone number (API ID/Hash already stored).
        Otherwise ask for all three: API ID, API Hash, Phone.
        """
        step = "phone" if has_api_creds else "credentials"
        instance.auth_pending = {"step": step, "value": None}

        if has_api_creds:
            await instance.notify_status({
                "auth_step": "phone",
                "message": "Enter your Telegram phone number (with country code)",
            })
        else:
            await instance.notify_status({
                "auth_step": "credentials",
                "message": "Enter your Telegram credentials to connect",
            })

        result = await self._wait_for_auth_input(instance, step, bot_profile_id, timeout=180)
        if not result:
            await instance.notify_status({"error": "Authentication timed out", "message": "No credentials provided"})
            return None

        if has_api_creds:
            return {"phone": result, "api_id": None, "api_hash": None}

        # result is a dict with api_id, api_hash, phone
        if isinstance(result, dict):
            return result
        return None

    async def _authenticate_with_phone(self, client: TelegramClient, instance, bot_profile_id: int, phone: str) -> bool:
        """Authenticate with phone number — send code, verify, handle 2FA."""
        # Send code
        try:
            result = await client.send_code_request(phone)
            phone_code_hash = result.phone_code_hash
            logger.info(f"Bot {bot_profile_id}: Code sent to {phone}")
        except FloodWaitError as e:
            error_msg = f"Too many attempts. Please wait {e.seconds} seconds."
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return False
        except Exception as e:
            error_msg = f"Failed to send code: {e}"
            logger.error(f"Bot {bot_profile_id}: {error_msg}")
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return False

        # Ask for verification code
        instance.auth_pending = {"step": "code", "value": None}
        await instance.notify_status({
            "auth_step": "code",
            "message": "Enter the verification code sent to your Telegram app",
        })

        code = await self._wait_for_auth_input(instance, "code", bot_profile_id, timeout=120)
        if not code:
            await instance.notify_status({"error": "Authentication timed out", "message": "No code provided"})
            return False

        # Try to sign in
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            return True
        except SessionPasswordNeededError:
            pass  # 2FA needed
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            await instance.notify_status({"error": f"Invalid or expired code", "message": f"Invalid or expired code: {e}"})
            return False
        except Exception as e:
            await instance.notify_status({"error": str(e), "message": f"Sign in failed: {e}"})
            return False

        # 2FA password
        instance.auth_pending = {"step": "password", "value": None}
        await instance.notify_status({
            "auth_step": "password",
            "message": "Enter your two-factor authentication password",
        })

        password = await self._wait_for_auth_input(instance, "password", bot_profile_id, timeout=120)
        if not password:
            await instance.notify_status({"error": "Authentication timed out", "message": "No password provided"})
            return False

        try:
            await client.sign_in(password=password)
            return True
        except PasswordHashInvalidError:
            await instance.notify_status({"error": "Incorrect 2FA password", "message": "Incorrect 2FA password"})
            return False
        except Exception as e:
            await instance.notify_status({"error": str(e), "message": f"2FA failed: {e}"})
            return False

    async def _wait_for_auth_input(self, instance, step: str, bot_profile_id: int, timeout: int = 120) -> Optional[str]:
        """Wait for user to submit auth input via the API."""
        elapsed = 0
        while elapsed < timeout:
            pending = getattr(instance, "auth_pending", None)
            if pending and pending.get("step") == step and pending.get("value") is not None:
                value = pending["value"]
                instance.auth_pending = None
                return value
            await asyncio.sleep(1)
            elapsed += 1
        return None

    def _save_credentials_to_db(self, bot_profile_id: int, api_id: int, api_hash: str):
        """Save Telegram API credentials to platform_config for future reconnects."""
        try:
            import json
            from app.database import SessionLocal, BotProfile
            db = SessionLocal()
            try:
                bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                if bot:
                    platform_config = json.loads(bot.platform_config or "{}")
                    platform_config["telegram_api_id"] = str(api_id)
                    platform_config["telegram_api_hash"] = api_hash
                    bot.platform_config = json.dumps(platform_config)
                    db.commit()
                    logger.info(f"Bot {bot_profile_id}: Saved Telegram API credentials to DB")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: Failed to save credentials: {e}")

    async def _sync_history(self, client: TelegramClient, instance, bot_profile_id: int):
        """Sync conversation history from Telegram using Telethon."""
        from app.platforms.message_handler import (
            get_db_session,
            find_or_create_conversation,
            save_user_message,
            update_conversation_stats,
        )
        from telethon.tl.types import User as TgUser

        sync_count = getattr(instance, 'history_sync_count', 50)
        if sync_count == -1:
            sync_count = 999  # "sync all" — cap at 999 dialogs

        logger.info(f"Bot {bot_profile_id}: Starting Telegram history sync (count={sync_count})")

        instance.history_sync_progress = {
            "total": 0, "completed": 0, "current_chat": "", "status": "running"
        }

        try:
            # Get recent dialogs (chats)
            dialogs = await client.get_dialogs(limit=sync_count)
            total = len(dialogs)
            instance.history_sync_progress["total"] = total

            for i, dialog in enumerate(dialogs):
                if getattr(instance, 'history_sync_stop_requested', False):
                    logger.info(f"Bot {bot_profile_id}: History sync stopped by user")
                    break

                chat_name = dialog.name or f"Chat {dialog.id}"
                instance.history_sync_progress["current_chat"] = chat_name
                instance.history_sync_progress["completed"] = i

                entity = dialog.entity
                is_group = not isinstance(entity, TgUser)
                chat_id = str(dialog.id)

                # Fetch recent messages for this chat
                try:
                    messages = await client.get_messages(dialog, limit=20)
                except Exception as e:
                    logger.warning(f"Bot {bot_profile_id}: Failed to get messages for {chat_name}: {e}")
                    continue

                if not messages:
                    continue

                with get_db_session() as db:
                    conversation = find_or_create_conversation(
                        db, bot_profile_id, chat_id, chat_name,
                        is_group=is_group, display_name=chat_name,
                    )

                    for msg in reversed(messages):  # oldest first
                        if not msg.text:
                            continue
                        sender = getattr(msg, 'sender', None)
                        sender_name = _get_user_display_name(sender) if sender else "Unknown"
                        sender_id = str(msg.sender_id) if msg.sender_id else ""

                        # Skip messages from self (our account)
                        is_outgoing = msg.out
                        role_name = sender_name if not is_outgoing else "Me"

                        save_user_message(
                            db, conversation.id,
                            msg.text,
                            sender_name=role_name,
                            sender_id=sender_id,
                            platform_message_id=str(msg.id),
                            timestamp=msg.date.replace(tzinfo=None) if msg.date else None,
                        )

                    update_conversation_stats(db, conversation.id)
                    db.commit()

            instance.history_sync_progress["completed"] = total
            instance.history_sync_progress["status"] = "completed"
            logger.info(f"Bot {bot_profile_id}: Telegram history sync completed ({total} chats)")

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: History sync failed: {e}", exc_info=True)
            instance.history_sync_progress["status"] = "completed"

    async def _save_account_info(self, bot_profile_id: int, me: TelegramUser):
        """Save Telegram account info to the bot profile in DB."""
        try:
            from app.database import SessionLocal, BotProfile
            db = SessionLocal()
            try:
                bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                if bot:
                    phone = getattr(me, "phone", "") or ""
                    name = _get_user_display_name(me)
                    # Reuse WhatsApp fields for account display
                    bot.whatsapp_phone = phone
                    bot.whatsapp_name = name
                    bot.whatsapp_push_name = getattr(me, "username", "") or ""
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: Failed to save account info: {e}")

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via Telegram."""
        client = _active_clients.get(bot_profile_id)
        if not client:
            return False
        try:
            await client.send_message(int(chat_id), message)
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
        client = _active_clients.get(bot_profile_id)
        if not client:
            return False
        try:
            path = Path(file_path)
            if not path.exists():
                return False
            await client.send_file(int(chat_id), path, caption=caption or None)
            return True
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send file: {e}")
            return False

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up Telegram resources."""
        client = _active_clients.pop(bot_profile_id, None)
        if client:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.disconnect())
            except RuntimeError:
                pass

    def get_contacts(self, bot_profile_id: int):
        """Get contact list from Telegram using Telethon."""
        client = _active_clients.get(bot_profile_id)
        if not client:
            return []
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context — use run_coroutine_threadsafe from a thread
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    self._async_get_contacts(client, bot_profile_id), loop
                )
                return future.result(timeout=30)
            else:
                return loop.run_until_complete(self._async_get_contacts(client, bot_profile_id))
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to get contacts: {e}")
            return []

    async def _async_get_contacts(self, client, bot_profile_id):
        """Async implementation of get_contacts."""
        from telethon.tl.types import User as TgUser
        contacts = []
        try:
            dialogs = await client.get_dialogs(limit=200)
            for dialog in dialogs:
                entity = dialog.entity
                if isinstance(entity, TgUser) and not entity.bot:
                    name = _get_user_display_name(entity)
                    phone = getattr(entity, 'phone', '') or ''
                    contacts.append({
                        "chat_id": str(dialog.id),
                        "name": name,
                        "phone": phone,
                        "profile_pic": "",
                    })
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error fetching contacts: {e}")
        return contacts

    def get_groups(self, bot_profile_id: int):
        """Get group list from Telegram using Telethon."""
        client = _active_clients.get(bot_profile_id)
        if not client:
            return []
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    self._async_get_groups(client, bot_profile_id), loop
                )
                return future.result(timeout=30)
            else:
                return loop.run_until_complete(self._async_get_groups(client, bot_profile_id))
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to get groups: {e}")
            return []

    async def _async_get_groups(self, client, bot_profile_id):
        """Async implementation of get_groups."""
        from telethon.tl.types import User as TgUser
        groups = []
        try:
            dialogs = await client.get_dialogs(limit=200)
            for dialog in dialogs:
                entity = dialog.entity
                if not isinstance(entity, TgUser):
                    name = getattr(entity, 'title', '') or f"Group {dialog.id}"
                    member_count = getattr(entity, 'participants_count', 0) or 0
                    groups.append({
                        "chat_id": str(dialog.id),
                        "name": name,
                        "member_count": member_count,
                    })
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error fetching groups: {e}")
        return groups


class _HandlerContext:
    """Holds per-bot context for processing Telegram messages."""

    def __init__(self, bot_profile_id, instance, ai_api_key, config, client, my_id):
        self.bot_profile_id = bot_profile_id
        self.instance = instance
        self.ai_api_key = ai_api_key
        self.config = config
        self.client = client
        self.my_id = my_id

    def _get_ai_provider(self):
        from app.ai.factory import get_ai_provider
        return get_ai_provider(
            provider_name=self.config.get("ai_provider", "openai"),
            api_key=self.ai_api_key,
            model=self.config.get("model"),
        )

    async def handle_message(self, event):
        """Handle an incoming Telegram message."""
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
            is_sender_approved,
            get_conversation_model,
            broadcast_user_message,
            broadcast_assistant_message,
            broadcast_typing,
        )

        message = event.message
        if not message:
            return

        # Get sender info
        sender = await event.get_sender()
        if not sender:
            return

        # Skip messages from self
        sender_id = getattr(sender, "id", 0)
        if sender_id == self.my_id:
            return

        # Get chat info
        chat = await event.get_chat()
        chat_id = str(event.chat_id)
        is_group = event.is_group or event.is_channel

        if is_group:
            chat_name = getattr(chat, "title", None) or f"Group {chat_id}"
        else:
            chat_name = _get_user_display_name(sender) or f"Chat {chat_id}"

        sender_name = _get_user_display_name(sender)
        text = message.text or message.message or ""
        platform_message_id = str(message.id)

        # Skip if group chats disabled
        if is_group and not self.config.get("group_chat_enabled", True):
            return

        logger.info(f"Bot {self.bot_profile_id}: Message from {sender_name} ({chat_id}): {text[:80]}")

        # Handle media
        file_url = None
        file_type_str = None
        file_name = None
        file_size = None
        media_analysis = None

        if message.media and not message.text:
            # It's a media-only message — set text placeholder
            if not text:
                text = "[Media]"

        with get_db_session() as db:
            conversation = find_or_create_conversation(
                db, self.bot_profile_id, chat_id, chat_name,
                is_group=is_group, display_name=chat_name,
            )

            # Dedup
            is_dup, existing = is_duplicate_message(
                db, conversation.id,
                platform_message_id=platform_message_id, content=text,
            )
            if is_dup and existing:
                if has_response_after(db, conversation.id, existing.id):
                    db.commit()
                    return
                db.commit()
                user_msg = existing
            else:
                user_msg = save_user_message(
                    db, conversation.id, text or "[Media]",
                    sender_name=sender_name,
                    sender_id=str(sender_id),
                    platform_message_id=platform_message_id,
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

            broadcast_user_message(conversation.id, self.bot_profile_id, user_msg, conversation)

            # Check if AI responses are enabled
            if not self.instance.ai_response_enabled:
                return

            if is_human_takeover_active(db, conversation.id):
                return

            if not is_sender_approved(db, conversation.id):
                return  # DM pairing: message saved but no AI response

            broadcast_typing(conversation.id, True, "Bot")

            # Response delay
            delay_min = self.config.get("response_delay_min", 0) or 0
            delay_max = self.config.get("response_delay_max", 0) or 0
            if delay_min > 0 or delay_max > 0:
                delay = random.uniform(delay_min, max(delay_min, delay_max))
                await asyncio.sleep(delay)

            try:
                system_prompt = self.config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db, conversation.id, system_prompt,
                    max_history=self.config.get("max_history", 20) or 20,
                    is_group=is_group, current_message=user_msg,
                )

                provider = self._get_ai_provider()
                # Check for per-conversation model override
                model_override = get_conversation_model(db, conversation.id)
                response = await asyncio.to_thread(
                    provider.chat_completion,
                    messages=ai_messages,
                    model=model_override,  # None = use provider default
                    max_tokens=self.config.get("max_tokens", 1000),
                    temperature=self.config.get("temperature", 0.7),
                )
                ai_text = response.content

                if not ai_text:
                    return

                assistant_msg = save_assistant_message(
                    db, conversation.id, ai_text, sender_name="AI Agent",
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

                broadcast_assistant_message(conversation.id, assistant_msg)

                # Send reply via Telegram
                await event.reply(ai_text)

            except Exception as e:
                logger.error(f"Bot {self.bot_profile_id}: AI response error: {e}", exc_info=True)
            finally:
                broadcast_typing(conversation.id, False, "Bot")
