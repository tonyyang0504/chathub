"""
Tests for the Telegram Platform Adapter.

Tests cover:
- Pure utility functions (_parse_credentials, _split_message, _get_user_display_name)
- Adapter class properties (platform_type, capabilities)
- send_message / send_file via mocked Telegram API
- run() startup error handling (missing token, invalid token)
- handle_message full flow (DB save, dedup, AI response, Telegram reply)
- handle_media flow (download, save, AI analysis, reply)
- Group chat filtering (disabled, mention required)
- Human takeover suppression
- AI response disabled suppression
"""

import asyncio
import base64
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from app.platforms.telegram.adapter import (
    TelegramAdapter,
    _parse_credentials,
    _split_message,
    _get_user_display_name,
    _active_apps,
    _HandlerContext,
)
from app.platforms.base import PlatformType, AuthMethod


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestParseCredentials:
    def test_with_separator(self):
        ai_key, tg_token = _parse_credentials("sk-abc123|||12345:AABB")
        assert ai_key == "sk-abc123"
        assert tg_token == "12345:AABB"

    def test_without_separator(self):
        ai_key, tg_token = _parse_credentials("sk-abc123")
        assert ai_key == "sk-abc123"
        assert tg_token is None

    def test_with_spaces_around_separator(self):
        ai_key, tg_token = _parse_credentials("sk-abc123 ||| 12345:AABB")
        assert ai_key == "sk-abc123"
        assert tg_token == "12345:AABB"

    def test_empty_string(self):
        ai_key, tg_token = _parse_credentials("")
        assert ai_key == ""
        assert tg_token is None

    def test_multiple_separators(self):
        ai_key, tg_token = _parse_credentials("key|||token|||extra")
        assert ai_key == "key"
        assert tg_token == "token|||extra"


class TestSplitMessage:
    def test_short_message(self):
        assert _split_message("hello", 4096) == ["hello"]

    def test_exact_limit(self):
        text = "a" * 4096
        assert _split_message(text, 4096) == [text]

    def test_split_at_newline(self):
        text = "a" * 3000 + "\n" + "b" * 3000
        chunks = _split_message(text, 4096)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 3000
        assert chunks[1] == "b" * 3000

    def test_split_at_space(self):
        text = "a" * 3000 + " " + "b" * 3000
        chunks = _split_message(text, 4096)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 3000

    def test_no_good_split_point(self):
        text = "a" * 5000
        chunks = _split_message(text, 4096)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 4096
        assert chunks[1] == "a" * 904

    def test_preserves_multiple_newlines(self):
        # rfind("\n") finds the LAST newline within the limit, so it splits there.
        # Only the split-point newline is stripped, earlier newlines stay in chunk 0.
        text = "a" * 4000 + "\n\n\nmore text"
        chunks = _split_message(text, 4010)
        assert len(chunks) == 2
        # rfind finds the \n at pos 4002 (last of 3 newlines within limit 4010)
        assert chunks[0] == "a" * 4000 + "\n\n"
        # Only the split-point \n is stripped
        assert chunks[1] == "more text"

    def test_empty_string(self):
        assert _split_message("", 4096) == [""]


class TestGetUserDisplayName:
    def test_full_name(self):
        user = MagicMock()
        user.first_name = "John"
        user.last_name = "Doe"
        user.username = "johndoe"
        assert _get_user_display_name(user) == "John Doe"

    def test_first_name_only(self):
        user = MagicMock()
        user.first_name = "John"
        user.last_name = None
        user.username = "johndoe"
        assert _get_user_display_name(user) == "John"

    def test_username_fallback(self):
        user = MagicMock()
        user.first_name = None
        user.last_name = None
        user.username = "johndoe"
        assert _get_user_display_name(user) == "johndoe"

    def test_none_user(self):
        assert _get_user_display_name(None) == ""

    def test_no_info(self):
        user = MagicMock()
        user.first_name = None
        user.last_name = None
        user.username = None
        assert _get_user_display_name(user) == ""


# ---------------------------------------------------------------------------
# Adapter property tests
# ---------------------------------------------------------------------------

