"""
iMessage Platform Adapter
Uses BlueBubbles server (macOS app) REST API for iMessage automation.

Requirements:
- Mac running BlueBubbles server (https://bluebubbles.app)
- BlueBubbles exposes REST API + WebSocket
- Configure server URL + password in bot settings

Setup:
1. Install BlueBubbles on a Mac
2. Enable Private API features
3. Get the server URL and password
4. Enter in ChatHub bot settings
"""

import asyncio
import logging
import random
from datetime import datetime
from typing import Dict, Optional

import httpx

from app.platforms.base import (
    PlatformAdapter, PlatformCapabilities, PlatformType, AuthMethod,
)

logger = logging.getLogger(__name__)

_active_clients: Dict[int, httpx.AsyncClient] = {}
_bb_config: Dict[int, dict] = {}


class iMessageAdapter(PlatformAdapter):
    """iMessage adapter via BlueBubbles server."""

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.IMESSAGE

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
            supports_profile_pic=False,
            auth_method=AuthMethod.CREDENTIALS,
            max_message_length=20000,
        )

    async def run(self, instance) -> None:
        """Start iMessage adapter via BlueBubbles polling."""
        from app.auth.utils import decrypt_string

        config = instance.config
        bot_profile_id = instance.bot_profile_id

        server_url = config.get("imessage_server_url", "")
        password = config.get("platform_token", "")

        if not server_url or not password:
            instance.error = "BlueBubbles server URL and password required"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            return

        server_url = server_url.rstrip("/")
        encrypted_key = config.get("api_key_encrypted", "")
        ai_api_key = decrypt_string(encrypted_key) if encrypted_key else ""

        client = httpx.AsyncClient(
            base_url=server_url,
            params={"password": password},
            timeout=30.0,
        )
        _active_clients[bot_profile_id] = client
        _bb_config[bot_profile_id] = {"server_url": server_url, "password": password}

        # Test connection
        try:
            resp = await client.get("/api/v1/server/info")
            if resp.status_code != 200:
                instance.error = f"BlueBubbles connection failed: HTTP {resp.status_code}"
                instance.is_running = False
                await instance.notify_status({"error": instance.error, "message": instance.error})
                await client.aclose()
                _active_clients.pop(bot_profile_id, None)
                return
            server_info = resp.json().get("data", {})
            os_version = server_info.get("os_version", "Unknown")
            logger.info(f"Bot {bot_profile_id}: BlueBubbles connected (macOS {os_version})")
        except Exception as e:
            instance.error = f"Cannot reach BlueBubbles: {e}"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            await client.aclose()
            _active_clients.pop(bot_profile_id, None)
            return

        # Save account info
        try:
            from app.database import SessionLocal, BotProfile as BPModel
            _db = SessionLocal()
            try:
                _bot = _db.query(BPModel).filter(BPModel.id == bot_profile_id).first()
                if _bot:
                    _bot.whatsapp_name = f"iMessage (macOS {os_version})"
                    _db.commit()
            finally:
                _db.close()
        except Exception:
            pass

        instance.whatsapp_connected = True
        await instance.notify_status({
            "message": f"iMessage connected via BlueBubbles",
            "connected": True, "platform": "imessage", "status": "running",
        })

        # Handler
        handler = _HandlerContext(bot_profile_id, instance, ai_api_key, config, client)
        last_message_ts = int(datetime.utcnow().timestamp() * 1000)

        logger.info(f"Bot {bot_profile_id}: iMessage polling started")

        try:
            while instance.is_running and not instance.stopped_by_user:
                try:
                    # Poll for new messages
                    resp = await client.post("/api/v1/message/query", json={
                        "after": last_message_ts,
                        "limit": 50,
                        "sort": "ASC",
                        "with": ["chat", "handle"],
                    })
                    if resp.status_code == 200:
                        messages = resp.json().get("data", [])
                        for msg in messages:
                            ts = msg.get("dateCreated", 0)
                            if ts > last_message_ts:
                                last_message_ts = ts
                            if not msg.get("isFromMe", False):
                                try:
                                    await handler.process_message(msg)
                                except Exception as e:
                                    logger.error(f"Bot {bot_profile_id}: Error processing: {e}")

                except httpx.TimeoutException:
                    pass
                except Exception as e:
                    logger.warning(f"Bot {bot_profile_id}: Poll error: {e}")
                    await asyncio.sleep(5)

                # Outbound queue
                if instance.has_outbound_messages():
                    for msg in instance.get_outbound_messages():
                        try:
                            await self.send_message(bot_profile_id, msg["chat_id"], "", msg["message"])
                        except Exception as e:
                            logger.error(f"Bot {bot_profile_id}: Outbound error: {e}")

                await asyncio.sleep(2)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: iMessage task cancelled")
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: iMessage error: {e}", exc_info=True)
            instance.error = str(e)
        finally:
            await client.aclose()
            _active_clients.pop(bot_profile_id, None)
            _bb_config.pop(bot_profile_id, None)
            instance.whatsapp_connected = False
            instance.is_running = False
            logger.info(f"Bot {bot_profile_id}: iMessage adapter stopped")

    async def send_message(self, bot_profile_id, chat_id, chat_name, message) -> bool:
        client = _active_clients.get(bot_profile_id)
        if not client:
            return False
        try:
            resp = await client.post("/api/v1/message/text", json={
                "chatGuid": chat_id,
                "message": message,
            })
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Send failed: {e}")
            return False

    async def send_file(self, bot_profile_id, chat_id, file_path, caption="", file_type="", chat_name="") -> bool:
        client = _active_clients.get(bot_profile_id)
        if not client:
            return False
        try:
            import base64
            from pathlib import Path
            path = Path(file_path)
            if not path.exists():
                return False
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            resp = await client.post("/api/v1/message/attachment", json={
                "chatGuid": chat_id,
                "attachment": f"data:{file_type};base64,{b64}",
                "name": path.name,
            })
            if caption:
                await self.send_message(bot_profile_id, chat_id, "", caption)
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Send file failed: {e}")
            return False

    def cleanup(self, bot_profile_id):
        client = _active_clients.pop(bot_profile_id, None)
        _bb_config.pop(bot_profile_id, None)
        if client:
            try:
                asyncio.get_running_loop().create_task(client.aclose())
            except RuntimeError:
                pass


