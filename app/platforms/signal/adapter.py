"""
Signal Platform Adapter
Uses signal-cli REST API for sending/receiving messages.

Signal requires a dedicated phone number. The adapter connects to a
signal-cli-rest-api instance (self-hosted or Docker) via HTTP.

Setup:
1. Run signal-cli-rest-api: docker run -p 8080:8080 bbernhard/signal-cli-rest-api
2. Register phone number via the API: POST /v1/register/{number}
3. Verify with SMS code: POST /v1/register/{number}/verify/{code}
4. Enter the API URL + phone number in ChatHub

Alternative: Use signal-cli directly (no Docker) — install signal-cli,
run `signal-cli daemon --http` to expose HTTP API on localhost.
"""

import asyncio
import json
import logging
import random
from datetime import datetime
from typing import Dict, Any, List, Optional

import httpx

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)

# Active Signal clients keyed by bot_profile_id
_active_clients: Dict[int, httpx.AsyncClient] = {}
_signal_config: Dict[int, dict] = {}


class SignalAdapter(PlatformAdapter):
    """Signal adapter using signal-cli REST API."""

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.SIGNAL

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=True,
            supports_read_receipts=True,
            supports_typing_indicator=False,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=True,
            supports_profile_pic=False,
            auth_method=AuthMethod.CREDENTIALS,
            max_message_length=10000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif",
                "video/mp4",
                "audio/mpeg", "audio/ogg",
                "application/pdf",
            ],
        )

    async def run(self, instance) -> None:
        """Start Signal adapter with polling loop."""
        from app.auth.utils import decrypt_string

        config = instance.config
        bot_profile_id = instance.bot_profile_id

        # Get Signal config
        api_url = config.get("signal_api_url", "http://localhost:8080")
        phone_number = config.get("platform_token") or config.get("signal_phone")

        if not phone_number:
            instance.error = "No Signal phone number configured"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            return

        # Ensure phone number has + prefix
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"

        # Get AI config
        encrypted_key = config.get("api_key_encrypted", "")
        ai_api_key = decrypt_string(encrypted_key) if encrypted_key else ""

        # Create HTTP client
        client = httpx.AsyncClient(base_url=api_url.rstrip("/"), timeout=30.0)
        _active_clients[bot_profile_id] = client
        _signal_config[bot_profile_id] = {
            "phone_number": phone_number,
            "api_url": api_url,
        }

        # Verify connection to signal-cli REST API
        try:
            resp = await client.get("/v1/about")
            if resp.status_code != 200:
                instance.error = f"Cannot connect to signal-cli REST API at {api_url}"
                instance.is_running = False
                await instance.notify_status({"error": instance.error, "message": instance.error})
                await client.aclose()
                _active_clients.pop(bot_profile_id, None)
                return
            logger.info(f"Bot {bot_profile_id}: Connected to signal-cli REST API at {api_url}")
        except Exception as e:
            instance.error = f"Cannot reach signal-cli REST API: {e}"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            await client.aclose()
            _active_clients.pop(bot_profile_id, None)
            return

        # Verify phone number is registered
        try:
            resp = await client.get(f"/v1/accounts")
            accounts = resp.json() if resp.status_code == 200 else []
            registered = any(a.get("number") == phone_number for a in accounts) if isinstance(accounts, list) else False
            if not registered:
                logger.warning(f"Bot {bot_profile_id}: Phone {phone_number} may not be registered. Trying anyway...")
        except Exception:
            pass

        # Save account info
        try:
            from app.database import SessionLocal, BotProfile as BPModel
            _db = SessionLocal()
            try:
                _bot = _db.query(BPModel).filter(BPModel.id == bot_profile_id).first()
                if _bot:
                    _bot.whatsapp_name = phone_number
                    _db.commit()
            finally:
                _db.close()
        except Exception:
            pass

        # Notify connected
        instance.whatsapp_connected = True
        await instance.notify_status({
            "message": f"Signal connected: {phone_number}",
            "connected": True,
            "platform": "signal",
            "status": "running",
            "account_info": {"name": phone_number, "phone": phone_number},
        })

        # Create handler
        handler = _HandlerContext(
            bot_profile_id=bot_profile_id,
            instance=instance,
            ai_api_key=ai_api_key,
            config=config,
            client=client,
            phone_number=phone_number,
        )

        logger.info(f"Bot {bot_profile_id}: Signal polling started for {phone_number}")

        # Polling loop — check for new messages via REST API
        try:
            while instance.is_running and not instance.stopped_by_user:
                try:
                    # Receive messages (long poll)
                    resp = await client.get(
                        f"/v1/receive/{phone_number}",
                        timeout=15.0,
                    )
                    if resp.status_code == 200:
                        messages = resp.json()
                        if isinstance(messages, list):
                            for msg in messages:
                                try:
                                    await handler.process_message(msg)
                                except Exception as e:
                                    logger.error(f"Bot {bot_profile_id}: Error processing message: {e}", exc_info=True)

                except httpx.TimeoutException:
                    pass  # Normal — long poll timeout
                except httpx.HTTPError as e:
                    logger.warning(f"Bot {bot_profile_id}: HTTP error: {e}")
                    await asyncio.sleep(5)

                # Process outbound queue
                if instance.has_outbound_messages():
                    outbound = instance.get_outbound_messages()
                    for msg in outbound:
                        try:
                            await self.send_message(bot_profile_id, msg["chat_id"], "", msg["message"])
                        except Exception as e:
                            logger.error(f"Bot {bot_profile_id}: Outbound error: {e}")

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: Signal task cancelled")
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Signal error: {e}", exc_info=True)
            instance.error = str(e)
            await instance.notify_status({"error": str(e), "message": f"Signal error: {e}"})
        finally:
            await client.aclose()
            _active_clients.pop(bot_profile_id, None)
            _signal_config.pop(bot_profile_id, None)
            instance.whatsapp_connected = False
            instance.is_running = False
            logger.info(f"Bot {bot_profile_id}: Signal adapter stopped")

    async def send_message(
        self, bot_profile_id: int, chat_id: str, chat_name: str, message: str,
    ) -> bool:
        """Send a text message via Signal."""
        client = _active_clients.get(bot_profile_id)
        cfg = _signal_config.get(bot_profile_id)
        if not client or not cfg:
            return False

        try:
            payload = {
                "message": message,
                "number": cfg["phone_number"],
                "recipients": [chat_id],
            }
            resp = await client.post("/v2/send", json=payload)
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send: {e}")
            return False

    async def send_file(
        self, bot_profile_id: int, chat_id: str, file_path: str,
        caption: str = "", file_type: str = "", chat_name: str = "",
    ) -> bool:
        """Send a file via Signal."""
        client = _active_clients.get(bot_profile_id)
        cfg = _signal_config.get(bot_profile_id)
        if not client or not cfg:
            return False

        try:
            import base64
            from pathlib import Path
            path = Path(file_path)
            if not path.exists():
                return False

            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()

            payload = {
                "message": caption or "",
                "number": cfg["phone_number"],
                "recipients": [chat_id],
                "base64_attachments": [f"data:{file_type or 'application/octet-stream'};filename={path.name};base64,{b64}"],
            }
            resp = await client.post("/v2/send", json=payload)
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send file: {e}")
            return False

    def cleanup(self, bot_profile_id: int) -> None:
        client = _active_clients.pop(bot_profile_id, None)
        _signal_config.pop(bot_profile_id, None)
        if client:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.aclose())
            except RuntimeError:
                pass


