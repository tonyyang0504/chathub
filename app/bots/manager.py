"""
Bot Instance Manager
Manages running bot instances for multiple users.
"""

import asyncio
import logging
from typing import Dict, Optional, Callable
from datetime import datetime
import threading

logger = logging.getLogger(__name__)


class BotInstance:
    """Represents a running bot instance."""

    def __init__(self, bot_profile_id: int, config: dict):
        self.bot_profile_id = bot_profile_id
        self.config = config
        self.is_running = False
        self.whatsapp_connected = False
        self.qr_code: Optional[str] = None
        self.error: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.qr_callbacks: list = []
        self.status_callbacks: list = []
        self.last_status: Optional[dict] = None
        self.stopped_by_user: bool = False  # Flag to track intentional stops
        self.outbound_queue: list = []  # Queue for scheduled/proactive messages
        self._queue_lock = threading.Lock()  # Thread-safe queue access

    def add_qr_callback(self, callback: Callable):
        """Add callback for QR code updates."""
        self.qr_callbacks.append(callback)

    def remove_qr_callback(self, callback: Callable):
        """Remove QR callback."""
        if callback in self.qr_callbacks:
            self.qr_callbacks.remove(callback)

    def add_status_callback(self, callback: Callable):
        """Add callback for status updates."""
        self.status_callbacks.append(callback)

    def remove_status_callback(self, callback: Callable):
        """Remove status callback."""
        if callback in self.status_callbacks:
            self.status_callbacks.remove(callback)

    async def notify_qr(self, qr_code: str):
        """Notify all QR callbacks."""
        self.qr_code = qr_code
        logger.info(f"Bot {self.bot_profile_id}: Notifying {len(self.qr_callbacks)} QR callbacks")
        for callback in self.qr_callbacks.copy():  # Use copy to avoid modification during iteration
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(qr_code)
                else:
                    callback(qr_code)
            except Exception as e:
                logger.error(f"QR callback error: {e}")

    async def notify_status(self, status: dict):
        """Notify all status callbacks."""
        self.last_status = status  # Store last status for late subscribers
        logger.info(f"Bot {self.bot_profile_id}: Notifying {len(self.status_callbacks)} status callbacks: {status.get('message', status)}")
        for callback in self.status_callbacks.copy():  # Use copy to avoid modification during iteration
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(status)
                else:
                    callback(status)
            except Exception as e:
                logger.error(f"Status callback error: {e}")

    def queue_outbound_message(self, chat_id: str, message: str):
        """Queue an outbound message to be sent by the bot.

        Args:
            chat_id: WhatsApp chat ID (e.g., "1234567890@c.us")
            message: Message text to send
        """
        with self._queue_lock:
            self.outbound_queue.append({
                'chat_id': chat_id,
                'message': message,
                'queued_at': datetime.now()
            })
            logger.info(f"Bot {self.bot_profile_id}: Queued outbound message for {chat_id}")

    def get_outbound_messages(self) -> list:
        """Get and clear all pending outbound messages.

        Returns:
            List of message dicts with 'chat_id' and 'message' keys
        """
        with self._queue_lock:
            messages = self.outbound_queue.copy()
            self.outbound_queue.clear()
            return messages

    def has_outbound_messages(self) -> bool:
        """Check if there are pending outbound messages."""
        with self._queue_lock:
            return len(self.outbound_queue) > 0


