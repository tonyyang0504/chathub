"""
Tests for the Discord platform adapter.

Uses mocks for discord.py, AI provider, and database to verify adapter
behavior without needing a real Discord bot token or connection.
"""

import asyncio
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
    PropertyMock,
)

import pytest
import discord

from app.platforms.discord.adapter import DiscordAdapter
from app.platforms.base import PlatformType, AuthMethod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter():
    return DiscordAdapter()


@pytest.fixture
def mock_instance():
    """A fake BotInstance with the fields the adapter reads."""
    inst = MagicMock()
    inst.bot_profile_id = 42
    inst.config = {
        "api_key": "sk-test-ai-key",
        "platform_token": "discord-bot-token-123",
        "ai_provider": "openai",
        "model": "gpt-4o-mini",
        "system_prompt": "You are helpful.",
        "temperature": 0.7,
        "max_tokens": 500,
        "max_history": 10,
        "group_chat_enabled": True,
        "respond_to_all_in_group": False,
        "response_delay_min": 0,
        "response_delay_max": 0,
    }
    inst.ai_response_enabled = True
    inst.is_running = True
    inst.platform_connected = False
    inst.error = None
    inst.stopped_by_user = False
    inst.notify_status = AsyncMock()
    inst.notify_qr = AsyncMock()
    return inst


@pytest.fixture
def mock_ai_provider():
    provider = MagicMock()
    response = MagicMock()
    response.content = "Hello! I'm your AI assistant."
    provider.chat_completion.return_value = response
    provider.supports_vision = True
    return provider


def _make_discord_user(*, user_id=100, name="TestUser", bot=False, discriminator="0001"):
    """Create a mock Discord user."""
    user = MagicMock(spec=discord.User)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.discriminator = discriminator
    user.bot = bot
    user.mention = f"<@{user_id}>"
    avatar = MagicMock()
    avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/abc.png"
    user.display_avatar = avatar
    return user


def _make_discord_message(
    *,
    content="Hello bot",
    author=None,
    channel=None,
    message_id=999,
    mentions=None,
    attachments=None,
):
    """Create a mock Discord message."""
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.content = content
    msg.author = author or _make_discord_user()
    msg.channel = channel or MagicMock(spec=discord.DMChannel)
    msg.mentions = mentions or []
    msg.attachments = attachments or []
    msg.created_at = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)

    # Make channel.send and channel.typing async
    msg.channel.send = AsyncMock()
    msg.channel.typing = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(),
        __aexit__=AsyncMock(),
    ))
    return msg


# ---------------------------------------------------------------------------
# Properties & Capabilities
# ---------------------------------------------------------------------------

class TestAdapterProperties:

    def test_platform_type(self, adapter):
        assert adapter.platform_type == PlatformType.DISCORD

    def test_auth_method(self, adapter):
        assert adapter.capabilities.auth_method == AuthMethod.API_TOKEN

    def test_max_message_length(self, adapter):
        assert adapter.capabilities.max_message_length == 2000

    def test_capabilities_flags(self, adapter):
        cap = adapter.capabilities
        assert cap.supports_groups is True
        assert cap.supports_media is True
        assert cap.supports_file_send is True
        assert cap.supports_reactions is True
        assert cap.supports_typing_indicator is True
        assert cap.supports_read_receipts is False
        assert cap.supports_history_sync is True
        assert cap.supports_contacts_list is True
        assert cap.supports_groups_list is True


# ---------------------------------------------------------------------------
# Message Splitting
# ---------------------------------------------------------------------------

