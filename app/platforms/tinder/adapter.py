"""
Tinder Platform Adapter
Uses Tinder's unofficial REST API with polling for message updates.

Auth flow:
    1. Phone number -> SMS OTP -> auth token
    2. Or Facebook token -> auth token
    Token is stored in bot session directory for reuse.

Rate limiting:
    Tinder aggressively rate-limits. The adapter uses exponential backoff
    and configurable poll intervals to stay under limits.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
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

TINDER_API_BASE = "https://api.gotinder.com"

# Default headers that Tinder expects
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Tinder/14.21.0 (iPhone; iOS 17.2; Scale/3.00)",
    "Accept": "application/json",
    "platform": "ios",
    "app-version": "5530",
}

# Rate limit defaults
DEFAULT_POLL_INTERVAL = 10  # seconds between message polls
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 120
RATE_LIMIT_BACKOFF_FACTOR = 2.0
MAX_CONSECUTIVE_ERRORS = 10


class TinderAPIError(Exception):
    """Tinder API request failed."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Tinder API {status_code}: {message}")


class TinderRateLimitError(TinderAPIError):
    """Rate limited by Tinder."""

    def __init__(self, retry_after: float = 60.0):
        self.retry_after = retry_after
        super().__init__(429, f"Rate limited, retry after {retry_after}s")


