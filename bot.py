"""
WhatsApp Bot - Main Entry Point.
Handles WhatsApp Web automation and message processing.
"""

import asyncio
import logging
import random
import time
import os
import json
from typing import Optional, Callable
from datetime import datetime

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from config import Config
from database import Database
from conversation import ConversationManager
from ai_handler import AIHandler

# Setup logging
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class WhatsAppBot:
    """WhatsApp Bot with AI-powered responses."""

    WHATSAPP_WEB_URL = "https://web.whatsapp.com"

    def __init__(
        self,
        system_prompt: str = None,
        on_message_callback: Callable = None
    ):
        """
        Initialize WhatsApp Bot.

        Args:
            system_prompt: Custom AI system prompt
            on_message_callback: Optional callback for custom message handling
        """
        # Validate config
        Config.validate()

        # Initialize components
        self.db = Database(Config.DATABASE_PATH)
        self.conversations = ConversationManager(self.db)
        self.ai = AIHandler(system_prompt=system_prompt)

        # Playwright components
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # State
        self.is_running = False
        self.is_authenticated = False
        self.on_message_callback = on_message_callback

        # Track processed messages to avoid duplicates
        self.processed_messages = set()

    async def start(self):
        """Start the WhatsApp bot."""
        logger.info("Starting WhatsApp Bot...")

        try:
            self.playwright = await async_playwright().start()

            # Launch browser
            self.browser = await self.playwright.chromium.launch(
                headless=False,  # Show browser for QR code scanning
                args=['--no-sandbox']
            )

            # Create context with persistent storage for session
            os.makedirs(Config.SESSION_PATH, exist_ok=True)
            self.context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

            # Load session if exists
            session_file = os.path.join(Config.SESSION_PATH, "session.json")
            if os.path.exists(session_file):
                try:
                    with open(session_file, 'r') as f:
                        cookies = json.load(f)
                    await self.context.add_cookies(cookies)
                    logger.info("Loaded existing session")
                except Exception as e:
                    logger.warning(f"Could not load session: {e}")

            self.page = await self.context.new_page()

            # Navigate to WhatsApp Web
            logger.info("Opening WhatsApp Web...")
            await self.page.goto(self.WHATSAPP_WEB_URL)

            # Wait for authentication
            await self._wait_for_auth()

            # Save session
            await self._save_session()

            # Start message listener
            self.is_running = True
            await self._listen_for_messages()

        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            raise
        finally:
            await self.stop()

    async def _wait_for_auth(self):
        """Wait for user to authenticate via QR code."""
        logger.info("Waiting for authentication...")
        logger.info("Please scan the QR code with your WhatsApp mobile app")

        # Wait for either QR code or already logged in state
        try:
            # Check if already authenticated (chat list visible)
            chat_list = await self.page.wait_for_selector(
                '[data-testid="chat-list"]',
                timeout=120000  # 2 minutes
            )
            if chat_list:
                self.is_authenticated = True
                logger.info("Authentication successful!")
                return

        except Exception:
            logger.error("Authentication timeout. Please scan the QR code faster.")
            raise

    async def _save_session(self):
        """Save browser session for future use."""
        try:
            cookies = await self.context.cookies()
            session_file = os.path.join(Config.SESSION_PATH, "session.json")
            with open(session_file, 'w') as f:
                json.dump(cookies, f)
            logger.info("Session saved")
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    async def _listen_for_messages(self):
        """Listen for incoming messages."""
        logger.info("Listening for messages...")

        last_check = datetime.now()

        while self.is_running:
            try:
                # Find unread messages
                unread_chats = await self.page.query_selector_all(
                    '[data-testid="cell-frame-container"] [data-testid="icon-unread-count"]'
                )

                for unread_indicator in unread_chats:
                    try:
                        # Click on the chat
                        chat_container = await unread_indicator.evaluate_handle(
                            'el => el.closest(\'[data-testid="cell-frame-container"]\')'
                        )
                        await chat_container.click()
                        await asyncio.sleep(1)

                        # Get chat info
                        chat_info = await self._get_current_chat_info()
                        if not chat_info:
                            continue

                        # Check if we should respond (group chat rules)
                        if not self._should_respond(chat_info):
                            continue

                        # Get latest messages
                        messages = await self._get_latest_messages()
                        if not messages:
                            continue

                        # Process each new message
                        for msg in messages:
                            msg_id = f"{chat_info['chat_id']}_{msg['timestamp']}"
                            if msg_id in self.processed_messages:
                                continue

                            self.processed_messages.add(msg_id)

                            # Only process incoming messages (not our own)
                            if not msg.get('is_outgoing', False):
                                await self._handle_message(chat_info, msg)

                    except Exception as e:
                        logger.error(f"Error processing chat: {e}")

                # Small delay between checks
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"Error in message listener: {e}")
                await asyncio.sleep(5)

    async def _get_current_chat_info(self) -> Optional[dict]:
        """Get information about the currently open chat."""
        try:
            # Get chat header info
            header = await self.page.query_selector('[data-testid="conversation-header"]')
            if not header:
                return None

            # Get chat name
            name_elem = await header.query_selector('[data-testid="conversation-info-header-chat-title"]')
            chat_name = await name_elem.inner_text() if name_elem else "Unknown"

            # Check if group chat (has group icon or participants)
            is_group = await header.query_selector('[data-testid="group-icon"]') is not None

            # Get chat ID from URL or element
            chat_id = chat_name.replace(" ", "_")  # Simplified

            return {
                "chat_id": chat_id,
                "chat_name": chat_name,
                "is_group": is_group
            }

        except Exception as e:
            logger.error(f"Error getting chat info: {e}")
            return None

    async def _get_latest_messages(self, limit: int = 5) -> list:
        """Get latest messages from current chat."""
        messages = []
        try:
            # Get message containers
            msg_containers = await self.page.query_selector_all(
                '[data-testid="msg-container"]'
            )

            for container in msg_containers[-limit:]:
                try:
                    # Get message text
                    text_elem = await container.query_selector('[data-testid="msg-text"]')
                    if not text_elem:
                        continue

                    text = await text_elem.inner_text()

                    # Check if outgoing
                    is_outgoing = await container.evaluate(
                        'el => el.closest(\'[data-testid="conv-msg-true"]\') !== null'
                    )

                    # Get sender for group chats
                    sender_elem = await container.query_selector('[data-testid="author"]')
                    sender = await sender_elem.inner_text() if sender_elem else None

                    # Get timestamp
                    time_elem = await container.query_selector('[data-testid="msg-meta"]')
                    timestamp = await time_elem.inner_text() if time_elem else str(time.time())

                    messages.append({
                        "content": text,
                        "is_outgoing": is_outgoing,
                        "sender": sender,
                        "timestamp": timestamp
                    })

                except Exception as e:
                    logger.debug(f"Error parsing message: {e}")

        except Exception as e:
            logger.error(f"Error getting messages: {e}")

        return messages

    def _should_respond(self, chat_info: dict) -> bool:
        """
        Check if bot should respond to this chat.

        Args:
            chat_info: Chat information dict

        Returns:
            True if should respond
        """
        # Check group chat settings
        if chat_info.get('is_group', False):
            if not Config.GROUP_CHAT_ENABLED:
                return False

            # If not responding to all in group, check for mention
            if not Config.RESPOND_TO_ALL_IN_GROUP:
                # Would need to check if bot was mentioned
                # For now, respond to all
                pass

        return True

    async def _handle_message(self, chat_info: dict, message: dict):
        """
        Handle an incoming message.

        Args:
            chat_info: Chat information
            message: Message data
        """
        chat_id = chat_info['chat_id']
        content = message['content']
        sender = message.get('sender')
        is_group = chat_info.get('is_group', False)

        logger.info(f"[{chat_id}] Received: {content[:50]}...")

        # Custom callback if provided
        if self.on_message_callback:
            result = self.on_message_callback(chat_info, message)
            if result is False:  # Explicitly return False to skip
                return

        # Store user message
        self.conversations.add_message(
            chat_id=chat_id,
            role="user",
            content=content,
            sender_name=sender,
            is_group=is_group
        )

        # Get conversation history
        history = self.conversations.get_history(chat_id, Config.MAX_HISTORY)

        # Generate AI response
        try:
            response = self.ai.generate_response(history)
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            response = "Sorry, I encountered an error. Please try again."

        # Store assistant response
        self.conversations.add_message(
            chat_id=chat_id,
            role="assistant",
            content=response,
            is_group=is_group
        )

        # Add human-like delay
        delay = random.uniform(
            Config.RESPONSE_DELAY_MIN,
            Config.RESPONSE_DELAY_MAX
        )
        logger.debug(f"Waiting {delay:.1f}s before responding...")
        await asyncio.sleep(delay)

        # Send response
        await self._send_message(response)
        logger.info(f"[{chat_id}] Sent: {response[:50]}...")

    async def _send_message(self, text: str):
        """
        Send a message to the current chat.

        Args:
            text: Message text to send
        """
        try:
            # Show typing indicator if enabled
            if Config.SHOW_TYPING_INDICATOR:
                # Click on input box to focus
                input_box = await self.page.wait_for_selector(
                    '[data-testid="conversation-compose-box-input"]',
                    timeout=5000
                )
                await input_box.click()
                await asyncio.sleep(0.5)

            # Type the message
            input_box = await self.page.query_selector(
                '[data-testid="conversation-compose-box-input"]'
            )
            await input_box.fill(text)

            # Small delay to simulate typing
            await asyncio.sleep(0.3)

            # Click send button
            send_button = await self.page.query_selector(
                '[data-testid="send"]'
            )
            await send_button.click()

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            raise

    async def send_message_to_chat(self, chat_name: str, text: str):
        """
        Send a message to a specific chat.

        Args:
            chat_name: Name of the chat to send to
            text: Message text
        """
        try:
            # Search for chat
            search_box = await self.page.wait_for_selector(
                '[data-testid="chat-list-search"]',
                timeout=5000
            )
            await search_box.fill(chat_name)
            await asyncio.sleep(1)

            # Click on the chat
            chat_item = await self.page.wait_for_selector(
                f'[data-testid="cell-frame-container"] span[title="{chat_name}"]',
                timeout=5000
            )
            await chat_item.click()
            await asyncio.sleep(0.5)

            # Send the message
            await self._send_message(text)

        except Exception as e:
            logger.error(f"Error sending message to {chat_name}: {e}")
            raise

    async def stop(self):
        """Stop the bot and cleanup resources."""
        logger.info("Stopping WhatsApp Bot...")
        self.is_running = False

        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

        self.db.close()
        logger.info("Bot stopped")

    def set_system_prompt(self, prompt: str):
        """
        Update the AI system prompt.

        Args:
            prompt: New system prompt
        """
        self.ai.set_system_prompt(prompt)

    def clear_conversation(self, chat_id: str):
        """
        Clear conversation history for a chat.

        Args:
            chat_id: Chat identifier
        """
        self.conversations.clear(chat_id)
        logger.info(f"Cleared conversation for {chat_id}")


async def main():
    """Main entry point."""
    # Example usage
    bot = WhatsAppBot(
        system_prompt=Config.SYSTEM_PROMPT
    )

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
