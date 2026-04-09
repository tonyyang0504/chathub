"""
LinkedIn Platform Adapter
Uses LinkedIn Messaging API with OAuth 2.0 three-legged flow.

LinkedIn API overview:
- Auth: OAuth 2.0 three-legged (authorization code -> access token, 60-day expiry)
- Messages: REST API (POST /rest/messages, GET /rest/conversations)
- Identifiers: URN-based (urn:li:person:xxx)
- Rate limits: Strict — 100 requests/day for most endpoints
- Partnership: Full messaging access requires LinkedIn Partnership approval

Graceful degradation:
- If API access is limited, notifies user via status callbacks
- Token refresh handled automatically before expiry
- Rate limit 429 responses trigger exponential backoff
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlencode

import httpx

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)

# LinkedIn API endpoints
LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_API_BASE = "https://api.linkedin.com/rest"

# LinkedIn API version header
LINKEDIN_API_VERSION = "202401"

# Polling interval for new messages (seconds)
POLL_INTERVAL = 15
# Max backoff on rate limit (seconds)
MAX_BACKOFF = 300
# Token refresh buffer (refresh 24 hours before expiry)
TOKEN_REFRESH_BUFFER = timedelta(hours=24)


class LinkedInAdapter(PlatformAdapter):
    """LinkedIn messaging adapter using OAuth 2.0 + REST API with polling.

    Auth flow:
    1. Bot config provides client_id, client_secret, redirect_uri
    2. run() generates an OAuth authorization URL and sends it via status callback
    3. User authorizes in browser, callback receives authorization code
    4. Adapter exchanges code for access token (60-day expiry)
    5. Adapter polls for new messages and processes them

    Bot config keys:
        - linkedin_client_id: OAuth app client ID
        - linkedin_client_secret: OAuth app client secret
        - linkedin_redirect_uri: OAuth callback URL
        - linkedin_access_token: (optional) Stored access token
        - linkedin_refresh_token: (optional) Stored refresh token
        - linkedin_token_expires_at: (optional) Token expiry ISO timestamp
        - linkedin_person_urn: (optional) Authenticated user's URN
        - linkedin_auth_code: (optional) Authorization code from OAuth callback
    """

    def __init__(self):
        self._http_clients: Dict[int, httpx.AsyncClient] = {}
        self._person_urns: Dict[int, str] = {}
        self._tokens: Dict[int, Dict[str, Any]] = {}
        self._last_sync_times: Dict[int, float] = {}
        self._seen_message_ids: Dict[int, set] = {}
        self._contacts_cache: Dict[int, List[Dict[str, Any]]] = {}
        self._oauth_states: Dict[int, str] = {}  # CSRF state per bot

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.LINKEDIN

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=False,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=True,
            supports_read_receipts=False,
            supports_typing_indicator=True,
            supports_history_sync=False,
            supports_contacts_list=True,
            supports_groups_list=False,
            supports_profile_pic=True,
            auth_method=AuthMethod.OAUTH,
            max_message_length=8000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif",
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ],
        )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self, instance) -> None:
        """Start LinkedIn bot: authenticate and enter message polling loop."""
        bot_id = instance.bot_profile_id

        # Merge platform_config from DB into instance.config so LinkedIn-specific
        # keys (client_id, client_secret, tokens, etc.) are accessible.
        self._load_platform_config(instance)
        config = instance.config

        await instance.notify_status({
            "message": "Initializing LinkedIn connection...",
            "status": "connecting",
        })

        # Create HTTP client for this bot
        self._http_clients[bot_id] = httpx.AsyncClient(timeout=30.0)
        self._seen_message_ids[bot_id] = set()

        try:
            # Attempt authentication
            authenticated = await self._authenticate(instance)
            if not authenticated:
                return

            # Mark as connected
            instance.whatsapp_connected = True  # Re-used field means "platform connected"
            await instance.notify_status({
                "message": "Connected to LinkedIn",
                "status": "connected",
                "platform": "linkedin",
            })

            # Update DB
            self._update_bot_running_status(bot_id, is_running=True)

            # Enter message polling loop
            await self._message_loop(instance)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_id}: LinkedIn adapter cancelled")
            raise
        except Exception as e:
            logger.error(f"Bot {bot_id}: LinkedIn adapter error: {e}", exc_info=True)
            instance.error = str(e)
            await instance.notify_status({
                "message": f"LinkedIn error: {e}",
                "status": "error",
            })
        finally:
            await self._cleanup_http_client(bot_id)
            instance.whatsapp_connected = False
            self._update_bot_running_status(bot_id, is_running=False)

    async def _message_loop(self, instance) -> None:
        """Poll for new messages and process them."""
        bot_id = instance.bot_profile_id
        backoff = POLL_INTERVAL

        while instance.is_running:
            try:
                # Check token expiry and refresh if needed
                await self._ensure_token_valid(bot_id, instance)

                # Process outbound messages first
                await self._process_outbound_queue(instance)

                # Poll for new messages
                await self._poll_messages(instance)

                # Reset backoff on success
                backoff = POLL_INTERVAL
                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                raise
            except RateLimitError:
                backoff = min(backoff * 2, MAX_BACKOFF)
                logger.warning(f"Bot {bot_id}: Rate limited, backing off {backoff}s")
                await instance.notify_status({
                    "message": f"Rate limited — waiting {backoff}s",
                    "status": "rate_limited",
                })
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.error(f"Bot {bot_id}: Poll error: {e}", exc_info=True)
                backoff = min(backoff * 2, MAX_BACKOFF)
                await asyncio.sleep(backoff)

    # ------------------------------------------------------------------
    # OAuth 2.0 Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self, instance) -> bool:
        """Handle OAuth 2.0 authentication flow.

        Returns True if authenticated, False if user action required.
        """
        bot_id = instance.bot_profile_id
        config = instance.config

        client_id = config.get("linkedin_client_id", "")
        client_secret = config.get("linkedin_client_secret", "")
        redirect_uri = config.get("linkedin_redirect_uri", "")

        if not client_id or not client_secret:
            await instance.notify_status({
                "message": "LinkedIn OAuth credentials missing. Set linkedin_client_id and linkedin_client_secret in bot config.",
                "status": "error",
            })
            instance.error = "Missing LinkedIn OAuth credentials"
            return False

        # Check for existing valid token (tokens are stored encrypted)
        stored_token_encrypted = config.get("linkedin_access_token", "")
        stored_expires = config.get("linkedin_token_expires_at", "")

        if stored_token_encrypted and stored_expires:
            try:
                from app.auth.utils import decrypt_string
                expires_at = datetime.fromisoformat(stored_expires)
                if datetime.now(timezone.utc) < expires_at - TOKEN_REFRESH_BUFFER:
                    # Token still valid — decrypt before use
                    stored_token = decrypt_string(stored_token_encrypted)
                    stored_refresh = config.get("linkedin_refresh_token", "")
                    if stored_refresh:
                        stored_refresh = decrypt_string(stored_refresh)
                    self._tokens[bot_id] = {
                        "access_token": stored_token,
                        "expires_at": expires_at,
                        "refresh_token": stored_refresh,
                    }
                    # Verify token and get person URN
                    person_urn = await self._get_person_urn(bot_id)
                    if person_urn:
                        self._person_urns[bot_id] = person_urn
                        logger.info(f"Bot {bot_id}: Authenticated with stored token as {person_urn}")
                        return True
                    # Token invalid, fall through to re-auth
            except (ValueError, TypeError):
                pass

        # Check for authorization code (from OAuth callback)
        auth_code = config.get("linkedin_auth_code", "")
        if auth_code:
            success = await self._exchange_auth_code(bot_id, auth_code, client_id, client_secret, redirect_uri)
            if success:
                person_urn = await self._get_person_urn(bot_id)
                if person_urn:
                    self._person_urns[bot_id] = person_urn
                    await self._store_token(bot_id)
                    logger.info(f"Bot {bot_id}: Authenticated via auth code as {person_urn}")
                    return True

        # Generate OAuth authorization URL for user with CSRF-safe state
        state = secrets.token_urlsafe(32)
        self._oauth_states[bot_id] = state
        scopes = "openid profile email w_member_social r_liteprofile r_emailaddress"

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scopes,
        }
        auth_url = f"{LINKEDIN_AUTH_URL}?{urlencode(params)}"

        await instance.notify_status({
            "message": "Please authorize LinkedIn access",
            "status": "awaiting_auth",
            "auth_url": auth_url,
            "auth_type": "oauth",
            "platform": "linkedin",
        })

        # Wait for auth code to be provided (via config update)
        logger.info(f"Bot {bot_id}: Waiting for LinkedIn OAuth authorization...")
        wait_timeout = 300  # 5 minutes
        wait_start = time.time()

        while instance.is_running and (time.time() - wait_start) < wait_timeout:
            await asyncio.sleep(2)
            # Check if auth code was provided via config update
            auth_code = instance.config.get("linkedin_auth_code", "")
            if auth_code:
                # Validate CSRF state parameter to prevent CSRF attacks
                returned_state = instance.config.get("linkedin_oauth_state", "")
                expected_state = self._oauth_states.get(bot_id, "")
                if not expected_state or not returned_state or returned_state != expected_state:
                    logger.warning(f"Bot {bot_id}: OAuth state mismatch — possible CSRF attack")
                    await instance.notify_status({
                        "message": "OAuth state validation failed. Please try again.",
                        "status": "error",
                    })
                    self._oauth_states.pop(bot_id, None)
                    return False
                self._oauth_states.pop(bot_id, None)

                success = await self._exchange_auth_code(
                    bot_id, auth_code, client_id, client_secret, redirect_uri
                )
                if success:
                    person_urn = await self._get_person_urn(bot_id)
                    if person_urn:
                        self._person_urns[bot_id] = person_urn
                        await self._store_token(bot_id)
                        return True
                await instance.notify_status({
                    "message": "OAuth authorization failed. Please try again.",
                    "status": "error",
                })
                return False

        if not instance.is_running:
            return False

        await instance.notify_status({
            "message": "OAuth authorization timed out (5 min). Restart to try again.",
            "status": "error",
        })
        return False

    async def _exchange_auth_code(
        self,
        bot_id: int,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> bool:
        """Exchange authorization code for access token."""
        client = self._http_clients.get(bot_id)
        if not client:
            return False

        try:
            resp = await client.post(
                LINKEDIN_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code != 200:
                logger.error(f"Bot {bot_id}: Token exchange failed: {resp.status_code} {resp.text}")
                return False

            data = resp.json()
            expires_in = data.get("expires_in", 5184000)  # Default 60 days
            self._tokens[bot_id] = {
                "access_token": data["access_token"],
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                "refresh_token": data.get("refresh_token", ""),
            }
            logger.info(f"Bot {bot_id}: OAuth token obtained, expires in {expires_in}s")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_id}: Token exchange error: {e}")
            return False

    async def _ensure_token_valid(self, bot_id: int, instance) -> None:
        """Check and refresh token if nearing expiry."""
        token_data = self._tokens.get(bot_id)
        if not token_data:
            return

        expires_at = token_data.get("expires_at")
        if not expires_at:
            return

        if datetime.now(timezone.utc) >= expires_at - TOKEN_REFRESH_BUFFER:
            refresh_token = token_data.get("refresh_token", "")
            if refresh_token:
                await self._refresh_access_token(bot_id, instance)
            else:
                logger.warning(f"Bot {bot_id}: Token expiring soon, no refresh token available")
                await instance.notify_status({
                    "message": "LinkedIn token expiring soon. Re-authorization may be required.",
                    "status": "warning",
                })

    async def _refresh_access_token(self, bot_id: int, instance) -> bool:
        """Refresh the access token using refresh token."""
        client = self._http_clients.get(bot_id)
        token_data = self._tokens.get(bot_id)
        config = instance.config

        if not client or not token_data:
            return False

        try:
            resp = await client.post(
                LINKEDIN_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token_data["refresh_token"],
                    "client_id": config.get("linkedin_client_id", ""),
                    "client_secret": config.get("linkedin_client_secret", ""),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code != 200:
                logger.error(f"Bot {bot_id}: Token refresh failed: {resp.status_code}")
                return False

            data = resp.json()
            expires_in = data.get("expires_in", 5184000)
            self._tokens[bot_id] = {
                "access_token": data["access_token"],
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                "refresh_token": data.get("refresh_token", token_data.get("refresh_token", "")),
            }
            await self._store_token(bot_id)
            logger.info(f"Bot {bot_id}: Token refreshed, expires in {expires_in}s")
            return True

        except Exception as e:
            logger.error(f"Bot {bot_id}: Token refresh error: {e}")
            return False

    async def _store_token(self, bot_id: int) -> None:
        """Persist token data to bot profile in DB (encrypted)."""
        token_data = self._tokens.get(bot_id)
        if not token_data:
            return

        try:
            from app.platforms.message_handler import get_db_session
            from app.database import BotProfile
            from app.auth.utils import encrypt_string

            with get_db_session() as db:
                bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
                if bot:
                    # Store in platform_config JSON field with encrypted tokens
                    platform_config = json.loads(bot.platform_config or "{}")
                    platform_config["linkedin_access_token"] = encrypt_string(token_data["access_token"])
                    platform_config["linkedin_token_expires_at"] = token_data["expires_at"].isoformat()
                    if token_data.get("refresh_token"):
                        platform_config["linkedin_refresh_token"] = encrypt_string(token_data["refresh_token"])
                    if self._person_urns.get(bot_id):
                        platform_config["linkedin_person_urn"] = self._person_urns[bot_id]
                    bot.platform_config = json.dumps(platform_config)
                    db.commit()
        except Exception as e:
            logger.error(f"Bot {bot_id}: Failed to store token: {e}")

    # ------------------------------------------------------------------
    # LinkedIn API helpers
    # ------------------------------------------------------------------

    def _get_headers(self, bot_id: int) -> Dict[str, str]:
        """Build API request headers."""
        token_data = self._tokens.get(bot_id, {})
        return {
            "Authorization": f"Bearer {token_data.get('access_token', '')}",
            "LinkedIn-Version": LINKEDIN_API_VERSION,
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

    async def _api_get(self, bot_id: int, path: str, params: dict = None) -> Optional[dict]:
        """Make a GET request to LinkedIn API."""
        client = self._http_clients.get(bot_id)
        if not client:
            return None

        resp = await client.get(
            f"{LINKEDIN_API_BASE}{path}",
            headers=self._get_headers(bot_id),
            params=params,
        )
        self._check_rate_limit(resp)

        if resp.status_code == 200:
            return resp.json()

        logger.error(f"Bot {bot_id}: API GET {path} failed: {resp.status_code} {resp.text}")
        return None

    async def _api_post(self, bot_id: int, path: str, body: dict) -> Optional[dict]:
        """Make a POST request to LinkedIn API."""
        client = self._http_clients.get(bot_id)
        if not client:
            return None

        resp = await client.post(
            f"{LINKEDIN_API_BASE}{path}",
            headers=self._get_headers(bot_id),
            json=body,
        )
        self._check_rate_limit(resp)

        if resp.status_code in (200, 201):
            try:
                return resp.json()
            except Exception:
                return {"status": "ok"}

        logger.error(f"Bot {bot_id}: API POST {path} failed: {resp.status_code} {resp.text}")
        return None

    def _check_rate_limit(self, resp: httpx.Response) -> None:
        """Raise RateLimitError if 429 response."""
        if resp.status_code == 429:
            raise RateLimitError(f"Rate limited: {resp.headers.get('Retry-After', 'unknown')}")

    async def _get_person_urn(self, bot_id: int) -> Optional[str]:
        """Get the authenticated user's person URN."""
        # Try cached first
        if bot_id in self._person_urns:
            return self._person_urns[bot_id]

        data = await self._api_get(bot_id, "/userinfo")
        if data and "sub" in data:
            return f"urn:li:person:{data['sub']}"

        # Fallback to /me endpoint
        client = self._http_clients.get(bot_id)
        if not client:
            return None

        try:
            resp = await client.get(
                "https://api.linkedin.com/v2/me",
                headers=self._get_headers(bot_id),
            )
            if resp.status_code == 200:
                data = resp.json()
                return f"urn:li:person:{data['id']}"
        except Exception as e:
            logger.error(f"Bot {bot_id}: Failed to get person URN: {e}")

        return None

    # ------------------------------------------------------------------
    # Message polling and processing
    # ------------------------------------------------------------------

    async def _poll_messages(self, instance) -> None:
        """Poll LinkedIn for new messages."""
        bot_id = instance.bot_profile_id
        person_urn = self._person_urns.get(bot_id)
        if not person_urn:
            return

        # Fetch recent conversations
        data = await self._api_get(
            bot_id,
            "/conversations",
            params={
                "q": "participant",
                "participant": person_urn,
                "count": 20,
            },
        )

        if not data or "elements" not in data:
            return

        for conversation in data.get("elements", []):
            await self._process_conversation(instance, conversation)

    async def _process_conversation(self, instance, conversation: dict) -> None:
        """Process a LinkedIn conversation for new messages."""
        bot_id = instance.bot_profile_id
        conv_id = conversation.get("id", "")
        if not conv_id:
            return

        # Fetch messages in this conversation
        data = await self._api_get(
            bot_id,
            f"/conversations/{conv_id}/events",
            params={"count": 10},
        )

        if not data or "elements" not in data:
            return

        person_urn = self._person_urns.get(bot_id, "")

        for event in data.get("elements", []):
            await self._process_message_event(instance, event, conv_id, person_urn)

    async def _process_message_event(
        self, instance, event: dict, conv_id: str, person_urn: str
    ) -> None:
        """Process a single message event."""
        bot_id = instance.bot_profile_id
        event_id = event.get("id", "")

        # Skip already-seen messages
        seen = self._seen_message_ids.get(bot_id, set())
        if event_id in seen:
            return

        # Only process message events
        event_type = event.get("eventContent", {}).get("com.linkedin.voyager.messaging.event.MessageEvent")
        if not event_type:
            # Try alternate structure
            event_type = event.get("eventContent", {}).get("messageEvent")
            if not event_type:
                seen.add(event_id)
                return

        # Skip own messages
        sender_urn = event.get("from", {}).get("member", "") or event.get("from", "")
        if isinstance(sender_urn, dict):
            sender_urn = sender_urn.get("member", "")
        if sender_urn == person_urn:
            seen.add(event_id)
            return

        # Extract message content
        body = event_type.get("body", "") or event_type.get("attributedBody", {}).get("text", "")
        if not body:
            seen.add(event_id)
            return

        seen.add(event_id)

        # Extract sender info
        sender_name = await self._resolve_person_name(bot_id, sender_urn)
        timestamp_ms = event.get("createdAt", 0)
        msg_timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) if timestamp_ms else None

        # Use shared message_handler for DB/AI/WebSocket
        await self._handle_incoming_message(
            instance=instance,
            conv_id=conv_id,
            sender_urn=sender_urn,
            sender_name=sender_name,
            message_text=body,
            platform_message_id=event_id,
            timestamp=msg_timestamp,
            attachments=event_type.get("attachments", []),
        )

    async def _handle_incoming_message(
        self,
        instance,
        conv_id: str,
        sender_urn: str,
        sender_name: str,
        message_text: str,
        platform_message_id: str = "",
        timestamp: datetime = None,
        attachments: list = None,
    ) -> None:
        """Process an incoming message using shared message_handler utilities."""
        from app.platforms.message_handler import (
            get_db_session,
            find_or_create_conversation,
            is_duplicate_message,
            save_user_message,
            save_assistant_message,
            update_conversation_stats,
            build_ai_messages,
            broadcast_user_message,
            broadcast_assistant_message,
            broadcast_typing,
            is_human_takeover_active,
            is_sender_approved,
            analyze_media_with_ai,
        )
        from app.ai.factory import get_ai_provider
        from app.auth.utils import decrypt_string

        bot_id = instance.bot_profile_id
        config = instance.config

        with get_db_session() as db:
            # Find or create conversation
            conversation = find_or_create_conversation(
                db,
                bot_id,
                chat_id=conv_id,
                chat_name=sender_name or conv_id,
                is_group=False,
                phone="",
                display_name=sender_name,
            )

            # Dedup check
            is_dup, existing = is_duplicate_message(
                db,
                conversation.id,
                platform_message_id=platform_message_id,
                content=message_text,
            )
            if is_dup:
                db.commit()
                return

            # Handle media attachments
            file_url = None
            file_type = None
            file_name = None
            file_size = None
            media_analysis = None

            if attachments:
                attachment = attachments[0]  # Process first attachment
                media_info = await self._process_attachment(bot_id, sender_name, attachment)
                if media_info:
                    file_url = media_info.get("file_url")
                    file_type = media_info.get("file_type")
                    file_name = media_info.get("file_name")
                    file_size = media_info.get("file_size")

                    # Analyze media with AI if provider is configured
                    if media_info.get("local_file_path") and config.get("ai_provider"):
                        try:
                            api_key = config.get("api_key", "")
                            if config.get("api_key_encrypted"):
                                api_key = decrypt_string(config["api_key_encrypted"])
                            provider = get_ai_provider(
                                config["ai_provider"], api_key, config.get("model", "")
                            )
                            media_analysis = analyze_media_with_ai(
                                provider, media_info["local_file_path"], file_type, message_text
                            )
                        except Exception as e:
                            logger.error(f"Bot {bot_id}: Media analysis error: {e}")

            # Save user message
            user_msg = save_user_message(
                db,
                conversation.id,
                message_text,
                sender_name=sender_name,
                sender_id=sender_urn,
                platform_message_id=platform_message_id,
                timestamp=timestamp,
                file_url=file_url,
                file_type=file_type,
                file_name=file_name,
                file_size=file_size,
                media_analysis=media_analysis,
            )
            update_conversation_stats(db, conversation.id)
            db.commit()

            # Broadcast via WebSocket
            broadcast_user_message(conversation.id, bot_id, user_msg, conversation)

            # Check AI toggle and human takeover
            if not instance.ai_response_enabled:
                return
            if is_human_takeover_active(db, conversation.id):
                return
            if not is_sender_approved(db, conversation.id):
                return

            # Generate AI response
            try:
                api_key = config.get("api_key", "")
                if config.get("api_key_encrypted"):
                    api_key = decrypt_string(config["api_key_encrypted"])

                provider = get_ai_provider(
                    config["ai_provider"], api_key, config.get("model", "")
                )

                system_prompt = config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db, conversation.id, system_prompt,
                    current_message=user_msg,
                )

                broadcast_typing(conversation.id, True, config.get("name", "Bot"))

                response = await asyncio.to_thread(
                    provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=config.get("max_tokens", 1000),
                    temperature=config.get("temperature", 0.7),
                )

                broadcast_typing(conversation.id, False)

                if response and response.content:
                    reply_text = response.content.strip()

                    # Save assistant message
                    assistant_msg = save_assistant_message(
                        db, conversation.id, reply_text,
                        sender_name=config.get("name", "AI Agent"),
                    )
                    update_conversation_stats(db, conversation.id)
                    db.commit()

                    broadcast_assistant_message(conversation.id, assistant_msg)

                    # Send reply via LinkedIn API
                    await self._send_linkedin_message(bot_id, conv_id, reply_text)

            except Exception as e:
                logger.error(f"Bot {bot_id}: AI response error: {e}", exc_info=True)
                broadcast_typing(conversation.id, False)

    async def _process_attachment(
        self, bot_id: int, chat_name: str, attachment: dict
    ) -> Optional[Dict[str, Any]]:
        """Download and save a LinkedIn message attachment."""
        from app.platforms.message_handler import save_media_file

        try:
            # LinkedIn attachment structure
            media_type = attachment.get("mediaType", "application/octet-stream")
            reference = attachment.get("reference", "")
            name = attachment.get("name", "")

            if not reference:
                return None

            # Download the attachment
            client = self._http_clients.get(bot_id)
            if not client:
                return None

            resp = await client.get(reference, headers=self._get_headers(bot_id))
            if resp.status_code != 200:
                logger.error(f"Bot {bot_id}: Failed to download attachment: {resp.status_code}")
                return None

            b64_data = base64.b64encode(resp.content).decode("utf-8")
            return save_media_file(
                b64_data, media_type, bot_id, chat_name,
                direction="received", original_filename=name,
            )
        except Exception as e:
            logger.error(f"Bot {bot_id}: Attachment processing error: {e}")
            return None

    async def _resolve_person_name(self, bot_id: int, person_urn: str) -> str:
        """Resolve a person URN to a display name."""
        if not person_urn:
            return "Unknown"

        # Extract person ID from URN
        person_id = person_urn.split(":")[-1] if ":" in person_urn else person_urn

        try:
            client = self._http_clients.get(bot_id)
            if not client:
                return person_id

            resp = await client.get(
                f"https://api.linkedin.com/v2/people/(id:{person_id})",
                headers=self._get_headers(bot_id),
                params={"projection": "(firstName,lastName)"},
            )
            if resp.status_code == 200:
                data = resp.json()
                first = data.get("firstName", {}).get("localized", {})
                last = data.get("lastName", {}).get("localized", {})
                # Get first available locale
                first_name = next(iter(first.values()), "") if first else ""
                last_name = next(iter(last.values()), "") if last else ""
                if first_name or last_name:
                    return f"{first_name} {last_name}".strip()
        except Exception as e:
            logger.debug(f"Bot {bot_id}: Could not resolve name for {person_urn}: {e}")

        return person_id

    # ------------------------------------------------------------------
    # Outbound messages
    # ------------------------------------------------------------------

    async def _process_outbound_queue(self, instance) -> None:
        """Send queued outbound messages."""
        messages = instance.get_outbound_messages()
        bot_id = instance.bot_profile_id

        for msg in messages:
            chat_id = msg.get("chat_id", "")
            message = msg.get("message", "")
            if chat_id and message:
                try:
                    await self._send_linkedin_message(bot_id, chat_id, message)
                except Exception as e:
                    logger.error(f"Bot {bot_id}: Failed to send queued message: {e}")

    async def _send_linkedin_message(
        self, bot_id: int, conversation_id: str, text: str
    ) -> bool:
        """Send a message via LinkedIn Messaging API."""
        person_urn = self._person_urns.get(bot_id)
        if not person_urn:
            logger.error(f"Bot {bot_id}: No person URN, cannot send message")
            return False

        body = {
            "recipients": [conversation_id],
            "body": text,
            "messageType": "MEMBER_TO_MEMBER",
        }

        result = await self._api_post(bot_id, "/messages", body)
        if result is not None:
            logger.info(f"Bot {bot_id}: Sent message to conversation {conversation_id}")
            return True
        return False

    async def _send_linkedin_message_to_person(
        self, bot_id: int, recipient_urn: str, text: str
    ) -> bool:
        """Send a message to a person by their URN (creates new conversation if needed)."""
        person_urn = self._person_urns.get(bot_id)
        if not person_urn:
            return False

        body = {
            "recipients": [recipient_urn],
            "body": text,
            "messageType": "MEMBER_TO_MEMBER",
        }

        result = await self._api_post(bot_id, "/messages", body)
        return result is not None

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
        """Send a text message to a LinkedIn conversation."""
        # Ensure we have a valid client and token (may be called externally)
        cleanup_after = await self._ensure_send_ready(bot_profile_id)
        try:
            # Split long messages if needed
            max_len = self.capabilities.max_message_length
            if len(message) > max_len:
                chunks = [message[i:i + max_len] for i in range(0, len(message), max_len)]
                for chunk in chunks:
                    success = await self._send_linkedin_message(bot_profile_id, chat_id, chunk)
                    if not success:
                        return False
                    await asyncio.sleep(1)  # Brief delay between chunks
                return True

            return await self._send_linkedin_message(bot_profile_id, chat_id, message)
        finally:
            if cleanup_after:
                await self._cleanup_http_client(bot_profile_id)

    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send a file via LinkedIn.

        LinkedIn requires uploading media first, then referencing in a message.
        """
        # Ensure we have a valid client and token (may be called externally)
        cleanup_after = await self._ensure_send_ready(bot_profile_id)

        person_urn = self._person_urns.get(bot_profile_id)
        if not person_urn:
            if cleanup_after:
                await self._cleanup_http_client(bot_profile_id)
            return False

        client = self._http_clients.get(bot_profile_id)
        if not client:
            return False

        try:
            path = Path(file_path)
            if not path.exists():
                logger.error(f"Bot {bot_profile_id}: File not found: {file_path}")
                return False

            # Step 1: Register upload
            register_body = {
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:messaging-image" if file_type.startswith("image/") else "urn:li:digitalmediaRecipe:messaging-attachment"],
                    "owner": person_urn,
                }
            }

            resp = await client.post(
                "https://api.linkedin.com/v2/assets?action=registerUpload",
                headers=self._get_headers(bot_profile_id),
                json=register_body,
            )
            self._check_rate_limit(resp)

            if resp.status_code not in (200, 201):
                logger.error(f"Bot {bot_profile_id}: Upload register failed: {resp.status_code}")
                if caption:
                    return await self._send_linkedin_message(bot_profile_id, chat_id, f"{caption}\n[File: {path.name}]")
                return False

            upload_data = resp.json()
            upload_url = upload_data.get("value", {}).get("uploadMechanism", {}).get(
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}
            ).get("uploadUrl", "")
            asset_urn = upload_data.get("value", {}).get("asset", "")

            if not upload_url or not asset_urn:
                if caption:
                    return await self._send_linkedin_message(bot_profile_id, chat_id, f"{caption}\n[File: {path.name}]")
                return False

            # Step 2: Upload file
            with open(path, "rb") as f:
                file_data = f.read()

            upload_resp = await client.put(
                upload_url,
                content=file_data,
                headers={
                    "Authorization": f"Bearer {self._tokens.get(bot_profile_id, {}).get('access_token', '')}",
                    "Content-Type": file_type or "application/octet-stream",
                },
            )

            if upload_resp.status_code not in (200, 201):
                logger.error(f"Bot {bot_profile_id}: File upload failed: {upload_resp.status_code}")
                if caption:
                    return await self._send_linkedin_message(bot_profile_id, chat_id, f"{caption}\n[File: {path.name}]")
                return False

            # Step 3: Send message with attachment
            msg_body = {
                "recipients": [chat_id],
                "body": caption or path.name,
                "messageType": "MEMBER_TO_MEMBER",
                "attachments": [{
                    "id": asset_urn,
                    "name": path.name,
                    "mediaType": file_type or "application/octet-stream",
                }],
            }

            result = await self._api_post(bot_profile_id, "/messages", msg_body)
            return result is not None

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: send_file error: {e}", exc_info=True)
            if caption:
                return await self._send_linkedin_message(bot_profile_id, chat_id, caption)
            return False
        finally:
            if cleanup_after:
                await self._cleanup_http_client(bot_profile_id)

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a bot."""
        self._seen_message_ids.pop(bot_profile_id, None)
        self._person_urns.pop(bot_profile_id, None)
        self._tokens.pop(bot_profile_id, None)
        self._contacts_cache.pop(bot_profile_id, None)
        self._last_sync_times.pop(bot_profile_id, None)
        self._oauth_states.pop(bot_profile_id, None)
        # HTTP client cleanup happens in _cleanup_http_client (async)

    def get_contacts(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Return cached contacts list."""
        return self._contacts_cache.get(bot_profile_id, [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_send_ready(self, bot_profile_id: int) -> bool:
        """Ensure HTTP client and token exist for sending.

        Called by send_message/send_file which may be invoked externally
        (hub scheduler, conversation routes) when the bot isn't actively polling.

        Returns:
            True if a temporary client was created (caller should clean up),
            False if an existing client was reused.
        """
        if bot_profile_id in self._http_clients and bot_profile_id in self._tokens:
            return False  # Already have client + token from run()

        # Load token from DB
        try:
            from app.platforms.message_handler import get_db_session
            from app.database import BotProfile

            with get_db_session() as db:
                bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                if not bot:
                    return False
                platform_config = json.loads(bot.platform_config or "{}")

                from app.auth.utils import decrypt_string

                token_encrypted = platform_config.get("linkedin_access_token", "")
                expires_at_str = platform_config.get("linkedin_token_expires_at", "")
                person_urn = platform_config.get("linkedin_person_urn", "")

                if not token_encrypted:
                    logger.error(f"Bot {bot_profile_id}: No stored LinkedIn token for send")
                    return False

                # Decrypt tokens before use
                token = decrypt_string(token_encrypted)
                refresh_token_encrypted = platform_config.get("linkedin_refresh_token", "")
                refresh_token = decrypt_string(refresh_token_encrypted) if refresh_token_encrypted else ""

                if bot_profile_id not in self._http_clients:
                    self._http_clients[bot_profile_id] = httpx.AsyncClient(timeout=30.0)

                self._tokens[bot_profile_id] = {
                    "access_token": token,
                    "expires_at": datetime.fromisoformat(expires_at_str) if expires_at_str else None,
                    "refresh_token": refresh_token,
                }
                if person_urn:
                    self._person_urns[bot_profile_id] = person_urn

                return True  # Caller should clean up

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to prepare for send: {e}")
            return False

    def _load_platform_config(self, instance) -> None:
        """Load LinkedIn-specific config from DB platform_config into instance.config.

        The start_bot route only populates generic bot config (ai_provider, model, etc.).
        LinkedIn-specific keys (client_id, client_secret, tokens) are stored in
        BotProfile.platform_config JSON column. This merges them in.
        Also loads the bot name for display purposes.
        """
        try:
            from app.platforms.message_handler import get_db_session
            from app.database import BotProfile

            with get_db_session() as db:
                bot = db.query(BotProfile).filter(
                    BotProfile.id == instance.bot_profile_id
                ).first()
                if bot:
                    # Merge platform_config JSON into instance.config
                    platform_config = json.loads(bot.platform_config or "{}")
                    for key, value in platform_config.items():
                        if key not in instance.config or not instance.config[key]:
                            instance.config[key] = value

                    # Also load bot name if not already in config
                    if not instance.config.get("name"):
                        instance.config["name"] = bot.name
        except Exception as e:
            logger.error(
                f"Bot {instance.bot_profile_id}: Failed to load platform config: {e}"
            )

    async def _cleanup_http_client(self, bot_id: int) -> None:
        """Close and remove HTTP client."""
        client = self._http_clients.pop(bot_id, None)
        if client:
            try:
                await client.aclose()
            except Exception:
                pass

    def _update_bot_running_status(self, bot_id: int, is_running: bool) -> None:
        """Update bot running status in database."""
        try:
            from app.platforms.message_handler import get_db_session
            from app.database import BotProfile

            with get_db_session() as db:
                bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
                if bot:
                    bot.is_running = is_running
                    db.commit()
        except Exception as e:
            logger.error(f"Bot {bot_id}: Failed to update running status: {e}")


class RateLimitError(Exception):
    """Raised when LinkedIn API returns 429."""
    pass