class TestAdapterProperties:
    def test_platform_type(self):
        adapter = TelegramAdapter()
        assert adapter.platform_type == PlatformType.TELEGRAM

    def test_capabilities(self):
        adapter = TelegramAdapter()
        caps = adapter.capabilities
        assert caps.auth_method == AuthMethod.API_TOKEN
        assert caps.max_message_length == 4096
        assert caps.supports_groups is True
        assert caps.supports_media is True
        assert caps.supports_file_send is True
        assert caps.supports_reactions is True
        assert caps.supports_typing_indicator is True
        assert caps.supports_read_receipts is False
        assert caps.supports_history_sync is False
        assert caps.supports_contacts_list is False


# ---------------------------------------------------------------------------
# send_message / send_file tests
# ---------------------------------------------------------------------------

class TestSendMessage:
    @pytest.fixture(autouse=True)
    def cleanup_apps(self):
        yield
        _active_apps.clear()

    @pytest.mark.asyncio
    async def test_send_message_success(self):
        app_mock = MagicMock()
        app_mock.bot.send_message = AsyncMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_message(1, "12345", "Test Chat", "Hello!")
        assert result is True
        app_mock.bot.send_message.assert_called_once_with(chat_id=12345, text="Hello!")

    @pytest.mark.asyncio
    async def test_send_message_no_app(self):
        adapter = TelegramAdapter()
        result = await adapter.send_message(999, "12345", "Test Chat", "Hello!")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_splits_long(self):
        app_mock = MagicMock()
        app_mock.bot.send_message = AsyncMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        long_msg = "a" * 5000
        result = await adapter.send_message(1, "12345", "Test", long_msg)
        assert result is True
        assert app_mock.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_send_message_api_error(self):
        app_mock = MagicMock()
        app_mock.bot.send_message = AsyncMock(side_effect=Exception("Telegram error"))
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_message(1, "12345", "Test", "Hello!")
        assert result is False


class TestSendFile:
    @pytest.fixture(autouse=True)
    def cleanup_apps(self):
        yield
        _active_apps.clear()

    @pytest.mark.asyncio
    async def test_send_photo(self, tmp_path):
        img_file = tmp_path / "test.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0")

        app_mock = MagicMock()
        app_mock.bot.send_photo = AsyncMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_file(1, "12345", str(img_file), "caption", "image/jpeg")
        assert result is True
        app_mock.bot.send_photo.assert_called_once()
        call_kwargs = app_mock.bot.send_photo.call_args[1]
        assert call_kwargs["chat_id"] == 12345
        assert call_kwargs["caption"] == "caption"

    @pytest.mark.asyncio
    async def test_send_document(self, tmp_path):
        doc_file = tmp_path / "test.pdf"
        doc_file.write_bytes(b"%PDF-1.4")

        app_mock = MagicMock()
        app_mock.bot.send_document = AsyncMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_file(1, "12345", str(doc_file), "", "application/pdf")
        assert result is True
        app_mock.bot.send_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_file_not_found(self):
        app_mock = MagicMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_file(1, "12345", "/nonexistent/file.jpg", "", "image/jpeg")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_file_no_app(self):
        adapter = TelegramAdapter()
        result = await adapter.send_file(999, "12345", "/some/file.jpg", "", "image/jpeg")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_video(self, tmp_path):
        vid_file = tmp_path / "test.mp4"
        vid_file.write_bytes(b"\x00\x00\x00\x1c")

        app_mock = MagicMock()
        app_mock.bot.send_video = AsyncMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_file(1, "12345", str(vid_file), "vid", "video/mp4")
        assert result is True
        app_mock.bot.send_video.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_audio(self, tmp_path):
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"\xff\xfb\x90\x00")

        app_mock = MagicMock()
        app_mock.bot.send_audio = AsyncMock()
        _active_apps[1] = app_mock

        adapter = TelegramAdapter()
        result = await adapter.send_file(1, "12345", str(audio_file), "", "audio/mpeg")
        assert result is True
        app_mock.bot.send_audio.assert_called_once()