class TestMessageSplitting:

    def test_short_message_no_split(self, adapter):
        assert adapter._split_message("hello", 2000) == ["hello"]

    def test_empty_message(self, adapter):
        assert adapter._split_message("", 2000) == [""]

    def test_exact_limit(self, adapter):
        text = "a" * 2000
        assert adapter._split_message(text, 2000) == [text]

    def test_split_at_space(self, adapter):
        text = "word " * 500  # 2500 chars
        chunks = adapter._split_message(text, 2000)
        assert all(len(c) <= 2000 for c in chunks)
        assert len(chunks) == 2
        assert not any(c.startswith(" ") for c in chunks)

    def test_split_at_newline_preferred(self, adapter):
        # Newlines should be preferred over spaces
        text = "a" * 1990 + "\n" + "b" * 100
        chunks = adapter._split_message(text, 2000)
        assert chunks[0] == "a" * 1990
        assert chunks[1] == "b" * 100

    def test_split_continuous_text(self, adapter):
        text = "x" * 5000
        chunks = adapter._split_message(text, 2000)
        assert len(chunks) == 3
        assert [len(c) for c in chunks] == [2000, 2000, 1000]

    def test_no_empty_chunks(self, adapter):
        text = "hello\n\n\nworld"
        chunks = adapter._split_message(text, 7)
        assert all(c for c in chunks), f"Empty chunk found in {chunks}"


# ---------------------------------------------------------------------------
# run() — startup and error handling
# ---------------------------------------------------------------------------

class TestRunLifecycle:

    @pytest.mark.asyncio
    async def test_run_no_token_reports_error(self, adapter):
        """If no bot token, run() should set error and return."""
        inst = MagicMock()
        inst.bot_profile_id = 1
        inst.config = {}  # no api_key, no platform_token
        inst.is_running = True
        inst.error = None
        inst.notify_status = AsyncMock()

        with patch("app.auth.utils.decrypt_string", return_value=""):
            await adapter.run(inst)

        assert inst.is_running is False
        assert inst.error == "No Discord bot token configured. Set platform_token in bot config."
        inst.notify_status.assert_any_call({
            "error": inst.error,
            "message": "No Discord bot token configured. Set platform_token in bot config.",
        })

    @pytest.mark.asyncio
    async def test_run_login_failure(self, adapter, mock_instance):
        """LoginFailure should set proper error."""
        with patch("app.ai.factory.get_ai_provider") as mock_get_ai, \
             patch("discord.Client") as MockClient:

            mock_get_ai.return_value = MagicMock()

            client_inst = MagicMock()
            client_inst.is_closed.return_value = True
            client_inst.start = AsyncMock(side_effect=discord.LoginFailure("bad token"))
            client_inst.close = AsyncMock()
            MockClient.return_value = client_inst

            await adapter.run(mock_instance)

        assert mock_instance.error == "Invalid bot token"
        assert mock_instance.is_running is False

    @pytest.mark.asyncio
    async def test_run_cancelled_reraises(self, adapter, mock_instance):
        """CancelledError should close client and re-raise."""
        with patch("app.ai.factory.get_ai_provider") as mock_get_ai, \
             patch("discord.Client") as MockClient:

            mock_get_ai.return_value = MagicMock()

            client_inst = MagicMock()
            client_inst.is_closed.return_value = True
            client_inst.start = AsyncMock(side_effect=asyncio.CancelledError())
            client_inst.close = AsyncMock()
            MockClient.return_value = client_inst

            with pytest.raises(asyncio.CancelledError):
                await adapter.run(mock_instance)

            client_inst.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_run_cleans_up_client_on_exit(self, adapter, mock_instance):
        """Client should be removed from _clients dict after run exits."""
        with patch("app.ai.factory.get_ai_provider") as mock_get_ai, \
             patch("discord.Client") as MockClient:

            mock_get_ai.return_value = MagicMock()

            client_inst = MagicMock()
            client_inst.is_closed.return_value = True
            client_inst.start = AsyncMock(side_effect=discord.LoginFailure("bad"))
            client_inst.close = AsyncMock()
            MockClient.return_value = client_inst

            await adapter.run(mock_instance)

        assert 42 not in adapter._clients
        assert mock_instance.platform_connected is False

    @pytest.mark.asyncio
    async def test_run_ai_provider_failure_still_connects(self, adapter, mock_instance):
        """If AI provider init fails, bot should still try to connect to Discord."""
        with patch("app.ai.factory.get_ai_provider", side_effect=ValueError("bad key")), \
             patch("discord.Client") as MockClient:

            client_inst = MagicMock()
            client_inst.is_closed.return_value = True
            client_inst.start = AsyncMock(side_effect=discord.LoginFailure("bad"))
            client_inst.close = AsyncMock()
            MockClient.return_value = client_inst

            await adapter.run(mock_instance)

        # Should have attempted to connect (LoginFailure means it tried)
        assert mock_instance.error == "Invalid bot token"


