"""
Bumble Platform Adapter
Implements PlatformAdapter using Bumble's unofficial REST API with polling.

Key behaviors:
- Auth via phone number + SMS verification code -> session token
- Polls for new messages at configurable intervals
- Fetches match list and conversation updates
- Rate-limited to avoid detection
- In heterosexual matches, women must message first (enforced by Bumble)
- Supports text messages and image sharing
"""

import asyncio
import base64
import json
import logging
import random
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.platforms.base import (
    AuthMethod,
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
)
from app.platforms.message_handler import (
    analyze_media_with_ai,
    broadcast_assistant_message,
    broadcast_typing,
    broadcast_user_message,
    build_ai_messages,
    find_or_create_conversation,
    get_db_session,
    is_duplicate_message,
    is_human_takeover_active,
    save_assistant_message,
    save_media_file,
    save_user_message,
    update_conversation_stats,
)

logger = logging.getLogger(__name__)

# Bumble API constants
BUMBLE_API_BASE = "https://bumble.com/mwebapi.phtml"
BUMBLE_API_VERSION = "2"

# Rate limiting
MIN_POLL_INTERVAL = 10  # seconds between polls
MIN_ACTION_DELAY = 1.0  # seconds between API actions

# Device fingerprint template
DEFAULT_DEVICE_ID_PREFIX = "bumble_web_"


def _generate_device_id() -> str:
    """Generate a stable device ID for session persistence."""
    unique = uuid.uuid4().hex[:16]
    return f"{DEFAULT_DEVICE_ID_PREFIX}{unique}"


def _jitter(base: float, factor: float = 0.3) -> float:
    """Add random jitter to a delay value."""
    return base + random.uniform(-base * factor, base * factor)