# ---------------------------------------------------------------------------
# run() error handling tests
# ---------------------------------------------------------------------------

class TestRunStartup:
    @pytest.fixture(autouse=True)
    def cleanup_apps(self):
        yield
        _active_apps.clear()

    @pytest.mark.asyncio
    async def test_run_no_token(self):
        """run() should fail gracefully when no Telegram token is provided."""
        adapter = TelegramAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 1
        instance.config = {
            "api_key_encrypted": "",
        }
        instance.notify_status = AsyncMock()

        await adapter.run(instance)

        assert instance.is_running is False
        assert "No Telegram bot token" in instance.error
        instance.notify_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.platforms.telegram.adapter.Bot")
    @patch("app.auth.utils.decrypt_string", return_value="sk-key|||invalid-token")
    async def test_run_invalid_token(self, mock_decrypt, mock_bot_cls):
        """run() should fail gracefully when the Telegram token is invalid."""
        bot_instance = AsyncMock()
        bot_instance.get_me = AsyncMock(side_effect=Exception("Unauthorized"))
        mock_bot_cls.return_value = bot_instance

        adapter = TelegramAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 1
        instance.config = {"api_key_encrypted": "encrypted_value"}
        instance.notify_status = AsyncMock()

        await adapter.run(instance)

        assert instance.is_running is False
        assert "Invalid Telegram bot token" in instance.error

    @pytest.mark.asyncio
    @patch("app.platforms.telegram.adapter.Application")
    @patch("app.platforms.telegram.adapter.Bot")
    @patch("app.auth.utils.decrypt_string", return_value="sk-key|||valid-token")
    async def test_run_connects_successfully(self, mock_decrypt, mock_bot_cls, mock_app_cls):
        """run() should notify connected status on successful token verification."""
        bot_info = MagicMock()
        bot_info.username = "testbot"
        bot_info.id = 123456
        bot_instance = AsyncMock()
        bot_instance.get_me = AsyncMock(return_value=bot_info)
        mock_bot_cls.return_value = bot_instance

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.updater = MagicMock()
        mock_app.updater.start_polling = AsyncMock()
        mock_app.updater.stop = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()

        builder_mock = MagicMock()
        builder_mock.token.return_value = builder_mock
        builder_mock.build.return_value = mock_app
        mock_app_cls.builder.return_value = builder_mock

        adapter = TelegramAdapter()
        instance = MagicMock()
        instance.bot_profile_id = 1
        instance.config = {"api_key_encrypted": "encrypted_value"}
        instance.notify_status = AsyncMock()
        # Make the while loop exit immediately
        instance.is_running = False
        instance.stopped_by_user = False

        await adapter.run(instance)

        # Should have notified connected status
        calls = instance.notify_status.call_args_list
        assert any("connected" in str(c) for c in calls)
        # Should have initialized and started polling
        mock_app.initialize.assert_called_once()
        mock_app.start.assert_called_once()
        mock_app.updater.start_polling.assert_called_once()
        # Should have shut down cleanly
        mock_app.updater.stop.assert_called_once()
        mock_app.stop.assert_called_once()
        mock_app.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# cleanup tests
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_removes_app(self):
        _active_apps[42] = MagicMock()
        adapter = TelegramAdapter()
        adapter.cleanup(42)
        assert 42 not in _active_apps

    def test_cleanup_nonexistent_noop(self):
        adapter = TelegramAdapter()
        adapter.cleanup(999)  # Should not raise


# ---------------------------------------------------------------------------
# Handler context tests (handle_message, handle_media)
# ---------------------------------------------------------------------------

