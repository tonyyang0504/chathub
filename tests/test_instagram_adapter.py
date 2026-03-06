"""
Tests for the Instagram Platform Adapter.

Tests cover:
- Adapter interface compliance
- Message splitting
- Webhook signature verification
- Webhook route handling (GET verify + POST receive)
- Incoming message processing (text, media, dedup, echo)
- Outbound message sending
- Error handling (bad token, network errors, non-JSON errors)
- Bot lifecycle (run/stop/cleanup)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import httpx

from app.platforms.instagram.adapter import InstagramAdapter, GRAPH_API_BASE
from app.platforms.base import PlatformAdapter, PlatformType, AuthMethod


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

class FakeBotInstance:
    """Mimics BotInstance from app.bots.manager."""

    def __init__(self, bot_profile_id=1, config=None):
        self.bot_profile_id = bot_profile_id
        self.config = config or {}
        self.is_running = True
        self.whatsapp_connected = False
        self.ai_response_enabled = False
        self.qr_callbacks = []
        self.status_callbacks = []
        self.outbound_queue = []
        self._statuses = []

    async def notify_status(self, status: dict):
        self._statuses.append(status)

    async def notify_qr(self, qr_code: str):
        pass

    def get_outbound_messages(self) -> list:
        msgs = self.outbound_queue.copy()
        self.outbound_queue.clear()
        return msgs

    def has_outbound_messages(self) -> bool:
        return len(self.outbound_queue) > 0


def _make_httpx_response(status_code=200, json_data=None, headers=None, content=b""):
    """Build a fake httpx.Response."""
    if json_data is not None and not content:
        content = json.dumps(json_data).encode()
        headers = dict(headers or {})
        headers.setdefault("content-type", "application/json")
    resp = httpx.Response(
        status_code=status_code,
        headers=headers or {},
        content=content,
    )
    return resp


def _graph_page_response(page_id="123", page_name="TestPage", ig_id="456"):
    return _make_httpx_response(200, json_data={
        "id": page_id,
        "name": page_name,
        "instagram_business_account": {"id": ig_id},
    })


def _graph_send_ok():
    return _make_httpx_response(200, json_data={"recipient_id": "user_1", "message_id": "mid.xxx"})


def _graph_send_error(msg="Rate limit hit"):
    return _make_httpx_response(400, json_data={"error": {"message": msg, "code": 100}})


def _graph_profile_response(name="Jane Doe"):
    return _make_httpx_response(200, json_data={"name": name, "username": "janedoe"})


def _build_webhook_payload(page_id="123", sender_id="user_1", text="Hello", mid="mid.abc",
                           timestamp=None, attachments=None, is_echo=False):
    """Build a Meta Instagram webhook payload."""
    message = {"mid": mid, "text": text}
    if is_echo:
        message["is_echo"] = True
    if attachments:
        message["attachments"] = attachments
    return {
        "object": "instagram",
        "entry": [{
            "id": page_id,
            "time": int(time.time()),
            "messaging": [{
                "sender": {"id": sender_id},
                "recipient": {"id": page_id},
                "timestamp": timestamp or int(time.time() * 1000),
                "message": message,
            }],
        }],
    }


# ---------------------------------------------------------------------------
# Interface & Properties
# ---------------------------------------------------------------------------

class TestAdapterInterface:
    def test_is_platform_adapter(self):
        adapter = InstagramAdapter()
        assert isinstance(adapter, PlatformAdapter)

    def test_platform_type(self):
        adapter = InstagramAdapter()
        assert adapter.platform_type == PlatformType.INSTAGRAM
        assert adapter.platform_type.value == "instagram"

    def test_capabilities(self):
        c = InstagramAdapter().capabilities
        assert c.supports_groups is False
        assert c.supports_media is True
        assert c.supports_file_send is False
        assert c.supports_reactions is True
        assert c.supports_read_receipts is True
        assert c.supports_typing_indicator is True
        assert c.supports_history_sync is False
        assert c.auth_method == AuthMethod.OAUTH
        assert c.max_message_length == 1000

    def test_initial_state(self):
        adapter = InstagramAdapter()
        assert adapter._active_bots == {}
        assert adapter._page_to_bot == {}
        assert adapter._http_client is None


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------

class TestMessageSplitting:
    def test_short_message_no_split(self):
        assert InstagramAdapter._split_message("hello", 1000) == ["hello"]

    def test_exact_limit(self):
        msg = "a" * 1000
        assert InstagramAdapter._split_message(msg, 1000) == [msg]

    def test_splits_on_space(self):
        msg = "word " * 250  # 1250 chars
        chunks = InstagramAdapter._split_message(msg, 1000)
        assert len(chunks) == 2
        assert all(len(c) <= 1000 for c in chunks)

    def test_splits_on_newline(self):
        msg = "x" * 500 + "\n" + "y" * 600
        chunks = InstagramAdapter._split_message(msg, 1000)
        assert len(chunks) == 2

    def test_splits_no_boundary(self):
        msg = "a" * 2500  # no spaces at all
        chunks = InstagramAdapter._split_message(msg, 1000)
        assert len(chunks) == 3
        assert chunks[0] == "a" * 1000
        assert chunks[1] == "a" * 1000
        assert chunks[2] == "a" * 500

    def test_empty_message(self):
        assert InstagramAdapter._split_message("", 1000) == [""]


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

class TestWebhookSignature:
    def setup_method(self):
        self.adapter = InstagramAdapter()

    def test_no_signature_no_secret_rejects(self):
        # No bots configured = fail closed (reject)
        assert self.adapter._verify_webhook_signature(b"body", "") is False

    def test_no_signature_with_secret_rejects(self):
        self.adapter._active_bots[1] = {"app_secret": "mysecret"}
        assert self.adapter._verify_webhook_signature(b"body", "") is False

    def test_valid_signature(self):
        secret = "test_secret"
        body = b'{"test": true}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        self.adapter._active_bots[1] = {"app_secret": secret}
        assert self.adapter._verify_webhook_signature(body, f"sha256={sig}") is True

    def test_invalid_signature(self):
        self.adapter._active_bots[1] = {"app_secret": "real_secret"}
        assert self.adapter._verify_webhook_signature(b"body", "sha256=badbadbad") is False

    def test_malformed_signature_header(self):
        self.adapter._active_bots[1] = {"app_secret": "secret"}
        assert self.adapter._verify_webhook_signature(b"body", "md5=abc") is False

    def test_no_secret_configured_with_signature_rejects(self):
        # Signature present but no bot has a secret — fail closed (reject)
        self.adapter._active_bots[1] = {"app_secret": ""}
        assert self.adapter._verify_webhook_signature(b"body", "sha256=anything") is False


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_run_no_access_token(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={"instagram_page_id": "123"})

        await adapter.run(instance)

        assert instance.is_running is False
        assert any("No access token" in s.get("error", "") for s in instance._statuses)

    @pytest.mark.asyncio
    async def test_run_no_page_id(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={"api_key": "token123"})

        await adapter.run(instance)

        assert instance.is_running is False
        assert any("No Instagram Page ID" in s.get("error", "") for s in instance._statuses)

    @pytest.mark.asyncio
    async def test_run_bad_token(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "bad_token",
            "instagram_page_id": "123",
        })

        error_resp = _make_httpx_response(401, json_data={
            "error": {"message": "Invalid OAuth token", "code": 190}
        })

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=error_resp)
            mock_client.return_value = client

            await adapter.run(instance)

        assert instance.is_running is False
        assert any("Token validation failed" in s.get("error", "") for s in instance._statuses)

    @pytest.mark.asyncio
    async def test_run_non_json_error_response(self):
        """Graph API returns HTML instead of JSON on error."""
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "token",
            "instagram_page_id": "123",
        })

        # Response that will raise on .json()
        error_resp = _make_httpx_response(502, content=b"<html>Bad Gateway</html>")

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=error_resp)
            mock_client.return_value = client

            await adapter.run(instance)

        assert instance.is_running is False
        assert any("HTTP 502" in s.get("error", "") for s in instance._statuses)

    @pytest.mark.asyncio
    async def test_run_network_error(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "token",
            "instagram_page_id": "123",
        })

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client.return_value = client

            await adapter.run(instance)

        assert instance.is_running is False
        assert any("Connection error" in s.get("error", "") for s in instance._statuses)

    @pytest.mark.asyncio
    async def test_run_success_and_stop(self):
        """Successful connection, then stop via is_running=False."""
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "valid_token",
            "instagram_page_id": "pg_123",
            "webhook_verify_token": "verify_me",
        })

        page_resp = _graph_page_response(page_id="pg_123", page_name="MyPage", ig_id="ig_456")

        with patch.object(adapter, '_get_client') as mock_client, \
             patch.object(adapter, '_ensure_webhook_routes'):
            client = AsyncMock()
            client.get = AsyncMock(return_value=page_resp)
            mock_client.return_value = client

            # Stop the bot after a short delay
            async def stop_after_delay():
                await asyncio.sleep(0.1)
                instance.is_running = False

            await asyncio.gather(
                adapter.run(instance),
                stop_after_delay(),
            )

        # Verify connected status was sent
        assert any(s.get("connected") for s in instance._statuses)
        assert any("MyPage" in s.get("message", "") for s in instance._statuses)
        # Cleanup happened
        assert instance.bot_profile_id not in adapter._active_bots
        assert instance.whatsapp_connected is False

    @pytest.mark.asyncio
    async def test_run_registers_in_lookup_maps(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "token",
            "instagram_page_id": "pg_99",
        })

        page_resp = _graph_page_response(page_id="pg_99", ig_id="ig_88")

        registered = False

        with patch.object(adapter, '_get_client') as mock_client, \
             patch.object(adapter, '_ensure_webhook_routes'):
            client = AsyncMock()
            client.get = AsyncMock(return_value=page_resp)
            mock_client.return_value = client

            async def check_and_stop():
                nonlocal registered
                await asyncio.sleep(0.05)
                registered = (
                    1 in adapter._active_bots and
                    adapter._page_to_bot.get("pg_99") == 1
                )
                instance.is_running = False

            await asyncio.gather(adapter.run(instance), check_and_stop())

        assert registered is True

    @pytest.mark.asyncio
    async def test_run_processes_outbound_queue(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "token",
            "instagram_page_id": "pg_1",
        })
        instance.outbound_queue = [{"chat_id": "user_99", "message": "Hi there!"}]

        page_resp = _graph_page_response(page_id="pg_1")

        sent_messages = []

        async def fake_send(bot_id, chat_id, chat_name, msg):
            sent_messages.append((chat_id, msg))
            return True

        with patch.object(adapter, '_get_client') as mock_client, \
             patch.object(adapter, '_ensure_webhook_routes'), \
             patch.object(adapter, 'send_message', side_effect=fake_send):
            client = AsyncMock()
            client.get = AsyncMock(return_value=page_resp)
            mock_client.return_value = client

            async def stop_after():
                await asyncio.sleep(0.15)
                instance.is_running = False

            await asyncio.gather(adapter.run(instance), stop_after())

        assert ("user_99", "Hi there!") in sent_messages


# ---------------------------------------------------------------------------
# Send message
# ---------------------------------------------------------------------------

class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_success(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(return_value=_graph_send_ok())
            mock_client.return_value = client

            result = await adapter.send_message(1, "user_1", "Jane", "Hello!")

        assert result is True
        client.post.assert_called_once()
        call_kwargs = client.post.call_args
        assert call_kwargs[1]["json"]["message"]["text"] == "Hello!"
        assert call_kwargs[1]["json"]["recipient"]["id"] == "user_1"

    @pytest.mark.asyncio
    async def test_send_message_not_active(self):
        adapter = InstagramAdapter()
        result = await adapter.send_message(999, "user_1", "Jane", "Hello!")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_api_error(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(return_value=_graph_send_error("Rate limit"))
            mock_client.return_value = client

            result = await adapter.send_message(1, "user_1", "Jane", "Hello!")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_network_error(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=httpx.ConnectError("timeout"))
            mock_client.return_value = client

            result = await adapter.send_message(1, "user_1", "Jane", "Hello!")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_splits_long(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(return_value=_graph_send_ok())
            mock_client.return_value = client

            long_msg = "word " * 250  # 1250 chars -> 2 chunks
            result = await adapter.send_message(1, "user_1", "Jane", long_msg)

        assert result is True
        assert client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_send_message_non_json_error(self):
        """API returns HTML error page."""
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}

        error_resp = _make_httpx_response(503, content=b"<html>Service Unavailable</html>")

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(return_value=error_resp)
            mock_client.return_value = client

            result = await adapter.send_message(1, "user_1", "Jane", "Hello!")

        assert result is False


# ---------------------------------------------------------------------------
# Send file
# ---------------------------------------------------------------------------

class TestSendFile:
    @pytest.mark.asyncio
    async def test_send_file_not_active(self):
        adapter = InstagramAdapter()
        result = await adapter.send_file(999, "user_1", "/tmp/test.jpg")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_file_with_caption_sends_text(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}

        with patch.object(adapter, 'send_message', new_callable=AsyncMock, return_value=True) as mock_send:
            result = await adapter.send_file(1, "user_1", "/tmp/test.jpg", caption="Check this out", chat_name="Jane")

        assert result is True
        mock_send.assert_called_once_with(1, "user_1", "Jane", "Check this out")

    @pytest.mark.asyncio
    async def test_send_file_no_caption_returns_false(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"access_token": "tok", "page_id": "pg_1"}
        result = await adapter.send_file(1, "user_1", "/tmp/test.jpg", chat_name="Jane")
        assert result is False


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_removes_bot(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"page_id": "pg_1"}
        adapter._page_to_bot["pg_1"] = 1

        adapter.cleanup(1)

        assert 1 not in adapter._active_bots
        assert "pg_1" not in adapter._page_to_bot

    def test_cleanup_nonexistent_bot(self):
        adapter = InstagramAdapter()
        adapter.cleanup(999)  # should not raise


# ---------------------------------------------------------------------------
# Webhook payload processing
# ---------------------------------------------------------------------------

class TestWebhookProcessing:
    @pytest.mark.asyncio
    async def test_ignores_non_instagram_object(self):
        adapter = InstagramAdapter()
        # Should not raise
        await adapter._process_webhook_payload({"object": "page", "entry": []})

    @pytest.mark.asyncio
    async def test_ignores_unknown_page(self):
        adapter = InstagramAdapter()
        payload = _build_webhook_payload(page_id="unknown_page", text="Hi")
        await adapter._process_webhook_payload(payload)
        # No error — just silently skipped

    @pytest.mark.asyncio
    async def test_echo_message_ignored(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "instance": FakeBotInstance(),
        }
        adapter._page_to_bot["pg_1"] = 1

        payload = _build_webhook_payload(page_id="pg_1", sender_id="user_1", is_echo=True)

        with patch.object(adapter, '_handle_incoming_message', new_callable=AsyncMock) as mock_handle:
            await adapter._process_webhook_payload(payload)

        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_page_sender_ignored(self):
        """Messages from the page itself (sender == page_id) are ignored."""
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "instance": FakeBotInstance(),
        }
        adapter._page_to_bot["pg_1"] = 1

        payload = _build_webhook_payload(page_id="pg_1", sender_id="pg_1", text="echo")

        with patch.object(adapter, '_handle_incoming_message', new_callable=AsyncMock) as mock_handle:
            await adapter._process_webhook_payload(payload)

        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ig_account_sender_ignored(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "instance": FakeBotInstance(),
        }
        adapter._page_to_bot["pg_1"] = 1

        payload = _build_webhook_payload(page_id="pg_1", sender_id="ig_1", text="self-msg")

        with patch.object(adapter, '_handle_incoming_message', new_callable=AsyncMock) as mock_handle:
            await adapter._process_webhook_payload(payload)

        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_message_dispatched(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance()
        adapter._active_bots[1] = {
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "instance": instance,
        }
        adapter._page_to_bot["pg_1"] = 1

        payload = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="Hello bot!")

        with patch.object(adapter, '_handle_incoming_message', new_callable=AsyncMock) as mock_handle:
            await adapter._process_webhook_payload(payload)

        mock_handle.assert_called_once()
        args = mock_handle.call_args[0]
        assert args[0] == 1  # bot_id
        assert args[2]["message"]["text"] == "Hello bot!"

    @pytest.mark.asyncio
    async def test_postback_dispatched(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance()
        adapter._active_bots[1] = {
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "instance": instance,
        }
        adapter._page_to_bot["pg_1"] = 1

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "pg_1",
                "time": int(time.time()),
                "messaging": [{
                    "sender": {"id": "user_1"},
                    "recipient": {"id": "pg_1"},
                    "timestamp": int(time.time() * 1000),
                    "postback": {"title": "Get Started", "payload": "GET_STARTED"},
                }],
            }],
        }

        with patch.object(adapter, '_handle_postback', new_callable=AsyncMock) as mock_pb:
            await adapter._process_webhook_payload(payload)

        mock_pb.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_receipt_handled(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "instance": FakeBotInstance(),
        }
        adapter._page_to_bot["pg_1"] = 1

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "pg_1",
                "time": int(time.time()),
                "messaging": [{
                    "sender": {"id": "user_1"},
                    "recipient": {"id": "pg_1"},
                    "timestamp": int(time.time() * 1000),
                    "read": {"watermark": int(time.time() * 1000)},
                }],
            }],
        }

        # Should not raise
        await adapter._process_webhook_payload(payload)


# ---------------------------------------------------------------------------
# Incoming message handling (integration-style with mocked DB/AI)
# ---------------------------------------------------------------------------

@dataclass
class FakeMessage:
    id: int = 1
    conversation_id: int = 1
    role: str = "user"
    content: str = "test"
    sender_name: str = "Jane"
    sender_id: str = "user_1"
    sender_phone: str = ""
    sender_profile_pic: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    whatsapp_message_id: str = ""
    file_url: str = None
    file_type: str = None
    file_name: str = None
    file_size: int = None
    file_pages: int = None
    media_analysis: str = None


@dataclass
class FakeConversation:
    id: int = 1
    bot_profile_id: int = 1
    chat_id: str = "user_1"
    chat_name: str = "Jane"
    display_name: str = "Jane"
    phone: str = ""
    is_group: bool = False
    profile_pic: str = ""
    message_count: int = 0
    last_message_at: datetime = field(default_factory=datetime.utcnow)
    human_takeover: bool = False


@dataclass
class FakeBotProfile:
    id: int = 1
    user_id: int = 1
    name: str = "TestBot"
    ai_provider: str = "openai"
    api_key_encrypted: str = "encrypted_key"
    model: str = "gpt-4o-mini"
    system_prompt: str = "You are a helpful assistant."
    temperature: float = 0.7
    max_tokens: int = 500
    max_history: int = 20


@dataclass
class FakeAIResponse:
    content: str = "AI reply"
    model: str = "gpt-4o-mini"
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)


class TestIncomingMessage:
    """Test _handle_incoming_message with fully mocked dependencies."""

    def _make_adapter_with_bot(self):
        adapter = InstagramAdapter()
        instance = FakeBotInstance(config={
            "api_key": "token",
            "instagram_page_id": "pg_1",
            "response_delay_min": 0,
            "response_delay_max": 0,
        })
        instance.ai_response_enabled = True
        adapter._active_bots[1] = {
            "access_token": "token",
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "instance": instance,
        }
        return adapter, instance

    def _mock_message_handler(self):
        """Return a dict of mocked message_handler functions."""
        fake_conv = FakeConversation()
        fake_user_msg = FakeMessage(id=10, content="Hello")
        fake_asst_msg = FakeMessage(id=11, role="assistant", content="AI reply", sender_name="TestBot")

        return {
            "get_db_session": MagicMock(return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock()),
                __exit__=MagicMock(return_value=False),
            )),
            "find_or_create_conversation": MagicMock(return_value=fake_conv),
            "save_user_message": MagicMock(return_value=fake_user_msg),
            "save_assistant_message": MagicMock(return_value=fake_asst_msg),
            "update_conversation_stats": MagicMock(),
            "is_duplicate_message": MagicMock(return_value=(False, None)),
            "has_response_after": MagicMock(return_value=False),
            "build_ai_messages": MagicMock(return_value=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ]),
            "is_human_takeover_active": MagicMock(return_value=False),
            "broadcast_user_message": MagicMock(),
            "broadcast_assistant_message": MagicMock(),
            "broadcast_typing": MagicMock(),
            "save_media_file": MagicMock(),
            "analyze_media_with_ai": MagicMock(),
            "log_activity": MagicMock(),
        }

    @pytest.mark.asyncio
    async def test_text_message_full_flow(self):
        """Full flow: receive text -> save -> AI -> send reply -> save reply."""
        adapter, instance = self._make_adapter_with_bot()
        mocks = self._mock_message_handler()

        event = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="Hello bot!")["entry"][0]["messaging"][0]

        fake_ai_response = FakeAIResponse(content="Hi there!")

        with patch("app.platforms.message_handler.get_db_session", mocks["get_db_session"]), \
             patch("app.platforms.message_handler.find_or_create_conversation", mocks["find_or_create_conversation"]), \
             patch("app.platforms.message_handler.save_user_message", mocks["save_user_message"]), \
             patch("app.platforms.message_handler.save_assistant_message", mocks["save_assistant_message"]), \
             patch("app.platforms.message_handler.update_conversation_stats", mocks["update_conversation_stats"]), \
             patch("app.platforms.message_handler.is_duplicate_message", mocks["is_duplicate_message"]), \
             patch("app.platforms.message_handler.has_response_after", mocks["has_response_after"]), \
             patch("app.platforms.message_handler.build_ai_messages", mocks["build_ai_messages"]), \
             patch("app.platforms.message_handler.is_human_takeover_active", mocks["is_human_takeover_active"]), \
             patch("app.platforms.message_handler.broadcast_user_message", mocks["broadcast_user_message"]), \
             patch("app.platforms.message_handler.broadcast_assistant_message", mocks["broadcast_assistant_message"]), \
             patch("app.platforms.message_handler.broadcast_typing", mocks["broadcast_typing"]), \
             patch("app.platforms.message_handler.log_activity", mocks["log_activity"]), \
             patch.object(adapter, '_get_user_profile', new_callable=AsyncMock, return_value="Jane"), \
             patch.object(adapter, '_send_typing_indicator', new_callable=AsyncMock), \
             patch.object(adapter, 'send_message', new_callable=AsyncMock, return_value=True) as mock_send, \
             patch.object(adapter, '_get_bot_profile', return_value=FakeBotProfile()), \
             patch("app.auth.utils.decrypt_string", return_value="decrypted_key"), \
             patch("app.ai.factory.get_ai_provider") as mock_ai_factory:

            mock_provider = MagicMock()
            mock_provider.chat_completion = MagicMock(return_value=fake_ai_response)
            mock_ai_factory.return_value = mock_provider

            await adapter._handle_incoming_message(1, adapter._active_bots[1], event)

        # Verify user message was saved
        mocks["save_user_message"].assert_called_once()
        save_call = mocks["save_user_message"].call_args
        assert save_call[0][2] == "Hello bot!"  # content

        # Verify AI was called
        mock_provider.chat_completion.assert_called_once()

        # Verify reply was sent
        mock_send.assert_called_once()
        send_args = mock_send.call_args[0]
        assert send_args[1] == "user_1"  # chat_id
        assert send_args[3] == "Hi there!"  # message

        # Verify assistant message was saved
        mocks["save_assistant_message"].assert_called_once()

        # Verify WebSocket broadcasts
        mocks["broadcast_user_message"].assert_called_once()
        mocks["broadcast_assistant_message"].assert_called_once()

    @pytest.mark.asyncio
    async def test_ai_disabled_skips_response(self):
        adapter, instance = self._make_adapter_with_bot()
        instance.ai_response_enabled = False
        mocks = self._mock_message_handler()

        event = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="Hi")["entry"][0]["messaging"][0]

        with patch("app.platforms.message_handler.get_db_session", mocks["get_db_session"]), \
             patch("app.platforms.message_handler.find_or_create_conversation", mocks["find_or_create_conversation"]), \
             patch("app.platforms.message_handler.save_user_message", mocks["save_user_message"]), \
             patch("app.platforms.message_handler.update_conversation_stats", mocks["update_conversation_stats"]), \
             patch("app.platforms.message_handler.is_duplicate_message", mocks["is_duplicate_message"]), \
             patch("app.platforms.message_handler.has_response_after", mocks["has_response_after"]), \
             patch("app.platforms.message_handler.is_human_takeover_active", mocks["is_human_takeover_active"]), \
             patch("app.platforms.message_handler.broadcast_user_message", mocks["broadcast_user_message"]), \
             patch("app.platforms.message_handler.broadcast_assistant_message", mocks["broadcast_assistant_message"]), \
             patch("app.platforms.message_handler.broadcast_typing", mocks["broadcast_typing"]), \
             patch("app.platforms.message_handler.log_activity", mocks["log_activity"]), \
             patch.object(adapter, '_get_user_profile', new_callable=AsyncMock, return_value="Jane"), \
             patch.object(adapter, '_get_bot_profile', return_value=FakeBotProfile()), \
             patch.object(adapter, 'send_message', new_callable=AsyncMock) as mock_send:

            await adapter._handle_incoming_message(1, adapter._active_bots[1], event)

        # User message saved
        mocks["save_user_message"].assert_called_once()
        # But no AI reply sent
        mock_send.assert_not_called()
        mocks["save_assistant_message"].assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_message_skipped(self):
        adapter, instance = self._make_adapter_with_bot()
        mocks = self._mock_message_handler()
        mocks["is_duplicate_message"] = MagicMock(return_value=(True, FakeMessage(id=5)))
        mocks["has_response_after"] = MagicMock(return_value=True)

        event = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="dup")["entry"][0]["messaging"][0]

        with patch("app.platforms.message_handler.get_db_session", mocks["get_db_session"]), \
             patch("app.platforms.message_handler.find_or_create_conversation", mocks["find_or_create_conversation"]), \
             patch("app.platforms.message_handler.save_user_message", mocks["save_user_message"]), \
             patch("app.platforms.message_handler.is_duplicate_message", mocks["is_duplicate_message"]), \
             patch("app.platforms.message_handler.has_response_after", mocks["has_response_after"]), \
             patch.object(adapter, '_get_user_profile', new_callable=AsyncMock, return_value="Jane"):

            await adapter._handle_incoming_message(1, adapter._active_bots[1], event)

        # Message was NOT saved (duplicate)
        mocks["save_user_message"].assert_not_called()

    @pytest.mark.asyncio
    async def test_human_takeover_skips_ai(self):
        adapter, instance = self._make_adapter_with_bot()
        mocks = self._mock_message_handler()
        mocks["is_human_takeover_active"] = MagicMock(return_value=True)

        event = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="help")["entry"][0]["messaging"][0]

        with patch("app.platforms.message_handler.get_db_session", mocks["get_db_session"]), \
             patch("app.platforms.message_handler.find_or_create_conversation", mocks["find_or_create_conversation"]), \
             patch("app.platforms.message_handler.save_user_message", mocks["save_user_message"]), \
             patch("app.platforms.message_handler.update_conversation_stats", mocks["update_conversation_stats"]), \
             patch("app.platforms.message_handler.is_duplicate_message", mocks["is_duplicate_message"]), \
             patch("app.platforms.message_handler.has_response_after", mocks["has_response_after"]), \
             patch("app.platforms.message_handler.is_human_takeover_active", mocks["is_human_takeover_active"]), \
             patch("app.platforms.message_handler.broadcast_user_message", mocks["broadcast_user_message"]), \
             patch("app.platforms.message_handler.broadcast_typing", mocks["broadcast_typing"]), \
             patch("app.platforms.message_handler.log_activity", mocks["log_activity"]), \
             patch.object(adapter, '_get_user_profile', new_callable=AsyncMock, return_value="Jane"), \
             patch.object(adapter, '_get_bot_profile', return_value=FakeBotProfile()), \
             patch.object(adapter, 'send_message', new_callable=AsyncMock) as mock_send:

            await adapter._handle_incoming_message(1, adapter._active_bots[1], event)

        # Message saved, but no AI response
        mocks["save_user_message"].assert_called_once()
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Typing indicator
# ---------------------------------------------------------------------------

class TestTypingIndicator:
    @pytest.mark.asyncio
    async def test_send_typing_on(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"page_id": "pg_1", "access_token": "tok"}

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(return_value=_make_httpx_response(200))
            mock_client.return_value = client

            await adapter._send_typing_indicator(1, "user_1", "typing_on")

        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"]["sender_action"] == "typing_on"
        assert call_kwargs["json"]["recipient"]["id"] == "user_1"

    @pytest.mark.asyncio
    async def test_typing_indicator_not_active(self):
        adapter = InstagramAdapter()
        # Should not raise
        await adapter._send_typing_indicator(999, "user_1", "typing_on")

    @pytest.mark.asyncio
    async def test_typing_indicator_error_suppressed(self):
        adapter = InstagramAdapter()
        adapter._active_bots[1] = {"page_id": "pg_1", "access_token": "tok"}

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=httpx.ConnectError("fail"))
            mock_client.return_value = client

            # Should not raise
            await adapter._send_typing_indicator(1, "user_1", "typing_on")


# ---------------------------------------------------------------------------
# User profile fetch
# ---------------------------------------------------------------------------

class TestUserProfile:
    @pytest.mark.asyncio
    async def test_fetch_profile_success(self):
        adapter = InstagramAdapter()

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=_graph_profile_response("Alice"))
            mock_client.return_value = client

            name = await adapter._get_user_profile("user_1", "tok")

        assert name == "Alice"

    @pytest.mark.asyncio
    async def test_fetch_profile_fallback_to_id(self):
        adapter = InstagramAdapter()

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=_make_httpx_response(403))
            mock_client.return_value = client

            name = await adapter._get_user_profile("user_1", "tok")

        assert name == "user_1"

    @pytest.mark.asyncio
    async def test_fetch_profile_network_error(self):
        adapter = InstagramAdapter()

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
            mock_client.return_value = client

            name = await adapter._get_user_profile("user_1", "tok")

        assert name == "user_1"


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------

class TestMediaDownload:
    @pytest.mark.asyncio
    async def test_download_success(self):
        adapter = InstagramAdapter()
        fake_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        resp = _make_httpx_response(200, content=fake_image, headers={"content-type": "image/png"})

        saved_result = {
            "file_url": "/media/bot_1/conversations/Jane/images/received/test.png",
            "file_type": "image/png",
            "file_name": "test.png",
            "file_size": len(fake_image),
            "local_file_path": "/tmp/test.png",
        }

        with patch.object(adapter, '_get_client') as mock_client, \
             patch("app.platforms.message_handler.save_media_file", return_value=saved_result) as mock_save:
            client = AsyncMock()
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await adapter._download_and_save_media(1, "Jane", "https://cdn.fbsbx.com/image.png", "image")

        assert result is not None
        assert result["file_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_download_strips_content_type_params(self):
        adapter = InstagramAdapter()
        fake_image = b"\xff\xd8\xff" + b"\x00" * 50

        resp = _make_httpx_response(200, content=fake_image, headers={"content-type": "image/jpeg; charset=utf-8"})

        with patch.object(adapter, '_get_client') as mock_client, \
             patch("app.platforms.message_handler.save_media_file", return_value={"file_type": "image/jpeg"}) as mock_save:
            client = AsyncMock()
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await adapter._download_and_save_media(1, "Jane", "https://cdn.fbsbx.com/img.jpg", "image")

        # Verify save_media_file was called with clean content type
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["media_type"] == "image/jpeg"  # no charset

    @pytest.mark.asyncio
    async def test_download_fallback_content_type(self):
        adapter = InstagramAdapter()

        resp = _make_httpx_response(200, content=b"data", headers={"content-type": ""})

        with patch.object(adapter, '_get_client') as mock_client, \
             patch("app.platforms.message_handler.save_media_file", return_value={"file_type": "video/mp4"}) as mock_save:
            client = AsyncMock()
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await adapter._download_and_save_media(1, "Jane", "https://cdn.fbsbx.com/video.mp4", "video")

        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["media_type"] == "video/mp4"

    @pytest.mark.asyncio
    async def test_download_failure(self):
        adapter = InstagramAdapter()

        resp = _make_httpx_response(404)

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await adapter._download_and_save_media(1, "Jane", "https://cdn.fbsbx.com/gone.png", "image")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_network_error(self):
        adapter = InstagramAdapter()

        with patch.object(adapter, '_get_client') as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
            mock_client.return_value = client

            result = await adapter._download_and_save_media(1, "Jane", "https://cdn.fbsbx.com/img.png", "image")

        assert result is None


# ---------------------------------------------------------------------------
# Postback handling
# ---------------------------------------------------------------------------

class TestPostback:
    @pytest.mark.asyncio
    async def test_postback_creates_message_event(self):
        adapter = InstagramAdapter()

        event = {
            "sender": {"id": "user_1"},
            "recipient": {"id": "pg_1"},
            "timestamp": int(time.time() * 1000),
            "postback": {"title": "Get Started", "payload": "GET_STARTED"},
        }

        with patch.object(adapter, '_handle_incoming_message', new_callable=AsyncMock) as mock_handle:
            await adapter._handle_postback(1, {}, event)

        mock_handle.assert_called_once()
        created_event = mock_handle.call_args[0][2]
        assert created_event["message"]["text"] == "Get Started"
        assert "postback" not in created_event

    @pytest.mark.asyncio
    async def test_postback_uses_payload_when_no_title(self):
        adapter = InstagramAdapter()

        event = {
            "sender": {"id": "user_1"},
            "recipient": {"id": "pg_1"},
            "timestamp": int(time.time() * 1000),
            "postback": {"title": "", "payload": "MENU_OPTION_1"},
        }

        with patch.object(adapter, '_handle_incoming_message', new_callable=AsyncMock) as mock_handle:
            await adapter._handle_postback(1, {}, event)

        created_event = mock_handle.call_args[0][2]
        assert created_event["message"]["text"] == "MENU_OPTION_1"


# ---------------------------------------------------------------------------
# HTTP client management
# ---------------------------------------------------------------------------

class TestHTTPClient:
    @pytest.mark.asyncio
    async def test_creates_client_on_first_call(self):
        adapter = InstagramAdapter()
        assert adapter._http_client is None

        client = await adapter._get_client()
        assert client is not None
        assert adapter._http_client is client

        await client.aclose()

    @pytest.mark.asyncio
    async def test_reuses_existing_client(self):
        adapter = InstagramAdapter()
        client1 = await adapter._get_client()
        client2 = await adapter._get_client()
        assert client1 is client2

        await client1.aclose()

    @pytest.mark.asyncio
    async def test_recreates_closed_client(self):
        adapter = InstagramAdapter()
        client1 = await adapter._get_client()
        await client1.aclose()

        client2 = await adapter._get_client()
        assert client2 is not client1
        assert not client2.is_closed

        await client2.aclose()


# ---------------------------------------------------------------------------
# Webhook routes (integration with FastAPI TestClient)
# ---------------------------------------------------------------------------

class TestWebhookRoutes:
    """Test webhook endpoints via FastAPI TestClient.

    These tests use the singleton adapter from the registry, since the
    webhook routes capture `self` at registration time.
    """

    @pytest.fixture(autouse=True)
    def _setup_adapter(self):
        """Get the singleton adapter and ensure webhook routes are registered."""
        from app.platforms.registry import platform_registry
        self.adapter = platform_registry.get_adapter(PlatformType.INSTAGRAM)
        # Ensure routes are registered
        InstagramAdapter._webhook_routes_registered = False
        self.adapter._ensure_webhook_routes()
        yield
        # Cleanup
        self.adapter._active_bots.clear()
        self.adapter._page_to_bot.clear()

    def test_webhook_verify_success(self, client):
        """GET /webhooks/instagram with correct verify token."""
        self.adapter._active_bots[1] = {
            "webhook_verify_token": "my_verify_token",
            "page_id": "pg_1",
            "access_token": "tok",
        }

        resp = client.get("/webhooks/instagram", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "my_verify_token",
            "hub.challenge": "challenge_string_123",
        })

        assert resp.status_code == 200
        assert resp.text == "challenge_string_123"

    def test_webhook_verify_bad_token(self, client):
        self.adapter._active_bots[1] = {
            "webhook_verify_token": "correct_token",
        }

        resp = client.get("/webhooks/instagram", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "challenge",
        })

        assert resp.status_code == 403

    def test_webhook_post_returns_200(self, client):
        """POST /webhooks/instagram should return 200 EVENT_RECEIVED."""
        secret = "test_app_secret"
        self.adapter._active_bots[1] = {
            "app_secret": secret,
            "page_id": "pg_1",
            "ig_account_id": "ig_1",
            "access_token": "tok",
            "webhook_verify_token": "vt",
            "instance": FakeBotInstance(),
        }
        self.adapter._page_to_bot["pg_1"] = 1

        payload = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="Hi")
        body = json.dumps(payload).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        resp = client.post(
            "/webhooks/instagram",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )

        assert resp.status_code == 200
        assert resp.text == "EVENT_RECEIVED"

    def test_webhook_post_bad_signature(self, client):
        self.adapter._active_bots[1] = {"app_secret": "real_secret"}

        payload = _build_webhook_payload(page_id="pg_1", sender_id="user_1", text="Hi")

        resp = client.post(
            "/webhooks/instagram",
            json=payload,
            headers={"X-Hub-Signature-256": "sha256=bad_sig"},
        )

        assert resp.status_code == 403

    def test_webhook_post_invalid_json(self, client):
        secret = "test_app_secret"
        self.adapter._active_bots[1] = {"app_secret": secret}
        body = b"not json at all"
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        resp = client.post(
            "/webhooks/instagram",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )

        assert resp.status_code == 400
