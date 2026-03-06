"""
WeChat Official Account Platform Adapter

Webhook-based integration that receives XML messages via HTTP POST
and replies using the WeChat Customer Service (kefu) async message API.

WeChat message flow:
1. User sends message to Official Account
2. WeChat server POSTs XML to our webhook endpoint
3. We verify signature (token + timestamp + nonce)
4. Parse XML message (text, image, voice, video, location, link)
5. Reply within 5 seconds with XML, or use async customer service API
6. For AI responses (slow), we return "success" immediately and send
   the AI reply via the customer service message API asynchronously.

Access token management:
- Obtained via AppID + AppSecret
- Valid for 2 hours, refreshed proactively before expiry
"""

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Dict, Any, Optional

import defusedxml.ElementTree as ET
import httpx

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)

WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"

# WeChat customer service API text message limit
WECHAT_MAX_MESSAGE_LENGTH = 2048


def verify_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """Verify WeChat server callback signature.

    WeChat sends signature = SHA1(sort([token, timestamp, nonce])).
    """
    parts = sorted([token, timestamp, nonce])
    computed = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, signature)


def parse_xml_message(xml_body: str) -> Dict[str, str]:
    """Parse a WeChat XML message into a flat dict.

    Returns dict with keys like ToUserName, FromUserName, CreateTime,
    MsgType, Content, MsgId, PicUrl, MediaId, Format, etc.
    """
    root = ET.fromstring(xml_body)
    result = {}
    for child in root:
        result[child.tag] = child.text or ""
    return result


