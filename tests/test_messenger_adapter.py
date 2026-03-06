"""
Tests for the Facebook Messenger platform adapter.
"""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.platforms.messenger.adapter import (
    MessengerAdapter,
    _bot_state,
    _get_state,
    _split_message,
    _verify_signature,
    _handle_message,
    _process_webhook_entries,
    _task_error_callback,
    cleanup_bot_state,
)


@pytest.fixture(autouse=True)
def clean_bot_state():
    """Ensure _bot_state is clean before and after each test."""
    _bot_state.clear()
    yield
    _bot_state.clear()


@pytest.fixture(autouse=True, scope="session")
def register_webhook_routes():
    """Ensure webhook routes are registered on the app before tests run."""
    from app.platforms.messenger.adapter import _ensure_webhook_routes
    _ensure_webhook_routes()


# ---------------------------------------------------------------------------
# Unit tests — pure functions
# ---------------------------------------------------------------------------

class TestSplitMessage:
    def test_short_message(self):
        assert _split_message("hello") == ["hello"]

    def test_empty_message(self):
        assert _split_message("") == [""]

    def test_exactly_at_limit(self):
        msg = "a" * 2000
        assert _split_message(msg, 2000) == [msg]

    def test_one_over_limit(self):
        msg = "a" * 2001
        chunks = _split_message(msg, 2000)
        assert len(chunks) == 2
        assert len(chunks[0]) == 2000
        assert len(chunks[1]) == 1

    def test_splits_at_space(self):
        # 10-char words with spaces => "wordwordwo wordwordwo ..."
        msg = " ".join(["abcdefghij"] * 250)  # ~2750 chars
        chunks = _split_message(msg, 2000)
        for chunk in chunks:
            assert len(chunk) <= 2000

    def test_splits_at_newline(self):
        msg = ("x" * 999 + "\n") * 3  # 3000 chars
        chunks = _split_message(msg, 2000)
        assert len(chunks) == 2
        assert all(len(c) <= 2000 for c in chunks)

    def test_no_content_lost(self):
        msg = " ".join(["word"] * 600)
        chunks = _split_message(msg, 2000)
        rejoined = " ".join(c.strip() for c in chunks)
        # Allow minor whitespace differences from lstrip
        assert rejoined.replace("  ", " ") == msg or len("".join(chunks)) >= len(msg) - len(chunks)