# ---------------------------------------------------------------------------
# _handle_message — full message processing pipeline
# ---------------------------------------------------------------------------

class TestHandleMessage:

    @pytest.mark.asyncio
    async def test_dm_message_full_pipeline(self, adapter, mock_instance, mock_ai_provider):
        """Test a complete DM message: save to DB, AI response, send to Discord."""
        message = _make_discord_message(content="What is Python?")

        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="TestBot", bot=True)

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        fake_user_msg = MagicMock()
        fake_user_msg.id = 50

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation) as mock_find, \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message", return_value=fake_user_msg), \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=False), \
             patch("app.platforms.discord.adapter.broadcast_typing"), \
             patch("app.platforms.discord.adapter.build_ai_messages", return_value=[{"role": "user", "content": "What is Python?"}]), \
             patch("app.platforms.discord.adapter.save_assistant_message") as mock_save_asst, \
             patch("app.platforms.discord.adapter.broadcast_assistant_message"), \
             patch("app.platforms.discord.adapter.log_activity"):

            # Mock DB session context manager
            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=mock_ai_provider,
                is_dm=True,
            )

        # AI was called
        mock_ai_provider.chat_completion.assert_called_once()

        # Response sent to Discord
        message.channel.send.assert_awaited()
        sent_text = message.channel.send.call_args_list[-1][0][0]
        assert sent_text == "Hello! I'm your AI assistant."

    @pytest.mark.asyncio
    async def test_server_message_strips_mention(self, adapter, mock_instance, mock_ai_provider):
        """Bot mention should be stripped from message content."""
        bot_user = _make_discord_user(user_id=200, name="TestBot", bot=True)
        mock_client = MagicMock()
        mock_client.user = bot_user

        message = _make_discord_message(
            content=f"<@200> what's up?",
            channel=MagicMock(spec=discord.TextChannel),
        )
        message.channel.name = "general"

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message") as mock_save_user, \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=False), \
             patch("app.platforms.discord.adapter.broadcast_typing"), \
             patch("app.platforms.discord.adapter.build_ai_messages", return_value=[]), \
             patch("app.platforms.discord.adapter.save_assistant_message"), \
             patch("app.platforms.discord.adapter.broadcast_assistant_message"), \
             patch("app.platforms.discord.adapter.log_activity"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            mock_save_user.return_value = MagicMock(id=1)

            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=mock_ai_provider,
                is_dm=False,
            )

        # The saved content should have the mention stripped
        # save_user_message(db, conversation_id, content, ...)
        call_args = mock_save_user.call_args
        saved_content = call_args[0][2]  # third positional arg = content
        assert "<@200>" not in saved_content
        assert "what's up?" in saved_content

    @pytest.mark.asyncio
    async def test_duplicate_message_skipped(self, adapter, mock_instance, mock_ai_provider):
        """Duplicate messages with existing responses should be skipped entirely."""
        message = _make_discord_message(content="dup")

        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="Bot", bot=True)

        fake_conversation = MagicMock()
        fake_conversation.id = 10
        existing_msg = MagicMock()
        existing_msg.id = 5

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(True, existing_msg)), \
             patch("app.platforms.discord.adapter.has_response_after", return_value=True), \
             patch("app.platforms.discord.adapter.save_user_message") as mock_save:

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=mock_ai_provider,
                is_dm=True,
            )

        # Should NOT save a new message or call AI
        mock_save.assert_not_called()
        mock_ai_provider.chat_completion.assert_not_called()
        message.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_human_takeover_skips_ai(self, adapter, mock_instance, mock_ai_provider):
        """When human takeover is active, AI response should be suppressed."""
        message = _make_discord_message(content="help")

        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="Bot", bot=True)

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message", return_value=MagicMock(id=1)), \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=True), \
             patch("app.platforms.discord.adapter.broadcast_typing"), \
             patch("app.platforms.discord.adapter.log_activity"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=mock_ai_provider,
                is_dm=True,
            )

        mock_ai_provider.chat_completion.assert_not_called()
        message.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ai_error_broadcasts_typing_off(self, adapter, mock_instance):
        """If AI call fails, typing indicator should be turned off."""
        message = _make_discord_message(content="crash")
        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="Bot", bot=True)

        bad_ai = MagicMock()
        bad_ai.chat_completion.side_effect = RuntimeError("API down")

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message", return_value=MagicMock(id=1)), \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=False), \
             patch("app.platforms.discord.adapter.broadcast_typing") as mock_typing, \
             patch("app.platforms.discord.adapter.build_ai_messages", return_value=[]), \
             patch("app.platforms.discord.adapter.log_activity"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=bad_ai,
                is_dm=True,
            )

        # Typing should be turned off after error
        # Last call should be broadcast_typing(conversation_id, False)
        typing_calls = mock_typing.call_args_list
        assert any(call[0][1] is False for call in typing_calls), \
            f"broadcast_typing(_, False) not called. Calls: {typing_calls}"

        message.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_long_ai_response_split(self, adapter, mock_instance):
        """AI responses longer than 2000 chars should be sent as multiple messages."""
        long_response = "x" * 3500
        ai_provider = MagicMock()
        response = MagicMock()
        response.content = long_response
        ai_provider.chat_completion.return_value = response

        message = _make_discord_message(content="give me a long reply")
        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="Bot", bot=True)

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message", return_value=MagicMock(id=1)), \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=False), \
             patch("app.platforms.discord.adapter.broadcast_typing"), \
             patch("app.platforms.discord.adapter.build_ai_messages", return_value=[]), \
             patch("app.platforms.discord.adapter.save_assistant_message"), \
             patch("app.platforms.discord.adapter.broadcast_assistant_message"), \
             patch("app.platforms.discord.adapter.log_activity"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=ai_provider,
                is_dm=True,
            )

        # Should have sent 2 messages (2000 + 1500)
        assert message.channel.send.await_count == 2
        for call in message.channel.send.call_args_list:
            sent = call[0][0]
            assert len(sent) <= 2000

    @pytest.mark.asyncio
    async def test_forbidden_error_handled(self, adapter, mock_instance, mock_ai_provider):
        """discord.Forbidden on send should be caught without crashing."""
        message = _make_discord_message(content="test")
        message.channel.send = AsyncMock(side_effect=discord.Forbidden(
            MagicMock(status=403), "Missing permissions"
        ))

        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="Bot", bot=True)

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message", return_value=MagicMock(id=1)), \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=False), \
             patch("app.platforms.discord.adapter.broadcast_typing"), \
             patch("app.platforms.discord.adapter.build_ai_messages", return_value=[]), \
             patch("app.platforms.discord.adapter.save_assistant_message") as mock_save_asst, \
             patch("app.platforms.discord.adapter.broadcast_assistant_message"), \
             patch("app.platforms.discord.adapter.log_activity"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            # Should not raise
            await adapter._handle_message(
                message=message,
                client=mock_client,
                instance=mock_instance,
                config=mock_instance.config,
                ai_provider=mock_ai_provider,
                is_dm=True,
            )

        # Assistant message should NOT be saved (send failed)
        mock_save_asst.assert_not_called()


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------