class AccessTokenManager:
    """Manages WeChat API access token with auto-refresh.

    The access token expires every 7200 seconds (2 hours).
    We refresh 5 minutes before expiry to avoid race conditions.
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: Optional[str] = None
        self._expires_at: float = 0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        if self._token and time.time() < self._expires_at - 300:
            return self._token
        async with self._lock:
            # Double-check after acquiring lock
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            await self._refresh()
            return self._token

    async def _refresh(self):
        """Fetch a new access token from WeChat API."""
        url = (
            f"{WECHAT_API_BASE}/token"
            f"?grant_type=client_credential"
            f"&appid={self.app_id}"
            f"&secret={self.app_secret}"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            data = resp.json()

        if "access_token" in data:
            self._token = data["access_token"]
            self._expires_at = time.time() + data.get("expires_in", 7200)
            logger.info(f"WeChat access token refreshed, expires in {data.get('expires_in', 7200)}s")
        else:
            errcode = data.get("errcode", "unknown")
            errmsg = data.get("errmsg", "unknown")
            raise RuntimeError(f"Failed to get WeChat access token: {errcode} - {errmsg}")


class WeChatAdapter(PlatformAdapter):
    """WeChat Official Account adapter using webhook + customer service API.

    Configuration (passed via instance.config):
        wechat_app_id: Official Account AppID
        wechat_app_secret: Official Account AppSecret
        wechat_token: Token configured in WeChat MP backend (for signature verification)
        wechat_aes_key: (optional) EncodingAESKey for message encryption
        webhook_port: Port for the webhook HTTP server (default: 8001)
    """

    def __init__(self):
        self._token_managers: Dict[int, AccessTokenManager] = {}
        self._bot_configs: Dict[int, dict] = {}

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.WECHAT

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=False,
            supports_read_receipts=False,
            supports_typing_indicator=False,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=False,
            supports_profile_pic=False,
            auth_method=AuthMethod.CREDENTIALS,
            max_message_length=WECHAT_MAX_MESSAGE_LENGTH,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif",
                "video/mp4",
                "audio/amr", "audio/mp3",
                "application/pdf",
            ],
        )

    def _get_token_manager(self, bot_profile_id: int) -> AccessTokenManager:
        """Get or create an AccessTokenManager for a bot.

        If not already in memory (e.g. external send_message call),
        loads credentials from the database.
        """
        if bot_profile_id in self._token_managers:
            return self._token_managers[bot_profile_id]

        config = self._bot_configs.get(bot_profile_id)
        if not config:
            # Load credentials from DB for external callers
            config = self._load_bot_config_from_db(bot_profile_id)
            self._bot_configs[bot_profile_id] = config

        app_id = config.get("wechat_app_id", "")
        app_secret = config.get("wechat_app_secret", "")
        if not app_id or not app_secret:
            raise RuntimeError(
                f"Bot {bot_profile_id}: WeChat AppID/AppSecret not configured"
            )

        mgr = AccessTokenManager(app_id, app_secret)
        self._token_managers[bot_profile_id] = mgr
        return mgr

    @staticmethod
    def _load_bot_config_from_db(bot_profile_id: int) -> dict:
        """Load WeChat credentials from the bot profile in DB.

        Used when send_message/send_file is called externally
        (e.g. from hub schedulers) without run() being active.
        """
        try:
            from app.database import BotProfile
            from app.auth.utils import decrypt_string
            from app.platforms.message_handler import get_db_session

            with get_db_session() as db:
                bot = db.query(BotProfile).filter(
                    BotProfile.id == bot_profile_id
                ).first()
                if not bot:
                    return {}

                # Parse credentials from the bot's config fields
                api_key = ""
                if bot.api_key_encrypted:
                    try:
                        api_key = decrypt_string(bot.api_key_encrypted)
                    except Exception:
                        pass

                config = {
                    "ai_provider": bot.ai_provider or "openai",
                    "model": bot.model or "gpt-4",
                    "api_key_encrypted": bot.api_key_encrypted or "",
                    "system_prompt": bot.system_prompt or "You are a helpful assistant.",
                    "name": bot.name or "Bot",
                }

                # WeChat-specific config from extra_config JSON column
                if hasattr(bot, "extra_config") and bot.extra_config:
                    import json
                    try:
                        extra = json.loads(bot.extra_config)
                        config.update(extra)
                    except (json.JSONDecodeError, TypeError):
                        pass

                return config
        except Exception as e:
            logger.error(f"Failed to load bot config from DB: {e}")
            return {}

    async def run(self, instance) -> None:
        """Start the WeChat webhook server and process incoming messages.

        Sets up an HTTP endpoint that:
        - Handles GET requests for WeChat server verification
        - Handles POST requests for incoming XML messages
        - Routes messages through shared message_handler for DB/AI processing
        - Sends AI replies via the customer service message API (async)
        """
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        bot_profile_id = instance.bot_profile_id
        config = instance.config
        self._bot_configs[bot_profile_id] = config

        wechat_token = config.get("wechat_token", "")
        app_id = config.get("wechat_app_id", "")
        app_secret = config.get("wechat_app_secret", "")
        webhook_port = int(config.get("webhook_port", 8001))

        if not app_id or not app_secret:
            error_msg = "WeChat AppID and AppSecret are required"
            instance.error = error_msg
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return

        if not wechat_token:
            error_msg = "WeChat verification token is required"
            instance.error = error_msg
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return

        # Initialize token manager
        token_mgr = AccessTokenManager(app_id, app_secret)
        self._token_managers[bot_profile_id] = token_mgr

        # Verify credentials by fetching initial token
        try:
            await token_mgr.get_token()
            logger.info(f"Bot {bot_profile_id}: WeChat credentials verified")
        except Exception as e:
            error_msg = f"WeChat authentication failed: {e}"
            instance.error = error_msg
            await instance.notify_status({"error": error_msg, "message": error_msg})
            return

        await instance.notify_status({
            "message": "WeChat connected",
            "connected": True,
        })
        instance.platform_connected = True  # Generic "connected" flag

        async def handle_verification(request: Request) -> PlainTextResponse:
            """Handle WeChat server URL verification (GET request)."""
            params = request.query_params
            signature = params.get("signature", "")
            timestamp = params.get("timestamp", "")
            nonce = params.get("nonce", "")
            echostr = params.get("echostr", "")

            if verify_signature(wechat_token, timestamp, nonce, signature):
                logger.info(f"Bot {bot_profile_id}: WeChat server verification passed")
                return PlainTextResponse(echostr)
            else:
                logger.warning(f"Bot {bot_profile_id}: WeChat verification failed")
                return PlainTextResponse("Verification failed", status_code=403)

        async def handle_message(request: Request) -> PlainTextResponse:
            """Handle incoming WeChat message (POST request).

            Returns "success" immediately and processes the message
            asynchronously to meet WeChat's 5-second response deadline.
            """
            # Verify signature
            params = request.query_params
            signature = params.get("signature", "")
            timestamp = params.get("timestamp", "")
            nonce = params.get("nonce", "")

            if not verify_signature(wechat_token, timestamp, nonce, signature):
                return PlainTextResponse("Invalid signature", status_code=403)

            body = await request.body()
            xml_body = body.decode("utf-8")

            # Parse XML message
            try:
                msg = parse_xml_message(xml_body)
            except ET.ParseError as e:
                logger.error(f"Bot {bot_profile_id}: Failed to parse XML: {e}")
                return PlainTextResponse("success")

            msg_type = msg.get("MsgType", "")
            from_user = msg.get("FromUserName", "")
            to_user = msg.get("ToUserName", "")
            msg_id = msg.get("MsgId", "")

            logger.info(
                f"Bot {bot_profile_id}: Received WeChat {msg_type} message "
                f"from {from_user}, MsgId={msg_id}"
            )

            # Process message asynchronously (don't block the 5s deadline)
            task = asyncio.create_task(
                self._process_incoming_message(
                    instance, msg, from_user, to_user, msg_type, msg_id
                )
            )
            task.add_done_callback(self._task_error_callback)

            # Return "success" immediately per WeChat requirement
            return PlainTextResponse("success")

        async def webhook_endpoint(request: Request) -> PlainTextResponse:
            """Route GET (verification) and POST (messages) to handlers."""
            if request.method == "GET":
                return await handle_verification(request)
            else:
                return await handle_message(request)

        app = Starlette(
            routes=[
                Route("/wechat/webhook", webhook_endpoint, methods=["GET", "POST"]),
            ]
        )

        # Run the webhook server
        import uvicorn
        server_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=webhook_port,
            log_level="info",
        )
        server = uvicorn.Server(server_config)

        logger.info(
            f"Bot {bot_profile_id}: Starting WeChat webhook on port {webhook_port}"
        )

        outbound_task = None
        try:
            # Process outbound queue in background while server runs
            outbound_task = asyncio.create_task(
                self._process_outbound_queue(instance)
            )
            outbound_task.add_done_callback(self._task_error_callback)
            await server.serve()
        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: WeChat adapter cancelled")
            server.should_exit = True
            raise
        finally:
            if outbound_task and not outbound_task.done():
                outbound_task.cancel()
                try:
                    await outbound_task
                except asyncio.CancelledError:
                    pass
            instance.platform_connected = False
            instance.is_running = False
            self._token_managers.pop(bot_profile_id, None)
            self._bot_configs.pop(bot_profile_id, None)

    @staticmethod
    def _task_error_callback(task: asyncio.Task):
        """Log unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Background task failed: {exc}", exc_info=exc)

    async def _process_incoming_message(
        self,
        instance,
        msg: Dict[str, str],
        from_user: str,
        to_user: str,
        msg_type: str,
        msg_id: str,
    ):
        """Process an incoming WeChat message through the shared message handler.

        Handles text, image, voice, video, location, and link message types.
        """
        from app.platforms.message_handler import (
            get_db_session,
            find_or_create_conversation,
            save_user_message,
            save_assistant_message,
            update_conversation_stats,
            is_duplicate_message,
            has_response_after,
            build_ai_messages,
            broadcast_user_message,
            broadcast_assistant_message,
            broadcast_typing,
            is_human_takeover_active,
            save_media_file,
            analyze_media_with_ai,
            log_activity,
        )
        from app.ai.factory import get_ai_provider
        from app.auth.utils import decrypt_string

        bot_profile_id = instance.bot_profile_id
        config = instance.config

        # Extract message content based on type
        content = ""
        file_info = None

        if msg_type == "text":
            content = msg.get("Content", "")

        elif msg_type == "image":
            pic_url = msg.get("PicUrl", "")
            content = "[Image]"
            if pic_url:
                file_info = await self._download_media(
                    bot_profile_id, pic_url, "image/jpeg", from_user
                )

        elif msg_type == "voice":
            media_id = msg.get("MediaId", "")
            recognition = msg.get("Recognition", "")
            content = recognition if recognition else "[Voice message]"
            if media_id:
                file_info = await self._download_wechat_media(
                    bot_profile_id, media_id, "audio/amr", from_user
                )

        elif msg_type in ("video", "shortvideo"):
            media_id = msg.get("MediaId", "")
            content = "[Video]"
            if media_id:
                file_info = await self._download_wechat_media(
                    bot_profile_id, media_id, "video/mp4", from_user
                )

        elif msg_type == "location":
            lat = msg.get("Location_X", "")
            lng = msg.get("Location_Y", "")
            label = msg.get("Label", "")
            content = f"[Location: {label} ({lat}, {lng})]"

        elif msg_type == "link":
            title = msg.get("Title", "")
            description = msg.get("Description", "")
            url = msg.get("Url", "")
            content = f"[Link: {title}]\n{description}\n{url}"

        elif msg_type == "event":
            event = msg.get("Event", "")
            if event == "subscribe":
                content = "[User subscribed]"
                # Events have no MsgId — generate a synthetic one
                msg_id = f"event_{event}_{from_user}_{int(time.time())}"
            elif event == "unsubscribe":
                logger.info(f"Bot {bot_profile_id}: User {from_user} unsubscribed")
                return
            else:
                logger.info(f"Bot {bot_profile_id}: Ignoring event type: {event}")
                return
        else:
            logger.info(f"Bot {bot_profile_id}: Unsupported message type: {msg_type}")
            return

        if not content and not file_info:
            return

        # Process through shared message handler
        with get_db_session() as db:
            # Find or create conversation
            conversation = find_or_create_conversation(
                db,
                bot_profile_id,
                chat_id=from_user,
                chat_name=from_user,
                is_group=False,
            )

            # Dedup check (only if we have a real MsgId)
            if msg_id:
                is_dup, existing = is_duplicate_message(
                    db,
                    conversation.id,
                    platform_message_id=msg_id,
                    content=content,
                )
                if is_dup:
                    if existing and has_response_after(db, conversation.id, existing.id):
                        logger.debug(f"Bot {bot_profile_id}: Skipping duplicate message {msg_id}")
                        return

            # Save user message
            user_msg = save_user_message(
                db,
                conversation.id,
                content,
                sender_name=from_user,
                sender_id=from_user,
                platform_message_id=msg_id or None,
                file_url=file_info["file_url"] if file_info else None,
                file_type=file_info["file_type"] if file_info else None,
                file_name=file_info["file_name"] if file_info else None,
                file_size=file_info["file_size"] if file_info else None,
                file_pages=file_info.get("file_pages") if file_info else None,
            )
            update_conversation_stats(db, conversation.id)
            db.commit()

            # Log activity
            try:
                from app.database import BotProfile
                bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                if bot:
                    log_activity(
                        db, bot.user_id,
                        action="wechat_message_received",
                        details=f"Message from {from_user} on bot '{config.get('name', 'Bot')}': {content[:80]}",
                    )
                    db.commit()
            except Exception:
                pass

            # Broadcast to WebSocket
            broadcast_user_message(conversation.id, bot_profile_id, user_msg, conversation)

            # Check human takeover
            if is_human_takeover_active(db, conversation.id):
                logger.info(f"Bot {bot_profile_id}: Human takeover active, skipping AI")
                return

            # Check if AI responses enabled
            if not instance.ai_response_enabled:
                return

            # Analyze media if present
            if file_info and file_info.get("local_file_path"):
                try:
                    api_key = decrypt_string(config.get("api_key_encrypted", ""))
                    ai = get_ai_provider(
                        config.get("ai_provider", "openai"),
                        api_key,
                        config.get("model", "gpt-4"),
                    )
                    media_analysis = await asyncio.to_thread(
                        analyze_media_with_ai,
                        ai, file_info["local_file_path"], file_info["file_type"], content,
                    )
                    if media_analysis:
                        user_msg.media_analysis = media_analysis
                        db.flush()
                        db.commit()
                except Exception as e:
                    logger.error(f"Bot {bot_profile_id}: Media analysis failed: {e}")

            # Generate AI response
            broadcast_typing(conversation.id, True)
            try:
                api_key = decrypt_string(config.get("api_key_encrypted", ""))
                ai = get_ai_provider(
                    config.get("ai_provider", "openai"),
                    api_key,
                    config.get("model", "gpt-4"),
                )
                system_prompt = config.get("system_prompt", "You are a helpful assistant.")
                messages = build_ai_messages(
                    db, conversation.id, system_prompt, current_message=user_msg
                )

                # Run synchronous AI call in thread to avoid blocking event loop
                response = await asyncio.to_thread(
                    ai.chat_completion, messages=messages
                )
                ai_text = response.content

                if ai_text:
                    # Save AI response to DB
                    bot_name = config.get("name", "Bot")
                    ai_msg = save_assistant_message(
                        db, conversation.id, ai_text, sender_name=bot_name
                    )
                    update_conversation_stats(db, conversation.id)
                    db.commit()

                    broadcast_assistant_message(conversation.id, ai_msg)

                    # Send via WeChat customer service API
                    # Split long messages if they exceed WeChat's limit
                    await self._send_text_with_splitting(
                        bot_profile_id, from_user, ai_text
                    )

            except Exception as e:
                logger.error(f"Bot {bot_profile_id}: AI response error: {e}", exc_info=True)
            finally:
                broadcast_typing(conversation.id, False)

    async def _send_text_with_splitting(
        self, bot_profile_id: int, to_user: str, content: str
    ):
        """Send text, splitting into multiple messages if over WeChat's limit."""
        if len(content) <= WECHAT_MAX_MESSAGE_LENGTH:
            await self._send_customer_service_message(bot_profile_id, to_user, content)
            return

        # Split on paragraph boundaries, falling back to hard split
        remaining = content
        while remaining:
            if len(remaining) <= WECHAT_MAX_MESSAGE_LENGTH:
                await self._send_customer_service_message(bot_profile_id, to_user, remaining)
                break

            # Try to split at a paragraph or sentence boundary
            chunk = remaining[:WECHAT_MAX_MESSAGE_LENGTH]
            split_pos = chunk.rfind("\n\n")
            if split_pos < WECHAT_MAX_MESSAGE_LENGTH // 2:
                split_pos = chunk.rfind("\n")
            if split_pos < WECHAT_MAX_MESSAGE_LENGTH // 2:
                split_pos = chunk.rfind(". ")
            if split_pos < WECHAT_MAX_MESSAGE_LENGTH // 2:
                split_pos = WECHAT_MAX_MESSAGE_LENGTH

            await self._send_customer_service_message(
                bot_profile_id, to_user, remaining[:split_pos].rstrip()
            )
            remaining = remaining[split_pos:].lstrip()

    async def _send_customer_service_message(
        self, bot_profile_id: int, to_user: str, content: str
    ) -> bool:
        """Send a text message via the WeChat Customer Service Message API.

        This is the async API used when we can't reply within the 5-second
        passive reply window.
        """
        token_mgr = self._get_token_manager(bot_profile_id)
        access_token = await token_mgr.get_token()

        url = f"{WECHAT_API_BASE}/message/custom/send?access_token={access_token}"
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "text": {"content": content},
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode != 0:
            logger.error(
                f"Bot {bot_profile_id}: Failed to send customer service message: "
                f"{errcode} - {data.get('errmsg', '')}"
            )
            return False

        logger.info(f"Bot {bot_profile_id}: Sent customer service message to {to_user}")
        return True

    async def _download_media(
        self,
        bot_profile_id: int,
        url: str,
        media_type: str,
        chat_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Download media from a direct URL and save it."""
        import base64
        from app.platforms.message_handler import save_media_file

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=30, follow_redirects=True)
                if resp.status_code != 200:
                    logger.error(f"Bot {bot_profile_id}: Failed to download media: HTTP {resp.status_code}")
                    return None
                data_b64 = base64.b64encode(resp.content).decode("utf-8")

            return save_media_file(
                data_b64, media_type, bot_profile_id, chat_name, direction="received"
            )
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Media download failed: {e}")
            return None

    async def _download_wechat_media(
        self,
        bot_profile_id: int,
        media_id: str,
        media_type: str,
        chat_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Download media from WeChat servers using media_id."""
        import base64
        from app.platforms.message_handler import save_media_file

        try:
            token_mgr = self._get_token_manager(bot_profile_id)
            access_token = await token_mgr.get_token()
            url = f"{WECHAT_API_BASE}/media/get?access_token={access_token}&media_id={media_id}"

            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=30, follow_redirects=True)
                if resp.status_code != 200:
                    logger.error(f"Bot {bot_profile_id}: Failed to download wechat media: HTTP {resp.status_code}")
                    return None

                # Check if response is JSON error instead of binary media
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type or "text/plain" in content_type:
                    data = resp.json()
                    logger.error(f"Bot {bot_profile_id}: WeChat media error: {data}")
                    return None

                data_b64 = base64.b64encode(resp.content).decode("utf-8")

            return save_media_file(
                data_b64, media_type, bot_profile_id, chat_name, direction="received"
            )
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: WeChat media download failed: {e}")
            return None

    async def _process_outbound_queue(self, instance):
        """Process outbound messages from the instance queue."""
        bot_profile_id = instance.bot_profile_id
        while True:
            try:
                await asyncio.sleep(2)
                if not instance.is_running:
                    break

                messages = instance.get_outbound_messages()
                for msg in messages:
                    chat_id = msg["chat_id"]
                    text = msg["message"]
                    try:
                        await self._send_customer_service_message(
                            bot_profile_id, chat_id, text
                        )
                    except Exception as e:
                        logger.error(
                            f"Bot {bot_profile_id}: Failed to send outbound message: {e}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Bot {bot_profile_id}: Outbound queue error: {e}")
                await asyncio.sleep(5)

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via the customer service API, splitting if needed."""
        await self._send_text_with_splitting(bot_profile_id, chat_id, message)
        return True

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send a file via the WeChat media upload + customer service API."""
        from pathlib import Path

        try:
            token_mgr = self._get_token_manager(bot_profile_id)
            access_token = await token_mgr.get_token()

            # Determine WeChat media type
            if file_type.startswith("image/"):
                wechat_type = "image"
            elif file_type.startswith("video/"):
                wechat_type = "video"
            elif file_type.startswith("audio/"):
                wechat_type = "voice"
            else:
                # WeChat doesn't support arbitrary file types via customer service API
                if caption:
                    await self._send_customer_service_message(
                        bot_profile_id, chat_id, f"[File: {Path(file_path).name}]\n{caption}"
                    )
                return False

            # Upload media to WeChat
            upload_url = (
                f"{WECHAT_API_BASE}/media/upload"
                f"?access_token={access_token}&type={wechat_type}"
            )

            file_obj = Path(file_path)
            if not file_obj.exists():
                logger.error(f"Bot {bot_profile_id}: File not found: {file_path}")
                return False

            async with httpx.AsyncClient() as client:
                with open(file_obj, "rb") as f:
                    files = {"media": (file_obj.name, f, file_type)}
                    resp = await client.post(upload_url, files=files, timeout=30)
                    data = resp.json()

            if "media_id" not in data:
                logger.error(f"Bot {bot_profile_id}: Media upload failed: {data}")
                return False

            media_id = data["media_id"]

            # Send media message via customer service API
            send_url = f"{WECHAT_API_BASE}/message/custom/send?access_token={access_token}"
            payload = {
                "touser": chat_id,
                "msgtype": wechat_type,
                wechat_type: {"media_id": media_id},
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(send_url, json=payload, timeout=10)
                result = resp.json()

            if result.get("errcode", 0) != 0:
                logger.error(f"Bot {bot_profile_id}: Send media failed: {result}")
                return False

            # Send caption separately if provided
            if caption:
                await self._send_customer_service_message(bot_profile_id, chat_id, caption)

            logger.info(f"Bot {bot_profile_id}: Sent {wechat_type} to {chat_id}")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: send_file error: {e}", exc_info=True)
            return False

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a bot."""
        self._token_managers.pop(bot_profile_id, None)
        self._bot_configs.pop(bot_profile_id, None)