class TestVerifySignature:
    def test_empty_state_rejects(self):
        assert _verify_signature(b"test", "") is False

    def test_no_secret_rejects_without_signature(self):
        """Fail closed: no app_secret configured means reject all webhooks."""
        _bot_state[1] = {"app_secret": "", "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        assert _verify_signature(b"test", "") is False

    def test_no_secret_rejects_with_signature(self):
        """Fail closed: no app_secret configured means reject even with signature."""
        _bot_state[1] = {"app_secret": "", "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        assert _verify_signature(b"test", "sha256=anything") is False

    def test_valid_signature_passes(self):
        secret = "my_app_secret"
        _bot_state[1] = {"app_secret": secret, "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        body = b'{"object":"page"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig) is True

    def test_invalid_signature_fails(self):
        _bot_state[1] = {"app_secret": "secret", "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        assert _verify_signature(b"test", "sha256=0000") is False

    def test_missing_signature_with_secret_fails(self):
        _bot_state[1] = {"app_secret": "secret", "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        assert _verify_signature(b"test", "") is False

    def test_wrong_algo_prefix_fails(self):
        _bot_state[1] = {"app_secret": "secret", "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        assert _verify_signature(b"test", "md5=abc") is False

    def test_multiple_bots_any_secret_matches(self):
        secret1 = "secret_one"
        secret2 = "secret_two"
        _bot_state[1] = {"app_secret": secret1, "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        _bot_state[2] = {"app_secret": secret2, "page_access_token": "t", "instance": None, "webhook_verify_token": "v"}
        body = b"payload"
        sig = "sha256=" + hmac.new(secret2.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig) is True


class TestStateManagement:
    def test_get_state_creates_default(self):
        state = _get_state(42)
        assert state["page_access_token"] is None
        assert state["app_secret"] is None
        assert state["page_id"] is None

    def test_get_state_returns_same_dict(self):
        s1 = _get_state(1)
        s1["page_access_token"] = "tok"
        s2 = _get_state(1)
        assert s2["page_access_token"] == "tok"

    def test_cleanup_removes_state(self):
        _get_state(5)
        assert 5 in _bot_state
        cleanup_bot_state(5)
        assert 5 not in _bot_state

    def test_cleanup_nonexistent_is_noop(self):
        cleanup_bot_state(999)  # should not raise


class TestAdapterProperties:
    def test_platform_type(self):
        adapter = MessengerAdapter()
        assert adapter.platform_type.value == "messenger"

    def test_capabilities(self):
        cap = MessengerAdapter().capabilities
        assert cap.supports_groups is False
        assert cap.supports_media is True
        assert cap.supports_file_send is True
        assert cap.supports_reactions is True
        assert cap.supports_read_receipts is True
        assert cap.supports_typing_indicator is True
        assert cap.supports_history_sync is False
        assert cap.supports_contacts_list is False
        assert cap.auth_method.value == "api_token"
        assert cap.max_message_length == 2000


# ---------------------------------------------------------------------------
# Integration tests — webhook endpoints via TestClient
# ---------------------------------------------------------------------------

class TestWebhookVerification:
    def test_webhook_verify_success(self, client):
        """Meta sends GET with hub.mode, hub.verify_token, hub.challenge."""
        # Register a bot so verify token exists
        _bot_state[1] = {
            "app_secret": "s",
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "my_verify_token",
        }

        resp = client.get("/webhooks/messenger", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "my_verify_token",
            "hub.challenge": "challenge_string_123",
        })
        assert resp.status_code == 200
        assert resp.text == "challenge_string_123"

    def test_webhook_verify_wrong_token(self, client):
        _bot_state[1] = {
            "app_secret": "s",
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "correct_token",
        }

        resp = client.get("/webhooks/messenger", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "challenge",
        })
        assert resp.status_code == 403

    def test_webhook_verify_missing_params(self, client):
        resp = client.get("/webhooks/messenger")
        assert resp.status_code == 400

    def test_webhook_verify_no_bots_registered(self, client):
        resp = client.get("/webhooks/messenger", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "any",
            "hub.challenge": "challenge",
        })
        assert resp.status_code == 403


class TestWebhookReceive:
    def test_non_page_object_returns_200(self, client):
        """Non-page events should be accepted but ignored."""
        secret = "test_secret"
        _bot_state[1] = {
            "app_secret": secret,
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "v",
        }

        body = json.dumps({"object": "instagram"}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        resp = client.post(
            "/webhooks/messenger",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200

    def test_valid_page_event_returns_200(self, client):
        """Valid page events return EVENT_RECEIVED immediately."""
        secret = "test_secret"
        _bot_state[1] = {
            "app_secret": secret,
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "v",
        }

        body = json.dumps({"object": "page", "entry": []}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        resp = client.post(
            "/webhooks/messenger",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200
        assert resp.text == "EVENT_RECEIVED"

    def test_no_secret_rejects(self, client):
        """Webhooks should be rejected when no app_secret is configured."""
        _bot_state[1] = {
            "app_secret": "",
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "v",
        }

        resp = client.post(
            "/webhooks/messenger",
            json={"object": "page", "entry": []},
        )
        assert resp.status_code == 403

    def test_signature_verification_rejects_bad_sig(self, client):
        """When app_secret is set, bad signatures are rejected."""
        _bot_state[1] = {
            "app_secret": "my_secret",
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "v",
        }

        resp = client.post(
            "/webhooks/messenger",
            json={"object": "page", "entry": []},
            headers={"X-Hub-Signature-256": "sha256=invalid"},
        )
        assert resp.status_code == 403

    def test_signature_verification_accepts_valid_sig(self, client):
        """When app_secret is set, valid signatures pass."""
        secret = "my_secret"
        _bot_state[1] = {
            "app_secret": secret,
            "page_access_token": "tok",
            "instance": None,
            "webhook_verify_token": "v",
        }

        body = json.dumps({"object": "page", "entry": []}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        resp = client.post(
            "/webhooks/messenger",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Async tests — adapter run, send, message handling
# ---------------------------------------------------------------------------

class TestAdapterRun:
    @pytest.mark.asyncio
    async def test_run_fails_without_token(self):
        """run() should fail gracefully if no token is configured."""
        adapter = MessengerAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 1
        instance.config = {}  # no token
        instance.is_running = True
        instance.notify_status = AsyncMock()

        await adapter.run(instance)

        instance.notify_status.assert_called_once()
        call_args = instance.notify_status.call_args[0][0]
        assert "error" in call_args
        assert instance.is_running is False

    @pytest.mark.asyncio
    async def test_run_fails_with_invalid_token(self):
        """run() should fail if Meta API rejects the token."""
        adapter = MessengerAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 2
        instance.config = {"api_key": "bad_token", "app_secret": "s", "webhook_verify_token": "v"}
        instance.is_running = True
        instance.notify_status = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": {"message": "Invalid OAuth token"}}

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await adapter.run(instance)

        assert instance.is_running is False
        calls = instance.notify_status.call_args_list
        assert any("Invalid" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_run_connects_with_valid_token(self):
        """run() should connect and enter main loop with valid token."""
        adapter = MessengerAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 3
        instance.config = {"api_key": "valid_token", "app_secret": "s", "webhook_verify_token": "v"}
        instance.is_running = True
        instance.stopped_by_user = False
        instance.notify_status = AsyncMock()
        instance.has_outbound_messages.return_value = False

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"name": "Test Page", "id": "12345"}

        # Stop the loop after one iteration
        call_count = 0
        original_sleep = asyncio.sleep

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                instance.is_running = False
            await original_sleep(0)

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with patch("app.platforms.messenger.adapter.asyncio.sleep", fake_sleep):
                await adapter.run(instance)

        # Check connected status was sent
        status_calls = [c[0][0] for c in instance.notify_status.call_args_list]
        assert any(s.get("connected") is True for s in status_calls)
        assert any("Test Page" in s.get("message", "") for s in status_calls)
        assert instance.whatsapp_connected is False  # cleaned up after loop exits

    @pytest.mark.asyncio
    async def test_run_processes_outbound_queue(self):
        """run() should send messages from the outbound queue."""
        adapter = MessengerAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 4
        instance.config = {"api_key": "valid_token", "app_secret": "s", "webhook_verify_token": "v"}
        instance.is_running = True
        instance.stopped_by_user = False
        instance.notify_status = AsyncMock()

        # First call: has messages. Second: no messages (then stop).
        instance.has_outbound_messages.side_effect = [True, False]
        instance.get_outbound_messages.return_value = [
            {"chat_id": "user123", "chat_name": "User", "message": "Hello!"}
        ]

        page_resp = MagicMock()
        page_resp.status_code = 200
        page_resp.json.return_value = {"name": "Test Page", "id": "12345"}

        send_resp = MagicMock()
        send_resp.status_code = 200
        send_resp.json.return_value = {"message_id": "mid.xxx"}

        call_count = 0
        all_posts = []
        _real_sleep = asyncio.sleep

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                instance.is_running = False
            await _real_sleep(0)

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = page_resp
            mock_client.post.side_effect = lambda *a, **kw: (all_posts.append(kw), send_resp)[1]
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with patch("app.platforms.messenger.adapter.asyncio.sleep", fake_sleep):
                await adapter.run(instance)

        # Verify send_message was called (at least one POST for the outbound message)
        assert len(all_posts) >= 1
        # Find the send call (has json with recipient)
        send_calls = [p for p in all_posts if "json" in p and "recipient" in p.get("json", {})]
        assert len(send_calls) == 1
        assert send_calls[0]["json"]["recipient"]["id"] == "user123"


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_success(self):
        adapter = MessengerAdapter()
        _bot_state[10] = {
            "page_access_token": "tok",
            "app_secret": "",
            "instance": None,
            "webhook_verify_token": "v",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message_id": "mid.123"}

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await adapter.send_message(10, "user_123", "User", "Hello!")

        assert result is True
        # Verify correct payload
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["recipient"]["id"] == "user_123"
        assert payload["message"]["text"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_message_no_token(self):
        adapter = MessengerAdapter()
        result = await adapter.send_message(999, "user", "User", "Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_api_error(self):
        adapter = MessengerAdapter()
        _bot_state[11] = {
            "page_access_token": "tok",
            "app_secret": "",
            "instance": None,
            "webhook_verify_token": "v",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": {"message": "Rate limited"}}
        mock_resp.text = "Rate limited"

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await adapter.send_message(11, "user_123", "User", "Hello!")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_long_message_splits(self):
        adapter = MessengerAdapter()
        _bot_state[12] = {
            "page_access_token": "tok",
            "app_secret": "",
            "instance": None,
            "webhook_verify_token": "v",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message_id": "mid.x"}

        long_msg = "word " * 500  # ~2500 chars

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await adapter.send_message(12, "user_123", "User", long_msg)

        assert result is True
        # Should have sent multiple chunks
        assert mock_client.post.call_count >= 2


class TestSendFile:
    @pytest.mark.asyncio
    async def test_send_file_success(self, tmp_path):
        adapter = MessengerAdapter()
        _bot_state[20] = {
            "page_access_token": "tok",
            "app_secret": "",
            "instance": None,
            "webhook_verify_token": "v",
        }

        # Create a test file
        test_file = tmp_path / "test.jpg"
        test_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message_id": "mid.file"}

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await adapter.send_file(
                20, "user_123", str(test_file),
                caption="A photo", file_type="image/jpeg", chat_name="User",
            )

        assert result is True
        # First post = file upload, second post = caption text
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_send_file_no_token(self):
        adapter = MessengerAdapter()
        result = await adapter.send_file(999, "user", "/nonexistent", file_type="image/png")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_file_not_found(self):
        adapter = MessengerAdapter()
        _bot_state[21] = {
            "page_access_token": "tok",
            "app_secret": "",
            "instance": None,
            "webhook_verify_token": "v",
        }
        result = await adapter.send_file(21, "user", "/nonexistent/file.jpg", file_type="image/jpeg")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_file_no_caption(self, tmp_path):
        adapter = MessengerAdapter()
        _bot_state[22] = {
            "page_access_token": "tok",
            "app_secret": "",
            "instance": None,
            "webhook_verify_token": "v",
        }

        test_file = tmp_path / "doc.pdf"
        test_file.write_bytes(b"%PDF-1.4" + b"\x00" * 100)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message_id": "mid.file"}

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await adapter.send_file(
                22, "user_123", str(test_file),
                file_type="application/pdf",
            )

        assert result is True
        # Only one post — no caption
        assert mock_client.post.call_count == 1


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_handle_incoming_message(self, db):
        """Test full inbound message handling with DB storage."""
        from app.database import Conversation, Message

        instance = MagicMock()
        instance.bot_profile_id = 30
        instance.is_running = True
        instance.ai_response_enabled = False  # Don't try AI
        instance.config = {"bot_name": "TestBot", "ai_provider": "openai", "api_key": "k", "model": "gpt-4o-mini"}

        _bot_state[30] = {
            "page_access_token": "tok",
            "app_secret": "s",
            "instance": instance,
            "webhook_verify_token": "v",
            "page_id": "page_789",
        }

        event = {
            "sender": {"id": "sender_456"},
            "recipient": {"id": "page_789"},
            "timestamp": int(time.time() * 1000),
            "message": {
                "mid": "mid.unique123",
                "text": "Hello bot!",
            },
        }

        # Mock profile fetch
        profile_resp = MagicMock()
        profile_resp.status_code = 200
        profile_resp.json.return_value = {"first_name": "John", "last_name": "Doe"}

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = profile_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            # Patch get_db_session to use our test db
            with patch("app.platforms.messenger.adapter.get_db_session") as mock_db_ctx:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(return_value=db)
                ctx.__exit__ = MagicMock(return_value=False)
                mock_db_ctx.return_value = ctx

                # Patch WebSocket broadcast (no event loop in test)
                with patch("app.platforms.messenger.adapter.broadcast_user_message"):
                    await _handle_message(30, instance, event, "sender_456")

        # Verify conversation was created
        conv = db.query(Conversation).filter(
            Conversation.bot_profile_id == 30,
            Conversation.chat_id == "sender_456",
        ).first()
        assert conv is not None
        assert conv.chat_name == "John Doe"
        assert conv.is_group is False

        # Verify message was saved
        msg = db.query(Message).filter(
            Message.conversation_id == conv.id,
            Message.whatsapp_message_id == "mid.unique123",
        ).first()
        assert msg is not None
        assert msg.content == "Hello bot!"
        assert msg.role == "user"
        assert msg.sender_name == "John Doe"

    @pytest.mark.asyncio
    async def test_duplicate_message_skipped(self, db):
        """Test that duplicate messages are skipped."""
        from app.database import Conversation, Message

        instance = MagicMock()
        instance.bot_profile_id = 31
        instance.is_running = True
        instance.ai_response_enabled = False
        instance.config = {"bot_name": "TestBot"}

        _bot_state[31] = {
            "page_access_token": "tok",
            "app_secret": "s",
            "instance": instance,
            "webhook_verify_token": "v",
            "page_id": "page_789",
        }

        event = {
            "sender": {"id": "sender_dup"},
            "recipient": {"id": "page_789"},
            "timestamp": int(time.time() * 1000),
            "message": {
                "mid": "mid.dup_test",
                "text": "Duplicate me!",
            },
        }

        profile_resp = MagicMock()
        profile_resp.status_code = 200
        profile_resp.json.return_value = {"first_name": "Jane", "last_name": "Doe"}

        with patch("app.platforms.messenger.adapter.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = profile_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with patch("app.platforms.messenger.adapter.get_db_session") as mock_db_ctx:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(return_value=db)
                ctx.__exit__ = MagicMock(return_value=False)
                mock_db_ctx.return_value = ctx

                with patch("app.platforms.messenger.adapter.broadcast_user_message"):
                    # Send message first time
                    await _handle_message(31, instance, event, "sender_dup")
                    # Send same message again
                    # Add a fake assistant response so dedup fully skips
                    conv = db.query(Conversation).filter(
                        Conversation.chat_id == "sender_dup"
                    ).first()
                    from app.platforms.message_handler import save_assistant_message
                    save_assistant_message(db, conv.id, "Bot reply")
                    db.commit()

                    await _handle_message(31, instance, event, "sender_dup")

        # Should only have 1 user message
        msgs = db.query(Message).filter(
            Message.role == "user",
            Message.whatsapp_message_id == "mid.dup_test",
        ).all()
        assert len(msgs) == 1


class TestProcessWebhookEntries:
    @pytest.mark.asyncio
    async def test_skips_echo_messages(self):
        """is_echo messages (sent by page) should be skipped."""
        instance = MagicMock()
        instance.is_running = True
        _bot_state[40] = {
            "page_access_token": "tok",
            "app_secret": "s",
            "instance": instance,
            "webhook_verify_token": "v",
            "page_id": "page_40",
        }

        entries = [{
            "id": "page_40",
            "messaging": [{
                "sender": {"id": "page_40"},
                "recipient": {"id": "user_123"},
                "message": {
                    "is_echo": True,
                    "mid": "mid.echo",
                    "text": "My own message",
                },
            }],
        }]

        with patch("app.platforms.messenger.adapter._handle_message") as mock_handle:
            await _process_webhook_entries(entries)
            mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_delivery_receipt(self):
        instance = MagicMock()
        instance.is_running = True
        _bot_state[41] = {
            "page_access_token": "tok",
            "app_secret": "s",
            "instance": instance,
            "webhook_verify_token": "v",
            "page_id": "page_41",
        }

        entries = [{
            "id": "page_41",
            "messaging": [{
                "sender": {"id": "user_123"},
                "recipient": {"id": "page_41"},
                "delivery": {
                    "mids": ["mid.123"],
                    "watermark": 12345,
                },
            }],
        }]

        with patch("app.platforms.messenger.adapter._handle_delivery") as mock_delivery:
            await _process_webhook_entries(entries)
            mock_delivery.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_read_receipt(self):
        instance = MagicMock()
        instance.is_running = True
        _bot_state[42] = {
            "page_access_token": "tok",
            "app_secret": "s",
            "instance": instance,
            "webhook_verify_token": "v",
            "page_id": "page_42",
        }

        entries = [{
            "id": "page_42",
            "messaging": [{
                "sender": {"id": "user_123"},
                "recipient": {"id": "page_42"},
                "read": {
                    "watermark": 12345,
                },
            }],
        }]

        with patch("app.platforms.messenger.adapter._handle_read") as mock_read:
            await _process_webhook_entries(entries)
            mock_read.assert_called_once()


class TestCleanup:
    def test_cleanup_removes_bot_state(self):
        adapter = MessengerAdapter()
        _get_state(50)
        _bot_state[50]["page_access_token"] = "tok"
        adapter.cleanup(50)
        assert 50 not in _bot_state