class _HandlerContext:
    """Holds per-bot context for processing Signal messages."""

    def __init__(self, bot_profile_id, instance, ai_api_key, config, client, phone_number):
        self.bot_profile_id = bot_profile_id
        self.instance = instance
        self.ai_api_key = ai_api_key
        self.config = config
        self.client = client
        self.phone_number = phone_number

    def _get_ai_provider(self):
        from app.ai.factory import get_ai_provider
        return get_ai_provider(
            provider_name=self.config.get("ai_provider", "openai"),
            api_key=self.ai_api_key,
            model=self.config.get("model"),
        )

    async def process_message(self, envelope: dict):
        """Process a Signal message envelope."""
        from app.platforms.message_handler import (
            get_db_session, find_or_create_conversation,
            save_user_message, save_assistant_message,
            update_conversation_stats, is_duplicate_message,
            has_response_after, build_ai_messages,
            is_human_takeover_active, is_sender_approved,
            broadcast_user_message, broadcast_assistant_message,
            broadcast_typing,
        )

        # Extract message data
        data_message = envelope.get("envelope", {}).get("dataMessage")
        if not data_message:
            return

        source = envelope.get("envelope", {}).get("source", "")
        source_name = envelope.get("envelope", {}).get("sourceName", source)
        text = data_message.get("message", "")
        timestamp = data_message.get("timestamp", 0)
        group_info = data_message.get("groupInfo")

        if not text or source == self.phone_number:
            return  # Skip empty messages and own messages

        # Determine chat type
        is_group = group_info is not None
        if is_group:
            chat_id = group_info.get("groupId", "")
            chat_name = group_info.get("groupName") or f"Group {chat_id[:8]}"
        else:
            chat_id = source
            chat_name = source_name or source

        sender_name = source_name or source
        platform_msg_id = str(timestamp)

        # Parse timestamp
        msg_time = None
        if timestamp:
            try:
                msg_time = datetime.utcfromtimestamp(timestamp / 1000)
            except (ValueError, TypeError, OSError):
                pass

        logger.info(f"Bot {self.bot_profile_id}: Signal message from {sender_name}: {text[:80]}")

        # Skip groups if disabled
        if is_group and not self.config.get("group_chat_enabled", True):
            return

        with get_db_session() as db:
            conversation = find_or_create_conversation(
                db, self.bot_profile_id, chat_id, chat_name,
                is_group=is_group, display_name=chat_name,
            )

            # Dedup
            is_dup, existing = is_duplicate_message(
                db, conversation.id,
                platform_message_id=platform_msg_id, content=text,
            )
            if is_dup and existing:
                if has_response_after(db, conversation.id, existing.id):
                    db.commit()
                    return
                db.commit()
                user_msg = existing
            else:
                user_msg = save_user_message(
                    db, conversation.id, text,
                    sender_name=sender_name, sender_id=source,
                    platform_message_id=platform_msg_id,
                    timestamp=msg_time,
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

            broadcast_user_message(conversation.id, self.bot_profile_id, user_msg, conversation)

            if not self.instance.ai_response_enabled:
                return
            if is_human_takeover_active(db, conversation.id):
                return
            if not is_sender_approved(db, conversation.id):
                return

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
                    db, conversation.id, ai_text, sender_name="AI Agent",
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

                broadcast_assistant_message(conversation.id, assistant_msg)

                # Send reply via Signal
                payload = {
                    "message": ai_text,
                    "number": self.phone_number,
                    "recipients": [chat_id] if not is_group else [],
                }
                if is_group:
                    payload["recipients"] = []
                    payload["group"] = chat_id

                await self.client.post("/v2/send", json=payload)

            except Exception as e:
                logger.error(f"Bot {self.bot_profile_id}: AI response error: {e}", exc_info=True)
            finally:
                broadcast_typing(conversation.id, False, "Bot")