class BotManager:
    """Manages all bot instances."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.instances: Dict[int, BotInstance] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def get_instance(self, bot_profile_id: int) -> Optional[BotInstance]:
        """Get a bot instance by profile ID."""
        return self.instances.get(bot_profile_id)

    def create_instance(self, bot_profile_id: int, config: dict) -> BotInstance:
        """Create a new bot instance."""
        if bot_profile_id in self.instances:
            # Return existing instance
            return self.instances[bot_profile_id]

        instance = BotInstance(bot_profile_id, config)
        self.instances[bot_profile_id] = instance
        return instance

    def remove_instance(self, bot_profile_id: int):
        """Remove a bot instance."""
        if bot_profile_id in self.instances:
            instance = self.instances[bot_profile_id]
            if instance.task and not instance.task.done():
                instance.task.cancel()
            del self.instances[bot_profile_id]

    async def start_bot(self, bot_profile_id: int, config: dict) -> BotInstance:
        """Start a bot instance."""
        logger.info(f"start_bot called for profile {bot_profile_id}")
        instance = self.create_instance(bot_profile_id, config)

        if instance.is_running:
            logger.info(f"Bot {bot_profile_id} already running")
            return instance

        # Import here to avoid circular imports
        from app.bots.whatsapp_bot import run_whatsapp_bot

        # Create task for running the bot
        logger.info(f"Creating task for bot {bot_profile_id}")
        instance.is_running = True
        instance.stopped_by_user = False  # Reset the flag when starting
        instance.error = None  # Clear any previous error

        async def run_bot_with_error_handling():
            try:
                await run_whatsapp_bot(instance)
            except Exception as e:
                logger.error(f"Bot {bot_profile_id} crashed: {e}", exc_info=True)
                instance.error = str(e)
                instance.is_running = False
                await instance.notify_status({"error": str(e), "message": f"Bot error: {e}"})

        instance.task = asyncio.create_task(run_bot_with_error_handling())

        logger.info(f"Started bot instance for profile {bot_profile_id}")
        return instance

    async def stop_bot(self, bot_profile_id: int) -> bool:
        """Stop a bot instance."""
        instance = self.get_instance(bot_profile_id)
        if not instance:
            return False

        # Mark as intentionally stopped by user (before cancelling task)
        instance.stopped_by_user = True
        instance.error = None  # Clear any previous error

        if instance.task and not instance.task.done():
            instance.task.cancel()
            try:
                await instance.task
            except asyncio.CancelledError:
                pass

        instance.is_running = False
        instance.whatsapp_connected = False

        logger.info(f"Stopped bot instance for profile {bot_profile_id}")
        return True

    def get_status(self, bot_profile_id: int) -> dict:
        """Get status of a bot instance."""
        instance = self.get_instance(bot_profile_id)
        if not instance:
            return {
                "is_running": False,
                "whatsapp_connected": False,
                "error": None
            }

        return {
            "is_running": instance.is_running,
            "whatsapp_connected": instance.whatsapp_connected,
            "qr_code": instance.qr_code,
            "error": instance.error
        }

    def get_all_running(self) -> list:
        """Get all running bot IDs."""
        return [
            bot_id for bot_id, instance in self.instances.items()
            if instance.is_running
        ]

    async def stop_all_bots(self):
        """Stop all running bot instances."""
        bot_ids = list(self.instances.keys())
        for bot_id in bot_ids:
            try:
                await self.stop_bot(bot_id)
            except Exception as e:
                logger.error(f"Error stopping bot {bot_id}: {e}")
        self.instances.clear()
        logger.info("All bots stopped")

    def is_bot_healthy(self, bot_profile_id: int) -> bool:
        """Check if a bot instance is healthy (running and task not done)."""
        instance = self.get_instance(bot_profile_id)
        if not instance:
            return False

        # Check if instance is marked as running
        if not instance.is_running:
            return False

        # Check if task exists and is not done/cancelled
        if instance.task is None:
            return False

        if instance.task.done() or instance.task.cancelled():
            return False

        return True

    def needs_recovery(self, bot_profile_id: int, db_is_running: bool) -> bool:
        """Check if a bot needs recovery (DB says running but no active instance)."""
        if not db_is_running:
            return False

        return not self.is_bot_healthy(bot_profile_id)

    async def recover_bot(self, bot_profile_id: int, config: dict) -> BotInstance:
        """Recover a bot that should be running but isn't."""
        logger.info(f"Recovering bot {bot_profile_id}")

        # Clean up any existing dead instance
        existing = self.get_instance(bot_profile_id)
        if existing:
            existing.is_running = False
            if existing.task and not existing.task.done():
                existing.task.cancel()
                try:
                    await existing.task
                except asyncio.CancelledError:
                    pass
            self.remove_instance(bot_profile_id)

        # Start fresh
        return await self.start_bot(bot_profile_id, config)

    def get_health_status(self, bot_profile_id: int) -> dict:
        """Get detailed health status of a bot."""
        instance = self.get_instance(bot_profile_id)

        if not instance:
            return {
                "has_instance": False,
                "is_running": False,
                "task_active": False,
                "whatsapp_connected": False,
                "error": None,
                "needs_recovery": False
            }

        task_active = instance.task is not None and not instance.task.done() and not instance.task.cancelled()

        return {
            "has_instance": True,
            "is_running": instance.is_running,
            "task_active": task_active,
            "whatsapp_connected": instance.whatsapp_connected,
            "error": instance.error,
            "needs_recovery": instance.is_running and not task_active
        }


# Global bot manager instance
bot_manager = BotManager()
