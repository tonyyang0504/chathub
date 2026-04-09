"""
Instagram Platform Adapter
Uses Meta Instagram Messaging API (Graph API) for DM automation.

Requires:
- Facebook Page linked to an Instagram Professional (Business/Creator) account
- Page Access Token with instagram_manage_messages permission
- Webhook subscription for instagram messaging events

Auth flow: OAUTH — user provides a Page Access Token (long-lived).
Messages arrive via webhook POST; replies sent via Graph API.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import time
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

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class InstagramAdapter(PlatformAdapter):
    """Instagram DM adapter using Meta's Instagram Messaging API.

    Webhook-based: receives messages via HTTP POST callbacks from Meta,
    sends replies via the Graph API.

    Config keys (from BotInstance.config):
        - api_key: Page Access Token (long-lived)
        - instagram_page_id: Facebook Page ID linked to Instagram account
        - instagram_app_secret: App secret for webhook signature verification
        - webhook_verify_token: Token for Meta webhook GET verification
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        # Map of bot_profile_id -> config for active bots
        self._active_bots: Dict[int, dict] = {}
        # Map of instagram_user_id (IGSID) -> bot_profile_id for routing webhooks
        self._page_to_bot: Dict[str, int] = {}

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.INSTAGRAM

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=False,
            supports_media=True,
            supports_file_send=False,
            supports_reactions=True,
            supports_read_receipts=True,
            supports_typing_indicator=True,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=False,
            supports_profile_pic=True,
            auth_method=AuthMethod.OAUTH,
            max_message_length=1000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "video/mp4",
                "audio/mpeg", "audio/mp4",
            ],
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    # ------------------------------------------------------------------
    # PlatformAdapter interface
    # ------------------------------------------------------------------

    async def run(self, instance) -> None:
        """Start the Instagram bot.

        Unlike WhatsApp (browser automation), Instagram is webhook-based.
        This method:
        1. Validates the access token
        2. Registers the bot for webhook routing
        3. Sets up the webhook routes on the FastAPI app
        4. Enters a keep-alive loop (processing outbound queue)
        """
        bot_id = instance.bot_profile_id
        config = instance.config
        access_token = config.get("api_key", "") or config.get("platform_token", "")
        page_id = config.get("instagram_page_id", "")

        if not access_token:
            await instance.notify_status({
                "error": "No access token configured",
                "message": "Instagram requires a Page Access Token. Configure it in bot settings.",
            })
            instance.is_running = False
            return

        if not page_id:
            await instance.notify_status({
                "error": "No Instagram Page ID configured",
                "message": "Set the Facebook Page ID linked to your Instagram account.",
            })
            instance.is_running = False
            return

        # Validate token by fetching page info
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{GRAPH_API_BASE}/{page_id}",
                params={"access_token": access_token, "fields": "id,name,instagram_business_account"},
            )
            if resp.status_code != 200:
                try:
                    error_data = resp.json().get("error", {})
                    error_msg = error_data.get("message", f"HTTP {resp.status_code}")
                except Exception:
                    error_msg = f"HTTP {resp.status_code}"
                await instance.notify_status({
                    "error": f"Token validation failed: {error_msg}",
                    "message": "Check your Page Access Token and Page ID.",
                })
                instance.is_running = False
                return

            page_data = resp.json()
            ig_account = page_data.get("instagram_business_account", {})
            ig_account_id = ig_account.get("id", "")
            page_name = page_data.get("name", "Unknown")

            logger.info(
                f"Bot {bot_id}: Instagram connected — Page '{page_name}' "
                f"(ID: {page_id}, IG Account: {ig_account_id})"
            )
        except httpx.HTTPError as e:
            await instance.notify_status({
                "error": f"Connection error: {e}",
                "message": "Could not reach Meta Graph API. Check your network.",
            })
            instance.is_running = False
            return

        # Store bot config for webhook routing
        self._active_bots[bot_id] = {
            "access_token": access_token,
            "page_id": page_id,
            "ig_account_id": ig_account_id,
            "app_secret": config.get("instagram_app_secret", ""),
            "webhook_verify_token": config.get("webhook_verify_token", "chathub_verify"),
            "instance": instance,
        }
        self._page_to_bot[page_id] = bot_id

        # Register webhook routes (idempotent)
        self._ensure_webhook_routes()

        # Notify connected
        instance.platform_connected = True  # Reuses the generic "connected" flag
        await instance.notify_status({
            "message": f"Instagram connected: {page_name}",
            "connected": True,
        })

        # Keep-alive loop: process outbound queue + token refresh
        try:
            while instance.is_running:
                await self._process_outbound_queue(instance)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            logger.info(f"Bot {bot_id}: Instagram adapter cancelled")
        finally:
            self._active_bots.pop(bot_id, None)
            self._page_to_bot.pop(page_id, None)
            instance.platform_connected = False
            # Only close HTTP client if no other bots are using this adapter
            if not self._active_bots and self._http_client and not self._http_client.is_closed:
                await self._http_client.aclose()
                self._http_client = None

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via Instagram Graph API.

        Args:
            bot_profile_id: Bot database ID
            chat_id: Instagram-scoped User ID (IGSID) of the recipient
            chat_name: Display name (for logging)
            message: Text to send (max 1000 chars)
        """
        bot_config = self._active_bots.get(bot_profile_id)
        if not bot_config:
            logger.error(f"Bot {bot_profile_id}: Not active, cannot send Instagram message")
            return False

        access_token = bot_config["access_token"]
        page_id = bot_config["page_id"]

        # Instagram DM API enforces 1000 char limit; split if needed
        chunks = self._split_message(message, 1000)

        client = await self._get_client()
        for chunk in chunks:
            try:
                resp = await client.post(
                    f"{GRAPH_API_BASE}/{page_id}/messages",
                    json={
                        "recipient": {"id": chat_id},
                        "message": {"text": chunk},
                    },
                    params={"access_token": access_token},
                )
                if resp.status_code != 200:
                    try:
                        error_data = resp.json().get("error", {})
                        error_msg = error_data.get("message", resp.status_code)
                    except Exception:
                        error_msg = f"HTTP {resp.status_code}"
                    logger.error(
                        f"Bot {bot_profile_id}: Failed to send IG message to {chat_id}: {error_msg}"
                    )
                    return False
            except httpx.HTTPError as e:
                logger.error(f"Bot {bot_profile_id}: HTTP error sending IG message: {e}")
                return False

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
        """Send media via Instagram Graph API.

        Instagram DM supports image/video/audio attachments via URL.
        Files must be publicly accessible or uploaded via the attachment API.
        """
        bot_config = self._active_bots.get(bot_profile_id)
        if not bot_config:
            logger.error(f"Bot {bot_profile_id}: Not active, cannot send Instagram file")
            return False

        # Instagram DM file send requires a publicly accessible URL.
        # For local files, we'd need to upload first. Log and skip for now.
        logger.warning(
            f"Bot {bot_profile_id}: Instagram file send not fully supported for local files. "
            f"File: {file_path}"
        )
        # If caption provided, send it as a text message
        if caption:
            return await self.send_message(bot_profile_id, chat_id, chat_name, caption)
        return False

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a stopped bot.

        Note: base class defines cleanup() as sync. We close the async HTTP
        client via the running event loop when possible, to avoid resource leaks.
        """
        config = self._active_bots.pop(bot_profile_id, None)
        if config:
            page_id = config.get("page_id", "")
            self._page_to_bot.pop(page_id, None)

        # If no more active bots, close the shared HTTP client
        if not self._active_bots and self._http_client and not self._http_client.is_closed:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._http_client.aclose())
                else:
                    loop.run_until_complete(self._http_client.aclose())
            except RuntimeError:
                # No event loop available — last resort
                pass
            self._http_client = None

        logger.debug(f"Bot {bot_profile_id}: Instagram adapter cleaned up")

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    _webhook_routes_registered = False

    def _ensure_webhook_routes(self):
        """Register webhook endpoints on the FastAPI app (once)."""
        if InstagramAdapter._webhook_routes_registered:
            return
        InstagramAdapter._webhook_routes_registered = True

        from fastapi import Request, Response
        from app.main import app

        @app.get("/webhooks/instagram")
        async def instagram_webhook_verify(request: Request):
            """Meta webhook verification (GET challenge)."""
            params = request.query_params
            mode = params.get("hub.mode", "")
            token = params.get("hub.verify_token", "")
            challenge = params.get("hub.challenge", "")

            # Find a bot with matching verify token
            for bot_config in self._active_bots.values():
                if mode == "subscribe" and token == bot_config["webhook_verify_token"]:
                    logger.info("Instagram webhook verified successfully")
                    return Response(content=challenge, media_type="text/plain")

            # Fallback: accept default verify token even if no bots are running
            if mode == "subscribe" and token == "chathub_verify" and challenge:
                logger.info("Instagram webhook verified (default token)")
                return Response(content=challenge, media_type="text/plain")

            logger.warning(f"Instagram webhook verification failed: mode={mode}")
            return Response(content="Verification failed", status_code=403)

        @app.post("/webhooks/instagram")
        async def instagram_webhook_receive(request: Request):
            """Receive Instagram messaging events from Meta."""
            body = await request.body()

            # Always verify webhook signature — reject if no app_secret is configured (fail-closed)
            signature = request.headers.get("X-Hub-Signature-256", "")
            if not self._verify_webhook_signature(body, signature):
                logger.warning("Instagram webhook: signature verification failed — rejecting request")
                return Response(content="Invalid signature", status_code=403)

            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return Response(content="Invalid JSON", status_code=400)

            # Process asynchronously to return 200 quickly (Meta requires < 20s)
            task = asyncio.create_task(self._process_webhook_payload(payload))
            task.add_done_callback(self._webhook_task_done)
            return Response(content="EVENT_RECEIVED", status_code=200)

        logger.info("Instagram webhook routes registered: /webhooks/instagram")

    @staticmethod
    def _webhook_task_done(task: asyncio.Task):
        """Log exceptions from webhook processing background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Instagram webhook processing error: {exc}", exc_info=exc)

    def _verify_webhook_signature(self, body: bytes, signature_header: str) -> bool:
        """Verify Meta's X-Hub-Signature-256 header.

        SECURITY: Fail-closed — if no app_secret is configured on any active
        bot, ALL webhook requests are rejected.  This prevents an attacker from
        sending forged payloads when the secret has not been set up yet.
        """
        if not self._active_bots:
            logger.warning("Instagram webhook rejected: no active bots configured")
            return False

        # Collect all non-empty app_secret values from active bots
        secrets = [
            c["app_secret"]
            for c in self._active_bots.values()
            if c.get("app_secret") and isinstance(c["app_secret"], str) and c["app_secret"].strip()
        ]

        if not secrets:
            # SECURITY: Fail closed — reject the request rather than skipping verification
            logger.error(
                "Instagram webhook REJECTED: no app_secret configured on any active bot. "
                "Set 'instagram_app_secret' in bot config to enable webhook verification."
            )
            return False

        if not signature_header or not signature_header.startswith("sha256="):
            return False

        expected_sig = signature_header[7:]

        for secret in secrets:
            computed = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(computed, expected_sig):
                return True

        return False

    async def _process_webhook_payload(self, payload: dict):
        """Process an Instagram webhook payload.

        Payload structure (messaging):
        {
            "object": "instagram",
            "entry": [{
                "id": "<page_id>",
                "time": 1234567890,
                "messaging": [{
                    "sender": {"id": "<IGSID>"},
                    "recipient": {"id": "<page_ig_id>"},
                    "timestamp": 1234567890000,
                    "message": {
                        "mid": "<message_id>",
                        "text": "Hello"
                    }
                }]
            }]
        }
        """
        if payload.get("object") != "instagram":
            return

        for entry in payload.get("entry", []):
            page_id = entry.get("id", "")
            bot_id = self._page_to_bot.get(page_id)
            if not bot_id:
                logger.debug(f"Instagram webhook: no bot for page {page_id}")
                continue

            bot_config = self._active_bots.get(bot_id)
            if not bot_config:
                continue

            for event in entry.get("messaging", []):
                await self._handle_messaging_event(bot_id, bot_config, event)

    async def _handle_messaging_event(self, bot_id: int, bot_config: dict, event: dict):
        """Handle a single messaging event from Instagram."""
        sender_id = event.get("sender", {}).get("id", "")

        # Ignore messages sent by the page itself (echo)
        # Meta includes is_echo flag on messages sent by the page
        message_data = event.get("message", {})
        if message_data.get("is_echo"):
            return
        if sender_id == bot_config.get("page_id") or sender_id == bot_config.get("ig_account_id"):
            return

        # Handle different event types
        if "message" in event:
            await self._handle_incoming_message(bot_id, bot_config, event)
        elif "postback" in event:
            await self._handle_postback(bot_id, bot_config, event)
        elif "read" in event:
            logger.debug(f"Bot {bot_id}: Read receipt from {sender_id}")
        elif "reaction" in event:
            logger.debug(f"Bot {bot_id}: Reaction from {sender_id}: {event['reaction']}")

    async def _handle_incoming_message(self, bot_id: int, bot_config: dict, event: dict):
        """Process an incoming Instagram DM."""
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
            broadcast_user_message,
            broadcast_assistant_message,
            broadcast_typing,
            save_media_file,
            analyze_media_with_ai,
            log_activity,
        )
        from app.ai.factory import get_ai_provider
        from app.auth.utils import decrypt_string

        sender_id = event["sender"]["id"]
        message_data = event.get("message", {})
        message_id = message_data.get("mid", "")
        text = message_data.get("text") or ""
        attachments = message_data.get("attachments", [])
        timestamp_ms = event.get("timestamp", 0)
        timestamp = datetime.utcfromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.utcnow()

        instance = bot_config["instance"]
        access_token = bot_config["access_token"]

        # Fetch sender profile for display name
        sender_name = await self._get_user_profile(sender_id, access_token)

        log_preview = text[:80] if text else f"[{len(attachments)} attachment(s)]"
        logger.info(f"Bot {bot_id}: IG message from {sender_name} ({sender_id}): {log_preview}")

        with get_db_session() as db:
            # Find or create conversation
            conversation = find_or_create_conversation(
                db, bot_id, sender_id, sender_name,
                is_group=False,
                phone="",
                display_name=sender_name,
            )

            # Dedup check
            is_dup, existing = is_duplicate_message(
                db, conversation.id,
                platform_message_id=message_id,
                content=text,
            )
            if is_dup:
                if existing and has_response_after(db, conversation.id, existing.id):
                    logger.debug(f"Bot {bot_id}: Duplicate IG message, already responded")
                    return
                elif existing:
                    logger.debug(f"Bot {bot_id}: Duplicate IG message, awaiting response")
                    return

            # Process media attachments
            file_url = None
            file_type = None
            file_name = None
            file_size = None
            media_analysis = None

            if attachments:
                attachment = attachments[0]  # Process first attachment
                att_type = attachment.get("type", "")
                att_payload = attachment.get("payload", {})
                att_url = att_payload.get("url", "")

                if att_type in ("image", "video", "audio") and att_url:
                    # Download the media
                    media_info = await self._download_and_save_media(
                        bot_id, sender_name, att_url, att_type
                    )
                    if media_info:
                        file_url = media_info["file_url"]
                        file_type = media_info["file_type"]
                        file_name = media_info.get("file_name")
                        file_size = media_info.get("file_size")

                        # Analyze image with AI if applicable
                        if file_type and file_type.startswith("image/") and media_info.get("local_file_path"):
                            try:
                                bot_profile = self._get_bot_profile(db, bot_id)
                                if bot_profile:
                                    api_key = decrypt_string(bot_profile.api_key_encrypted)
                                    ai_provider = get_ai_provider(
                                        bot_profile.ai_provider, api_key, bot_profile.model
                                    )
                                    media_analysis = await asyncio.to_thread(
                                        analyze_media_with_ai,
                                        ai_provider, media_info["local_file_path"],
                                        file_type, text,
                                    )
                            except Exception as e:
                                logger.error(f"Bot {bot_id}: Media analysis error: {e}")

                    if not text:
                        text = f"[{att_type.title()} attachment]"

            # Save incoming message
            user_msg = save_user_message(
                db, conversation.id, text,
                sender_name=sender_name,
                sender_id=sender_id,
                platform_message_id=message_id,
                timestamp=timestamp,
                file_url=file_url,
                file_type=file_type,
                file_name=file_name,
                file_size=file_size,
                media_analysis=media_analysis,
            )
            update_conversation_stats(db, conversation.id)
            db.commit()

            # Log activity
            bot_profile = self._get_bot_profile(db, bot_id)
            if bot_profile:
                log_activity(
                    db, bot_id,
                    "instagram_message_received",
                    f"Message from {sender_name} to bot '{bot_profile.name}': {text[:80]}",
                )
                db.commit()

            # Broadcast to WebSocket
            broadcast_user_message(conversation.id, bot_id, user_msg, conversation)

            # Check if AI responses are enabled
            if not instance.ai_response_enabled:
                logger.debug(f"Bot {bot_id}: AI responses disabled, skipping")
                return

            # Check human takeover
            if is_human_takeover_active(db, conversation.id):
                logger.debug(f"Bot {bot_id}: Human takeover active, skipping AI")
                return

            if not is_sender_approved(db, conversation.id):
                return

            # Send typing indicator
            await self._send_typing_indicator(bot_id, sender_id, "typing_on")
            broadcast_typing(conversation.id, True)

            # Generate AI response
            try:
                bot_profile = self._get_bot_profile(db, bot_id)
                if not bot_profile:
                    logger.error(f"Bot {bot_id}: Bot profile not found")
                    return

                api_key = decrypt_string(bot_profile.api_key_encrypted)
                ai_provider = get_ai_provider(
                    bot_profile.ai_provider, api_key, bot_profile.model
                )

                ai_messages = build_ai_messages(
                    db, conversation.id,
                    bot_profile.system_prompt or "You are a helpful assistant.",
                    max_history=bot_profile.max_history or 20,
                    is_group=False,
                    current_message=user_msg,
                )

                response = await asyncio.to_thread(
                    ai_provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=bot_profile.max_tokens or 1000,
                    temperature=bot_profile.temperature if bot_profile.temperature is not None else 0.7,
                )
                ai_text = response.content

                if not ai_text:
                    logger.warning(f"Bot {bot_id}: Empty AI response")
                    return

                # Add response delay (human-like)
                delay_min = instance.config.get("response_delay_min", 1)
                delay_max = instance.config.get("response_delay_max", 3)
                delay = random.uniform(delay_min, delay_max)
                await asyncio.sleep(delay)

                # Send reply via Instagram API
                sent = await self.send_message(bot_id, sender_id, sender_name, ai_text)

                if sent:
                    # Save assistant message
                    assistant_msg = save_assistant_message(
                        db, conversation.id, ai_text,
                        sender_name=bot_profile.name or "AI Agent",
                    )
                    update_conversation_stats(db, conversation.id)
                    db.commit()

                    broadcast_assistant_message(conversation.id, assistant_msg)

                    log_activity(
                        db, bot_id,
                        "instagram_message_sent",
                        f"AI reply to {sender_name} via bot '{bot_profile.name}': {ai_text[:80]}",
                    )
                    db.commit()

            except Exception as e:
                logger.error(f"Bot {bot_id}: AI response error: {e}", exc_info=True)
            finally:
                await self._send_typing_indicator(bot_id, sender_id, "typing_off")
                broadcast_typing(conversation.id, False)

    async def _handle_postback(self, bot_id: int, bot_config: dict, event: dict):
        """Handle Instagram ice-breaker / postback events."""
        postback = event.get("postback", {})
        payload = postback.get("payload", "")
        title = postback.get("title", "")
        logger.info(f"Bot {bot_id}: Postback from {event['sender']['id']}: {title} ({payload})")
        # Treat postback like a text message with the payload as text
        event_copy = dict(event)
        event_copy["message"] = {"mid": f"postback_{int(time.time())}", "text": title or payload}
        event_copy.pop("postback", None)
        await self._handle_incoming_message(bot_id, bot_config, event_copy)

    # ------------------------------------------------------------------
    # Instagram API helpers
    # ------------------------------------------------------------------

    async def _get_user_profile(self, user_id: str, access_token: str) -> str:
        """Fetch Instagram user's name via Graph API."""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{GRAPH_API_BASE}/{user_id}",
                params={"access_token": access_token, "fields": "name,username"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("name") or data.get("username") or user_id
        except Exception as e:
            logger.debug(f"Could not fetch IG profile for {user_id}: {e}")
        return user_id

    async def _send_typing_indicator(self, bot_profile_id: int, recipient_id: str, action: str):
        """Send typing indicator via Graph API.

        Args:
            action: "typing_on" or "typing_off"
        """
        bot_config = self._active_bots.get(bot_profile_id)
        if not bot_config:
            return

        try:
            client = await self._get_client()
            await client.post(
                f"{GRAPH_API_BASE}/{bot_config['page_id']}/messages",
                json={
                    "recipient": {"id": recipient_id},
                    "sender_action": action,
                },
                params={"access_token": bot_config["access_token"]},
            )
        except Exception as e:
            logger.debug(f"Bot {bot_profile_id}: Typing indicator error: {e}")

    async def _download_and_save_media(
        self,
        bot_profile_id: int,
        chat_name: str,
        media_url: str,
        media_type_hint: str,
    ) -> Optional[Dict[str, Any]]:
        """Download media from Instagram CDN and save locally."""
        from app.platforms.message_handler import save_media_file

        try:
            client = await self._get_client()
            resp = await client.get(media_url, follow_redirects=True)
            if resp.status_code != 200:
                logger.error(f"Bot {bot_profile_id}: Failed to download IG media: HTTP {resp.status_code}")
                return None

            content_type = resp.headers.get("content-type", "")
            # Strip charset and other params (e.g. "image/jpeg; charset=utf-8")
            if content_type:
                content_type = content_type.split(";")[0].strip()
            if not content_type:
                type_map = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}
                content_type = type_map.get(media_type_hint, "application/octet-stream")

            b64_data = base64.b64encode(resp.content).decode("utf-8")

            return save_media_file(
                base64_data=b64_data,
                media_type=content_type,
                bot_profile_id=bot_profile_id,
                chat_name=chat_name,
                direction="received",
            )
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error downloading IG media: {e}")
            return None

    async def _process_outbound_queue(self, instance):
        """Process queued outbound messages (from scheduler, scripts, etc.)."""
        messages = instance.get_outbound_messages()
        for msg in messages:
            chat_id = msg.get("chat_id", "")
            text = msg.get("message", "")
            if chat_id and text:
                await self.send_message(instance.bot_profile_id, chat_id, chat_id, text)

    def _get_bot_profile(self, db, bot_profile_id: int):
        """Load BotProfile from DB."""
        from app.database import BotProfile
        return db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()

    @staticmethod
    def _split_message(text: str, max_len: int) -> List[str]:
        """Split a long message into chunks respecting word boundaries."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Find last space or newline within limit
            split_at = text.rfind(" ", 0, max_len)
            if split_at <= 0:
                split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip()
        return chunks
