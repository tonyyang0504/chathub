"""
Slack Platform Adapter
Uses Slack Bolt SDK with Socket Mode for real-time messaging.

Auth: Bot Token (xoxb-) + App Token (xapp-) from api.slack.com
Socket Mode = WebSocket connection, no webhook/ngrok needed.

Setup:
1. Create app at api.slack.com
2. Enable Socket Mode → generate App Token (xapp-)
3. Add Bot Token Scopes: chat:write, files:write, users:read, channels:read, im:read, groups:read
4. Enable Event Subscriptions: message.im, message.channels, message.groups, app_mention
5. Install app to workspace → get Bot Token (xoxb-)
"""

import asyncio
import logging
import random
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)

logger = logging.getLogger(__name__)

# Active Slack apps keyed by bot_profile_id
_active_apps: Dict[int, Any] = {}
_active_handlers: Dict[int, Any] = {}


class SlackAdapter(PlatformAdapter):
    """Slack adapter using Bolt SDK with Socket Mode."""

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.SLACK

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=True,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=True,
            supports_read_receipts=True,
            supports_typing_indicator=False,
            supports_history_sync=True,
            supports_contacts_list=True,
            supports_groups_list=True,
            supports_profile_pic=True,
            auth_method=AuthMethod.API_TOKEN,
            max_message_length=4000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "video/mp4",
                "audio/mpeg", "audio/ogg",
                "application/pdf",
            ],
        )

    async def run(self, instance) -> None:
        """Start Slack bot via Socket Mode."""
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        config = instance.config
        bot_profile_id = instance.bot_profile_id

        # Get tokens
        bot_token = config.get("platform_token")
        app_token = config.get("slack_app_token")

        if not bot_token:
            instance.error = "No Slack Bot Token configured (xoxb-)"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            return

        if not app_token:
            instance.error = "No Slack App Token configured (xapp-). Enable Socket Mode in your Slack app settings."
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            return

        # Get AI config
        from app.auth.utils import decrypt_string
        encrypted_key = config.get("api_key_encrypted", "")
        ai_api_key = decrypt_string(encrypted_key) if encrypted_key else ""

        # Create Bolt app
        app = AsyncApp(token=bot_token)
        _active_apps[bot_profile_id] = app

        # Get bot user ID to filter self-messages
        try:
            auth_result = await app.client.auth_test()
            bot_user_id = auth_result["user_id"]
            bot_name = auth_result.get("user", "SlackBot")
            team_name = auth_result.get("team", "Workspace")
            logger.info(f"Bot {bot_profile_id}: Slack connected as {bot_name} in {team_name}")
        except Exception as e:
            instance.error = f"Invalid Slack Bot Token: {e}"
            instance.is_running = False
            await instance.notify_status({"error": instance.error, "message": instance.error})
            _active_apps.pop(bot_profile_id, None)
            return

        # Save account info to DB
        try:
            from app.database import SessionLocal, BotProfile as BPModel
            _db = SessionLocal()
            try:
                _bot = _db.query(BPModel).filter(BPModel.id == bot_profile_id).first()
                if _bot:
                    _bot.whatsapp_name = f"{bot_name} ({team_name})"
                    _db.commit()
            finally:
                _db.close()
        except Exception:
            pass

        # Create handler context
        handler = _HandlerContext(
            bot_profile_id=bot_profile_id,
            instance=instance,
            ai_api_key=ai_api_key,
            config=config,
            app=app,
            bot_user_id=bot_user_id,
        )
        _active_handlers[bot_profile_id] = handler

        # Register event handlers
        @app.event("message")
        async def handle_message(event, say, client):
            await handler.handle_message(event, say, client)

        @app.event("app_mention")
        async def handle_mention(event, say, client):
            await handler.handle_message(event, say, client)

        # Notify connected
        instance.whatsapp_connected = True
        await instance.notify_status({
            "message": f"Slack connected: {bot_name} in {team_name}",
            "connected": True,
            "platform": "slack",
            "status": "running",
            "account_info": {
                "name": f"{bot_name} ({team_name})",
            },
        })

        # Start Socket Mode
        socket_handler = AsyncSocketModeHandler(app, app_token)

        try:
            await socket_handler.connect_async()
            logger.info(f"Bot {bot_profile_id}: Slack Socket Mode connected")

            # Keep alive + process outbound queue
            while instance.is_running and not instance.stopped_by_user:
                # History sync
                if getattr(instance, 'history_sync_requested', False):
                    instance.history_sync_requested = False
                    instance.history_sync_active = True
                    try:
                        await self._sync_history(app, instance, bot_profile_id, bot_user_id)
                    except Exception as e:
                        logger.error(f"Bot {bot_profile_id}: History sync error: {e}", exc_info=True)
                    finally:
                        instance.history_sync_active = False

                # Outbound messages
                if instance.has_outbound_messages():
                    outbound = instance.get_outbound_messages()
                    for msg in outbound:
                        try:
                            await app.client.chat_postMessage(
                                channel=msg["chat_id"],
                                text=msg["message"],
                            )
                        except Exception as e:
                            logger.error(f"Bot {bot_profile_id}: Outbound send error: {e}")

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_profile_id}: Slack task cancelled")
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Slack error: {e}", exc_info=True)
            instance.error = str(e)
            await instance.notify_status({"error": str(e), "message": f"Slack error: {e}"})
        finally:
            try:
                await socket_handler.close_async()
            except Exception:
                pass
            _active_apps.pop(bot_profile_id, None)
            _active_handlers.pop(bot_profile_id, None)
            instance.whatsapp_connected = False
            instance.is_running = False
            logger.info(f"Bot {bot_profile_id}: Slack adapter stopped")

    async def _sync_history(self, app, instance, bot_profile_id, bot_user_id):
        """Sync conversation history from Slack."""
        from app.platforms.message_handler import (
            get_db_session, find_or_create_conversation,
            save_user_message, update_conversation_stats,
        )

        sync_count = getattr(instance, 'history_sync_count', 50)
        logger.info(f"Bot {bot_profile_id}: Starting Slack history sync")

        instance.history_sync_progress = {
            "total": 0, "completed": 0, "current_chat": "", "status": "running"
        }

        try:
            # Get recent conversations
            result = await app.client.conversations_list(
                types="im,mpim,public_channel,private_channel",
                limit=min(sync_count, 100),
            )
            channels = result.get("channels", [])
            total = len(channels)
            instance.history_sync_progress["total"] = total

            for i, channel in enumerate(channels):
                if getattr(instance, 'history_sync_stop_requested', False):
                    break

                ch_id = channel["id"]
                ch_name = channel.get("name") or channel.get("user") or ch_id
                is_im = channel.get("is_im", False)
                is_group = not is_im

                if is_im:
                    # Get user name for DMs
                    try:
                        user_info = await app.client.users_info(user=channel.get("user", ""))
                        ch_name = user_info["user"]["real_name"] or user_info["user"]["name"]
                    except Exception:
                        pass
                else:
                    ch_name = f"#{ch_name}"

                instance.history_sync_progress["current_chat"] = ch_name
                instance.history_sync_progress["completed"] = i

                # Fetch recent messages
                try:
                    history = await app.client.conversations_history(channel=ch_id, limit=20)
                    messages = history.get("messages", [])
                except Exception as e:
                    logger.warning(f"Bot {bot_profile_id}: Failed to get history for {ch_name}: {e}")
                    continue

                if not messages:
                    continue

                with get_db_session() as db:
                    conversation = find_or_create_conversation(
                        db, bot_profile_id, ch_id, ch_name,
                        is_group=is_group, display_name=ch_name,
                    )

                    for msg in reversed(messages):
                        text = msg.get("text", "")
                        if not text or msg.get("subtype"):
                            continue
                        user_id = msg.get("user", "")
                        if user_id == bot_user_id:
                            continue  # Skip bot's own messages

                        # Get user name
                        sender_name = user_id
                        try:
                            ui = await app.client.users_info(user=user_id)
                            sender_name = ui["user"].get("real_name") or ui["user"].get("name", user_id)
                        except Exception:
                            pass

                        from datetime import datetime
                        ts = msg.get("ts", "")
                        msg_time = None
                        if ts:
                            try:
                                msg_time = datetime.utcfromtimestamp(float(ts))
                            except (ValueError, TypeError):
                                pass

                        save_user_message(
                            db, conversation.id, text,
                            sender_name=sender_name,
                            sender_id=user_id,
                            platform_message_id=ts,
                            timestamp=msg_time,
                        )

                    update_conversation_stats(db, conversation.id)
                    db.commit()

            instance.history_sync_progress["completed"] = total
            instance.history_sync_progress["status"] = "completed"
            logger.info(f"Bot {bot_profile_id}: Slack history sync completed ({total} channels)")

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: History sync failed: {e}", exc_info=True)
            instance.history_sync_progress["status"] = "completed"

    async def send_message(
        self, bot_profile_id: int, chat_id: str, chat_name: str, message: str,
    ) -> bool:
        """Send a text message via Slack."""
        app = _active_apps.get(bot_profile_id)
        if not app:
            return False
        try:
            # Split long messages (Slack limit: 4000 chars)
            chunks = _split_message(message, 4000)
            for chunk in chunks:
                await app.client.chat_postMessage(channel=chat_id, text=chunk)
            return True
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send message: {e}")
            return False

    async def send_file(
        self, bot_profile_id: int, chat_id: str, file_path: str,
        caption: str = "", file_type: str = "", chat_name: str = "",
    ) -> bool:
        """Send a file via Slack."""
        app = _active_apps.get(bot_profile_id)
        if not app:
            return False
        try:
            path = Path(file_path)
            if not path.exists():
                return False
            await app.client.files_upload_v2(
                channel=chat_id,
                file=str(path),
                title=path.name,
                initial_comment=caption or None,
            )
            return True
        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Failed to send file: {e}")
            return False

    def cleanup(self, bot_profile_id: int) -> None:
        _active_apps.pop(bot_profile_id, None)
        _active_handlers.pop(bot_profile_id, None)

    def get_contacts(self, bot_profile_id: int):
        return []

    def get_groups(self, bot_profile_id: int):
        return []