class _HandlerContext:
    def __init__(self, bot_profile_id, instance, ai_api_key, config, client):
        self.bot_profile_id = bot_profile_id
        self.instance = instance
        self.ai_api_key = ai_api_key
        self.config = config
        self.client = client

    def _get_ai_provider(self):
        from app.ai.factory import get_ai_provider
        return get_ai_provider(
            self.config.get("ai_provider", "openai"), self.ai_api_key, self.config.get("model")
        )

    async def process_message(self, msg):
        from app.platforms.message_handler import (
            get_db_session, find_or_create_conversation, save_user_message,
            save_assistant_message, update_conversation_stats, is_duplicate_message,
            has_response_after, build_ai_messages, is_human_takeover_active,
            is_sender_approved, broadcast_user_message, broadcast_assistant_message,
            broadcast_typing,
        )

        text = msg.get("text", "")
        if not text:
            return

        # Extract chat info
        chats = msg.get("chats", [])
        if not chats:
            return
        chat = chats[0]
        chat_id = chat.get("guid", "")
        chat_name = chat.get("displayName") or chat.get("chatIdentifier", chat_id)
        is_group = chat.get("style", 0) == 43  # 43 = group, 45 = DM

        handle = msg.get("handle", {})
        sender_id = handle.get("address", "")
        sender_name = handle.get("firstName") or handle.get("address", "Unknown")
        if handle.get("lastName"):
            sender_name += f" {handle['lastName']}"

        platform_msg_id = str(msg.get("guid", ""))
        msg_time = None
        ts = msg.get("dateCreated", 0)
        if ts:
            try:
                msg_time = datetime.utcfromtimestamp(ts / 1000)
            except (ValueError, TypeError, OSError):
                pass

        logger.info(f"Bot {self.bot_profile_id}: iMessage from {sender_name}: {text[:80]}")

        with get_db_session() as db:
            conversation = find_or_create_conversation(
                db, self.bot_profile_id, chat_id, chat_name,
                is_group=is_group, display_name=chat_name,
            )

            is_dup, existing = is_duplicate_message(db, conversation.id, platform_message_id=platform_msg_id, content=text)
            if is_dup and existing:
                if has_response_after(db, conversation.id, existing.id):
                    db.commit()
                    return
                db.commit()
                user_msg = existing
            else:
                user_msg = save_user_message(
                    db, conversation.id, text,
                    sender_name=sender_name, sender_id=sender_id,
                    platform_message_id=platform_msg_id, timestamp=msg_time,
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

            delay_min = self.config.get("response_delay_min", 0) or 0
            delay_max = self.config.get("response_delay_max", 0) or 0
            if delay_min > 0 or delay_max > 0:
                await asyncio.sleep(random.uniform(delay_min, max(delay_min, delay_max)))

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

                assistant_msg = save_assistant_message(db, conversation.id, ai_text, sender_name="AI Agent")
                update_conversation_stats(db, conversation.id)
                db.commit()
                broadcast_assistant_message(conversation.id, assistant_msg)

                # Send via BlueBubbles (reply to original message)
                await self.client.post("/api/v1/message/text", json={
                    "chatGuid": chat_id, "message": ai_text,
                })

            except Exception as e:
                logger.error(f"Bot {self.bot_profile_id}: AI error: {e}", exc_info=True)
            finally:
                broadcast_typing(conversation.id, False, "Bot")
