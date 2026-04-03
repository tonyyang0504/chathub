"""
Facebook Messenger Platform Adapter
Webhook-based integration using Meta Graph API / Send API.

Requires:
- Facebook Page with Messenger permissions
- Page Access Token (stored as bot's API key or platform_token)
- Webhook Verify Token (stored in bot config as 'webhook_verify_token')
- App Secret for signature verification (stored in bot config as 'app_secret')

The adapter registers webhook routes on the FastAPI app to receive
inbound messages and delivery/read receipts from Meta.
"""

import asyncio
import hashlib
import hmac
import json
import logging
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
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# Per-bot state keyed by bot_profile_id
_bot_state: Dict[int, Dict[str, Any]] = {}


def _get_state(bot_profile_id: int) -> Dict[str, Any]:
    if bot_profile_id not in _bot_state:
        _bot_state[bot_profile_id] = {
            "page_access_token": None,
            "app_secret": None,
            "webhook_verify_token": None,
            "page_id": None,
            "instance": None,
        }
    return _bot_state[bot_profile_id]


def cleanup_bot_state(bot_profile_id: int):
    _bot_state.pop(bot_profile_id, None)


class MessengerAdapter(PlatformAdapter):
    """Facebook Messenger adapter using Meta Send API.

    Integration type: Webhook-based.
    - Receives messages via HTTP POST webhook from Meta
    - Sends messages via Graph API Send API
    - Auth: Page Access Token (API_TOKEN)
    """

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.MESSENGER

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=False,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=True,
            supports_read_receipts=True,
            supports_typing_indicator=True,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=False,
            supports_profile_pic=True,
            auth_method=AuthMethod.API_TOKEN,
            max_message_length=2000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif",
                "video/mp4",
                "audio/mpeg", "audio/ogg",
                "application/pdf",
            ],
        )

    async def run(self, instance) -> None:
        """Start the Messenger bot.

        Registers webhook routes on the FastAPI app, then enters a keep-alive
        loop that processes the outbound message queue. Incoming messages are
        handled by the webhook endpoint asynchronously.
        """
        bot_id = instance.bot_profile_id
        config = instance.config

        # Extract credentials from config
        page_access_token = config.get("page_access_token") or config.get("api_key") or config.get("platform_token")
        app_secret = config.get("app_secret", "")
        webhook_verify_token = config.get("webhook_verify_token", "")

        if not page_access_token:
            await instance.notify_status({
                "error": "No Page Access Token configured",
                "message": "Messenger requires a Page Access Token. Set it in bot settings.",
            })
            instance.is_running = False
            return

        if not app_secret:
            await instance.notify_status({
                "error": "No App Secret configured",
                "message": "Messenger requires an App Secret for webhook signature verification.",
            })
            instance.is_running = False
            return

        if not webhook_verify_token:
            await instance.notify_status({
                "error": "No Webhook Verify Token configured",
                "message": "Messenger requires a Webhook Verify Token. Set a unique value in bot settings.",
            })
            instance.is_running = False
            return

        state = _get_state(bot_id)
        state["page_access_token"] = page_access_token
        state["app_secret"] = app_secret
        state["webhook_verify_token"] = webhook_verify_token
        state["instance"] = instance

        # Register webhook routes (idempotent — skips if already registered)
        _ensure_webhook_routes()

        # Verify token by fetching page info
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GRAPH_API_BASE}/me",
                    params={"access_token": page_access_token},
                )
                if resp.status_code != 200:
                    error_msg = resp.json().get("error", {}).get("message", resp.text)
                    await instance.notify_status({
                        "error": f"Invalid Page Access Token: {error_msg}",
                        "message": "Failed to verify Messenger credentials.",
                    })
                    instance.is_running = False
                    # Update DB so recovery doesn't keep retrying
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
                page_info = resp.json()
                page_name = page_info.get("name", "Unknown Page")
                state["page_id"] = str(page_info.get("id", ""))
        except Exception as e:
            await instance.notify_status({
                "error": f"Failed to connect to Meta API: {e}",
                "message": "Check your internet connection and token.",
            })
            instance.is_running = False
            return

        # Connected successfully — save account info to DB
        instance.whatsapp_connected = True  # reuse field as "platform connected"
        try:
            from app.database import SessionLocal, BotProfile as BPModel
            _db = SessionLocal()
            try:
                _bot = _db.query(BPModel).filter(BPModel.id == bot_id).first()
                if _bot:
                    _bot.whatsapp_name = page_name
                    _db.commit()
            finally:
                _db.close()
        except Exception as e:
            logger.warning(f"Bot {bot_id}: Failed to save Messenger account info: {e}")

        await instance.notify_status({
            "message": f"Connected to Messenger as '{page_name}'",
            "connected": True,
            "account_info": {"name": page_name},
        })

        logger.info(f"Bot {bot_id}: Messenger connected as '{page_name}'")

        # Main loop: process outbound queue, sync history, stay alive
        try:
            while instance.is_running and not instance.stopped_by_user:
                # Check for history sync request
                if getattr(instance, 'history_sync_requested', False):
                    instance.history_sync_requested = False
                    instance.history_sync_active = True
                    try:
                        await self._sync_history(bot_id, page_access_token, instance, page_id=state.get("page_id", ""))
                    except Exception as e:
                        logger.error(f"Bot {bot_id}: History sync error: {e}", exc_info=True)
                    finally:
                        instance.history_sync_active = False

                # Process outbound messages from the queue
                if instance.has_outbound_messages():
                    outbound = instance.get_outbound_messages()
                    for msg in outbound:
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
            logger.info(f"Bot {bot_id}: Messenger adapter cancelled")
        finally:
            instance.whatsapp_connected = False
            state["instance"] = None
            logger.info(f"Bot {bot_id}: Messenger adapter stopped")

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message via Messenger Send API."""
        state = _get_state(bot_profile_id)
        token = state.get("page_access_token")
        if not token:
            logger.error(f"Bot {bot_profile_id}: No page access token for sending")
            return False

        # Split long messages (Messenger limit is 2000 chars)
        chunks = _split_message(message, 2000)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for chunk in chunks:
                    payload = {
                        "recipient": {"id": chat_id},
                        "message": {"text": chunk},
                        "messaging_type": "RESPONSE",
                    }
                    resp = await client.post(
                        f"{GRAPH_API_BASE}/me/messages",
                        params={"access_token": token},
                        json=payload,
                    )
                    if resp.status_code != 200:
                        error = resp.json().get("error", {})
                        logger.error(
                            f"Bot {bot_profile_id}: Send API error: "
                            f"{error.get('message', resp.text)}"
                        )
                        return False

            logger.info(f"Bot {bot_profile_id}: Sent message to {chat_id}")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Send message error: {e}")
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
        """Send a file/media via Messenger Send API."""
        state = _get_state(bot_profile_id)
        token = state.get("page_access_token")
        if not token:
            return False

        # Determine attachment type
        if file_type.startswith("image/"):
            attachment_type = "image"
        elif file_type.startswith("video/"):
            attachment_type = "video"
        elif file_type.startswith("audio/"):
            attachment_type = "audio"
        else:
            attachment_type = "file"

        try:
            from pathlib import Path
            path = Path(file_path)
            if not path.exists():
                logger.error(f"Bot {bot_profile_id}: File not found: {file_path}")
                return False

            async with httpx.AsyncClient(timeout=60) as client:
                # Upload via multipart form
                with open(path, "rb") as f:
                    message_payload = json.dumps({
                        "attachment": {
                            "type": attachment_type,
                            "payload": {"is_reusable": False},
                        }
                    })
                    files = {
                        "filedata": (path.name, f, file_type or "application/octet-stream"),
                    }
                    data = {
                        "recipient": json.dumps({"id": chat_id}),
                        "message": message_payload,
                        "messaging_type": "RESPONSE",
                    }
                    resp = await client.post(
                        f"{GRAPH_API_BASE}/me/messages",
                        params={"access_token": token},
                        data=data,
                        files=files,
                    )

                if resp.status_code != 200:
                    error = resp.json().get("error", {})
                    logger.error(
                        f"Bot {bot_profile_id}: Send file error: "
                        f"{error.get('message', resp.text)}"
                    )
                    return False

            # Send caption as a follow-up text if provided
            if caption:
                await self.send_message(bot_profile_id, chat_id, chat_name, caption)

            logger.info(f"Bot {bot_profile_id}: Sent {attachment_type} to {chat_id}")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Send file error: {e}")
            return False

    async def _sync_history(self, bot_id: int, page_access_token: str, instance, page_id: str = ""):
        """Sync conversation history from Messenger via Graph API."""
        from app.platforms.message_handler import (
            get_db_session,
            find_or_create_conversation,
            save_user_message,
            update_conversation_stats,
        )

        sync_count = getattr(instance, 'history_sync_count', 50)
        logger.info(f"Bot {bot_id}: Starting Messenger history sync (count={sync_count}, page_id={page_id})")

        instance.history_sync_progress = {
            "total": 0, "completed": 0, "current_chat": "", "status": "running"
        }

        try:
            client = httpx.AsyncClient(timeout=30.0)

            # Fetch conversations from Graph API
            resp = await client.get(
                f"{GRAPH_API_BASE}/me/conversations",
                params={
                    "access_token": page_access_token,
                    "limit": str(min(sync_count, 100)),
                    "fields": "id,participants,updated_time",
                },
            )
            if resp.status_code != 200:
                logger.error(f"Bot {bot_id}: Failed to fetch conversations: {resp.text[:200]}")
                instance.history_sync_progress["status"] = "completed"
                return

            data = resp.json()
            conversations = data.get("data", [])
            total = len(conversations)
            instance.history_sync_progress["total"] = total

            # Detect the page's participant ID by finding the ID that appears in ALL conversations.
            # The page is a participant in every conversation; external users only appear in one.
            page_participant_ids = set()
            page_participant_ids.add(page_id)  # Graph API page ID
            # Also get /me ID
            try:
                me_resp = await client.get(
                    f"{GRAPH_API_BASE}/me",
                    params={"access_token": page_access_token, "fields": "id,name"},
                )
                if me_resp.status_code == 200:
                    page_participant_ids.add(me_resp.json().get("id", ""))
            except Exception:
                pass
            # Find participant appearing in ALL conversations (that's the page)
            if len(conversations) >= 2:
                from collections import Counter
                all_pids = []
                for c in conversations:
                    for p in c.get("participants", {}).get("data", []):
                        all_pids.append(p.get("id", ""))
                for pid, count in Counter(all_pids).items():
                    if count == len(conversations):
                        page_participant_ids.add(pid)
            logger.info(f"Bot {bot_id}: Page participant IDs: {page_participant_ids}")

            for i, conv in enumerate(conversations):
                if getattr(instance, 'history_sync_stop_requested', False):
                    break

                conv_id = conv.get("id", "")
                participants = conv.get("participants", {}).get("data", [])
                # Find the non-page participant
                chat_name = ""
                sender_id = ""
                for p in participants:
                    if p.get("id") not in page_participant_ids:
                        chat_name = p.get("name", "")
                        sender_id = p.get("id", "")
                        break

                # Skip conversations where no external participant was found
                if not sender_id:
                    logger.info(f"Bot {bot_id}: Skipping conv {conv_id} — no external participant (all are page IDs)")
                    continue
                logger.info(f"Bot {bot_id}: Processing conv {conv_id} — contact: {chat_name} (id: {sender_id})")

                # Use sender_id as chat_id (consistent with real-time messages)
                effective_chat_id = sender_id or conv_id

                instance.history_sync_progress["current_chat"] = chat_name or "Unknown"
                instance.history_sync_progress["completed"] = i

                # Fetch messages for this conversation
                try:
                    msg_resp = await client.get(
                        f"{GRAPH_API_BASE}/{conv_id}/messages",
                        params={"access_token": page_access_token, "limit": "20", "fields": "message,from,created_time"},
                    )
                    if msg_resp.status_code != 200:
                        continue

                    messages = msg_resp.json().get("data", [])
                    if not messages:
                        continue

                    # Try to get name from first message if participants didn't have it
                    if not chat_name:
                        for msg in messages:
                            msg_from = msg.get("from", {})
                            if msg_from.get("id") != str(bot_id) and msg_from.get("name"):
                                chat_name = msg_from["name"]
                                break

                    # Also try to find existing conversation by conv_id (fallback lookup)
                    with get_db_session() as db:
                        from app.database import Conversation
                        # First try sender_id, then conv_id
                        conversation = db.query(Conversation).filter(
                            Conversation.bot_profile_id == bot_id,
                            Conversation.chat_id == effective_chat_id,
                        ).first()

                        # If not found by sender_id, try conv_id as chat_id
                        if not conversation and sender_id and sender_id != conv_id:
                            conversation = db.query(Conversation).filter(
                                Conversation.bot_profile_id == bot_id,
                                Conversation.chat_id == conv_id,
                            ).first()

                        if not conversation:
                            conversation = find_or_create_conversation(
                                db, bot_id, effective_chat_id, chat_name or "Unknown",
                                is_group=False, display_name=chat_name or "Unknown",
                            )
                        elif chat_name and conversation.chat_name in ("Unknown", ""):
                            # Update name if we now have a better one
                            conversation.chat_name = chat_name
                            conversation.display_name = chat_name
                            db.flush()

                        for msg in reversed(messages):
                            text = msg.get("message", "")
                            if not text:
                                continue
                            msg_from = msg.get("from", {})
                            msg_sender_id = msg_from.get("id", "")
                            platform_msg_id = msg.get("id", "")

                            # Parse actual message timestamp
                            msg_timestamp = None
                            created_time = msg.get("created_time")
                            if created_time:
                                try:
                                    msg_timestamp = datetime.fromisoformat(created_time.replace("Z", "+00:00")).replace(tzinfo=None)
                                except (ValueError, AttributeError):
                                    pass

                            if msg_sender_id in page_participant_ids:
                                # Outgoing message (sent by the page/bot)
                                from app.database import Message
                                if platform_msg_id:
                                    existing = db.query(Message).filter(
                                        Message.conversation_id == conversation.id,
                                        Message.whatsapp_message_id == platform_msg_id,
                                    ).first()
                                    if existing:
                                        continue
                                bot_msg = Message(
                                    conversation_id=conversation.id,
                                    role="assistant",
                                    content=text,
                                    whatsapp_message_id=platform_msg_id,
                                    timestamp=msg_timestamp or datetime.utcnow(),
                                )
                                db.add(bot_msg)
                            else:
                                # Incoming message (from the contact)
                                save_user_message(
                                    db, conversation.id, text,
                                    sender_name=msg_from.get("name", chat_name or "Unknown"),
                                    sender_id=msg_sender_id,
                                    platform_message_id=platform_msg_id,
                                    timestamp=msg_timestamp,
                                )

                        update_conversation_stats(db, conversation.id)
                        db.commit()

                except Exception as e:
                    logger.warning(f"Bot {bot_id}: Failed to sync conversation {conv_id}: {e}")

            instance.history_sync_progress["completed"] = total
            instance.history_sync_progress["status"] = "completed"
            logger.info(f"Bot {bot_id}: Messenger history sync completed ({total} conversations)")

        except Exception as e:
            logger.error(f"Bot {bot_id}: History sync failed: {e}", exc_info=True)
            instance.history_sync_progress["status"] = "completed"

    def cleanup(self, bot_profile_id: int) -> None:
        cleanup_bot_state(bot_profile_id)


# ---------------------------------------------------------------------------
# Webhook route registration
# ---------------------------------------------------------------------------

def _task_error_callback(task: asyncio.Task):
    """Log exceptions from background webhook processing tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Messenger webhook processing error: {exc}", exc_info=exc)


