"""
WhatsApp Business API Platform Adapter
Webhook-based integration using Meta WhatsApp Cloud API.

Requires:
- WhatsApp Business Account
- Phone Number ID (from Meta Business Suite)
- Permanent Access Token (System User token)
- App Secret for webhook signature verification (optional)

The adapter registers webhook routes on the FastAPI app to receive
inbound messages from the WhatsApp Cloud API.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Dict, Any, List, Optional

import httpx

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
    is_sender_approved,
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# Per-bot state keyed by bot_profile_id
_bot_state: Dict[int, Dict[str, Any]] = {}


def _get_state(bot_profile_id: int) -> Dict[str, Any]:
    if bot_profile_id not in _bot_state:
        _bot_state[bot_profile_id] = {
            "access_token": None,
            "phone_number_id": None,
            "app_secret": None,
            "webhook_verify_token": None,
            "display_phone_number": None,
            "instance": None,
        }
    return _bot_state[bot_profile_id]


def cleanup_bot_state(bot_profile_id: int):
    _bot_state.pop(bot_profile_id, None)


# Webhook route registration flag
_webhook_routes_registered = False


class WhatsAppBusinessAdapter(PlatformAdapter):
    """WhatsApp Business API adapter using Meta Cloud API.

    Integration type: Webhook-based.
    - Receives messages via HTTP POST webhook from Meta
    - Sends messages via WhatsApp Cloud API
    - Auth: Phone Number ID + Access Token (API_TOKEN)
    """

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.WHATSAPP_BUSINESS

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
            supports_groups_list=False,
            supports_profile_pic=False,
            auth_method=AuthMethod.API_TOKEN,
            max_message_length=4096,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "video/mp4", "video/3gpp",
                "audio/aac", "audio/mp4", "audio/mpeg", "audio/amr", "audio/ogg",
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ],
        )

    async def run(self, instance) -> None:
        """Start the WhatsApp Business bot.

        Registers webhook routes, validates credentials, then enters keep-alive loop.
        """
        bot_id = instance.bot_profile_id
        config = instance.config

        # Extract credentials
        access_token = config.get("platform_token") or config.get("api_key")
        phone_number_id = config.get("phone_number_id") or config.get("whatsapp_phone_number_id")
        app_secret = config.get("app_secret", "")
        webhook_verify_token = config.get("webhook_verify_token", "chathub_whatsapp_business")

        if not access_token:
            await instance.notify_status({
                "error": "No Access Token configured",
                "message": "WhatsApp Business API requires an Access Token from Meta Business Suite.",
            })
            instance.is_running = False
            return

        if not phone_number_id:
            await instance.notify_status({
                "error": "No Phone Number ID configured",
                "message": "WhatsApp Business API requires a Phone Number ID from the API Setup page.",
            })
            instance.is_running = False
            return

        state = _get_state(bot_id)
        state["access_token"] = access_token
        state["phone_number_id"] = phone_number_id
        state["app_secret"] = app_secret
        state["webhook_verify_token"] = webhook_verify_token
        state["instance"] = instance

        # Register webhook routes
        _ensure_webhook_routes()

        # Verify credentials by fetching phone number info
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GRAPH_API_BASE}/{phone_number_id}",
                    params={"access_token": access_token},
                )
                if resp.status_code != 200:
                    error_msg = resp.json().get("error", {}).get("message", resp.text)
                    await instance.notify_status({
                        "error": f"Invalid credentials: {error_msg}",
                        "message": "Failed to verify WhatsApp Business API credentials.",
                    })
                    instance.is_running = False
                    try:
                        from app.database import SessionLocal, BotProfile as BPModel
                        _db = SessionLocal()
                        try:
                            _bot = _db.query(BPModel).filter(BPModel.id == bot_id).first()
                            if _bot:
                                _bot.is_running = False
                                _db.commit()
                        finally:
                            _db.close()
                    except Exception:
                        pass
                    return

                phone_info = resp.json()
                display_phone = phone_info.get("display_phone_number", phone_number_id)
                verified_name = phone_info.get("verified_name", "WhatsApp Business")
                state["display_phone_number"] = display_phone

        except Exception as e:
            await instance.notify_status({
                "error": f"Failed to connect to Meta API: {e}",
                "message": "Check your internet connection and credentials.",
            })
            instance.is_running = False
            return

        # Save account info to DB
        instance.whatsapp_connected = True
        try:
            from app.database import SessionLocal, BotProfile as BPModel
            _db = SessionLocal()
            try:
                _bot = _db.query(BPModel).filter(BPModel.id == bot_id).first()
                if _bot:
                    _bot.whatsapp_name = verified_name
                    _bot.whatsapp_phone = display_phone
                    _db.commit()
            finally:
                _db.close()
        except Exception as e:
            logger.warning(f"Bot {bot_id}: Failed to save WA Business account info: {e}")

        await instance.notify_status({
            "message": f"Connected to WhatsApp Business API as '{verified_name}' ({display_phone})",
            "connected": True,
            "account_info": {"name": verified_name, "phone": display_phone},
        })

        logger.info(f"Bot {bot_id}: WhatsApp Business API connected as '{verified_name}' ({display_phone})")

        # Keep-alive loop
        try:
            while instance.is_running and not instance.stopped_by_user:
                # Process outbound messages
                if instance.has_outbound_messages():
                    for msg in instance.get_outbound_messages():
                        try:
                            await self.send_message(
                                bot_id,
                                msg["chat_id"],
                                msg.get("chat_name", ""),
                                msg["message"],
                            )
                        except Exception as e:
                            logger.error(f"Bot {bot_id}: Outbound send error: {e}")

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_id}: WhatsApp Business adapter cancelled")
        finally:
            instance.whatsapp_connected = False
            state["instance"] = None
            logger.info(f"Bot {bot_id}: WhatsApp Business adapter stopped")

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via WhatsApp Cloud API."""
        state = _get_state(bot_profile_id)
        token = state.get("access_token")
        phone_id = state.get("phone_number_id")
        if not token or not phone_id:
            logger.error(f"Bot {bot_profile_id}: No credentials for sending")
            return False

        # Split long messages (WhatsApp limit is 4096 chars)
        chunks = _split_message(message, 4096)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for chunk in chunks:
                    payload = {
                        "messaging_product": "whatsapp",
                        "to": chat_id,
                        "type": "text",
                        "text": {"body": chunk},
                    }
                    resp = await client.post(
                        f"{GRAPH_API_BASE}/{phone_id}/messages",
                        headers={"Authorization": f"Bearer {token}"},
                        json=payload,
                    )
                    if resp.status_code not in (200, 201):
                        error = resp.json().get("error", {})
                        logger.error(
                            f"Bot {bot_profile_id}: WA Business send error: "
                            f"{error.get('message', resp.text)}"
                        )
                        return False

            logger.info(f"Bot {bot_profile_id}: Sent WA Business message to {chat_id}")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Send message error: {e}")
            return False

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        file_path: str,
        caption: str = "",
    ) -> bool:
        """Send a media file via WhatsApp Cloud API."""
        state = _get_state(bot_profile_id)
        token = state.get("access_token")
        phone_id = state.get("phone_number_id")
        if not token or not phone_id:
            return False

        # Determine media type
        ext = os.path.splitext(file_path)[1].lower()
        type_map = {
            ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image",
            ".mp4": "video", ".3gp": "video",
            ".mp3": "audio", ".ogg": "audio", ".aac": "audio", ".amr": "audio",
            ".pdf": "document", ".doc": "document", ".docx": "document",
            ".xls": "document", ".xlsx": "document",
        }
        media_type = type_map.get(ext, "document")

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                # Upload media
                with open(file_path, "rb") as f:
                    mime_type = {
                        "image": "image/jpeg", "video": "video/mp4",
                        "audio": "audio/mpeg", "document": "application/octet-stream",
                    }.get(media_type, "application/octet-stream")

                    upload_resp = await client.post(
                        f"{GRAPH_API_BASE}/{phone_id}/media",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (os.path.basename(file_path), f, mime_type)},
                        data={"messaging_product": "whatsapp"},
                    )

                if upload_resp.status_code not in (200, 201):
                    logger.error(f"Bot {bot_profile_id}: Media upload failed: {upload_resp.text}")
                    return False

                media_id = upload_resp.json().get("id")

                # Send media message
                payload = {
                    "messaging_product": "whatsapp",
                    "to": chat_id,
                    "type": media_type,
                    media_type: {"id": media_id},
                }
                if caption and media_type in ("image", "video", "document"):
                    payload[media_type]["caption"] = caption

                resp = await client.post(
                    f"{GRAPH_API_BASE}/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                if resp.status_code not in (200, 201):
                    logger.error(f"Bot {bot_profile_id}: Media send failed: {resp.text}")
                    return False

            logger.info(f"Bot {bot_profile_id}: Sent {media_type} to {chat_id}")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Send file error: {e}")
            return False

    async def cleanup(self, bot_id: int) -> None:
        cleanup_bot_state(bot_id)

    async def get_contacts(self, bot_id: int) -> List[Dict[str, Any]]:
        return []  # WhatsApp Cloud API doesn't expose contacts list

    async def get_groups(self, bot_id: int) -> List[Dict[str, Any]]:
        return []  # WhatsApp Cloud API doesn't expose groups list


# ============== Webhook Handling ==============

def _ensure_webhook_routes():
    """Register webhook routes on the FastAPI app (idempotent)."""
    global _webhook_routes_registered
    if _webhook_routes_registered:
        return

    try:
        from app.main import app
        from fastapi import Request
        from fastapi.responses import PlainTextResponse, JSONResponse

        @app.get("/webhooks/whatsapp-business")
        async def wa_business_webhook_verify(request: Request):
            """Meta webhook verification challenge."""
            params = request.query_params
            mode = params.get("hub.mode")
            token = params.get("hub.verify_token")
            challenge = params.get("hub.challenge")

            if mode == "subscribe":
                # Find a bot with matching verify token
                for bot_id, state in _bot_state.items():
                    if state.get("webhook_verify_token") == token:
                        logger.info(f"Bot {bot_id}: WA Business webhook verified")
                        return PlainTextResponse(challenge)

                # Fallback: accept with default token
                if token == "chathub_whatsapp_business":
                    return PlainTextResponse(challenge)

            return PlainTextResponse("Forbidden", status_code=403)

        @app.post("/webhooks/whatsapp-business")
        async def wa_business_webhook_receive(request: Request):
            """Process inbound WhatsApp Business API events."""
            body = await request.body()

            # Signature verification (if app_secret configured)
            signature = request.headers.get("X-Hub-Signature-256", "")
            if signature:
                for bot_id, state in _bot_state.items():
                    secret = state.get("app_secret")
                    if secret and _verify_signature(body, signature, secret):
                        break

            try:
                data = json.loads(body)
            except Exception:
                return JSONResponse({"status": "error"}, status_code=400)

            if data.get("object") == "whatsapp_business_account":
                asyncio.create_task(_process_webhook_entries(data.get("entry", [])))

            return JSONResponse({"status": "ok"})

        _webhook_routes_registered = True
        logger.info("WhatsApp Business webhook routes registered")

    except Exception as e:
        logger.error(f"Failed to register WA Business webhook routes: {e}")


def _verify_signature(body: bytes, signature: str, app_secret: str) -> bool:
    """Verify Meta webhook signature."""
    expected = "sha256=" + hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _process_webhook_entries(entries: list):
    """Process WhatsApp Cloud API webhook entries."""
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if change.get("field") != "messages":
                continue

            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")

            # Find the bot for this phone number ID
            bot_id = None
            state = None
            for bid, s in _bot_state.items():
                if s.get("phone_number_id") == phone_number_id and s.get("instance"):
                    bot_id = bid
                    state = s
                    break

            if not bot_id or not state:
                logger.debug(f"WA Business: No bot found for phone_number_id {phone_number_id}")
                continue

            # Process messages
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            # Build contact name lookup
            contact_names = {}
            for c in contacts:
                wa_id = c.get("wa_id", "")
                name = c.get("profile", {}).get("name", "")
                if wa_id and name:
                    contact_names[wa_id] = name

            for msg in messages:
                try:
                    await _handle_message(bot_id, state, msg, contact_names)
                except Exception as e:
                    logger.error(f"Bot {bot_id}: WA Business message handling error: {e}", exc_info=True)

            # Process status updates (read receipts, etc.)
            for status in value.get("statuses", []):
                pass  # Could handle delivery/read receipts here


async def _handle_message(bot_id: int, state: dict, msg: dict, contact_names: dict):
    """Handle a single inbound WhatsApp message."""
    instance = state.get("instance")
    if not instance:
        return

    sender = msg.get("from", "")
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")
    timestamp_str = msg.get("timestamp", "")

    sender_name = contact_names.get(sender, sender)

    # Parse timestamp
    try:
        timestamp = datetime.fromtimestamp(int(timestamp_str))
    except (ValueError, TypeError):
        timestamp = datetime.utcnow()

    # Extract message content
    content = ""
    media_url = None
    media_type = None

    if msg_type == "text":
        content = msg.get("text", {}).get("body", "")
    elif msg_type in ("image", "video", "audio", "document", "sticker"):
        media_info = msg.get(msg_type, {})
        content = media_info.get("caption", f"[Media: {msg_type}]")
        media_id = media_info.get("id")
        if media_id:
            media_url = await _get_media_url(state, media_id)
            media_type = media_info.get("mime_type", f"{msg_type}/*")
    elif msg_type == "location":
        loc = msg.get("location", {})
        content = f"[Location: {loc.get('latitude', 0)}, {loc.get('longitude', 0)}]"
    elif msg_type == "contacts":
        content = "[Contact card shared]"
    elif msg_type == "reaction":
        return  # Skip reactions for now
    else:
        content = f"[{msg_type} message]"

    if not content and not media_url:
        return

    # Get DB session and process
    db = get_db_session()
    try:
        from app.database import BotProfile
        bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
        if not bot:
            return

        # Dedup check
        if is_duplicate_message(db, msg_id, sender, content):
            return

        # Find or create conversation
        conversation = find_or_create_conversation(
            db=db,
            bot_profile_id=bot_id,
            chat_id=sender,
            chat_name=sender_name,
            phone=f"+{sender}" if not sender.startswith("+") else sender,
            is_group=False,
            platform_type="whatsapp_business",
        )

        # Save user message
        save_user_message(
            db=db,
            conversation=conversation,
            content=content,
            sender=sender,
            sender_name=sender_name,
            wa_message_id=msg_id,
            timestamp=timestamp,
        )

        # Broadcast to WebSocket
        broadcast_user_message(conversation.id, {
            "content": content,
            "sender": sender,
            "sender_name": sender_name,
            "timestamp": timestamp.isoformat(),
        })

        update_conversation_stats(db, conversation)

        # Handle media if present
        if media_url:
            try:
                media_content = await _download_media(state, media_url)
                if media_content:
                    file_ext = _get_extension(media_type or "")
                    file_path = save_media_file(media_content, f"wa_business_{msg_id}{file_ext}")
                    if file_path:
                        media_analysis = await analyze_media_with_ai(
                            bot, file_path, media_type or "", db
                        )
                        if media_analysis:
                            content = media_analysis
            except Exception as e:
                logger.warning(f"Bot {bot_id}: Media processing error: {e}")

        # Check human takeover and DM approval
        if is_human_takeover_active(db, conversation.id):
            return

        if not is_sender_approved(db, conversation.id):
            return

        # Check if AI response is enabled
        if not bot.ai_response_enabled:
            return

        # Check if already responded
        if has_response_after(db, conversation.id, timestamp):
            return

        # Generate AI response
        broadcast_typing(conversation.id, True)
        try:
            ai_messages = build_ai_messages(
                db=db,
                bot=bot,
                conversation=conversation,
                latest_content=content,
            )

            from app.ai.factory import get_ai_provider
            provider = get_ai_provider(bot.ai_provider, bot.api_key_encrypted, bot.model)
            reply = await provider.chat_completion(ai_messages)

            if reply:
                # Apply response delay
                import random
                delay = random.uniform(
                    bot.response_delay_min or 1,
                    bot.response_delay_max or 3,
                )
                await asyncio.sleep(delay)

                # Send reply
                sent = await WhatsAppBusinessAdapter().send_message(
                    bot_id, sender, sender_name, reply
                )
                if sent:
                    save_assistant_message(db, conversation, reply)
                    broadcast_assistant_message(conversation.id, {
                        "content": reply,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    update_conversation_stats(db, conversation)

                    # Mark as read
                    await _mark_as_read(state, msg_id)

        except Exception as e:
            logger.error(f"Bot {bot_id}: AI response error: {e}", exc_info=True)
        finally:
            broadcast_typing(conversation.id, False)

    finally:
        db.close()


async def _get_media_url(state: dict, media_id: str) -> Optional[str]:
    """Get download URL for a media file."""
    token = state.get("access_token")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return resp.json().get("url")
    except Exception as e:
        logger.error(f"WA Business: Failed to get media URL: {e}")
    return None


async def _download_media(state: dict, url: str) -> Optional[bytes]:
    """Download media file from WhatsApp servers."""
    token = state.get("access_token")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        logger.error(f"WA Business: Failed to download media: {e}")
    return None


async def _mark_as_read(state: dict, message_id: str):
    """Mark a message as read."""
    token = state.get("access_token")
    phone_id = state.get("phone_number_id")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{GRAPH_API_BASE}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
    except Exception:
        pass  # Read receipts are best-effort


def _split_message(text: str, max_length: int) -> List[str]:
    """Split a message into chunks that fit the platform's max length."""
    if len(text) <= max_length:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = text.rfind(" ", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks


def _get_extension(mime_type: str) -> str:
    """Get file extension from MIME type."""
    mime_map = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/3gpp": ".3gp",
        "audio/aac": ".aac", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
        "audio/amr": ".amr", "audio/ogg": ".ogg",
        "application/pdf": ".pdf",
    }
    return mime_map.get(mime_type, ".bin")
