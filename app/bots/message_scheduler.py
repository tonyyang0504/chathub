"""
Scheduled Message Scheduler

Background task that checks for pending scheduled messages and sends them at the appropriate time.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy.orm import Session
from app.database import SessionLocal, ScheduledMessage, BotProfile

logger = logging.getLogger(__name__)


class MessageScheduler:
    def __init__(self):
        self._task = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Message scheduler started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Message scheduler stopped")

    async def _run(self):
        while self._running:
            try:
                await self._check_and_send()
            except Exception as e:
                logger.error(f"Message scheduler error: {e}")
            await asyncio.sleep(30)  # Check every 30 seconds

    async def _check_and_send(self):
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            pending = db.query(ScheduledMessage).filter(
                ScheduledMessage.is_active == True,
                ScheduledMessage.is_sent == False,
                ScheduledMessage.scheduled_time <= now
            ).all()

            for msg in pending:
                try:
                    bot = db.query(BotProfile).filter(BotProfile.id == msg.bot_profile_id).first()
                    if not bot or not bot.is_running:
                        logger.warning(f"Scheduled message {msg.id}: bot {msg.bot_profile_id} not running, skipping")
                        continue

                    from app.bots.whatsapp_bot import send_whatsapp_message
                    success = await send_whatsapp_message(
                        bot_profile_id=msg.bot_profile_id,
                        chat_id=msg.chat_id,
                        chat_name=msg.chat_name or "",
                        message=msg.message
                    )

                    if success:
                        msg.is_sent = True
                        db.commit()
                        logger.info(f"Scheduled message {msg.id} sent to {msg.chat_name} ({msg.chat_id})")
                    else:
                        logger.error(f"Scheduled message {msg.id} failed to send")

                except Exception as e:
                    logger.error(f"Error sending scheduled message {msg.id}: {e}")

        finally:
            db.close()


message_scheduler = MessageScheduler()