def _split_message(text: str, max_length: int = 4000) -> list:
    if len(text) <= max_length:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1 or split_at < max_length // 2:
            split_at = text.rfind(" ", 0, max_length)
        if split_at == -1 or split_at < max_length // 2:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n ")
    return chunks


class _HandlerContext:
    """Holds per-bot context for processing Slack messages."""

    def __init__(self, bot_profile_id, instance, ai_api_key, config, app, bot_user_id):
        self.bot_profile_id = bot_profile_id
        self.instance = instance
        self.ai_api_key = ai_api_key
        self.config = config
        self.app = app
        self.bot_user_id = bot_user_id

    def _get_ai_provider(self):
        from app.ai.factory import get_ai_provider
        return get_ai_provider(
            provider_name=self.config.get("ai_provider", "openai"),
            api_key=self.ai_api_key,
            model=self.config.get("model"),
        )

    async def handle_message(self, event, say, client):
        """Handle incoming Slack message."""
        from app.platforms.message_handler import (
            get_db_session, find_or_create_conversation,
            save_user_message, save_assistant_message,
            update_conversation_stats, is_duplicate_message,
            has_response_after, build_ai_messages,
            is_human_takeover_active, is_sender_approved,
            broadcast_user_message, broadcast_assistant_message,
            broadcast_typing,
        )

        # Ignore bot's own messages and subtypes (joins, leaves, etc.)
        if event.get("user") == self.bot_user_id:
            return
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        message_ts = event.get("ts", "")

        # Determine if DM or channel
        is_dm = channel_type in ("im", "mpim")
        is_group = not is_dm

        # Get user info
        sender_name = user_id
        try:
            user_info = await client.users_info(user=user_id)
            sender_name = user_info["user"].get("real_name") or user_info["user"].get("name", user_id)
        except Exception:
            pass

        # Get channel name
        if is_dm:
            chat_name = sender_name
        else:
            try:
                ch_info = await client.conversations_info(channel=channel_id)
                chat_name = f"#{ch_info['channel'].get('name', channel_id)}"
            except Exception:
                chat_name = f"#{channel_id}"

        # Strip bot mention from text
        text = text.replace(f"<@{self.bot_user_id}>", "").strip()

        # In channels, only respond if mentioned (unless respond_to_all_in_group)
        if is_group and not self.config.get("respond_to_all_in_group", False):
            if f"<@{self.bot_user_id}>" not in event.get("text", ""):
                # Not mentioned — save message but don't respond
                return

        logger.info(f"Bot {self.bot_profile_id}: Slack message from {sender_name} ({channel_id}): {text[:80]}")

        with get_db_session() as db:
            conversation = find_or_create_conversation(
                db, self.bot_profile_id, channel_id, chat_name,
                is_group=is_group, display_name=chat_name,
            )

            # Dedup
            is_dup, existing = is_duplicate_message(
                db, conversation.id,
                platform_message_id=message_ts, content=text,
            )
            if is_dup and existing:
                if has_response_after(db, conversation.id, existing.id):
                    db.commit()
                    return
                db.commit()
                user_msg = existing
            else:
                user_msg = save_user_message(
                    db, conversation.id, text,
                    sender_name=sender_name, sender_id=user_id,
                    platform_message_id=message_ts,
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

            broadcast_user_message(conversation.id, self.bot_profile_id, user_msg, conversation)

            if not self.instance.ai_response_enabled:
                return

            if is_human_takeover_active(db, conversation.id):
                return

            if not is_sender_approved(db, conversation.id):
                return

            broadcast_typing(conversation.id, True, "Bot")

            # Response delay
            delay_min = self.config.get("response_delay_min", 0) or 0
            delay_max = self.config.get("response_delay_max", 0) or 0
            if delay_min > 0 or delay_max > 0:
                delay = random.uniform(delay_min, max(delay_min, delay_max))
                await asyncio.sleep(delay)

            try:
                system_prompt = self.config.get("system_prompt", "You are a helpful assistant.")
                ai_messages = build_ai_messages(
                    db, conversation.id, system_prompt,
                    max_history=self.config.get("max_history", 20) or 20,
                    is_group=is_group, current_message=user_msg,
                )

                provider = self._get_ai_provider()
                response = await asyncio.to_thread(
                    provider.chat_completion,
                    messages=ai_messages,
                    max_tokens=self.config.get("max_tokens", 1000),
                    temperature=self.config.get("temperature", 0.7),
                )
                ai_text = response.content

                if not ai_text:
                    return

                assistant_msg = save_assistant_message(
                    db, conversation.id, ai_text, sender_name="AI Agent",
                )
                update_conversation_stats(db, conversation.id)
                db.commit()

                broadcast_assistant_message(conversation.id, assistant_msg)

                # Send via Slack
                chunks = _split_message(ai_text, 4000)
                for chunk in chunks:
                    await say(chunk)

            except Exception as e:
                logger.error(f"Bot {self.bot_profile_id}: AI response error: {e}", exc_info=True)
            finally:
                broadcast_typing(conversation.id, False, "Bot")