class TestAttachments:

    @pytest.mark.asyncio
    async def test_download_attachment(self, adapter):
        """Attachment should be downloaded, base64-encoded, and saved."""
        attachment = MagicMock(spec=discord.Attachment)
        attachment.read = AsyncMock(return_value=b"fake image data")
        attachment.content_type = "image/png"
        attachment.filename = "screenshot.png"

        with patch("app.platforms.discord.adapter.save_media_file") as mock_save:
            mock_save.return_value = {
                "file_url": "/media/bot_1/test.png",
                "file_type": "image/png",
                "file_name": "screenshot.png",
                "file_size": 15,
                "file_pages": None,
                "local_file_path": "/tmp/test.png",
            }

            result = await adapter._download_attachment(attachment, 1, "TestChat")

        assert result is not None
        assert result["file_type"] == "image/png"
        mock_save.assert_called_once()
        # Verify base64 encoding
        call_kwargs = mock_save.call_args[1]
        decoded = base64.b64decode(call_kwargs["base64_data"])
        assert decoded == b"fake image data"

    @pytest.mark.asyncio
    async def test_download_attachment_failure(self, adapter):
        """Attachment download failure should return None, not crash."""
        attachment = MagicMock(spec=discord.Attachment)
        attachment.read = AsyncMock(side_effect=Exception("CDN error"))

        result = await adapter._download_attachment(attachment, 1, "TestChat")
        assert result is None


