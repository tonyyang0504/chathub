"""
Tests for WeChat Platform Adapter.

Tests cover:
- Signature verification
- XML message parsing
- Webhook endpoint (GET verification, POST messages)
- Message processing flow (text, image, voice, events)
- Access token management
- Message splitting for long responses
- Customer service API message sending
- Outbound queue processing
- send_message / send_file public API
"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
from starlette.testclient import TestClient as StarletteTestClient

from app.platforms.wechat.adapter import (
    WeChatAdapter,
    AccessTokenManager,
    verify_signature,
    parse_xml_message,
    WECHAT_API_BASE,
    WECHAT_MAX_MESSAGE_LENGTH,
)
from app.platforms.base import PlatformType, AuthMethod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_signature(token: str, timestamp: str, nonce: str) -> str:
    """Compute the expected WeChat signature."""
    parts = sorted([token, timestamp, nonce])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def make_text_xml(from_user="oUser123", to_user="gh_bot", content="Hello", msg_id="100001"):
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"</xml>"
    )


def make_image_xml(from_user="oUser123", to_user="gh_bot", msg_id="100002"):
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[image]]></MsgType>"
        f"<PicUrl><![CDATA[http://example.com/pic.jpg]]></PicUrl>"
        f"<MediaId><![CDATA[media_abc]]></MediaId>"
        f"<MsgId>{msg_id}</MsgId>"
        f"</xml>"
    )


def make_event_xml(event="subscribe", from_user="oUser123", to_user="gh_bot"):
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[event]]></MsgType>"
        f"<Event><![CDATA[{event}]]></Event>"
        f"</xml>"
    )


def make_voice_xml(from_user="oUser123", to_user="gh_bot", recognition="", msg_id="100003"):
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[voice]]></MsgType>"
        f"<MediaId><![CDATA[voice_media_123]]></MediaId>"
        f"<Format><![CDATA[amr]]></Format>"
        f"<Recognition><![CDATA[{recognition}]]></Recognition>"
        f"<MsgId>{msg_id}</MsgId>"
        f"</xml>"
    )


def make_location_xml(from_user="oUser123", to_user="gh_bot", msg_id="100004"):
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[location]]></MsgType>"
        f"<Location_X>23.134521</Location_X>"
        f"<Location_Y>113.358803</Location_Y>"
        f"<Scale>20</Scale>"
        f"<Label><![CDATA[Guangzhou]]></Label>"
        f"<MsgId>{msg_id}</MsgId>"
        f"</xml>"
    )


def make_link_xml(from_user="oUser123", to_user="gh_bot", msg_id="100005"):
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[link]]></MsgType>"
        f"<Title><![CDATA[Test Article]]></Title>"
        f"<Description><![CDATA[A test article]]></Description>"
        f"<Url><![CDATA[http://example.com/article]]></Url>"
        f"<MsgId>{msg_id}</MsgId>"
        f"</xml>"
    )


# ---------------------------------------------------------------------------
# Signature Verification
# ---------------------------------------------------------------------------

class TestVerifySignature:

    def test_valid_signature(self):
        token, ts, nonce = "mytoken", "1234567890", "abc123"
        sig = make_signature(token, ts, nonce)
        assert verify_signature(token, ts, nonce, sig) is True

    def test_invalid_signature(self):
        assert verify_signature("tok", "123", "abc", "wrong_sig") is False

    def test_empty_token(self):
        sig = make_signature("", "123", "abc")
        assert verify_signature("", "123", "abc", sig) is True

    def test_sorting_order_matters(self):
        """Signature is SHA1 of sorted [token, timestamp, nonce]."""
        token, ts, nonce = "z_token", "a_timestamp", "m_nonce"
        sig = make_signature(token, ts, nonce)
        assert verify_signature(token, ts, nonce, sig) is True


# ---------------------------------------------------------------------------
# XML Parsing
# ---------------------------------------------------------------------------

class TestParseXmlMessage:

    def test_text_message(self):
        msg = parse_xml_message(make_text_xml(content="Hi there"))
        assert msg["MsgType"] == "text"
        assert msg["Content"] == "Hi there"
        assert msg["FromUserName"] == "oUser123"
        assert msg["ToUserName"] == "gh_bot"
        assert msg["MsgId"] == "100001"

    def test_image_message(self):
        msg = parse_xml_message(make_image_xml())
        assert msg["MsgType"] == "image"
        assert msg["PicUrl"] == "http://example.com/pic.jpg"
        assert msg["MediaId"] == "media_abc"

    def test_event_message(self):
        msg = parse_xml_message(make_event_xml("subscribe"))
        assert msg["MsgType"] == "event"
        assert msg["Event"] == "subscribe"
        assert msg.get("MsgId", "") == ""  # Events have no MsgId

    def test_voice_with_recognition(self):
        msg = parse_xml_message(make_voice_xml(recognition="Hello world"))
        assert msg["MsgType"] == "voice"
        assert msg["Recognition"] == "Hello world"
        assert msg["MediaId"] == "voice_media_123"

    def test_location_message(self):
        msg = parse_xml_message(make_location_xml())
        assert msg["MsgType"] == "location"
        assert msg["Location_X"] == "23.134521"
        assert msg["Location_Y"] == "113.358803"
        assert msg["Label"] == "Guangzhou"

    def test_link_message(self):
        msg = parse_xml_message(make_link_xml())
        assert msg["MsgType"] == "link"
        assert msg["Title"] == "Test Article"
        assert msg["Url"] == "http://example.com/article"

    def test_empty_fields(self):
        xml = "<xml><MsgType><![CDATA[text]]></MsgType><Content></Content></xml>"
        msg = parse_xml_message(xml)
        assert msg["MsgType"] == "text"
        assert msg["Content"] == ""

    def test_invalid_xml_raises(self):
        with pytest.raises(Exception):
            parse_xml_message("not xml at all")


# ---------------------------------------------------------------------------
# Adapter Properties
# ---------------------------------------------------------------------------

class TestWeChatAdapterProperties:

    def test_platform_type(self):
        adapter = WeChatAdapter()
        assert adapter.platform_type == PlatformType.WECHAT

    def test_capabilities(self):
        adapter = WeChatAdapter()
        cap = adapter.capabilities
        assert cap.auth_method == AuthMethod.CREDENTIALS
        assert cap.supports_groups is True
        assert cap.supports_media is True
        assert cap.supports_file_send is True
        assert cap.supports_reactions is False
        assert cap.supports_typing_indicator is False
        assert cap.supports_history_sync is False
        assert cap.max_message_length == 2048


# ---------------------------------------------------------------------------
# Access Token Manager
# ---------------------------------------------------------------------------

class TestAccessTokenManager:

    @pytest.mark.asyncio
    async def test_get_token_fetches_on_first_call(self):
        mgr = AccessTokenManager("app123", "secret456")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "tok_abc",
            "expires_in": 7200,
        }

        with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await mgr.get_token()
            assert token == "tok_abc"
            mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_token_caches(self):
        mgr = AccessTokenManager("app123", "secret456")
        mgr._token = "cached_tok"
        mgr._expires_at = time.time() + 3600  # Still valid

        token = await mgr.get_token()
        assert token == "cached_tok"

    @pytest.mark.asyncio
    async def test_get_token_refreshes_when_near_expiry(self):
        mgr = AccessTokenManager("app123", "secret456")
        mgr._token = "old_tok"
        mgr._expires_at = time.time() + 100  # Within 300s refresh window

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new_tok",
            "expires_in": 7200,
        }

        with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await mgr.get_token()
            assert token == "new_tok"

    @pytest.mark.asyncio
    async def test_get_token_raises_on_error(self):
        mgr = AccessTokenManager("bad_id", "bad_secret")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 40013,
            "errmsg": "invalid appid",
        }

        with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="invalid appid"):
                await mgr.get_token()


# ---------------------------------------------------------------------------
# Webhook Endpoint (via Starlette test client)
# ---------------------------------------------------------------------------

class TestWebhookEndpoint:
    """Test the webhook HTTP endpoint (GET verification + POST messages)."""

    TOKEN = "test_token"
    APP_ID = "wx_test_appid"
    APP_SECRET = "wx_test_secret"

    def _make_adapter_and_app(self):
        """Create an adapter with a Starlette app for testing."""
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        adapter = WeChatAdapter()
        wechat_token = self.TOKEN

        async def handle_verification(request: Request) -> PlainTextResponse:
            params = request.query_params
            sig = params.get("signature", "")
            ts = params.get("timestamp", "")
            nonce = params.get("nonce", "")
            echostr = params.get("echostr", "")
            if verify_signature(wechat_token, ts, nonce, sig):
                return PlainTextResponse(echostr)
            return PlainTextResponse("Verification failed", status_code=403)

        async def handle_message(request: Request) -> PlainTextResponse:
            params = request.query_params
            sig = params.get("signature", "")
            ts = params.get("timestamp", "")
            nonce = params.get("nonce", "")
            if not verify_signature(wechat_token, ts, nonce, sig):
                return PlainTextResponse("Invalid signature", status_code=403)
            return PlainTextResponse("success")

        async def webhook_endpoint(request: Request) -> PlainTextResponse:
            if request.method == "GET":
                return await handle_verification(request)
            return await handle_message(request)

        starlette_app = Starlette(
            routes=[Route("/wechat/webhook", webhook_endpoint, methods=["GET", "POST"])]
        )
        return adapter, starlette_app

    def _sign_params(self, timestamp="1234567890", nonce="testnonce"):
        sig = make_signature(self.TOKEN, timestamp, nonce)
        return {"signature": sig, "timestamp": timestamp, "nonce": nonce}

    def test_get_verification_valid(self):
        _, starlette_app = self._make_adapter_and_app()
        client = StarletteTestClient(starlette_app)

        params = self._sign_params()
        params["echostr"] = "echo_test_12345"

        resp = client.get("/wechat/webhook", params=params)
        assert resp.status_code == 200
        assert resp.text == "echo_test_12345"

    def test_get_verification_invalid_sig(self):
        _, starlette_app = self._make_adapter_and_app()
        client = StarletteTestClient(starlette_app)

        resp = client.get("/wechat/webhook", params={
            "signature": "bad_signature",
            "timestamp": "123",
            "nonce": "abc",
            "echostr": "test",
        })
        assert resp.status_code == 403

    def test_post_message_valid_signature(self):
        _, starlette_app = self._make_adapter_and_app()
        client = StarletteTestClient(starlette_app)

        params = self._sign_params()
        xml = make_text_xml(content="Hello bot")

        resp = client.post("/wechat/webhook", params=params, content=xml)
        assert resp.status_code == 200
        assert resp.text == "success"

    def test_post_message_invalid_signature(self):
        _, starlette_app = self._make_adapter_and_app()
        client = StarletteTestClient(starlette_app)

        xml = make_text_xml(content="Hello")
        resp = client.post("/wechat/webhook", params={
            "signature": "wrong",
            "timestamp": "123",
            "nonce": "abc",
        }, content=xml)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Message Splitting
# ---------------------------------------------------------------------------

class TestMessageSplitting:

    @pytest.mark.asyncio
    async def test_short_message_no_split(self):
        adapter = WeChatAdapter()
        adapter._token_managers[1] = MagicMock()

        with patch.object(adapter, "_send_customer_service_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            await adapter._send_text_with_splitting(1, "user1", "Short message")
            mock_send.assert_called_once_with(1, "user1", "Short message")

    @pytest.mark.asyncio
    async def test_long_message_splits(self):
        adapter = WeChatAdapter()

        calls = []
        async def capture_send(bot_id, to_user, content):
            calls.append(content)
            return True

        with patch.object(adapter, "_send_customer_service_message", side_effect=capture_send):
            # Create a message that's 3x the limit
            long_msg = "A" * WECHAT_MAX_MESSAGE_LENGTH + "\n\n" + "B" * WECHAT_MAX_MESSAGE_LENGTH + "\n\n" + "C" * 100
            await adapter._send_text_with_splitting(1, "user1", long_msg)

            assert len(calls) >= 2
            # All content should be preserved
            recombined = "".join(calls)
            assert "A" * 100 in recombined
            assert "B" * 100 in recombined
            assert "C" * 100 in recombined

    @pytest.mark.asyncio
    async def test_split_at_paragraph_boundary(self):
        adapter = WeChatAdapter()

        calls = []
        async def capture_send(bot_id, to_user, content):
            calls.append(content)
            return True

        with patch.object(adapter, "_send_customer_service_message", side_effect=capture_send):
            # Build message with paragraph break near the limit
            part1 = "A" * (WECHAT_MAX_MESSAGE_LENGTH - 100)
            part2 = "B" * 200
            long_msg = part1 + "\n\n" + part2

            await adapter._send_text_with_splitting(1, "user1", long_msg)

            assert len(calls) == 2
            assert calls[0] == part1  # Split at \n\n boundary
            assert calls[1] == part2


# ---------------------------------------------------------------------------
# Customer Service Message Sending
# ---------------------------------------------------------------------------

class TestSendCustomerServiceMessage:

    @pytest.mark.asyncio
    async def test_send_success(self):
        adapter = WeChatAdapter()
        token_mgr = AsyncMock()
        token_mgr.get_token.return_value = "test_access_token"
        adapter._token_managers[1] = token_mgr
        adapter._bot_configs[1] = {"wechat_app_id": "id", "wechat_app_secret": "sec"}

        mock_response = MagicMock()
        mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

        with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter._send_customer_service_message(1, "oUser1", "Hello!")
            assert result is True

            # Verify the payload
            call_args = mock_client.post.call_args
            assert "message/custom/send" in call_args[0][0]
            payload = call_args[1]["json"]
            assert payload["touser"] == "oUser1"
            assert payload["msgtype"] == "text"
            assert payload["text"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_failure(self):
        adapter = WeChatAdapter()
        token_mgr = AsyncMock()
        token_mgr.get_token.return_value = "test_access_token"
        adapter._token_managers[1] = token_mgr
        adapter._bot_configs[1] = {"wechat_app_id": "id", "wechat_app_secret": "sec"}

        mock_response = MagicMock()
        mock_response.json.return_value = {"errcode": 45015, "errmsg": "response out of time limit"}

        with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter._send_customer_service_message(1, "oUser1", "Hello!")
            assert result is False


# ---------------------------------------------------------------------------
# send_message / send_file public API
# ---------------------------------------------------------------------------

class TestPublicSendAPI:

    @pytest.mark.asyncio
    async def test_send_message_delegates(self):
        adapter = WeChatAdapter()
        with patch.object(adapter, "_send_customer_service_message", new_callable=AsyncMock, return_value=True) as mock:
            result = await adapter.send_message(1, "oUser1", "UserName", "Test msg")
            assert result is True
            mock.assert_called_once_with(1, "oUser1", "Test msg")

    @pytest.mark.asyncio
    async def test_send_file_image(self):
        adapter = WeChatAdapter()
        token_mgr = AsyncMock()
        token_mgr.get_token.return_value = "tok123"
        adapter._token_managers[1] = token_mgr
        adapter._bot_configs[1] = {"wechat_app_id": "id", "wechat_app_secret": "sec"}

        # Mock upload response + send response
        upload_response = MagicMock()
        upload_response.json.return_value = {"media_id": "uploaded_media_1", "type": "image"}
        send_response = MagicMock()
        send_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # Fake JPEG
            tmp_path = f.name

        try:
            # Each `async with httpx.AsyncClient()` creates a separate context,
            # so we need the mock class to return a fresh mock each time.
            # Both share the same post tracker via side_effect list.
            post_responses = [upload_response, send_response]
            post_call_log = []

            async def mock_post(*args, **kwargs):
                post_call_log.append((args, kwargs))
                return post_responses[len(post_call_log) - 1]

            def make_mock_client():
                mc = AsyncMock()
                mc.post = mock_post
                return mc

            with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(side_effect=lambda: make_mock_client())
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                # Also mock the caption send
                with patch.object(adapter, "_send_customer_service_message", new_callable=AsyncMock, return_value=True):
                    result = await adapter.send_file(
                        1, "oUser1", tmp_path, caption="Look!", file_type="image/jpeg"
                    )

                assert result is True
                assert len(post_call_log) == 2

                # First call: upload
                assert "media/upload" in post_call_log[0][0][0]

                # Second call: send media
                payload = post_call_log[1][1]["json"]
                assert payload["msgtype"] == "image"
                assert payload["image"]["media_id"] == "uploaded_media_1"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_send_file_unsupported_type_sends_caption(self):
        adapter = WeChatAdapter()
        token_mgr = AsyncMock()
        token_mgr.get_token.return_value = "tok123"
        adapter._token_managers[1] = token_mgr
        adapter._bot_configs[1] = {"wechat_app_id": "id", "wechat_app_secret": "sec"}

        with patch.object(adapter, "_send_customer_service_message", new_callable=AsyncMock, return_value=True) as mock:
            result = await adapter.send_file(
                1, "oUser1", "/fake/file.zip", caption="Here's the file",
                file_type="application/zip"
            )
            # Returns False (unsupported type) but still sends caption
            assert result is False
            mock.assert_called_once()
            assert "file.zip" in mock.call_args[0][2]

    @pytest.mark.asyncio
    async def test_send_file_not_found(self):
        adapter = WeChatAdapter()
        token_mgr = AsyncMock()
        token_mgr.get_token.return_value = "tok123"
        adapter._token_managers[1] = token_mgr
        adapter._bot_configs[1] = {"wechat_app_id": "id", "wechat_app_secret": "sec"}

        result = await adapter.send_file(
            1, "oUser1", "/nonexistent/file.jpg", file_type="image/jpeg"
        )
        assert result is False


# ---------------------------------------------------------------------------
# Incoming Message Processing
# ---------------------------------------------------------------------------

class TestProcessIncomingMessage:
    """Test _process_incoming_message with mocked DB/AI.

    Since _process_incoming_message uses lazy imports (from ... import),
    we must patch at the source module (app.platforms.message_handler),
    not at app.platforms.wechat.adapter.
    """

    MH = "app.platforms.message_handler"  # Patch target for message_handler functions

    def _make_instance(self, ai_enabled=True):
        instance = MagicMock()
        instance.bot_profile_id = 1
        instance.ai_response_enabled = ai_enabled
        instance.config = {
            "wechat_app_id": "wx_test",
            "wechat_app_secret": "secret_test",
            "wechat_token": "tok",
            "ai_provider": "openai",
            "api_key_encrypted": "encrypted_key",
            "model": "gpt-4",
            "system_prompt": "You are helpful.",
            "name": "TestBot",
        }
        return instance

    @pytest.mark.asyncio
    async def test_text_message_saves_to_db(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=False)

        msg = parse_xml_message(make_text_xml(content="Hi bot"))

        mock_conv = MagicMock(id=10, chat_name="oUser123", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="Hi bot", sender_name="oUser123",
            sender_id="oUser123", sender_profile_pic="",
            timestamp=MagicMock(isoformat=MagicMock(return_value="2025-01-01T00:00:00"))
        )

        with patch(f"{self.MH}.get_db_session") as mock_db_ctx, \
             patch(f"{self.MH}.find_or_create_conversation", return_value=mock_conv) as mock_find, \
             patch(f"{self.MH}.is_duplicate_message", return_value=(False, None)), \
             patch(f"{self.MH}.save_user_message", return_value=mock_user_msg) as mock_save, \
             patch(f"{self.MH}.update_conversation_stats") as mock_stats, \
             patch(f"{self.MH}.broadcast_user_message") as mock_broadcast, \
             patch(f"{self.MH}.is_human_takeover_active", return_value=False):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "text", "100001"
            )

            # Verify conversation was found/created
            mock_find.assert_called_once_with(
                mock_db, 1, chat_id="oUser123", chat_name="oUser123", is_group=False
            )
            # Verify message was saved
            mock_save.assert_called_once()
            save_kwargs = mock_save.call_args
            assert save_kwargs[0][2] == "Hi bot"  # content
            assert save_kwargs[1]["platform_message_id"] == "100001"
            # Verify stats updated
            mock_stats.assert_called_once()
            # Verify WebSocket broadcast
            mock_broadcast.assert_called_once()

    @pytest.mark.asyncio
    async def test_text_message_generates_ai_response(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=True)

        msg = parse_xml_message(make_text_xml(content="What is AI?"))

        mock_conv = MagicMock(id=10, chat_name="oUser123", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="What is AI?", media_analysis=None,
            sender_name="oUser123", sender_id="oUser123",
            sender_profile_pic="",
            timestamp=MagicMock(isoformat=MagicMock(return_value="2025-01-01T00:00:00"))
        )

        @dataclass
        class FakeAIResponse:
            content: str = "AI is artificial intelligence."

        mock_ai = MagicMock()
        mock_ai.chat_completion.return_value = FakeAIResponse()

        mock_ai_msg = MagicMock(
            id=101, content="AI is artificial intelligence.",
            sender_name="TestBot", sender_id="",
            sender_profile_pic="/static/images/ai-agent.svg",
            timestamp=MagicMock(isoformat=MagicMock(return_value="2025-01-01T00:00:01"))
        )

        with patch(f"{self.MH}.get_db_session") as mock_db_ctx, \
             patch(f"{self.MH}.find_or_create_conversation", return_value=mock_conv), \
             patch(f"{self.MH}.is_duplicate_message", return_value=(False, None)), \
             patch(f"{self.MH}.save_user_message", return_value=mock_user_msg), \
             patch(f"{self.MH}.update_conversation_stats"), \
             patch(f"{self.MH}.broadcast_user_message"), \
             patch(f"{self.MH}.broadcast_assistant_message") as mock_bc_ai, \
             patch(f"{self.MH}.broadcast_typing"), \
             patch(f"{self.MH}.is_human_takeover_active", return_value=False), \
             patch(f"{self.MH}.build_ai_messages", return_value=[{"role": "user", "content": "What is AI?"}]), \
             patch(f"{self.MH}.save_assistant_message", return_value=mock_ai_msg) as mock_save_ai, \
             patch("app.auth.utils.decrypt_string", return_value="real_api_key"), \
             patch("app.ai.factory.get_ai_provider", return_value=mock_ai), \
             patch("asyncio.to_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(adapter, "_send_text_with_splitting", new_callable=AsyncMock) as mock_send:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "text", "100001"
            )

            # AI response saved
            mock_save_ai.assert_called_once()
            assert mock_save_ai.call_args[0][2] == "AI is artificial intelligence."
            # Broadcast AI response
            mock_bc_ai.assert_called_once()
            # Sent to user via WeChat
            mock_send.assert_called_once_with(1, "oUser123", "AI is artificial intelligence.")

    @pytest.mark.asyncio
    async def test_duplicate_message_skipped(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=True)

        msg = parse_xml_message(make_text_xml(content="Dup"))
        mock_conv = MagicMock(id=10)
        mock_existing = MagicMock(id=50)

        with patch(f"{self.MH}.get_db_session") as mock_db_ctx, \
             patch(f"{self.MH}.find_or_create_conversation", return_value=mock_conv), \
             patch(f"{self.MH}.is_duplicate_message", return_value=(True, mock_existing)), \
             patch(f"{self.MH}.has_response_after", return_value=True), \
             patch(f"{self.MH}.save_user_message") as mock_save:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "text", "100001"
            )

            # Message should NOT be saved
            mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_human_takeover_skips_ai(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=True)

        msg = parse_xml_message(make_text_xml(content="Help"))
        mock_conv = MagicMock(id=10, chat_name="oUser123", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="Help", sender_name="oUser123",
            sender_id="oUser123", sender_profile_pic="",
            timestamp=MagicMock(isoformat=MagicMock(return_value="2025-01-01T00:00:00"))
        )

        with patch(f"{self.MH}.get_db_session") as mock_db_ctx, \
             patch(f"{self.MH}.find_or_create_conversation", return_value=mock_conv), \
             patch(f"{self.MH}.is_duplicate_message", return_value=(False, None)), \
             patch(f"{self.MH}.save_user_message", return_value=mock_user_msg), \
             patch(f"{self.MH}.update_conversation_stats"), \
             patch(f"{self.MH}.broadcast_user_message"), \
             patch(f"{self.MH}.is_human_takeover_active", return_value=True), \
             patch(f"{self.MH}.build_ai_messages") as mock_ai_msgs:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "text", "100001"
            )

            # AI should NOT be called
            mock_ai_msgs.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_subscribe_processed(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=False)

        msg = parse_xml_message(make_event_xml("subscribe"))
        mock_conv = MagicMock(id=10, chat_name="oUser123", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="[User subscribed]",
            sender_name="oUser123", sender_id="oUser123",
            sender_profile_pic="",
            timestamp=MagicMock(isoformat=MagicMock(return_value="2025-01-01T00:00:00"))
        )

        with patch(f"{self.MH}.get_db_session") as mock_db_ctx, \
             patch(f"{self.MH}.find_or_create_conversation", return_value=mock_conv), \
             patch(f"{self.MH}.is_duplicate_message", return_value=(False, None)), \
             patch(f"{self.MH}.save_user_message", return_value=mock_user_msg) as mock_save, \
             patch(f"{self.MH}.update_conversation_stats"), \
             patch(f"{self.MH}.broadcast_user_message"), \
             patch(f"{self.MH}.is_human_takeover_active", return_value=False):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "event", ""
            )

            # Subscribe event should be saved
            mock_save.assert_called_once()
            assert mock_save.call_args[0][2] == "[User subscribed]"

    @pytest.mark.asyncio
    async def test_event_unsubscribe_ignored(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=False)

        msg = parse_xml_message(make_event_xml("unsubscribe"))

        with patch(f"{self.MH}.save_user_message") as mock_save:
            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "event", ""
            )

            # Unsubscribe should NOT create conversation or save message
            mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_message_type_ignored(self):
        adapter = WeChatAdapter()
        instance = self._make_instance(ai_enabled=False)

        msg = {"MsgType": "unknown_type"}

        with patch(f"{self.MH}.save_user_message") as mock_save:
            await adapter._process_incoming_message(
                instance, msg, "oUser123", "gh_bot", "unknown_type", "999"
            )

            mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:

    def test_cleanup_removes_state(self):
        adapter = WeChatAdapter()
        adapter._token_managers[1] = MagicMock()
        adapter._bot_configs[1] = {"some": "config"}

        adapter.cleanup(1)

        assert 1 not in adapter._token_managers
        assert 1 not in adapter._bot_configs

    def test_cleanup_nonexistent_bot_noop(self):
        adapter = WeChatAdapter()
        adapter.cleanup(999)  # Should not raise


# ---------------------------------------------------------------------------
# Run method (start/stop lifecycle)
# ---------------------------------------------------------------------------

class TestRunLifecycle:

    @pytest.mark.asyncio
    async def test_run_fails_without_credentials(self):
        adapter = WeChatAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 1
        instance.config = {}  # No app_id or app_secret
        instance.notify_status = AsyncMock()

        await adapter.run(instance)

        # Should set error and notify status
        assert instance.error == "WeChat AppID and AppSecret are required"
        instance.notify_status.assert_called()

    @pytest.mark.asyncio
    async def test_run_fails_on_bad_credentials(self):
        adapter = WeChatAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 2
        instance.config = {
            "wechat_app_id": "bad_id",
            "wechat_app_secret": "bad_secret",
            "wechat_token": "tok",
        }
        instance.notify_status = AsyncMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"errcode": 40013, "errmsg": "invalid appid"}

        with patch("app.platforms.wechat.adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.run(instance)

        assert "authentication failed" in instance.error.lower()
        instance.notify_status.assert_called()