def _make_update(
    text="Hello bot",
    chat_id=12345,
    chat_type="private",
    user_id=67890,
    first_name="John",
    last_name="Doe",
    username="johndoe",
    message_id=1001,
    chat_title=None,
    caption=None,
    photo=None,
    document=None,
    audio=None,
    video=None,
    voice=None,
    reply_to_message=None,
):
    """Build a mock Update object that looks like a Telegram update."""
    update = MagicMock()
    msg = MagicMock()

    msg.chat_id = chat_id
    msg.chat.type = chat_type
    msg.chat.title = chat_title
    msg.message_id = message_id
    msg.text = text
    msg.caption = caption
    msg.reply_text = AsyncMock()
    msg.reply_to_message = reply_to_message

    user = MagicMock()
    user.id = user_id
    user.first_name = first_name
    user.last_name = last_name
    user.username = username
    msg.from_user = user

    msg.photo = photo
    msg.document = document
    msg.audio = audio
    msg.video = video
    msg.voice = voice

    update.message = msg
    return update


def _make_context(bot_username="testbot", bot_id=999):
    """Build a mock ContextTypes.DEFAULT_TYPE."""
    ctx = MagicMock()
    bot_user = MagicMock()
    bot_user.username = bot_username
    bot_user.id = bot_id
    ctx.bot.get_me = AsyncMock(return_value=bot_user)
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock()
    return ctx


def _make_handler_context(ai_response_enabled=True, **config_overrides):
    """Build a _HandlerContext with a mock instance."""
    config = {
        "ai_provider": "openai",
        "model": "gpt-4o-mini",
        "system_prompt": "You are a test bot.",
        "max_history": 10,
        "max_tokens": 100,
        "temperature": 0.5,
        "response_delay_min": 0,
        "response_delay_max": 0,
        "group_chat_enabled": False,
        "respond_to_all_in_group": False,
    }
    config.update(config_overrides)

    instance = MagicMock()
    instance.ai_response_enabled = ai_response_enabled
    instance.bot_profile_id = 1

    return _HandlerContext(
        bot_profile_id=1,
        instance=instance,
        ai_api_key="sk-test-key",
        config=config,
    )