class BumbleSession:
    """Manages Bumble API session state."""

    def __init__(self, device_id: str = None):
        self.device_id: str = device_id or _generate_device_id()
        self.session_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.authenticated: bool = False
        self.last_request_time: float = 0
        self._request_count: int = 0

    def get_headers(self) -> Dict[str, str]:
        """Build request headers with device fingerprint."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://bumble.com",
            "Referer": "https://bumble.com/app",
            "X-Pingback": self.device_id,
        }
        if self.session_token:
            headers["X-Session-Token"] = self.session_token
        return headers


class BumbleAPIClient:
    """HTTP client for Bumble's unofficial API with rate limiting."""

    def __init__(self, session: BumbleSession):
        self.session = session
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                http2=False,
            )

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _rate_limit(self):
        """Enforce minimum delay between requests."""
        now = time.monotonic()
        elapsed = now - self.session.last_request_time
        min_delay = _jitter(MIN_ACTION_DELAY)
        if elapsed < min_delay:
            await asyncio.sleep(min_delay - elapsed)
        self.session.last_request_time = time.monotonic()
        self.session._request_count += 1

    async def _request(
        self,
        body: dict,
        *,
        retries: int = 2,
    ) -> Tuple[bool, dict]:
        """Make a request to Bumble API.

        Args:
            body: JSON request body
            retries: Number of retries on transient errors

        Returns:
            Tuple of (success, response_data)
        """
        await self._ensure_client()
        await self._rate_limit()

        for attempt in range(retries + 1):
            try:
                resp = await self._client.post(
                    BUMBLE_API_BASE,
                    json=body,
                    headers=self.session.get_headers(),
                )

                if resp.status_code == 429:
                    wait = min(30, 5 * (2 ** attempt)) + random.uniform(1, 5)
                    logger.warning(f"Bumble rate limited, waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 401:
                    logger.error("Bumble session expired (401)")
                    self.session.authenticated = False
                    return False, {"error": "session_expired"}

                if resp.status_code >= 500:
                    if attempt < retries:
                        await asyncio.sleep(_jitter(2.0 * (attempt + 1)))
                        continue
                    return False, {"error": f"server_error_{resp.status_code}"}

                if resp.status_code != 200:
                    return False, {"error": f"http_{resp.status_code}", "body": resp.text[:500]}

                data = resp.json()

                # Update session token if returned
                if "session_token" in data:
                    self.session.session_token = data["session_token"]

                return True, data

            except httpx.TimeoutException:
                if attempt < retries:
                    await asyncio.sleep(_jitter(3.0))
                    continue
                return False, {"error": "timeout"}
            except httpx.HTTPError as e:
                logger.error(f"Bumble HTTP error: {e}")
                if attempt < retries:
                    await asyncio.sleep(_jitter(2.0))
                    continue
                return False, {"error": str(e)}
            except json.JSONDecodeError:
                return False, {"error": "invalid_json"}

        return False, {"error": "max_retries"}

    # ---------------------------------------------------------------
    # Auth endpoints
    # ---------------------------------------------------------------

    async def request_sms_code(self, phone: str) -> Tuple[bool, dict]:
        """Request SMS verification code."""
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 2,
                "server_login_by_phone": {
                    "phone": phone,
                    "device_id": self.session.device_id,
                },
            }],
        }
        return await self._request(body)

    async def verify_sms_code(self, phone: str, code: str) -> Tuple[bool, dict]:
        """Verify SMS code and get session token."""
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 3,
                "server_login_by_phone_verify": {
                    "phone": phone,
                    "code": code,
                    "device_id": self.session.device_id,
                },
            }],
        }
        ok, data = await self._request(body)
        if ok and data.get("body"):
            for msg in data["body"]:
                if msg.get("message_type") == 3:
                    resp = msg.get("client_login_success", {})
                    self.session.session_token = resp.get("session_token")
                    self.session.user_id = str(resp.get("user_id", ""))
                    if self.session.session_token:
                        self.session.authenticated = True
                        return True, resp
        return ok, data

    # ---------------------------------------------------------------
    # Messaging endpoints
    # ---------------------------------------------------------------

    async def get_conversations(self, offset: int = 0) -> Tuple[bool, dict]:
        """Fetch conversation list (matches with messages)."""
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 245,
                "server_get_conversations": {
                    "offset": offset,
                    "preferred_count": 30,
                    "folder_id": 0,
                },
            }],
        }
        return await self._request(body)

    async def get_chat_messages(
        self, user_id: str, *, count: int = 20, cursor: str = None
    ) -> Tuple[bool, dict]:
        """Fetch messages for a specific match."""
        msg_body = {
            "user_id": user_id,
            "preferred_count": count,
        }
        if cursor:
            msg_body["cursor"] = cursor
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 102,
                "server_get_chat_messages": msg_body,
            }],
        }
        return await self._request(body)

    async def send_text_message(self, user_id: str, text: str) -> Tuple[bool, dict]:
        """Send a text message to a match."""
        msg_id = str(uuid.uuid4())
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 104,
                "server_send_chat_message": {
                    "user_id": user_id,
                    "message_id": msg_id,
                    "message_type": 1,
                    "text": text,
                },
            }],
        }
        return await self._request(body)

    async def send_image_message(
        self, user_id: str, image_url: str
    ) -> Tuple[bool, dict]:
        """Send an image message to a match."""
        msg_id = str(uuid.uuid4())
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 104,
                "server_send_chat_message": {
                    "user_id": user_id,
                    "message_id": msg_id,
                    "message_type": 3,
                    "image_url": image_url,
                },
            }],
        }
        return await self._request(body)

    async def upload_image(self, image_data: bytes, content_type: str) -> Tuple[bool, str]:
        """Upload an image to Bumble and return the URL.

        Args:
            image_data: Raw image bytes
            content_type: MIME type (e.g. image/jpeg)

        Returns:
            Tuple of (success, image_url_or_error)
        """
        await self._ensure_client()
        await self._rate_limit()

        try:
            resp = await self._client.post(
                "https://bumble.com/upload/chat_photo",
                content=image_data,
                headers={
                    **self.session.get_headers(),
                    "Content-Type": content_type,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                url = data.get("url") or data.get("photo_url", "")
                if url:
                    return True, url
                return False, "no url in response"
            return False, f"upload failed: {resp.status_code}"
        except Exception as e:
            return False, str(e)

    async def get_user_profile(self, user_id: str) -> Tuple[bool, dict]:
        """Fetch a user's profile."""
        body = {
            "version": BUMBLE_API_VERSION,
            "$gpb": "badoo.bma.BadooMessage",
            "body": [{
                "message_type": 403,
                "server_get_user": {
                    "user_id": user_id,
                },
            }],
        }
        return await self._request(body)


class BumbleAdapter(PlatformAdapter):
    """Bumble adapter using unofficial REST API with polling.

    Auth: Phone + SMS verification -> session token
    Messaging: Poll-based with configurable intervals
    Limitations: No groups, no file send, women message first in hetero matches
    """

    def __init__(self):
        self._sessions: Dict[int, BumbleSession] = {}  # bot_profile_id -> session
        self._clients: Dict[int, BumbleAPIClient] = {}
        self._known_message_ids: Dict[int, set] = {}  # bot_id -> set of known bumble msg IDs
        self._match_cache: Dict[int, Dict[str, dict]] = {}  # bot_id -> {user_id: match_info}
        self._poll_intervals: Dict[int, float] = {}

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.BUMBLE

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=False,
            supports_media=True,
            supports_file_send=False,
            supports_reactions=False,
            supports_read_receipts=False,
            supports_typing_indicator=False,
            supports_history_sync=False,
            supports_contacts_list=True,
            supports_groups_list=False,
            supports_profile_pic=True,
            auth_method=AuthMethod.CREDENTIALS,
            max_message_length=5000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
            ],
        )

    def _get_session(self, bot_profile_id: int) -> BumbleSession:
        if bot_profile_id not in self._sessions:
            self._sessions[bot_profile_id] = BumbleSession()
        return self._sessions[bot_profile_id]

    def _get_client(self, bot_profile_id: int) -> BumbleAPIClient:
        if bot_profile_id not in self._clients:
            session = self._get_session(bot_profile_id)
            self._clients[bot_profile_id] = BumbleAPIClient(session)
        return self._clients[bot_profile_id]

    # -------------------------------------------------------------------
    # Session persistence
    # -------------------------------------------------------------------

    def _session_file(self, bot_profile_id: int) -> Path:
        """Path to persisted session data."""
        import sys
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent.parent.parent
        else:
            base = Path(__file__).resolve().parent.parent.parent
        path = base / "data" / "sessions" / f"bot_{bot_profile_id}" / "bumble_session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _save_session(self, bot_profile_id: int):
        from app.auth.utils import encrypt_string

        session = self._get_session(bot_profile_id)
        data = {
            "device_id": session.device_id,
            "session_token": encrypt_string(session.session_token) if session.session_token else None,
            "user_id": session.user_id,
        }
        try:
            self._session_file(bot_profile_id).write_text(json.dumps(data))
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to save Bumble session: {e}")

    def _load_session(self, bot_profile_id: int) -> bool:
        """Load persisted session. Returns True if a session was loaded."""
        from app.auth.utils import decrypt_string

        path = self._session_file(bot_profile_id)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            session = self._get_session(bot_profile_id)
            session.device_id = data.get("device_id", session.device_id)
            encrypted_token = data.get("session_token")
            session.session_token = decrypt_string(encrypted_token) if encrypted_token else None
            session.user_id = data.get("user_id")
            if session.session_token:
                session.authenticated = True
                return True
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to load Bumble session: {e}")
        return False

    # -------------------------------------------------------------------
    # PlatformAdapter interface
    # -------------------------------------------------------------------

    async def run(self, instance) -> None:
        """Main run loop: authenticate, then poll for messages."""
        bot_id = instance.bot_profile_id
        config = instance.config

        await instance.notify_status({
            "message": "Initializing Bumble connection...",
            "status": "connecting",
        })

        # Try to restore existing session
        client = self._get_client(bot_id)
        session_loaded = self._load_session(bot_id)

        if session_loaded:
            logger.info(f"Bot {bot_id}: Restored Bumble session, validating...")
            await instance.notify_status({
                "message": "Validating saved session...",
                "status": "connecting",
            })
            # Validate by fetching conversations
            ok, _ = await client.get_conversations()
            if not ok:
                logger.info(f"Bot {bot_id}: Saved session invalid, need re-auth")
                self._get_session(bot_id).authenticated = False
                session_loaded = False

        if not session_loaded:
            # Need fresh authentication
            authenticated = await self._authenticate(instance, client, config)
            if not authenticated:
                instance.is_running = False
                await instance.notify_status({
                    "message": "Authentication failed",
                    "status": "error",
                    "error": "Failed to authenticate with Bumble",
                })
                return

        # Save session for future restarts
        self._save_session(bot_id)

        # Mark connected
        instance.whatsapp_connected = True  # reused field for "platform connected"
        await instance.notify_status({
            "message": "Connected to Bumble",
            "status": "connected",
        })

        logger.info(f"Bot {bot_id}: Bumble connected, starting poll loop")

        # Initialize known messages to avoid processing old messages
        await self._initialize_known_messages(bot_id, client)

        # Enter main poll loop
        poll_interval = float(config.get("poll_interval", 15))
        self._poll_intervals[bot_id] = max(MIN_POLL_INTERVAL, poll_interval)

        try:
            while instance.is_running:
                try:
                    # Poll for new messages
                    await self._poll_messages(bot_id, instance, client)

                    # Process outbound queue
                    await self._process_outbound(bot_id, instance, client)

                    # Wait with jitter
                    interval = self._poll_intervals.get(bot_id, 15)
                    await asyncio.sleep(_jitter(interval, factor=0.2))

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        f"Bot {bot_id}: Poll loop error: {e}", exc_info=True
                    )
                    # Back off on errors
                    await asyncio.sleep(_jitter(30))

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_id}: Bumble run loop cancelled")
        finally:
            instance.whatsapp_connected = False
            await client.close()
            logger.info(f"Bot {bot_id}: Bumble adapter stopped")

    async def _authenticate(
        self, instance, client: BumbleAPIClient, config: dict
    ) -> bool:
        """Authenticate via phone + SMS verification.

        Uses instance status/QR callbacks to communicate with UI.
        The UI should provide credentials via config or a callback mechanism.
        """
        bot_id = instance.bot_profile_id
        phone = config.get("bumble_phone", "")

        if not phone:
            await instance.notify_status({
                "message": "Bumble phone number required in bot config",
                "status": "error",
                "auth_required": True,
                "auth_type": "phone",
            })
            # Wait for phone to be provided (up to 5 minutes)
            for _ in range(300):
                if not instance.is_running:
                    return False
                phone = instance.config.get("bumble_phone", "")
                if phone:
                    break
                await asyncio.sleep(1)
            if not phone:
                return False

        # Request SMS code
        await instance.notify_status({
            "message": f"Sending SMS code to {phone[-4:].rjust(len(phone), '*')}",
            "status": "connecting",
        })

        ok, resp = await client.request_sms_code(phone)
        if not ok:
            logger.error(f"Bot {bot_id}: Failed to request SMS: {resp}")
            return False

        # Wait for verification code
        await instance.notify_status({
            "message": "Enter SMS verification code",
            "status": "awaiting_code",
            "auth_required": True,
            "auth_type": "sms_code",
        })

        code = ""
        for _ in range(300):  # 5 minute timeout
            if not instance.is_running:
                return False
            code = instance.config.get("bumble_sms_code", "")
            if code:
                break
            await asyncio.sleep(1)

        if not code:
            return False

        # Verify code
        ok, resp = await client.verify_sms_code(phone, code)
        if not ok:
            logger.error(f"Bot {bot_id}: SMS verification failed: {resp}")
            await instance.notify_status({
                "message": "SMS verification failed",
                "status": "error",
            })
            return False

        logger.info(f"Bot {bot_id}: Bumble authenticated successfully")
        return True

    async def _initialize_known_messages(
        self, bot_id: int, client: BumbleAPIClient
    ):
        """Load existing message IDs so we don't reprocess old messages."""
        self._known_message_ids[bot_id] = set()

        ok, data = await client.get_conversations()
        if not ok:
            return

        conversations = self._extract_conversations(data)
        for conv in conversations:
            user_id = conv.get("user_id", "")
            if not user_id:
                continue
            ok2, msg_data = await client.get_chat_messages(user_id, count=10)
            if ok2:
                for msg in self._extract_messages(msg_data):
                    msg_id = msg.get("id", "")
                    if msg_id:
                        self._known_message_ids[bot_id].add(msg_id)
            # Avoid hammering the API
            await asyncio.sleep(_jitter(1.5))

    async def _poll_messages(
        self, bot_id: int, instance, client: BumbleAPIClient
    ):
        """Poll for new messages across all conversations."""
        ok, data = await client.get_conversations()
        if not ok:
            if data.get("error") == "session_expired":
                logger.warning(f"Bot {bot_id}: Session expired during poll")
                instance.is_running = False
                await instance.notify_status({
                    "message": "Bumble session expired, please restart",
                    "status": "error",
                    "error": "session_expired",
                })
            return

        conversations = self._extract_conversations(data)

        # Update match cache
        self._match_cache[bot_id] = {}
        for conv in conversations:
            uid = conv.get("user_id", "")
            if uid:
                self._match_cache[bot_id][uid] = conv

        for conv in conversations:
            user_id = conv.get("user_id", "")
            if not user_id:
                continue

            # Check if conversation has unread messages
            unread = conv.get("unread_count", 0)
            if unread <= 0:
                continue

            ok2, msg_data = await client.get_chat_messages(user_id, count=unread + 5)
            if not ok2:
                continue

            messages = self._extract_messages(msg_data)
            known = self._known_message_ids.get(bot_id, set())

            for msg in messages:
                msg_id = msg.get("id", "")
                if not msg_id or msg_id in known:
                    continue

                known.add(msg_id)

                # Only process incoming messages (not our own)
                sender_id = str(msg.get("sender_id", ""))
                session = self._get_session(bot_id)
                if sender_id == session.user_id:
                    continue

                await self._handle_incoming_message(
                    bot_id, instance, msg, conv
                )

            # Small delay between conversations
            await asyncio.sleep(_jitter(0.5))

    async def _handle_incoming_message(
        self,
        bot_id: int,
        instance,
        msg: dict,
        conv: dict,
    ):
        """Process a single incoming message."""
        config = instance.config
        user_id = str(conv.get("user_id", ""))
        chat_name = conv.get("name", f"Match {user_id}")
        msg_text = msg.get("text", "")
        msg_id = str(msg.get("id", ""))
        msg_type = msg.get("type", 1)  # 1=text, 3=image
        image_url = msg.get("image_url", "")

        timestamp = None
        ts_val = msg.get("timestamp")
        if ts_val:
            try:
                timestamp = datetime.utcfromtimestamp(int(ts_val))
            except (ValueError, TypeError, OSError):
                timestamp = datetime.utcnow()
        else:
            timestamp = datetime.utcnow()

        logger.info(
            f"Bot {bot_id}: New Bumble message from '{chat_name}': "
            f"'{msg_text[:50]}' (type={msg_type})"
        )

        with get_db_session() as db:
            # Find or create conversation
            conversation = find_or_create_conversation(
                db,
                bot_id,
                chat_id=user_id,
                chat_name=chat_name,
                is_group=False,
                phone="",
                display_name=chat_name,
                profile_pic=conv.get("profile_pic", ""),
            )

            # Dedup check
            is_dup, _ = is_duplicate_message(
                db,
                conversation.id,
                platform_message_id=msg_id,
                content=msg_text,
            )
            if is_dup:
                return

            # Handle image messages
            file_info = None
            media_analysis = None
            if msg_type == 3 and image_url:
                file_info = await self._download_and_save_image(
                    bot_id, image_url, chat_name
                )
                if file_info and config.get("ai_provider"):
                    try:
                        from app.ai.factory import get_ai_provider
                        from app.auth.utils import decrypt_string

                        api_key = decrypt_string(config.get("api_key_encrypted", ""))
                        provider = get_ai_provider(
                            config["ai_provider"], api_key, config.get("model", "")
                        )
                        media_analysis = analyze_media_with_ai(
                            provider,
                            file_info["local_file_path"],
                            file_info["file_type"],
                            msg_text,
                        )
                    except Exception as e:
                        logger.error(f"Bot {bot_id}: Image analysis error: {e}")

            # Save incoming message
            saved_msg = save_user_message(
                db,
                conversation.id,
                content=msg_text or (f"[Image: {media_analysis}]" if media_analysis else "[Image]"),
                sender_name=chat_name,
                sender_id=user_id,
                platform_message_id=msg_id,
                timestamp=timestamp,
                file_url=file_info["file_url"] if file_info else None,
                file_type=file_info["file_type"] if file_info else None,
                file_name=file_info["file_name"] if file_info else None,
                file_size=file_info["file_size"] if file_info else None,
                media_analysis=media_analysis,
            )
            update_conversation_stats(db, conversation.id)
            db.commit()

            # Broadcast to WebSocket
            broadcast_user_message(
                conversation.id, bot_id, saved_msg, conversation
            )

            # Check human takeover
            if is_human_takeover_active(db, conversation.id):
                logger.info(f"Bot {bot_id}: Human takeover active, skipping AI")
                return

            # Generate AI response if enabled
            if not instance.ai_response_enabled:
                return
            if not config.get("ai_provider") or not config.get("api_key_encrypted"):
                return

            broadcast_typing(conversation.id, True, "Bot")

            try:
                from app.ai.factory import get_ai_provider
                from app.auth.utils import decrypt_string

                api_key = decrypt_string(config["api_key_encrypted"])
                provider = get_ai_provider(
                    config["ai_provider"], api_key, config.get("model", "")
                )

                system_prompt = config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db,
                    conversation.id,
                    system_prompt,
                    current_message=saved_msg,
                )

                response = await asyncio.to_thread(
                    provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=int(config.get("max_tokens", 500)),
                    temperature=float(config.get("temperature", 0.7)),
                )
                reply_text = response.content

                if reply_text:
                    # Truncate to max message length
                    if len(reply_text) > 5000:
                        reply_text = reply_text[:4997] + "..."

                    # Send via Bumble API
                    client = self._get_client(bot_id)
                    ok, _ = await client.send_text_message(user_id, reply_text)

                    if ok:
                        # Save assistant message
                        bot_name = config.get("name", "Bot")
                        ai_msg = save_assistant_message(
                            db,
                            conversation.id,
                            reply_text,
                            sender_name=bot_name,
                        )
                        update_conversation_stats(db, conversation.id)
                        db.commit()

                        broadcast_assistant_message(conversation.id, ai_msg)
                    else:
                        logger.error(f"Bot {bot_id}: Failed to send Bumble reply")

            except Exception as e:
                logger.error(
                    f"Bot {bot_id}: AI response error: {e}", exc_info=True
                )
            finally:
                broadcast_typing(conversation.id, False)

    async def _download_and_save_image(
        self, bot_id: int, image_url: str, chat_name: str
    ) -> Optional[Dict[str, Any]]:
        """Download an image from Bumble and save locally."""
        client = self._get_client(bot_id)
        try:
            await client._ensure_client()
            session = self._get_session(bot_id)
            resp = await client._client.get(
                image_url, headers=session.get_headers()
            )
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("content-type", "image/jpeg")
            image_b64 = base64.b64encode(resp.content).decode("utf-8")
            return save_media_file(
                image_b64, content_type, bot_id, chat_name, direction="received"
            )
        except Exception as e:
            logger.error(f"Bot {bot_id}: Image download error: {e}")
            return None

    async def _process_outbound(
        self, bot_id: int, instance, client: BumbleAPIClient
    ):
        """Send queued outbound messages."""
        messages = instance.get_outbound_messages()
        for item in messages:
            chat_id = item.get("chat_id", "")
            text = item.get("message", "")
            if chat_id and text:
                ok, _ = await client.send_text_message(chat_id, text)
                if not ok:
                    logger.error(
                        f"Bot {bot_id}: Failed to send outbound to {chat_id}"
                    )
                await asyncio.sleep(_jitter(1.0))

    # -------------------------------------------------------------------
    # Data extraction helpers
    # -------------------------------------------------------------------

    def _extract_conversations(self, data: dict) -> List[dict]:
        """Extract conversation list from Bumble API response."""
        result = []
        for body_msg in data.get("body", []):
            convs = body_msg.get("client_get_conversations", {}).get(
                "conversations", []
            )
            for conv in convs:
                user = conv.get("user", {})
                user_id = str(user.get("user_id", ""))
                name = user.get("name", "")
                profile_pic = ""
                photos = user.get("photos", [])
                if photos:
                    profile_pic = photos[0].get("large_url", "") or photos[0].get("url", "")

                result.append({
                    "user_id": user_id,
                    "name": name,
                    "profile_pic": profile_pic,
                    "unread_count": conv.get("unread_count", 0),
                    "last_message": conv.get("last_message", {}),
                    "gender": user.get("gender", 0),
                })
        return result

    def _extract_messages(self, data: dict) -> List[dict]:
        """Extract messages from Bumble chat messages response."""
        result = []
        for body_msg in data.get("body", []):
            messages = body_msg.get("client_get_chat_messages", {}).get(
                "messages", []
            )
            for msg in messages:
                result.append({
                    "id": str(msg.get("id", "")),
                    "sender_id": str(msg.get("from_person_id", "")),
                    "text": msg.get("text", ""),
                    "type": msg.get("message_type", 1),
                    "image_url": msg.get("image_url", ""),
                    "timestamp": msg.get("date_modified", msg.get("date_created", "")),
                })
        return result

    # -------------------------------------------------------------------
    # PlatformAdapter: send_message / send_file
    # -------------------------------------------------------------------

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message to a Bumble match."""
        client = self._get_client(bot_profile_id)
        session = self._get_session(bot_profile_id)

        if not session.authenticated:
            logger.error(f"Bot {bot_profile_id}: Cannot send - not authenticated")
            return False

        # Truncate
        if len(message) > 5000:
            message = message[:4997] + "..."

        ok, _ = await client.send_text_message(chat_id, message)
        if ok:
            logger.info(
                f"Bot {bot_profile_id}: Sent Bumble message to {chat_name}"
            )
        else:
            logger.error(
                f"Bot {bot_profile_id}: Failed to send Bumble message to {chat_name}"
            )
        return ok

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send an image to a Bumble match. Only images are supported."""
        if not file_type.startswith("image/"):
            logger.warning(
                f"Bot {bot_profile_id}: Bumble only supports image sending, "
                f"got {file_type}"
            )
            return False

        client = self._get_client(bot_profile_id)
        session = self._get_session(bot_profile_id)

        if not session.authenticated:
            logger.error(f"Bot {bot_profile_id}: Cannot send file - not authenticated")
            return False

        try:
            path = Path(file_path)
            if not path.exists():
                logger.error(f"Bot {bot_profile_id}: File not found: {file_path}")
                return False

            image_data = path.read_bytes()
            ok, url_or_err = await client.upload_image(image_data, file_type)
            if not ok:
                logger.error(
                    f"Bot {bot_profile_id}: Image upload failed: {url_or_err}"
                )
                return False

            ok, _ = await client.send_image_message(chat_id, url_or_err)
            if ok and caption:
                # Send caption as separate text message
                await asyncio.sleep(_jitter(0.5))
                await client.send_text_message(chat_id, caption)

            return ok

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: send_file error: {e}")
            return False

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a bot."""
        self._known_message_ids.pop(bot_profile_id, None)
        self._match_cache.pop(bot_profile_id, None)
        self._poll_intervals.pop(bot_profile_id, None)

        client = self._clients.pop(bot_profile_id, None)
        if client:
            # Schedule async close if possible
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.close())
            except RuntimeError:
                pass

        self._sessions.pop(bot_profile_id, None)

    def get_contacts(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Get match list as contacts."""
        cache = self._match_cache.get(bot_profile_id, {})
        return [
            {
                "chat_id": uid,
                "name": info.get("name", f"Match {uid}"),
                "profile_pic": info.get("profile_pic", ""),
            }
            for uid, info in cache.items()
        ]