_webhook_routes_registered = False


def _ensure_webhook_routes():
    """Register Messenger webhook routes on the FastAPI app (once)."""
    global _webhook_routes_registered
    if _webhook_routes_registered:
        return
    _webhook_routes_registered = True

    from fastapi import FastAPI, Request, Response
    from app.main import app

    @app.get("/webhooks/messenger")
    async def messenger_webhook_verify(request: Request):
        """Handle Meta webhook verification challenge."""
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")

        if mode == "subscribe" and token and challenge:
            # Check against all registered bots' verify tokens
            for state in _bot_state.values():
                if state.get("webhook_verify_token") == token:
                    logger.info("Messenger webhook verified")
                    return Response(content=challenge, media_type="text/plain")

            # Fallback: accept default verify token even if no bots are running
            if token == "chathub_verify":
                logger.info("Messenger webhook verified (default token)")
                return Response(content=challenge, media_type="text/plain")

            logger.warning(f"Messenger webhook verify token mismatch: {token}")
            return Response(content="Forbidden", status_code=403)

        return Response(content="Bad Request", status_code=400)

    @app.post("/webhooks/messenger")
    async def messenger_webhook_receive(request: Request):
        """Handle inbound Messenger webhook events."""
        body = await request.body()

        # Signature verification
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(body, signature):
            logger.warning("Messenger webhook: invalid signature")
            return Response(content="Forbidden", status_code=403)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return Response(content="Bad Request", status_code=400)

        if data.get("object") != "page":
            return Response(content="OK", status_code=200)

        # Process entries asynchronously (return 200 quickly to Meta)
        task = asyncio.create_task(_process_webhook_entries(data.get("entry", [])))
        task.add_done_callback(_task_error_callback)

        return Response(content="EVENT_RECEIVED", status_code=200)

    logger.info("Messenger webhook routes registered at /webhooks/messenger")