class TestHandleMessage:
    """Test the handle_message flow with mocked DB and AI."""

    @pytest.mark.asyncio
    async def test_saves_message_and_skips_ai_when_disabled(self):
        """When AI is disabled, message should be saved but no reply sent."""
        handler = _make_handler_context(ai_response_enabled=False)
        update = _make_update(text="Hello")
        ctx = _make_context()

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="")
        mock_msg = MagicMock(
            id=100, content="Hello", sender_name="John Doe", sender_id="67890",
            sender_profile_pic="", timestamp=datetime.utcnow(),
        )

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv) as mock_find, \
             patch("app.platforms.message_handler.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_msg) as mock_save, \
             patch("app.platforms.message_handler.update_conversation_stats") as mock_stats, \
             patch("app.platforms.message_handler.broadcast_user_message") as mock_broadcast:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await handler.handle_message(update, ctx)

            mock_find.assert_called_once()
            mock_save.assert_called_once()
            mock_stats.assert_called_once()
            mock_broadcast.assert_called_once()
            # No reply should be sent
            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_ai_response_flow(self):
        """When AI is enabled, should get AI response and reply."""
        handler = _make_handler_context(ai_response_enabled=True)
        update = _make_update(text="What is Python?")
        ctx = _make_context()

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="", human_takeover=False)
        mock_user_msg = MagicMock(
            id=100, content="What is Python?", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )
        mock_assistant_msg = MagicMock(
            id=101, content="Python is a programming language.", sender_name="AI Agent",
            sender_id="", sender_profile_pic="/static/images/ai-agent.svg",
            timestamp=datetime.utcnow(),
        )

        mock_ai_response = MagicMock()
        mock_ai_response.content = "Python is a programming language."

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_user_msg), \
             patch("app.platforms.message_handler.save_assistant_message", return_value=mock_assistant_msg), \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.is_human_takeover_active", return_value=False), \
             patch("app.platforms.message_handler.build_ai_messages", return_value=[{"role": "user", "content": "What is Python?"}]), \
             patch("app.platforms.message_handler.broadcast_user_message"), \
             patch("app.platforms.message_handler.broadcast_assistant_message") as mock_broadcast_ai, \
             patch("app.platforms.message_handler.broadcast_typing"), \
             patch.object(handler, "_get_ai_provider") as mock_provider_fn:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            provider = MagicMock()
            provider.chat_completion.return_value = mock_ai_response
            mock_provider_fn.return_value = provider

            await handler.handle_message(update, ctx)

            # Should have replied via Telegram
            update.message.reply_text.assert_called_once_with("Python is a programming language.")
            # Should have broadcast assistant message
            mock_broadcast_ai.assert_called_once()
            # Should have sent typing action
            ctx.bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_human_takeover_suppresses_ai(self):
        """When human takeover is active, no AI response should be generated."""
        handler = _make_handler_context(ai_response_enabled=True)
        update = _make_update(text="Help me")
        ctx = _make_context()

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="Help me", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_user_msg), \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.is_human_takeover_active", return_value=True), \
             patch("app.platforms.message_handler.broadcast_user_message"), \
             patch("app.platforms.message_handler.broadcast_typing"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await handler.handle_message(update, ctx)

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_with_existing_response_skips(self):
        """Duplicate message with existing response should be skipped entirely."""
        handler = _make_handler_context(ai_response_enabled=True)
        update = _make_update(text="Hello")
        ctx = _make_context()

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="")
        existing_msg = MagicMock(id=50)

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.is_duplicate_message", return_value=(True, existing_msg)), \
             patch("app.platforms.message_handler.has_response_after", return_value=True), \
             patch("app.platforms.message_handler.save_user_message") as mock_save, \
             patch("app.platforms.message_handler.broadcast_user_message") as mock_broadcast:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await handler.handle_message(update, ctx)

            # Should NOT save a new message
            mock_save.assert_not_called()
            # Should NOT broadcast
            mock_broadcast.assert_not_called()
            # Should NOT reply
            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_without_response_generates_ai(self):
        """Duplicate message WITHOUT existing response should still generate AI response."""
        handler = _make_handler_context(ai_response_enabled=True)
        update = _make_update(text="Hello")
        ctx = _make_context()

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="")
        existing_msg = MagicMock(
            id=50, content="Hello", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )

        mock_assistant_msg = MagicMock(
            id=101, content="Hi there!", sender_name="AI Agent",
            sender_id="", sender_profile_pic="/static/images/ai-agent.svg",
            timestamp=datetime.utcnow(),
        )

        mock_ai_response = MagicMock()
        mock_ai_response.content = "Hi there!"

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.is_duplicate_message", return_value=(True, existing_msg)), \
             patch("app.platforms.message_handler.has_response_after", return_value=False), \
             patch("app.platforms.message_handler.save_user_message") as mock_save, \
             patch("app.platforms.message_handler.save_assistant_message", return_value=mock_assistant_msg), \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.is_human_takeover_active", return_value=False), \
             patch("app.platforms.message_handler.build_ai_messages", return_value=[]), \
             patch("app.platforms.message_handler.broadcast_user_message"), \
             patch("app.platforms.message_handler.broadcast_assistant_message"), \
             patch("app.platforms.message_handler.broadcast_typing"), \
             patch.object(handler, "_get_ai_provider") as mock_provider_fn:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            provider = MagicMock()
            provider.chat_completion.return_value = mock_ai_response
            mock_provider_fn.return_value = provider

            await handler.handle_message(update, ctx)

            # Should NOT save a new message (duplicate)
            mock_save.assert_not_called()
            # SHOULD reply with AI response
            update.message.reply_text.assert_called_once_with("Hi there!")

    @pytest.mark.asyncio
    async def test_group_chat_disabled_skips(self):
        """Group messages should be skipped when group_chat_enabled is False."""
        handler = _make_handler_context(group_chat_enabled=False)
        update = _make_update(text="Hello group", chat_type="group", chat_title="My Group")
        ctx = _make_context()

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation") as mock_find:

            await handler.handle_message(update, ctx)

            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_chat_requires_mention(self):
        """In groups without respond_to_all, should skip when bot not mentioned."""
        handler = _make_handler_context(group_chat_enabled=True, respond_to_all_in_group=False)
        update = _make_update(
            text="Hello everyone",
            chat_type="supergroup",
            chat_title="My Group",
        )
        ctx = _make_context(bot_username="testbot")

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation") as mock_find:

            await handler.handle_message(update, ctx)

            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_chat_responds_to_mention(self):
        """In groups, should respond when bot is @mentioned."""
        handler = _make_handler_context(
            ai_response_enabled=False,
            group_chat_enabled=True,
            respond_to_all_in_group=False,
        )
        update = _make_update(
            text="Hey @testbot what's up?",
            chat_type="group",
            chat_title="My Group",
        )
        ctx = _make_context(bot_username="testbot")

        mock_conv = MagicMock(id=10, chat_name="My Group", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="Hey @testbot what's up?", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv) as mock_find, \
             patch("app.platforms.message_handler.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_user_msg), \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.broadcast_user_message"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await handler.handle_message(update, ctx)

            # Should have processed the message (found/created conversation)
            mock_find.assert_called_once()

    @pytest.mark.asyncio
    async def test_null_message_ignored(self):
        """update.message=None should be ignored."""
        handler = _make_handler_context()
        update = MagicMock()
        update.message = None
        ctx = _make_context()

        # Should not raise
        await handler.handle_message(update, ctx)

    @pytest.mark.asyncio
    async def test_null_text_ignored(self):
        """update.message with no text should be ignored."""
        handler = _make_handler_context()
        update = MagicMock()
        update.message.text = None
        ctx = _make_context()

        await handler.handle_message(update, ctx)


class TestHandleMedia:
    """Test the handle_media flow with mocked download and AI."""

    @pytest.mark.asyncio
    async def test_photo_message_saved(self):
        """Photo message should be downloaded, saved, and stored in DB."""
        handler = _make_handler_context(ai_response_enabled=False)

        # Build photo mock
        photo = MagicMock()
        photo.file_id = "photo_file_id"
        photo.file_unique_id = "photo_unique"

        file_obj = MagicMock()
        file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"\xff\xd8\xff\xe0test"))

        update = _make_update(text=None, photo=[photo], caption="My photo")
        ctx = _make_context()
        ctx.bot.get_file = AsyncMock(return_value=file_obj)

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="My photo", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )

        media_info = {
            "file_url": "/media/bot_1/photo.jpg",
            "file_type": "image/jpeg",
            "file_name": "photo.jpg",
            "file_size": 8,
            "file_pages": None,
            "local_file_path": "/tmp/photo.jpg",
        }

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_user_msg) as mock_save, \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.broadcast_user_message"), \
             patch("app.platforms.message_handler.save_media_file", return_value=media_info) as mock_media:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await handler.handle_media(update, ctx)

            # Should have downloaded file
            ctx.bot.get_file.assert_called_once_with("photo_file_id")
            file_obj.download_as_bytearray.assert_called_once()
            # Should have saved media
            mock_media.assert_called_once()
            assert mock_media.call_args[1]["media_type"] == "image/jpeg"
            # Should have saved user message with file info
            mock_save.assert_called_once()
            call_kwargs = mock_save.call_args[1]
            assert call_kwargs["file_url"] == "/media/bot_1/photo.jpg"
            assert call_kwargs["file_type"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_document_message(self):
        """Document message should use document mime type."""
        handler = _make_handler_context(ai_response_enabled=False)

        doc = MagicMock()
        doc.file_id = "doc_file_id"
        doc.mime_type = "application/pdf"
        doc.file_name = "report.pdf"

        file_obj = MagicMock()
        file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"%PDF-1.4"))

        update = _make_update(text=None, document=doc)
        ctx = _make_context()
        ctx.bot.get_file = AsyncMock(return_value=file_obj)

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="")
        mock_user_msg = MagicMock(
            id=100, content="[Media: application/pdf]", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )

        media_info = {
            "file_url": "/media/bot_1/report.pdf",
            "file_type": "application/pdf",
            "file_name": "report.pdf",
            "file_size": 8,
            "file_pages": 3,
            "local_file_path": "/tmp/report.pdf",
        }

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_user_msg) as mock_save, \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.broadcast_user_message"), \
             patch("app.platforms.message_handler.save_media_file", return_value=media_info):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await handler.handle_media(update, ctx)

            call_kwargs = mock_save.call_args[1]
            assert call_kwargs["file_type"] == "application/pdf"
            assert call_kwargs["file_name"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_media_with_ai_response(self):
        """Media message with AI enabled should analyze and reply."""
        handler = _make_handler_context(ai_response_enabled=True)

        photo = MagicMock()
        photo.file_id = "photo_id"
        photo.file_unique_id = "photo_uniq"

        file_obj = MagicMock()
        file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"\xff\xd8data"))

        update = _make_update(text=None, photo=[photo], caption="What is this?")
        ctx = _make_context()
        ctx.bot.get_file = AsyncMock(return_value=file_obj)

        mock_conv = MagicMock(id=10, chat_name="John Doe", profile_pic="", human_takeover=False)
        mock_user_msg = MagicMock(
            id=100, content="What is this?", sender_name="John Doe",
            sender_id="67890", sender_profile_pic="", timestamp=datetime.utcnow(),
        )
        mock_assistant_msg = MagicMock(
            id=101, content="This is a photo of a cat.", sender_name="AI Agent",
            sender_id="", sender_profile_pic="/static/images/ai-agent.svg",
            timestamp=datetime.utcnow(),
        )

        media_info = {
            "file_url": "/media/bot_1/photo.jpg",
            "file_type": "image/jpeg",
            "file_name": "photo.jpg",
            "file_size": 8,
            "file_pages": None,
            "local_file_path": "/tmp/photo.jpg",
        }

        mock_ai_response = MagicMock()
        mock_ai_response.content = "This is a photo of a cat."

        with patch("app.platforms.message_handler.get_db_session") as mock_db_ctx, \
             patch("app.platforms.message_handler.find_or_create_conversation", return_value=mock_conv), \
             patch("app.platforms.message_handler.save_user_message", return_value=mock_user_msg), \
             patch("app.platforms.message_handler.save_assistant_message", return_value=mock_assistant_msg), \
             patch("app.platforms.message_handler.update_conversation_stats"), \
             patch("app.platforms.message_handler.is_human_takeover_active", return_value=False), \
             patch("app.platforms.message_handler.build_ai_messages", return_value=[]), \
             patch("app.platforms.message_handler.broadcast_user_message"), \
             patch("app.platforms.message_handler.broadcast_assistant_message"), \
             patch("app.platforms.message_handler.broadcast_typing"), \
             patch("app.platforms.message_handler.save_media_file", return_value=media_info), \
             patch("app.platforms.message_handler.analyze_media_with_ai", return_value="A cat sitting"), \
             patch.object(handler, "_get_ai_provider") as mock_provider_fn:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            provider = MagicMock()
            provider.chat_completion.return_value = mock_ai_response
            mock_provider_fn.return_value = provider

            await handler.handle_media(update, ctx)

            update.message.reply_text.assert_called_once_with("This is a photo of a cat.")

    @pytest.mark.asyncio
    async def test_group_media_skipped_when_disabled(self):
        """Media in groups should be skipped when group_chat_enabled is False."""
        handler = _make_handler_context(group_chat_enabled=False)

        photo = MagicMock()
        photo.file_id = "photo_id"
        photo.file_unique_id = "photo_uniq"

        update = _make_update(
            text=None, photo=[photo], chat_type="group", chat_title="Group",
        )
        ctx = _make_context()

        with patch("app.platforms.message_handler.save_media_file") as mock_media:
            await handler.handle_media(update, ctx)
            mock_media.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_message_ignored(self):
        """update.message=None should be ignored for media handler."""
        handler = _make_handler_context()
        update = MagicMock()
        update.message = None
        ctx = _make_context()

        await handler.handle_media(update, ctx)


class TestHandleStart:
    @pytest.mark.asyncio
    async def test_start_command(self):
        handler = _make_handler_context()
        update = _make_update(text="/start")
        ctx = _make_context()

        await handler.handle_start(update, ctx)

        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "AI assistant" in reply_text