class TinderAdapter(PlatformAdapter):
    """Tinder adapter using unofficial REST API with polling.

    Config keys (from instance.config):
        tinder_phone: Phone number for SMS auth (e.g., "+1234567890")
        tinder_otp_code: SMS OTP code (set after requesting OTP)
        tinder_fb_token: Facebook access token (alternative auth)
        tinder_auth_token: Pre-existing X-Auth-Token (skip auth flow)
        tinder_poll_interval: Seconds between polls (default 10)
        tinder_refresh_token: Refresh token for re-auth
    """

    def __init__(self):
        self._clients: Dict[int, httpx.AsyncClient] = {}
        self._auth_tokens: Dict[int, str] = {}
        self._matches_cache: Dict[int, Dict[str, Any]] = {}
        self._last_activity: Dict[int, str] = {}  # bot_id -> last activity timestamp
        self._poll_intervals: Dict[int, float] = {}
        self._running: Dict[int, bool] = {}

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.TINDER

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=False,
            supports_media=True,
            supports_file_send=False,
            supports_reactions=True,
            supports_read_receipts=False,
            supports_typing_indicator=False,
            supports_history_sync=False,
            supports_contacts_list=True,
            supports_groups_list=False,
            supports_profile_pic=True,
            auth_method=AuthMethod.CREDENTIALS,
            max_message_length=5000,
            supported_media_types=["image/gif"],
        )

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _session_dir(self, bot_profile_id: int) -> Path:
        """Get the session directory for storing auth tokens."""
        import sys
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent.parent.parent
        else:
            base_dir = Path(__file__).resolve().parent.parent.parent
        session_dir = base_dir / "data" / "sessions" / f"bot_{bot_profile_id}" / "tinder"
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _save_auth_token(self, bot_profile_id: int, token: str, refresh_token: str = ""):
        """Persist auth token to disk for session reuse (encrypted)."""
        from app.auth.utils import encrypt_string

        session_file = self._session_dir(bot_profile_id) / "session.json"
        data = {
            "auth_token": encrypt_string(token),
            "refresh_token": encrypt_string(refresh_token) if refresh_token else "",
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        session_file.write_text(json.dumps(data))
        logger.info(f"Bot {bot_profile_id}: Saved Tinder auth token")

    def _load_auth_token(self, bot_profile_id: int) -> Optional[Dict[str, str]]:
        """Load persisted auth token (decrypting from disk)."""
        from app.auth.utils import decrypt_string

        session_file = self._session_dir(bot_profile_id) / "session.json"
        if session_file.exists():
            try:
                data = json.loads(session_file.read_text())
                data["auth_token"] = decrypt_string(data["auth_token"])
                if data.get("refresh_token"):
                    data["refresh_token"] = decrypt_string(data["refresh_token"])
                return data
            except (json.JSONDecodeError, KeyError, Exception):
                logger.warning(f"Bot {bot_profile_id}: Failed to load/decrypt session file")
                pass
        return None

    # ------------------------------------------------------------------
    # HTTP client helpers
    # ------------------------------------------------------------------

    def _get_client(self, bot_profile_id: int) -> httpx.AsyncClient:
        """Get or create an httpx client for a bot."""
        if bot_profile_id not in self._clients:
            self._clients[bot_profile_id] = httpx.AsyncClient(
                base_url=TINDER_API_BASE,
                headers=DEFAULT_HEADERS.copy(),
                timeout=30.0,
            )
        return self._clients[bot_profile_id]

    async def _close_client(self, bot_profile_id: int):
        """Close and remove the httpx client."""
        client = self._clients.pop(bot_profile_id, None)
        if client:
            await client.aclose()

    async def _api_request(
        self,
        bot_profile_id: int,
        method: str,
        url: str,
        *,
        json_data: dict = None,
        params: dict = None,
        auth_required: bool = True,
    ) -> dict:
        """Make an authenticated API request with rate limit handling.

        Raises:
            TinderRateLimitError: On 429 responses
            TinderAPIError: On other error responses
        """
        client = self._get_client(bot_profile_id)
        headers = {}

        if auth_required:
            token = self._auth_tokens.get(bot_profile_id)
            if not token:
                raise TinderAPIError(401, "No auth token available")
            headers["X-Auth-Token"] = token

        try:
            response = await client.request(
                method,
                url,
                json=json_data,
                params=params,
                headers=headers,
            )
        except httpx.TimeoutException:
            raise TinderAPIError(0, "Request timed out")
        except httpx.ConnectError:
            raise TinderAPIError(0, "Connection failed")

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "60"))
            raise TinderRateLimitError(retry_after)

        if response.status_code == 401:
            self._auth_tokens.pop(bot_profile_id, None)
            raise TinderAPIError(401, "Auth token expired or invalid")

        if response.status_code >= 400:
            body = response.text[:200]
            raise TinderAPIError(response.status_code, body)

        if not response.content:
            return {}
        return response.json()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self, instance) -> str:
        """Authenticate with Tinder and return an X-Auth-Token.

        Tries in order:
        1. Pre-existing auth token from config or saved session
        2. Phone/SMS verification
        3. Facebook token auth
        """
        bot_id = instance.bot_profile_id
        config = instance.config

        # 1. Check for pre-existing token
        token = config.get("tinder_auth_token")
        if token:
            self._auth_tokens[bot_id] = token
            if await self._validate_token(bot_id):
                logger.info(f"Bot {bot_id}: Using pre-existing Tinder auth token")
                return token
            self._auth_tokens.pop(bot_id, None)

        # 1b. Check saved session
        saved = self._load_auth_token(bot_id)
        if saved and saved.get("auth_token"):
            self._auth_tokens[bot_id] = saved["auth_token"]
            if await self._validate_token(bot_id):
                logger.info(f"Bot {bot_id}: Restored Tinder session from disk")
                return saved["auth_token"]
            self._auth_tokens.pop(bot_id, None)
            # Try refresh token
            if saved.get("refresh_token"):
                token = await self._refresh_auth(bot_id, saved["refresh_token"])
                if token:
                    return token

        # 2. Phone/SMS auth
        phone = config.get("tinder_phone")
        if phone:
            return await self._phone_auth(instance, phone)

        # 3. Facebook token auth
        fb_token = config.get("tinder_fb_token")
        if fb_token:
            return await self._facebook_auth(bot_id, fb_token)

        raise TinderAPIError(
            401,
            "No auth credentials provided. Set tinder_auth_token, tinder_phone, or tinder_fb_token in config.",
        )

    async def _validate_token(self, bot_profile_id: int) -> bool:
        """Check if the current auth token is still valid."""
        try:
            await self._api_request(bot_profile_id, "GET", "/v2/profile?include=user")
            return True
        except TinderAPIError:
            return False

    async def _refresh_auth(self, bot_profile_id: int, refresh_token: str) -> Optional[str]:
        """Attempt to refresh the auth token."""
        try:
            data = await self._api_request(
                bot_profile_id,
                "POST",
                "/v2/auth/token/refresh",
                json_data={"refresh_token": refresh_token},
                auth_required=False,
            )
            token = data.get("data", {}).get("api_token")
            refresh = data.get("data", {}).get("refresh_token", refresh_token)
            if token:
                self._auth_tokens[bot_profile_id] = token
                self._save_auth_token(bot_profile_id, token, refresh)
                logger.info(f"Bot {bot_profile_id}: Refreshed Tinder auth token")
                return token
        except TinderAPIError as e:
            logger.warning(f"Bot {bot_profile_id}: Token refresh failed: {e}")
        return None

    async def _phone_auth(self, instance, phone: str) -> str:
        """Authenticate via phone number + SMS OTP.

        Flow:
        1. Send OTP request
        2. Notify instance to prompt user for OTP
        3. Wait for OTP code in instance.config['tinder_otp_code']
        4. Verify OTP -> get auth token
        """
        bot_id = instance.bot_profile_id

        # Step 1: Request OTP
        await instance.notify_status({
            "message": f"Requesting Tinder SMS code for {phone}...",
            "auth_state": "otp_requesting",
        })

        await self._api_request(
            bot_id,
            "POST",
            "/v2/auth/sms/send?auth_type=sms",
            json_data={"phone_number": phone},
            auth_required=False,
        )

        # Step 2: Wait for user to provide OTP code
        await instance.notify_status({
            "message": "Enter Tinder SMS verification code",
            "auth_state": "otp_waiting",
            "requires_input": True,
            "input_field": "tinder_otp_code",
        })

        otp_code = None
        for _ in range(120):  # Wait up to 2 minutes
            if not self._running.get(bot_id, True):
                raise TinderAPIError(0, "Bot stopped while waiting for OTP")
            otp_code = instance.config.get("tinder_otp_code")
            if otp_code:
                break
            await asyncio.sleep(1)

        if not otp_code:
            raise TinderAPIError(0, "OTP code not provided within timeout")

        # Step 3: Verify OTP
        await instance.notify_status({
            "message": "Verifying SMS code...",
            "auth_state": "otp_verifying",
        })

        data = await self._api_request(
            bot_id,
            "POST",
            "/v2/auth/sms/validate?auth_type=sms",
            json_data={
                "phone_number": phone,
                "otp_code": otp_code,
            },
            auth_required=False,
        )

        validation_token = data.get("data", {}).get("validation_token")
        if not validation_token:
            raise TinderAPIError(401, "OTP validation failed - no validation token returned")

        # Step 4: Get auth token
        data = await self._api_request(
            bot_id,
            "POST",
            "/v2/auth/login/sms",
            json_data={
                "phone_number": phone,
                "validation_token": validation_token,
            },
            auth_required=False,
        )

        token = data.get("data", {}).get("api_token")
        refresh = data.get("data", {}).get("refresh_token", "")
        if not token:
            raise TinderAPIError(401, "SMS auth failed - no API token returned")

        self._auth_tokens[bot_id] = token
        self._save_auth_token(bot_id, token, refresh)

        # Clear the OTP code from config
        instance.config.pop("tinder_otp_code", None)

        logger.info(f"Bot {bot_id}: Authenticated via phone SMS")
        return token

    async def _facebook_auth(self, bot_profile_id: int, fb_token: str) -> str:
        """Authenticate using a Facebook access token."""
        data = await self._api_request(
            bot_profile_id,
            "POST",
            "/v2/auth/login/facebook",
            json_data={"token": fb_token},
            auth_required=False,
        )

        token = data.get("data", {}).get("api_token")
        refresh = data.get("data", {}).get("refresh_token", "")
        if not token:
            raise TinderAPIError(401, "Facebook auth failed - no API token returned")

        self._auth_tokens[bot_profile_id] = token
        self._save_auth_token(bot_profile_id, token, refresh)
        logger.info(f"Bot {bot_profile_id}: Authenticated via Facebook token")
        return token

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    async def _get_matches(self, bot_profile_id: int, page_token: str = None) -> Dict[str, Any]:
        """Fetch match list (conversations)."""
        params = {"count": "60", "message": "1"}
        if page_token:
            params["page_token"] = page_token

        data = await self._api_request(
            bot_profile_id, "GET", "/v2/matches", params=params
        )
        return data.get("data", {})

    async def _get_messages(
        self, bot_profile_id: int, match_id: str, page_token: str = None
    ) -> Dict[str, Any]:
        """Fetch messages for a specific match."""
        params = {"count": "100"}
        if page_token:
            params["page_token"] = page_token

        data = await self._api_request(
            bot_profile_id, "GET", f"/v2/matches/{match_id}/messages", params=params
        )
        return data.get("data", {})

    async def _send_message_api(
        self, bot_profile_id: int, match_id: str, content: str
    ) -> Dict[str, Any]:
        """Send a text message to a match."""
        data = await self._api_request(
            bot_profile_id,
            "POST",
            f"/user/matches/{match_id}",
            json_data={"message": content},
        )
        return data

    async def _send_gif(
        self, bot_profile_id: int, match_id: str, gif_url: str
    ) -> Dict[str, Any]:
        """Send a GIF to a match via Giphy integration."""
        data = await self._api_request(
            bot_profile_id,
            "POST",
            f"/user/matches/{match_id}",
            json_data={
                "type": "gif",
                "gif": {"url": gif_url},
            },
        )
        return data

    async def _get_profile(self, bot_profile_id: int) -> Dict[str, Any]:
        """Get the authenticated user's profile."""
        data = await self._api_request(
            bot_profile_id, "GET", "/v2/profile?include=user"
        )
        return data.get("data", {}).get("user", {})

    async def _get_updates(self, bot_profile_id: int, last_activity: str = "") -> Dict[str, Any]:
        """Get updates since last activity date.

        This is the primary polling endpoint - returns new matches and messages.
        """
        data = await self._api_request(
            bot_profile_id,
            "POST",
            "/updates",
            json_data={"last_activity_date": last_activity},
        )
        return data

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self, instance) -> None:
        """Start the Tinder adapter main loop.

        1. Authenticate
        2. Load existing matches
        3. Poll for new messages
        4. Process messages with AI
        5. Send outbound queue
        """
        bot_id = instance.bot_profile_id
        self._running[bot_id] = True
        poll_interval = float(instance.config.get("tinder_poll_interval", DEFAULT_POLL_INTERVAL))
        self._poll_intervals[bot_id] = max(MIN_POLL_INTERVAL, poll_interval)
        consecutive_errors = 0

        try:
            # Step 1: Authenticate
            await instance.notify_status({"message": "Connecting to Tinder...", "status": "connecting"})
            token = await self._authenticate(instance)
            self._auth_tokens[bot_id] = token

            # Step 2: Fetch profile to confirm connection
            profile = await self._get_profile(bot_id)
            my_user_id = profile.get("_id", "")
            profile_name = profile.get("name", "Unknown")

            instance.whatsapp_connected = True  # reuse the connected flag
            await instance.notify_status({
                "message": f"Connected to Tinder as {profile_name}",
                "status": "connected",
                "connected": True,
                "profile_name": profile_name,
            })
            logger.info(f"Bot {bot_id}: Connected to Tinder as {profile_name} (user_id={my_user_id})")

            # Step 3: Initial match load
            await self._load_initial_matches(bot_id)

            # Set initial activity date to now to avoid processing old messages
            self._last_activity[bot_id] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            # Step 4: Main poll loop
            while self._running.get(bot_id, False):
                try:
                    # Poll for updates
                    updates = await self._get_updates(
                        bot_id, self._last_activity.get(bot_id, "")
                    )

                    # Process new matches
                    new_matches = updates.get("matches", [])
                    for match_data in new_matches:
                        await self._process_match_update(instance, match_data, my_user_id)

                    # Update last activity timestamp
                    last_activity = updates.get("last_activity_date")
                    if last_activity:
                        self._last_activity[bot_id] = last_activity

                    # Process outbound message queue
                    await self._process_outbound_queue(instance)

                    # Reset error counter on success
                    consecutive_errors = 0
                    self._poll_intervals[bot_id] = max(
                        MIN_POLL_INTERVAL,
                        float(instance.config.get("tinder_poll_interval", DEFAULT_POLL_INTERVAL)),
                    )

                except TinderRateLimitError as e:
                    consecutive_errors += 1
                    wait_time = e.retry_after
                    self._poll_intervals[bot_id] = min(
                        MAX_POLL_INTERVAL,
                        self._poll_intervals[bot_id] * RATE_LIMIT_BACKOFF_FACTOR,
                    )
                    logger.warning(
                        f"Bot {bot_id}: Rate limited, waiting {wait_time}s "
                        f"(poll interval now {self._poll_intervals[bot_id]}s)"
                    )
                    await instance.notify_status({
                        "message": f"Rate limited, pausing {int(wait_time)}s...",
                        "status": "rate_limited",
                    })
                    await asyncio.sleep(wait_time)
                    continue

                except TinderAPIError as e:
                    consecutive_errors += 1
                    if e.status_code == 401:
                        logger.warning(f"Bot {bot_id}: Auth expired, re-authenticating...")
                        try:
                            token = await self._authenticate(instance)
                            self._auth_tokens[bot_id] = token
                            continue
                        except TinderAPIError:
                            logger.error(f"Bot {bot_id}: Re-auth failed")
                            raise
                    logger.error(f"Bot {bot_id}: API error: {e}")

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"Bot {bot_id}: Poll loop error: {e}", exc_info=True)

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    error_msg = f"Too many consecutive errors ({consecutive_errors}), stopping"
                    logger.error(f"Bot {bot_id}: {error_msg}")
                    await instance.notify_status({
                        "message": error_msg,
                        "status": "error",
                        "error": error_msg,
                    })
                    break

                await asyncio.sleep(self._poll_intervals.get(bot_id, DEFAULT_POLL_INTERVAL))

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_id}: Tinder adapter cancelled")

        except TinderAPIError as e:
            logger.error(f"Bot {bot_id}: Fatal Tinder API error: {e}")
            instance.error = str(e)
            await instance.notify_status({
                "message": str(e),
                "status": "error",
                "error": str(e),
            })

        finally:
            self._running[bot_id] = False
            instance.is_running = False
            instance.whatsapp_connected = False
            await self._close_client(bot_id)
            self._matches_cache.pop(bot_id, None)
            self._last_activity.pop(bot_id, None)
            self._poll_intervals.pop(bot_id, None)
            logger.info(f"Bot {bot_id}: Tinder adapter stopped")

    # ------------------------------------------------------------------
    # Match & message processing
    # ------------------------------------------------------------------

    async def _load_initial_matches(self, bot_profile_id: int):
        """Load existing matches into cache (without processing messages)."""
        try:
            data = await self._get_matches(bot_profile_id)
            matches = data.get("matches", [])
            cache = {}
            for m in matches:
                match_id = m.get("_id", m.get("id", ""))
                if match_id:
                    cache[match_id] = {
                        "match_id": match_id,
                        "person": m.get("person", {}),
                        "last_message_sent": self._extract_last_msg_time(m),
                    }
            self._matches_cache[bot_profile_id] = cache
            logger.info(f"Bot {bot_profile_id}: Loaded {len(cache)} Tinder matches")
        except TinderAPIError as e:
            logger.warning(f"Bot {bot_profile_id}: Failed to load matches: {e}")
            self._matches_cache[bot_profile_id] = {}

    def _extract_last_msg_time(self, match_data: dict) -> str:
        """Extract the timestamp of the last message in a match."""
        messages = match_data.get("messages", [])
        if messages:
            return messages[-1].get("sent_date", "")
        return match_data.get("created_date", "")

    async def _process_match_update(self, instance, match_data: dict, my_user_id: str):
        """Process a match update from the /updates endpoint.

        Creates/updates conversations and processes new messages.
        """
        bot_id = instance.bot_profile_id
        match_id = match_data.get("_id", match_data.get("id", ""))
        if not match_id:
            return

        person = match_data.get("person", {})
        person_name = person.get("name", "Match")
        person_id = person.get("_id", "")
        photos = person.get("photos", [])
        profile_pic = photos[0].get("url", "") if photos else ""

        # Update cache
        if bot_id not in self._matches_cache:
            self._matches_cache[bot_id] = {}
        self._matches_cache[bot_id][match_id] = {
            "match_id": match_id,
            "person": person,
            "last_message_sent": self._extract_last_msg_time(match_data),
        }

        # Process new messages from this match
        messages = match_data.get("messages", [])
        for msg in messages:
            sender_id = msg.get("from", "")
            # Skip our own messages
            if sender_id == my_user_id:
                continue

            await self._handle_incoming_message(
                instance,
                match_id=match_id,
                message_id=msg.get("_id", ""),
                content=msg.get("message", ""),
                sender_name=person_name,
                sender_id=person_id,
                profile_pic=profile_pic,
                timestamp_str=msg.get("sent_date", ""),
                msg_type=msg.get("type", ""),
                gif_data=msg.get("gif"),
            )

    async def _handle_incoming_message(
        self,
        instance,
        *,
        match_id: str,
        message_id: str,
        content: str,
        sender_name: str,
        sender_id: str,
        profile_pic: str,
        timestamp_str: str,
        msg_type: str = "",
        gif_data: dict = None,
    ):
        """Process a single incoming message using shared message_handler utilities."""
        bot_id = instance.bot_profile_id

        # Handle GIF messages
        if msg_type == "gif" and gif_data:
            gif_url = gif_data.get("url", "")
            content = content or f"[GIF: {gif_url}]"

        if not content:
            return

        # Parse timestamp
        timestamp = None
        if timestamp_str:
            try:
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        with message_handler.get_db_session() as db:
            # Find or create conversation
            conversation = message_handler.find_or_create_conversation(
                db,
                bot_profile_id=bot_id,
                chat_id=match_id,
                chat_name=sender_name,
                is_group=False,
                phone="",
                display_name=sender_name,
                profile_pic=profile_pic,
            )

            # Dedup check
            is_dup, existing = message_handler.is_duplicate_message(
                db,
                conversation.id,
                platform_message_id=message_id,
                content=content,
            )
            if is_dup:
                db.commit()
                return

            # Save incoming message
            user_msg = message_handler.save_user_message(
                db,
                conversation.id,
                content,
                sender_name=sender_name,
                sender_id=sender_id,
                sender_profile_pic=profile_pic,
                platform_message_id=message_id,
                timestamp=timestamp,
            )
            message_handler.update_conversation_stats(db, conversation.id)
            db.commit()

            # WebSocket broadcast
            message_handler.broadcast_user_message(
                conversation.id, bot_id, user_msg, conversation
            )

            # Check human takeover
            if message_handler.is_human_takeover_active(db, conversation.id):
                logger.info(f"Bot {bot_id}: Human takeover active for {sender_name}, skipping AI")
                return

            # Check if AI responses are enabled
            if not instance.ai_response_enabled:
                return

            # Generate AI response
            try:
                from app.database import BotProfile
                from app.ai.factory import get_ai_provider
                from app.auth.utils import decrypt_string

                bot_profile = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
                if not bot_profile:
                    return

                api_key = decrypt_string(bot_profile.api_key_encrypted)
                ai_provider = get_ai_provider(
                    bot_profile.ai_provider, api_key, bot_profile.model
                )

                ai_messages = message_handler.build_ai_messages(
                    db,
                    conversation.id,
                    bot_profile.system_prompt,
                    max_history=bot_profile.max_history,
                    is_group=False,
                )

                # Show typing indicator via WS
                message_handler.broadcast_typing(conversation.id, True, sender="Bot")

                # Run blocking AI call in thread pool to avoid blocking event loop
                def _generate_ai_response():
                    return ai_provider.chat_completion(
                        messages=ai_messages,
                        max_tokens=bot_profile.max_tokens,
                        temperature=bot_profile.temperature,
                    )

                response = await asyncio.get_event_loop().run_in_executor(
                    None, _generate_ai_response
                )

                message_handler.broadcast_typing(conversation.id, False)

                if response and response.content:
                    reply_text = response.content.strip()

                    # Respect max message length
                    if len(reply_text) > 5000:
                        reply_text = reply_text[:4997] + "..."

                    # Random delay to seem more natural
                    import random
                    delay = random.uniform(
                        bot_profile.response_delay_min,
                        bot_profile.response_delay_max,
                    )
                    await asyncio.sleep(delay)

                    # Send the reply
                    sent = await self._send_reply(bot_id, match_id, reply_text)

                    if sent:
                        assistant_msg = message_handler.save_assistant_message(
                            db,
                            conversation.id,
                            reply_text,
                            sender_name=bot_profile.name,
                        )
                        message_handler.update_conversation_stats(db, conversation.id)
                        db.commit()

                        message_handler.broadcast_assistant_message(
                            conversation.id, assistant_msg
                        )

            except Exception as e:
                message_handler.broadcast_typing(conversation.id, False)
                logger.error(f"Bot {bot_id}: AI response error: {e}", exc_info=True)

    async def _send_reply(self, bot_profile_id: int, match_id: str, text: str) -> bool:
        """Send a reply with retry on rate limit."""
        for attempt in range(3):
            try:
                await self._send_message_api(bot_profile_id, match_id, text)
                return True
            except TinderRateLimitError as e:
                if attempt < 2:
                    logger.warning(
                        f"Bot {bot_profile_id}: Rate limited sending reply, "
                        f"retrying in {e.retry_after}s"
                    )
                    await asyncio.sleep(e.retry_after)
                else:
                    logger.error(f"Bot {bot_profile_id}: Failed to send reply after retries")
                    return False
            except TinderAPIError as e:
                logger.error(f"Bot {bot_profile_id}: Send reply failed: {e}")
                return False
        return False

    # ------------------------------------------------------------------
    # Outbound queue processing
    # ------------------------------------------------------------------

    async def _process_outbound_queue(self, instance):
        """Process queued outbound messages (scheduled, proactive)."""
        messages = instance.get_outbound_messages()
        for msg in messages:
            match_id = msg["chat_id"]
            text = msg["message"]
            try:
                sent = await self._send_reply(instance.bot_profile_id, match_id, text)
                if sent:
                    with message_handler.get_db_session() as db:
                        conversation = message_handler.find_or_create_conversation(
                            db,
                            bot_profile_id=instance.bot_profile_id,
                            chat_id=match_id,
                            chat_name=match_id,
                        )
                        from app.database import BotProfile
                        bot = db.query(BotProfile).filter(
                            BotProfile.id == instance.bot_profile_id
                        ).first()
                        assistant_msg = message_handler.save_assistant_message(
                            db,
                            conversation.id,
                            text,
                            sender_name=bot.name if bot else "Bot",
                        )
                        message_handler.update_conversation_stats(db, conversation.id)
                        db.commit()
                        message_handler.broadcast_assistant_message(
                            conversation.id, assistant_msg
                        )
            except Exception as e:
                logger.error(
                    f"Bot {instance.bot_profile_id}: Failed to send outbound to {match_id}: {e}"
                )

    # ------------------------------------------------------------------
    # PlatformAdapter interface methods
    # ------------------------------------------------------------------

    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message to a Tinder match."""
        if bot_profile_id not in self._auth_tokens:
            logger.error(f"Bot {bot_profile_id}: No auth token for sending message")
            return False

        return await self._send_reply(bot_profile_id, chat_id, message)

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send a file to a Tinder match.

        Tinder only supports GIF sending. For GIFs, file_path should be a URL.
        Other file types are not supported.
        """
        if file_type == "image/gif" or file_path.endswith(".gif"):
            if bot_profile_id not in self._auth_tokens:
                return False
            try:
                await self._send_gif(bot_profile_id, chat_id, file_path)
                return True
            except TinderAPIError as e:
                logger.error(f"Bot {bot_profile_id}: Failed to send GIF: {e}")
                return False

        logger.warning(f"Bot {bot_profile_id}: Tinder only supports GIF file sending")
        return False

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a bot."""
        self._running.pop(bot_profile_id, None)
        self._auth_tokens.pop(bot_profile_id, None)
        self._matches_cache.pop(bot_profile_id, None)
        self._last_activity.pop(bot_profile_id, None)
        self._poll_intervals.pop(bot_profile_id, None)
        # Client cleanup is async, handled in run() finally block
        self._clients.pop(bot_profile_id, None)

    def get_contacts(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Get the match list as contacts."""
        cache = self._matches_cache.get(bot_profile_id, {})
        contacts = []
        for match_id, match_info in cache.items():
            person = match_info.get("person", {})
            photos = person.get("photos", [])
            contacts.append({
                "chat_id": match_id,
                "name": person.get("name", "Match"),
                "phone": "",
                "profile_pic": photos[0].get("url", "") if photos else "",
                "bio": person.get("bio", ""),
            })
        return contacts