def _verify_signature(body: bytes, signature_header: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta."""
    # If no bots are registered, reject (nothing to verify against)
    if not _bot_state:
        return False

    # Collect secrets from active bots — strip whitespace and reject empty values
    secrets = [
        s.get("app_secret").strip()
        for s in _bot_state.values()
        if s.get("app_secret") and s.get("app_secret").strip()
    ]

    # Fail closed: if no bot has a valid app_secret configured, reject all webhooks
    if not secrets:
        logger.warning("Messenger webhook: no valid app_secret configured — rejecting request")
        return False

    # Signature header is required when secrets are configured
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_sig = signature_header[7:]

    # Try each bot's app_secret
    for secret in secrets:
        computed = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(computed, expected_sig):
            return True

    return False


# ---------------------------------------------------------------------------
# Inbound message processing
# ---------------------------------------------------------------------------

async def _process_webhook_entries(entries: List[Dict]):
    """Process webhook entry objects from Meta."""
    for entry in entries:
        page_id = str(entry.get("id", ""))
        messaging_events = entry.get("messaging", [])

        for event in messaging_events:
            sender_id = str(event.get("sender", {}).get("id", ""))
            recipient_id = str(event.get("recipient", {}).get("id", ""))

            # Skip messages sent by the page itself
            if sender_id == recipient_id:
                continue

            # Find the bot instance for this page
            instance, bot_id = _find_bot_for_page(sender_id, recipient_id, page_id)
            if not instance:
                logger.debug(f"No active bot found for page event (page={page_id})")
                continue

            # Skip if sender is the page (echo of sent messages)
            if event.get("message", {}).get("is_echo"):
                continue

            if "message" in event:
                await _handle_message(bot_id, instance, event, sender_id)
            elif "delivery" in event:
                _handle_delivery(bot_id, event)
            elif "read" in event:
                _handle_read(bot_id, event)


def _find_bot_for_page(
    sender_id: str, recipient_id: str, page_id: str
) -> tuple:
    """Find the active bot instance handling this page.

    Matches by page_id — the entry-level ``id`` field from Meta's webhook
    payload, which is always the receiving page's ID regardless of message
    direction.

    Returns (instance, bot_profile_id) or (None, None).
    """
    if not page_id:
        return None, None

    for bot_id, state in _bot_state.items():
        inst = state.get("instance")
        if not inst or not inst.is_running:
            continue
        stored_page_id = state.get("page_id")
        if stored_page_id and stored_page_id == page_id:
            return inst, bot_id

    return None, None


async def _handle_message(
    bot_id: int, instance, event: Dict, sender_id: str
):
    """Handle an inbound message event from Messenger."""
    message = event.get("message", {})
    msg_id = message.get("mid", "")
    text = message.get("text", "")
    attachments = message.get("attachments", [])
    timestamp_ms = event.get("timestamp", 0)
    msg_timestamp = datetime.utcfromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.utcnow()

    # Get sender profile info
    sender_name = await _get_user_profile(bot_id, sender_id)

    config = instance.config
    bot_name = config.get("bot_name", "AI Agent")

    with get_db_session() as db:
        # Find or create conversation
        conversation = find_or_create_conversation(
            db,
            bot_id,
            sender_id,
            sender_name,
            is_group=False,
            phone=sender_id,
            display_name=sender_name,
        )

        # Deduplication
        is_dup, existing = is_duplicate_message(
            db,
            conversation.id,
            platform_message_id=msg_id,
            content=text,
        )
        if is_dup:
            if existing and has_response_after(db, conversation.id, existing.id):
                logger.debug(f"Bot {bot_id}: Skipping duplicate message {msg_id}")
                db.commit()
                return

        # Process attachments
        file_info = None
        media_analysis = None

        if attachments:
            file_info, media_analysis = await _process_attachments(
                bot_id, attachments, sender_name, config, text
            )

        # Build content
        content = text
        if not content and file_info:
            content = f"[{file_info.get('file_type', 'file').split('/')[0].title()} attachment]"

        # Save user message
        user_msg = save_user_message(
            db,
            conversation.id,
            content,
            sender_name=sender_name,
            sender_id=sender_id,
            platform_message_id=msg_id,
            timestamp=msg_timestamp,
            file_url=file_info.get("file_url") if file_info else None,
            file_type=file_info.get("file_type") if file_info else None,
            file_name=file_info.get("file_name") if file_info else None,
            file_size=file_info.get("file_size") if file_info else None,
            file_pages=file_info.get("file_pages") if file_info else None,
            media_analysis=media_analysis,
        )

        update_conversation_stats(db, conversation.id)
        db.commit()

        # Broadcast to WebSocket
        broadcast_user_message(conversation.id, bot_id, user_msg, conversation)

        # Check human takeover
        if is_human_takeover_active(db, conversation.id):
            logger.info(f"Bot {bot_id}: Human takeover active, skipping AI for {sender_id}")
            return

        # Check AI response toggle
        if not instance.ai_response_enabled:
            logger.debug(f"Bot {bot_id}: AI response disabled, skipping for {sender_id}")
            return

        # Generate and send AI response
        await _generate_ai_response(
            bot_id, instance, db, conversation, user_msg, sender_id, sender_name
        )


async def _generate_ai_response(
    bot_id: int,
    instance,
    db,
    conversation,
    user_msg,
    sender_id: str,
    sender_name: str,
):
    """Generate an AI response and send it."""
    config = instance.config

    # Show typing indicator
    await _send_typing_action(bot_id, sender_id, "typing_on")
    broadcast_typing(conversation.id, True)

    try:
        # Build AI messages
        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        max_history = config.get("max_history", 20)
        ai_messages = build_ai_messages(
            db, conversation.id, system_prompt, max_history,
            is_group=False, current_message=user_msg,
        )

        # Get AI provider
        from app.ai.factory import get_ai_provider
        provider = get_ai_provider(
            config.get("ai_provider", "openai"),
            config.get("api_key", ""),
            config.get("model", "gpt-4o-mini"),
        )

        # Generate response (run in thread to avoid blocking the event loop)
        response = await asyncio.to_thread(
            provider.chat_completion,
            messages=ai_messages,
            max_tokens=config.get("max_tokens", 1000),
            temperature=config.get("temperature", 0.7),
        )

        ai_text = response.content
        if not ai_text:
            return

        # Optional response delay
        delay_min = config.get("response_delay_min", 0)
        delay_max = config.get("response_delay_max", 0)
        if delay_min or delay_max:
            import random
            delay = random.uniform(delay_min, delay_max)
            await asyncio.sleep(delay)

        # Send response via Messenger
        adapter = MessengerAdapter()
        sent = await adapter.send_message(bot_id, sender_id, "", ai_text)

        if sent:
            # Save assistant message
            assistant_msg = save_assistant_message(
                db,
                conversation.id,
                ai_text,
                sender_name=config.get("bot_name", "AI Agent"),
            )
            update_conversation_stats(db, conversation.id)
            db.commit()

            # Broadcast to WebSocket
            broadcast_assistant_message(conversation.id, assistant_msg)

    except Exception as e:
        logger.error(f"Bot {bot_id}: AI response error: {e}", exc_info=True)
    finally:
        await _send_typing_action(bot_id, sender_id, "typing_off")
        broadcast_typing(conversation.id, False)


async def _process_attachments(
    bot_id: int,
    attachments: List[Dict],
    chat_name: str,
    config: Dict,
    user_text: str,
) -> tuple:
    """Process message attachments. Returns (file_info, media_analysis)."""
    file_info = None
    media_analysis = None

    for attachment in attachments:
        att_type = attachment.get("type", "")
        payload = attachment.get("payload", {})
        url = payload.get("url", "")

        if not url:
            continue

        # Download the attachment
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue

                content_type = resp.headers.get("content-type", "application/octet-stream")
                # Strip charset/params from content-type (e.g. "image/jpeg; charset=utf-8")
                content_type = content_type.split(";")[0].strip()
                # Map Messenger attachment types to MIME types
                if att_type == "image" and not content_type.startswith("image/"):
                    content_type = "image/jpeg"
                elif att_type == "video" and not content_type.startswith("video/"):
                    content_type = "video/mp4"
                elif att_type == "audio" and not content_type.startswith("audio/"):
                    content_type = "audio/mpeg"

                import base64
                b64_data = base64.b64encode(resp.content).decode("utf-8")

                file_info = save_media_file(
                    b64_data,
                    content_type,
                    bot_id,
                    chat_name,
                    direction="received",
                )

                if file_info and content_type.startswith("image/"):
                    # Analyze image with AI
                    try:
                        from app.ai.factory import get_ai_provider
                        provider = get_ai_provider(
                            config.get("ai_provider", "openai"),
                            config.get("api_key", ""),
                            config.get("model", "gpt-4o-mini"),
                        )
                        media_analysis = await asyncio.to_thread(
                            analyze_media_with_ai,
                            provider,
                            file_info["local_file_path"],
                            content_type,
                            user_text,
                        )
                    except Exception as e:
                        logger.error(f"Bot {bot_id}: Media analysis error: {e}")

                # Only process first attachment
                break

        except Exception as e:
            logger.error(f"Bot {bot_id}: Attachment download error: {e}")

    return file_info, media_analysis


# ---------------------------------------------------------------------------
# Messenger API helpers
# ---------------------------------------------------------------------------

async def _get_user_profile(bot_id: int, user_id: str) -> str:
    """Fetch user profile name from Messenger."""
    state = _get_state(bot_id)
    token = state.get("page_access_token")
    if not token:
        return user_id

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/{user_id}",
                params={
                    "fields": "first_name,last_name",
                    "access_token": token,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                first = data.get("first_name", "")
                last = data.get("last_name", "")
                return f"{first} {last}".strip() or user_id
    except Exception as e:
        logger.debug(f"Bot {bot_id}: Could not fetch profile for {user_id}: {e}")

    return user_id


async def _send_typing_action(bot_id: int, recipient_id: str, action: str):
    """Send typing_on or typing_off sender action."""
    state = _get_state(bot_id)
    token = state.get("page_access_token")
    if not token:
        return

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{GRAPH_API_BASE}/me/messages",
                params={"access_token": token},
                json={
                    "recipient": {"id": recipient_id},
                    "sender_action": action,
                },
            )
    except Exception:
        pass  # Typing indicators are best-effort


def _handle_delivery(bot_id: int, event: Dict):
    """Handle delivery receipt — log for debugging."""
    mids = event.get("delivery", {}).get("mids", [])
    if mids:
        logger.debug(f"Bot {bot_id}: Delivery receipt for {len(mids)} message(s)")


def _handle_read(bot_id: int, event: Dict):
    """Handle read receipt — log for debugging."""
    watermark = event.get("read", {}).get("watermark", 0)
    if watermark:
        logger.debug(f"Bot {bot_id}: Read receipt up to {watermark}")


def _split_message(text: str, max_length: int = 2000) -> List[str]:
    """Split a message into chunks respecting the max length."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Try to split at a newline or space
        split_at = text.rfind("\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = text.rfind(" ", 0, max_length)
        if split_at < max_length // 2:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()

    return chunks
