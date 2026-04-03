"""
LINE Platform Adapter
Implements LINE Messaging API webhook-based integration.

LINE bots work differently from WhatsApp browser automation:
- Receive messages via HTTP webhook (LINE pushes events to us)
- Reply using reply tokens (valid for 1 minute) or push message API
- Authentication via Channel Access Token + Channel Secret
- Webhook signature validation using HMAC-SHA256
"""

import asyncio
import base64
import hashlib
import hmac
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import httpx

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)
from app.platforms import message_handler

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2"
LINE_API_DATA = "https://api-data.line.me/v2"


class LineAdapter(PlatformAdapter):
    """LINE Messaging API adapter using webhooks.

    Unlike WhatsApp (browser automation with long-running loop), LINE uses:
    - Webhook endpoint to receive events (registered in run())
    - REST API to send replies and push messages
    - Channel Access Token for API auth
    - Channel Secret for webhook signature validation
    """

    def __init__(self):
        # Per-bot state: bot_profile_id -> config dict
        self._bot_configs: Dict[int, Dict[str, Any]] = {}
        # Per-bot HTTP clients
        self._http_clients: Dict[int, httpx.AsyncClient] = {}
        # Per-bot AI providers
        self._ai_providers: Dict[int, Any] = {}

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.LINE

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=False,
            supports_read_receipts=True,
            supports_typing_indicator=True,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=True,
            supports_profile_pic=True,
            auth_method=AuthMethod.API_TOKEN,
            max_message_length=5000,
            supported_media_types=[
                "image/jpeg", "image/png",
                "video/mp4",
                "audio/m4a", "audio/mpeg",
                "application/pdf",
            ],
        )

    async def run(self, instance) -> None:
        """Register this bot and set up for webhook processing.

        Unlike WhatsApp's long-running browser loop, LINE bots are event-driven:
        - We register the bot config so the webhook handler can find it
        - The webhook endpoint (registered in FastAPI routes) calls handle_webhook()
        - We keep the task alive to match the BotManager lifecycle
        """
        bot_profile_id = instance.bot_profile_id
        config = instance.config

        # Extract LINE credentials from config (fallback to platform_token)
        channel_access_token = self._get_credential(config, "channel_access_token") or config.get("platform_token")
        channel_secret = self._get_credential(config, "channel_secret")

        if not channel_access_token or not channel_secret:
            error_msg = "LINE Channel Access Token and Channel Secret are required"
            logger.error(f"Bot {bot_profile_id}: {error_msg}")
            instance.error = error_msg
            instance.is_running = False
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return

        # Store config for webhook processing
        self._bot_configs[bot_profile_id] = {
            "channel_access_token": channel_access_token,
            "channel_secret": channel_secret,
            **config,
        }

        # Create HTTP client for LINE API calls
        self._http_clients[bot_profile_id] = httpx.AsyncClient(
            base_url=LINE_API_BASE,
            headers={
                "Authorization": f"Bearer {channel_access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        # Initialize AI provider
        try:
            self._init_ai_provider(bot_profile_id, config)
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: AI provider init failed: {e}")

        # Mark as connected (LINE webhook is always "connected" once configured)
        instance.platform_connected = True  # Reuses the generic connected flag
        instance.is_running = True
        await instance.notify_status({
            "status": "connected",
            "message": "LINE bot connected and ready for webhooks",
        })

        logger.info(f"Bot {bot_profile_id}: LINE adapter ready for webhook events")

        # Keep the task alive (process outbound queue periodically)
        try:
            while instance.is_running and not instance.stopped_by_user:
                # Process any outbound (push) messages from the queue
                if instance.has_outbound_messages():
                    messages = instance.get_outbound_messages()
                    for msg in messages:
                        try:
                            await self._push_text_message(
                                bot_profile_id, msg["chat_id"], msg["message"]
                            )
                        except Exception as e:
                            logger.error(
                                f"Bot {bot_profile_id}: Failed to send queued message: {e}"
                            )
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: LINE adapter task cancelled")
        finally:
            instance.platform_connected = False
            instance.is_running = False
            logger.info(f"Bot {bot_profile_id}: LINE adapter stopped")

    def _get_credential(self, config: dict, key: str) -> Optional[str]:
        """Extract a credential from config, decrypting if needed."""
        # Check for direct value first
        value = config.get(key)
        if value:
            return value

        # Check for encrypted version
        encrypted_key = f"{key}_encrypted"
        encrypted = config.get(encrypted_key)
        if encrypted:
            try:
                from app.auth.utils import decrypt_string
                return decrypt_string(encrypted)
            except Exception as e:
                logger.error(f"Failed to decrypt {key}: {e}")
                return None

        return None

    def _init_ai_provider(self, bot_profile_id: int, config: dict):
        """Initialize the AI provider for a bot."""
        from app.ai.factory import get_ai_provider
        from app.auth.utils import decrypt_string

        api_key = config.get("api_key")
        if not api_key and config.get("api_key_encrypted"):
            api_key = decrypt_string(config["api_key_encrypted"])

        if api_key:
            provider = get_ai_provider(
                config.get("ai_provider", "openai"),
                api_key,
                model=config.get("model"),
            )
            self._ai_providers[bot_profile_id] = provider
            logger.info(
                f"Bot {bot_profile_id}: AI provider "
                f"({config.get('ai_provider', 'openai')}) initialized"
            )

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    def validate_signature(self, bot_profile_id: int, body: bytes, signature: str) -> bool:
        """Validate LINE webhook signature using HMAC-SHA256.

        Args:
            bot_profile_id: Bot to validate for
            body: Raw request body bytes
            signature: X-Line-Signature header value

        Returns:
            True if signature is valid
        """
        config = self._bot_configs.get(bot_profile_id)
        if not config:
            return False

        channel_secret = config["channel_secret"]
        hash_value = hmac.new(
            channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(hash_value).decode("utf-8")
        return hmac.compare_digest(signature, expected)

    async def handle_webhook(self, bot_profile_id: int, events: List[Dict]) -> None:
        """Process LINE webhook events.

        Args:
            bot_profile_id: Bot that received the events
            events: List of LINE event objects
        """
        from app.bots.manager import bot_manager

        instance = bot_manager.get_instance(bot_profile_id)
        if not instance or not instance.is_running:
            logger.warning(f"Bot {bot_profile_id}: Received webhook but bot is not running")
            return

        for event in events:
            event_type = event.get("type")
            try:
                if event_type == "message":
                    await self._handle_message_event(bot_profile_id, instance, event)
                elif event_type == "follow":
                    await self._handle_follow_event(bot_profile_id, event)
                elif event_type == "unfollow":
                    await self._handle_unfollow_event(bot_profile_id, event)
                elif event_type == "join":
                    await self._handle_join_event(bot_profile_id, event)
                elif event_type == "leave":
                    await self._handle_leave_event(bot_profile_id, event)
                else:
                    logger.debug(
                        f"Bot {bot_profile_id}: Unhandled event type: {event_type}"
                    )
            except Exception as e:
                logger.error(
                    f"Bot {bot_profile_id}: Error handling {event_type} event: {e}",
                    exc_info=True,
                )

    async def _handle_message_event(
        self, bot_profile_id: int, instance, event: Dict
    ) -> None:
        """Process an incoming message event."""
        config = self._bot_configs.get(bot_profile_id, {})
        source = event.get("source", {})
        message = event.get("message", {})
        reply_token = event.get("replyToken")
        timestamp = event.get("timestamp", 0)

        # Determine chat context
        source_type = source.get("type")  # user, group, room
        user_id = source.get("userId", "")
        is_group = source_type in ("group", "room")

        if source_type == "group":
            chat_id = source.get("groupId", "")
        elif source_type == "room":
            chat_id = source.get("roomId", "")
        else:
            chat_id = user_id

        if not chat_id:
            logger.warning(f"Bot {bot_profile_id}: No chat_id in event")
            return

        # Get sender profile
        sender_name = "Unknown"
        sender_pic = ""
        try:
            profile = await self._get_user_profile(bot_profile_id, user_id, chat_id if is_group else None, source_type)
            if profile:
                sender_name = profile.get("displayName", "Unknown")
                sender_pic = profile.get("pictureUrl", "")
        except Exception as e:
            logger.debug(f"Bot {bot_profile_id}: Could not get profile for {user_id}: {e}")

        # Chat name: use group name for groups, sender name for DMs
        chat_name = sender_name
        if is_group:
            try:
                group_info = await self._get_group_info(bot_profile_id, chat_id, source_type)
                chat_name = group_info.get("groupName", chat_id) if group_info else chat_id
            except Exception:
                chat_name = chat_id

        # Parse message content
        msg_type = message.get("type", "text")
        msg_id = message.get("id", "")
        text_content = ""
        file_info = None

        if msg_type == "text":
            text_content = message.get("text", "")
        elif msg_type == "sticker":
            package_id = message.get("packageId", "")
            sticker_id = message.get("stickerId", "")
            text_content = f"[Sticker: package={package_id}, id={sticker_id}]"
        elif msg_type == "location":
            title = message.get("title", "")
            address = message.get("address", "")
            lat = message.get("latitude", "")
            lng = message.get("longitude", "")
            text_content = f"[Location: {title or address or f'{lat},{lng}'}]"
            if address and title:
                text_content = f"[Location: {title} - {address}]"
        elif msg_type in ("image", "video", "audio", "file"):
            file_info = await self._download_content(bot_profile_id, msg_id, msg_type, message, chat_name)
            if msg_type == "file":
                text_content = f"[File: {message.get('fileName', 'document')}]"
            else:
                text_content = f"[{msg_type.capitalize()}]"

        if not text_content and not file_info:
            return

        # Process through shared message handler
        msg_timestamp = datetime.utcfromtimestamp(timestamp / 1000) if timestamp else datetime.utcnow()

        with message_handler.get_db_session() as db:
            # Get bot owner user_id for activity logging
            from app.database import BotProfile
            bot_profile = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
            owner_user_id = bot_profile.user_id if bot_profile else None

            # Find or create conversation
            conversation = message_handler.find_or_create_conversation(
                db,
                bot_profile_id,
                chat_id,
                chat_name,
                is_group=is_group,
                phone=user_id,
                display_name=sender_name if not is_group else chat_name,
                profile_pic=sender_pic,
            )

            # Dedup check
            is_dup, existing = message_handler.is_duplicate_message(
                db,
                conversation.id,
                platform_message_id=msg_id,
                content=text_content,
            )
            if is_dup:
                logger.debug(f"Bot {bot_profile_id}: Duplicate message {msg_id}, skipping")
                if existing and message_handler.has_response_after(db, conversation.id, existing.id):
                    db.commit()
                    return
                # Duplicate but no AI response yet — skip saving, still generate response below
                user_msg = existing
                db.commit()
            else:
                # Media analysis
                media_analysis = None
                file_kwargs = {}
                if file_info:
                    file_kwargs = {
                        "file_url": file_info.get("file_url"),
                        "file_type": file_info.get("file_type"),
                        "file_name": file_info.get("file_name"),
                        "file_size": file_info.get("file_size"),
                        "file_pages": file_info.get("file_pages"),
                    }
                    ai_provider = self._ai_providers.get(bot_profile_id)
                    if ai_provider and file_info.get("local_file_path"):
                        media_analysis = await asyncio.to_thread(
                            message_handler.analyze_media_with_ai,
                            ai_provider,
                            file_info["local_file_path"],
                            file_info.get("file_type", ""),
                            text_content,
                        )
                        file_kwargs["media_analysis"] = media_analysis

                # Save user message
                user_msg = message_handler.save_user_message(
                    db,
                    conversation.id,
                    text_content,
                    sender_name=sender_name,
                    sender_id=user_id,
                    sender_phone=user_id,
                    sender_profile_pic=sender_pic,
                    platform_message_id=msg_id,
                    timestamp=msg_timestamp,
                    **file_kwargs,
                )
                message_handler.update_conversation_stats(db, conversation.id)

                # Log activity
                if owner_user_id:
                    message_handler.log_activity(
                        db, owner_user_id,
                        "line_message_received",
                        f"Message from {sender_name} in '{chat_name}': {text_content[:80]}",
                    )

                db.commit()

                # Broadcast via WebSocket
                message_handler.broadcast_user_message(
                    conversation.id, bot_profile_id, user_msg, conversation
                )

            # Generate AI response if enabled
            if not instance.ai_response_enabled:
                logger.debug(f"Bot {bot_profile_id}: AI responses disabled, skipping")
                return

            if message_handler.is_human_takeover_active(db, conversation.id):
                logger.debug(f"Bot {bot_profile_id}: Human takeover active, skipping AI")
                return

            # Check group chat setting
            if is_group and not config.get("group_chat_enabled", True):
                return

            # Generate AI response
            ai_provider = self._ai_providers.get(bot_profile_id)
            if not ai_provider:
                return

            message_handler.broadcast_typing(conversation.id, True, "Bot")
            await self._send_loading_animation(bot_profile_id, chat_id)

            try:
                system_prompt = config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = message_handler.build_ai_messages(
                    db,
                    conversation.id,
                    system_prompt,
                    max_history=config.get("max_history", 20),
                    is_group=is_group,
                    current_message=user_msg,
                )

                response = await asyncio.to_thread(
                    ai_provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=config.get("max_tokens", 1000),
                    temperature=config.get("temperature", 0.7),
                )
                ai_text = response.content

                if ai_text:
                    # Send reply via LINE (use reply token if fresh, else push)
                    sent = False
                    if reply_token:
                        sent = await self._reply_message(bot_profile_id, reply_token, ai_text)
                    if not sent:
                        # Reply token expired or failed, use push API
                        await self._push_text_message(bot_profile_id, chat_id, ai_text)

                    # Save assistant message
                    bot_name = config.get("name", "AI Agent")
                    assistant_msg = message_handler.save_assistant_message(
                        db,
                        conversation.id,
                        ai_text,
                        sender_name=bot_name,
                    )
                    message_handler.update_conversation_stats(db, conversation.id)

                    if owner_user_id:
                        message_handler.log_activity(
                            db, owner_user_id,
                            "line_message_sent",
                            f"AI reply to {sender_name} in '{chat_name}': {ai_text[:80]}",
                        )

                    db.commit()

                    message_handler.broadcast_assistant_message(
                        conversation.id, assistant_msg
                    )

            except Exception as e:
                logger.error(
                    f"Bot {bot_profile_id}: AI response error: {e}", exc_info=True
                )
            finally:
                message_handler.broadcast_typing(conversation.id, False, "Bot")

    async def _handle_follow_event(self, bot_profile_id: int, event: Dict) -> None:
        """Handle user following (adding) the bot."""
        user_id = event.get("source", {}).get("userId", "")
        logger.info(f"Bot {bot_profile_id}: User {user_id} followed the bot")

    async def _handle_unfollow_event(self, bot_profile_id: int, event: Dict) -> None:
        """Handle user unfollowing (blocking) the bot."""
        user_id = event.get("source", {}).get("userId", "")
        logger.info(f"Bot {bot_profile_id}: User {user_id} unfollowed the bot")

    async def _handle_join_event(self, bot_profile_id: int, event: Dict) -> None:
        """Handle bot being added to a group."""
        source = event.get("source", {})
        group_id = source.get("groupId") or source.get("roomId", "")
        logger.info(f"Bot {bot_profile_id}: Joined group/room {group_id}")

    async def _handle_leave_event(self, bot_profile_id: int, event: Dict) -> None:
        """Handle bot being removed from a group."""
        source = event.get("source", {})
        group_id = source.get("groupId") or source.get("roomId", "")
        logger.info(f"Bot {bot_profile_id}: Left group/room {group_id}")

    # ------------------------------------------------------------------
    # LINE API calls
    # ------------------------------------------------------------------

    async def _get_http_client(self, bot_profile_id: int) -> Optional[httpx.AsyncClient]:
        """Get the HTTP client for a bot."""
        client = self._http_clients.get(bot_profile_id)
        if client and not client.is_closed:
            return client
        return None

    async def _reply_message(
        self, bot_profile_id: int, reply_token: str, text: str
    ) -> bool:
        """Reply using a reply token (free, but expires in 1 minute)."""
        client = await self._get_http_client(bot_profile_id)
        if not client:
            return False

        # Split long messages
        chunks = self._split_message(text)
        messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]  # LINE max 5 per reply

        try:
            resp = await client.post(
                "/bot/message/reply",
                json={"replyToken": reply_token, "messages": messages},
            )
            if resp.status_code == 200:
                return True
            logger.warning(
                f"Bot {bot_profile_id}: Reply failed ({resp.status_code}): {resp.text}"
            )
            return False
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Reply error: {e}")
            return False

    async def _push_text_message(
        self, bot_profile_id: int, to: str, text: str
    ) -> bool:
        """Send a push message (costs messaging quota)."""
        client = await self._get_http_client(bot_profile_id)
        if not client:
            return False

        chunks = self._split_message(text)
        messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]

        try:
            resp = await client.post(
                "/bot/message/push",
                json={"to": to, "messages": messages},
            )
            if resp.status_code == 200:
                return True
            logger.warning(
                f"Bot {bot_profile_id}: Push failed ({resp.status_code}): {resp.text}"
            )
            return False
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Push error: {e}")
            return False

    async def _push_media_message(
        self,
        bot_profile_id: int,
        to: str,
        media_type: str,
        media_url: str,
        preview_url: str = "",
        caption: str = "",
    ) -> bool:
        """Send a media push message."""
        client = await self._get_http_client(bot_profile_id)
        if not client:
            return False

        messages = []

        if media_type.startswith("image/"):
            messages.append({
                "type": "image",
                "originalContentUrl": media_url,
                "previewImageUrl": preview_url or media_url,
            })
        elif media_type.startswith("video/"):
            messages.append({
                "type": "video",
                "originalContentUrl": media_url,
                "previewImageUrl": preview_url or media_url,
            })
        elif media_type.startswith("audio/"):
            messages.append({
                "type": "audio",
                "originalContentUrl": media_url,
                "duration": 60000,  # Default 60s, LINE requires this field
            })
        else:
            # LINE doesn't support arbitrary file types natively
            # Send as text link
            messages.append({
                "type": "text",
                "text": f"File: {media_url}",
            })

        if caption:
            messages.append({"type": "text", "text": caption})

        try:
            resp = await client.post(
                "/bot/message/push",
                json={"to": to, "messages": messages[:5]},
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Media push error: {e}")
            return False

    async def _send_loading_animation(self, bot_profile_id: int, chat_id: str) -> None:
        """Send LINE loading animation (typing indicator) to a chat."""
        client = await self._get_http_client(bot_profile_id)
        if not client:
            return
        try:
            await client.post(
                "/bot/chat/loading",
                json={"chatId": chat_id, "loadingSeconds": 10},
            )
        except Exception as e:
            logger.debug(f"Bot {bot_profile_id}: Loading animation error: {e}")

    async def _get_user_profile(
        self, bot_profile_id: int, user_id: str, group_id: str = None, source_type: str = "user"
    ) -> Optional[Dict]:
        """Get LINE user profile."""
        client = await self._get_http_client(bot_profile_id)
        if not client:
            return None

        try:
            if group_id and source_type == "group":
                resp = await client.get(f"/bot/group/{group_id}/member/{user_id}")
            elif group_id and source_type == "room":
                resp = await client.get(f"/bot/room/{group_id}/member/{user_id}")
            else:
                resp = await client.get(f"/bot/profile/{user_id}")

            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug(f"Bot {bot_profile_id}: Profile fetch error: {e}")
        return None

    async def _get_group_info(
        self, bot_profile_id: int, group_id: str, source_type: str = "group"
    ) -> Optional[Dict]:
        """Get LINE group/room summary."""
        client = await self._get_http_client(bot_profile_id)
        if not client:
            return None

        try:
            if source_type == "group":
                resp = await client.get(f"/bot/group/{group_id}/summary")
            else:
                return None  # Rooms don't have summary API

            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    async def _download_content(
        self, bot_profile_id: int, message_id: str, msg_type: str, message: Dict,
        chat_name: str = "line_media",
    ) -> Optional[Dict]:
        """Download media content from LINE and save locally."""
        config = self._bot_configs.get(bot_profile_id, {})
        token = config.get("channel_access_token")
        if not token:
            return None

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    f"{LINE_API_DATA}/bot/message/{message_id}/content",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Bot {bot_profile_id}: Content download failed: {resp.status_code}"
                    )
                    return None

                content_type = resp.headers.get("content-type", "application/octet-stream")
                file_bytes = resp.content

            # Use actual content-type from response; fall back to defaults only if missing
            fallback_map = {
                "image": "image/jpeg",
                "video": "video/mp4",
                "audio": "audio/m4a",
            }
            if content_type and content_type != "application/octet-stream":
                file_type = content_type.split(";")[0].strip()
            else:
                file_type = fallback_map.get(msg_type, content_type)
            file_name = message.get("fileName", f"{msg_type}_{message_id}")

            # Save using shared utility
            b64_data = base64.b64encode(file_bytes).decode("utf-8")
            return message_handler.save_media_file(
                b64_data,
                file_type,
                bot_profile_id,
                chat_name,
                direction="received",
                original_filename=file_name,
            )

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Content download error: {e}")
            return None

    def _split_message(self, text: str) -> List[str]:
        """Split text into chunks that fit LINE's 5000 char limit."""
        max_len = self.capabilities.max_message_length or 5000
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try to split at newline
            split_pos = text.rfind("\n", 0, max_len)
            if split_pos <= 0:
                split_pos = text.rfind(" ", 0, max_len)
            if split_pos <= 0:
                split_pos = max_len
            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip()
        return chunks

    # ------------------------------------------------------------------
    # PlatformAdapter interface
    # ------------------------------------------------------------------

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via push API."""
        return await self._push_text_message(bot_profile_id, chat_id, message)

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send a file via push API.

        LINE requires publicly accessible URLs for media. If file_path is local,
        we need the server's public URL. For now, send as a text notification
        for non-URL paths.
        """
        if file_path.startswith("http"):
            return await self._push_media_message(
                bot_profile_id, chat_id, file_type, file_path, caption=caption
            )

        # Local file - send notification text
        file_name = Path(file_path).name
        msg = f"[File: {file_name}]"
        if caption:
            msg = f"{caption}\n{msg}"
        return await self._push_text_message(bot_profile_id, chat_id, msg)

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a bot."""
        self._bot_configs.pop(bot_profile_id, None)
        self._ai_providers.pop(bot_profile_id, None)

        client = self._http_clients.pop(bot_profile_id, None)
        if client and not client.is_closed:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.aclose())
            except RuntimeError:
                # No running event loop — close synchronously via new loop
                try:
                    asyncio.run(client.aclose())
                except Exception:
                    pass

    def get_bot_profile_id_by_channel(self, channel_id: str) -> Optional[int]:
        """Find bot_profile_id by LINE channel ID.

        Used by the webhook route to route incoming events to the correct bot.
        """
        # For now, we match by checking stored configs
        # In production, you'd store channel_id -> bot_profile_id mapping
        for bot_id, config in self._bot_configs.items():
            if config.get("line_channel_id") == channel_id:
                return bot_id
        return None

    def get_active_bot_ids(self) -> List[int]:
        """Get all active LINE bot profile IDs."""
        return list(self._bot_configs.keys())

    def is_bot_active(self, bot_profile_id: int) -> bool:
        """Check if a bot is registered and active."""
        return bot_profile_id in self._bot_configs