# ---------------------------------------------------------------------------
# send_message / send_file
# ---------------------------------------------------------------------------

class TestSendMessage:

    @pytest.mark.asyncio
    async def test_send_message_to_channel(self, adapter):
        """send_message should resolve channel and send text."""
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        mock_client = MagicMock()
        mock_client.is_closed.return_value = False
        mock_client.get_channel.return_value = mock_channel
        adapter._clients[1] = mock_client

        result = await adapter.send_message(1, "channel_12345", "#general", "hello")
        assert result is True
        mock_channel.send.assert_awaited_once_with("hello")

    @pytest.mark.asyncio
    async def test_send_message_to_dm(self, adapter):
        """send_message should resolve DM channel and send text."""
        mock_dm_channel = MagicMock()
        mock_dm_channel.send = AsyncMock()

        mock_user = MagicMock()
        mock_user.create_dm = AsyncMock(return_value=mock_dm_channel)

        mock_client = MagicMock()
        mock_client.is_closed.return_value = False
        mock_client.fetch_user = AsyncMock(return_value=mock_user)
        adapter._clients[1] = mock_client

        result = await adapter.send_message(1, "dm_99999", "SomeUser", "hi there")
        assert result is True
        mock_dm_channel.send.assert_awaited_once_with("hi there")

    @pytest.mark.asyncio
    async def test_send_message_no_client(self, adapter):
        """send_message should return False if no client connected."""
        result = await adapter.send_message(999, "channel_1", "#test", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_closed_client(self, adapter):
        mock_client = MagicMock()
        mock_client.is_closed.return_value = True
        adapter._clients[1] = mock_client

        result = await adapter.send_message(1, "channel_1", "#test", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_forbidden(self, adapter):
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock(side_effect=discord.Forbidden(
            MagicMock(status=403), "Missing perms"
        ))

        mock_client = MagicMock()
        mock_client.is_closed.return_value = False
        mock_client.get_channel.return_value = mock_channel
        adapter._clients[1] = mock_client

        result = await adapter.send_message(1, "channel_1", "#test", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_splits_long_text(self, adapter):
        """Long messages should be split and sent as multiple chunks."""
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        mock_client = MagicMock()
        mock_client.is_closed.return_value = False
        mock_client.get_channel.return_value = mock_channel
        adapter._clients[1] = mock_client

        long_msg = "x" * 4500
        result = await adapter.send_message(1, "channel_1", "#test", long_msg)
        assert result is True
        assert mock_channel.send.await_count == 3  # 2000+2000+500

    @pytest.mark.asyncio
    async def test_send_file(self, adapter):
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        mock_client = MagicMock()
        mock_client.is_closed.return_value = False
        mock_client.get_channel.return_value = mock_channel
        adapter._clients[1] = mock_client

        with patch("app.platforms.discord.adapter.discord.File") as MockFile:
            MockFile.return_value = "fake_file_obj"
            result = await adapter.send_file(
                1, "channel_1", "/path/to/file.pdf",
                caption="Check this out", file_type="application/pdf", chat_name="#test",
            )

        assert result is True
        mock_channel.send.assert_awaited_once_with(content="Check this out", file="fake_file_obj")

    @pytest.mark.asyncio
    async def test_send_file_no_caption(self, adapter):
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        mock_client = MagicMock()
        mock_client.is_closed.return_value = False
        mock_client.get_channel.return_value = mock_channel
        adapter._clients[1] = mock_client

        with patch("app.platforms.discord.adapter.discord.File") as MockFile:
            MockFile.return_value = "file"
            await adapter.send_file(1, "channel_1", "/path/to/file.pdf")

        mock_channel.send.assert_awaited_once_with(content=None, file="file")


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------

class TestResolveChannel:

    @pytest.mark.asyncio
    async def test_resolve_dm_channel(self, adapter):
        dm_channel = MagicMock()
        user = MagicMock()
        user.create_dm = AsyncMock(return_value=dm_channel)

        client = MagicMock()
        client.fetch_user = AsyncMock(return_value=user)

        result = await adapter._resolve_channel(client, "dm_12345")
        assert result is dm_channel
        client.fetch_user.assert_awaited_once_with(12345)

    @pytest.mark.asyncio
    async def test_resolve_server_channel_cached(self, adapter):
        channel = MagicMock()
        client = MagicMock()
        client.get_channel.return_value = channel

        result = await adapter._resolve_channel(client, "channel_67890")
        assert result is channel
        client.get_channel.assert_called_once_with(67890)

    @pytest.mark.asyncio
    async def test_resolve_server_channel_fetch(self, adapter):
        fetched = MagicMock()
        client = MagicMock()
        client.get_channel.return_value = None
        client.fetch_channel = AsyncMock(return_value=fetched)

        result = await adapter._resolve_channel(client, "channel_67890")
        assert result is fetched

    @pytest.mark.asyncio
    async def test_resolve_unknown_format(self, adapter):
        client = MagicMock()
        result = await adapter._resolve_channel(client, "unknown_123")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_error(self, adapter):
        client = MagicMock()
        client.fetch_user = AsyncMock(side_effect=discord.NotFound(
            MagicMock(status=404), "Unknown user"
        ))

        result = await adapter._resolve_channel(client, "dm_99999")
        assert result is None


# ---------------------------------------------------------------------------
# Cleanup, get_contacts, get_groups
# ---------------------------------------------------------------------------

class TestCleanupAndLists:

    def test_cleanup_removes_client(self, adapter):
        client = MagicMock()
        client.is_closed.return_value = True
        adapter._clients[1] = client

        adapter.cleanup(1)
        assert 1 not in adapter._clients

    def test_cleanup_nonexistent_bot(self, adapter):
        # Should not raise
        adapter.cleanup(999)

    def test_get_contacts_no_client(self, adapter):
        assert adapter.get_contacts(999) == []

    def test_get_contacts(self, adapter):
        member1 = MagicMock()
        member1.id = 100
        member1.display_name = "Alice"
        member1.bot = False
        member1.display_avatar = MagicMock()
        member1.display_avatar.url = "https://cdn.discordapp.com/alice.png"

        member2 = MagicMock()
        member2.id = 200
        member2.display_name = "Bob"
        member2.bot = False
        member2.display_avatar = None

        bot_member = MagicMock()
        bot_member.id = 300
        bot_member.bot = True

        guild = MagicMock()
        guild.members = [member1, member2, bot_member]

        client = MagicMock()
        client.guilds = [guild]
        adapter._clients[1] = client

        contacts = adapter.get_contacts(1)
        assert len(contacts) == 2
        assert contacts[0]["chat_id"] == "dm_100"
        assert contacts[0]["name"] == "Alice"
        assert contacts[1]["chat_id"] == "dm_200"
        assert contacts[1]["profile_pic"] == ""

    def test_get_contacts_deduplicates(self, adapter):
        """Same user in multiple guilds should appear once."""
        member = MagicMock()
        member.id = 100
        member.display_name = "Alice"
        member.bot = False
        member.display_avatar = None

        guild1 = MagicMock()
        guild1.members = [member]
        guild2 = MagicMock()
        guild2.members = [member]

        client = MagicMock()
        client.guilds = [guild1, guild2]
        adapter._clients[1] = client

        contacts = adapter.get_contacts(1)
        assert len(contacts) == 1

    def test_get_groups(self, adapter):
        channel1 = MagicMock()
        channel1.id = 111
        channel1.name = "general"

        channel2 = MagicMock()
        channel2.id = 222
        channel2.name = "dev"

        guild = MagicMock()
        guild.name = "MyServer"
        guild.text_channels = [channel1, channel2]
        guild.member_count = 50

        client = MagicMock()
        client.guilds = [guild]
        adapter._clients[1] = client

        groups = adapter.get_groups(1)
        assert len(groups) == 2
        assert groups[0] == {
            "chat_id": "channel_111",
            "name": "MyServer / #general",
            "member_count": 50,
        }

    def test_get_groups_no_client(self, adapter):
        assert adapter.get_groups(999) == []


# ---------------------------------------------------------------------------
# run_in_executor usage verification (no deadlock)
# ---------------------------------------------------------------------------

class TestExecutorUsage:
    """Verify that blocking calls go through run_in_executor, not directly."""

    @pytest.mark.asyncio
    async def test_db_operations_run_in_executor(self, adapter, mock_instance, mock_ai_provider):
        """DB save and broadcast should be dispatched to thread pool."""
        message = _make_discord_message(content="test executor")
        mock_client = MagicMock()
        mock_client.user = _make_discord_user(user_id=200, name="Bot", bot=True)

        fake_conversation = MagicMock()
        fake_conversation.id = 10

        executor_calls = []
        original_run_in_executor = asyncio.get_event_loop().run_in_executor

        async def tracking_executor(executor, fn, *args):
            executor_calls.append(fn.__name__ if hasattr(fn, '__name__') else str(fn))
            return fn(*args) if args else fn()

        with patch("app.platforms.discord.adapter.get_db_session") as mock_db_ctx, \
             patch("app.platforms.discord.adapter.find_or_create_conversation", return_value=fake_conversation), \
             patch("app.platforms.discord.adapter.is_duplicate_message", return_value=(False, None)), \
             patch("app.platforms.discord.adapter.save_user_message", return_value=MagicMock(id=1)), \
             patch("app.platforms.discord.adapter.update_conversation_stats"), \
             patch("app.platforms.discord.adapter.broadcast_user_message"), \
             patch("app.platforms.discord.adapter.is_human_takeover_active", return_value=False), \
             patch("app.platforms.discord.adapter.broadcast_typing"), \
             patch("app.platforms.discord.adapter.build_ai_messages", return_value=[]), \
             patch("app.platforms.discord.adapter.save_assistant_message"), \
             patch("app.platforms.discord.adapter.broadcast_assistant_message"), \
             patch("app.platforms.discord.adapter.log_activity"):

            mock_db = MagicMock()
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            loop = asyncio.get_running_loop()
            with patch.object(loop, "run_in_executor", side_effect=tracking_executor):
                await adapter._handle_message(
                    message=message,
                    client=mock_client,
                    instance=mock_instance,
                    config=mock_instance.config,
                    ai_provider=mock_ai_provider,
                    is_dm=True,
                )

        # Should have at least 3 executor calls: _db_save_and_check, _generate_ai_response, _db_save_response
        assert len(executor_calls) >= 3, f"Expected >=3 executor calls, got {len(executor_calls)}: {executor_calls}"
