"""
Scripted Conversations Scheduler

Background task that executes scripted conversations at scheduled times.
"""
import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict

from sqlalchemy.orm import Session

from app.database import (
    SessionLocal, ConversationScript, ScriptMessage, ScriptExecution,
    Hub, BotProfile, Conversation, HubBotMembership
)
from app.bots.whatsapp_bot import send_whatsapp_message

logger = logging.getLogger(__name__)


def calculate_group_stagger_delay(
    group_count: int,
    sending_speed_mode: str = "auto",
    stagger_delay_min: int = None,
    stagger_delay_max: int = None
) -> Tuple[float, float]:
    """
    Calculate the delay range between group starts based on settings and group count.
    Returns (min_delay, max_delay) in seconds.

    Modes:
    - 'fast': Minimal delay (1-2s) regardless of group count
    - 'custom': Use provided stagger_delay_min/max values
    - 'auto': Scale delay based on number of groups
    """
    # Fast mode - minimal delay
    if sending_speed_mode == "fast":
        return (1, 2)

    # Custom mode - use provided values
    if sending_speed_mode == "custom":
        min_delay = stagger_delay_min if stagger_delay_min is not None else 3
        max_delay = stagger_delay_max if stagger_delay_max is not None else 6
        return (min_delay, max_delay)

    # Auto mode - scale based on group count
    if group_count <= 5:
        # No stagger needed for small group counts
        return (0, 0)
    elif group_count <= 15:
        # Light stagger: 2-4 seconds between groups
        return (2, 4)
    elif group_count <= 30:
        # Moderate stagger: 3-6 seconds between groups
        return (3, 6)
    elif group_count <= 50:
        # Higher stagger: 5-10 seconds between groups
        return (5, 10)
    else:
        # Heavy stagger for 50+ groups: 8-15 seconds between groups
        return (8, 15)


def convert_to_whatsapp_markdown(text: str) -> str:
    """
    Convert standard markdown to WhatsApp-compatible format.
    """
    if not text:
        return text

    # Remove markdown headers (###, ##, #) and make them bold
    text = re.sub(r'^#{1,6}\s*(.+)$', r'*\1*', text, flags=re.MULTILINE)

    # Convert **bold** to *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)

    # Convert __bold__ to *bold*
    text = re.sub(r'__(.+?)__', r'*\1*', text)

    # Convert ~~strikethrough~~ to ~strikethrough~
    text = re.sub(r'~~(.+?)~~', r'~\1~', text)

    # Clean up any double asterisks that might remain
    text = re.sub(r'\*\*+', '*', text)

    return text


class ScriptScheduler:
    """Background scheduler for executing scripted conversations."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 30  # Check every 30 seconds
        self._active_executions: Dict[int, asyncio.Task] = {}  # script_id -> task

    async def start(self):
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Script scheduler started.")

    async def stop(self):
        """Stop the scheduler."""
        self._running = False

        # Cancel all active executions
        for script_id, task in self._active_executions.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._active_executions.clear()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Script scheduler stopped.")

    def cancel_script(self, script_id: int):
        """Cancel a running script execution."""
        if script_id in self._active_executions:
            self._active_executions[script_id].cancel()
            logger.info(f"Cancelled active execution for script {script_id}")
        else:
            logger.info(f"Script {script_id} not in active executions")

    async def _run_loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                await self._check_scheduled_scripts()
            except Exception as e:
                logger.error(f"Error in script scheduler loop: {e}")

            await asyncio.sleep(self._check_interval)

    async def _check_scheduled_scripts(self):
        """Check for scripts that need to be executed."""
        db = SessionLocal()
        try:
            now = datetime.utcnow()

            # Find scripts that are scheduled and due
            scripts = db.query(ConversationScript).filter(
                ConversationScript.status == "scheduled",
                ConversationScript.scheduled_for <= now
            ).all()

            for script in scripts:
                # Skip if already executing
                if script.id in self._active_executions:
                    continue

                logger.info(f"Starting execution of script {script.id}: {script.name}")

                # Create execution record
                execution = ScriptExecution(
                    script_id=script.id,
                    status="running"
                )
                db.add(execution)
                script.status = "running"
                db.commit()
                db.refresh(execution)

                # Start execution task
                task = asyncio.create_task(
                    self._execute_script(script.id, execution.id)
                )
                self._active_executions[script.id] = task

            # Check for recurring scripts
            await self._check_recurring_scripts(db, now)

        except Exception as e:
            logger.error(f"Error checking scheduled scripts: {e}")
        finally:
            db.close()

    async def _check_recurring_scripts(self, db: Session, now: datetime):
        """Check for recurring scripts that need to run."""
        try:
            # Find completed recurring scripts that need to run again
            scripts = db.query(ConversationScript).filter(
                ConversationScript.schedule_type == "recurring",
                ConversationScript.status == "completed",
                ConversationScript.recurring_frequency.isnot(None)
            ).all()

            for script in scripts:
                if not script.recurring_time:
                    continue

                # Parse recurring time
                try:
                    hour, minute = map(int, script.recurring_time.split(":"))
                except (ValueError, AttributeError):
                    continue

                # Check if we should run today
                should_run = False
                today_run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # Check recurring days
                recurring_days = None
                if script.recurring_days:
                    try:
                        recurring_days = json.loads(script.recurring_days)
                    except:
                        pass

                current_day = now.strftime("%a").lower()[:3]
                if recurring_days and current_day not in recurring_days:
                    continue

                # Check if within date range
                if script.recurring_start_date and now < script.recurring_start_date:
                    continue
                if script.recurring_end_date and now > script.recurring_end_date:
                    continue

                # Check if it's time to run
                if script.last_run_at:
                    # Check based on frequency
                    if script.recurring_frequency == "daily":
                        if now >= today_run_time and script.last_run_at.date() < now.date():
                            should_run = True
                    elif script.recurring_frequency == "weekly":
                        days_since_last = (now - script.last_run_at).days
                        if days_since_last >= 7 and now >= today_run_time:
                            should_run = True
                else:
                    # First run
                    if now >= today_run_time:
                        should_run = True

                if should_run and script.id not in self._active_executions:
                    logger.info(f"Starting recurring execution of script {script.id}: {script.name}")

                    execution = ScriptExecution(
                        script_id=script.id,
                        status="running"
                    )
                    db.add(execution)
                    script.status = "running"
                    db.commit()
                    db.refresh(execution)

                    task = asyncio.create_task(
                        self._execute_script(script.id, execution.id)
                    )
                    self._active_executions[script.id] = task

        except Exception as e:
            logger.error(f"Error checking recurring scripts: {e}")

    async def _execute_script_for_group(
        self,
        script_id: int,
        group: dict,
        messages_data: List[dict],
        hub_bot_ids: List[int],
        start_offset: float = 0
    ) -> Tuple[int, int]:
        """
        Execute a script's messages for a single group.
        Returns (messages_sent, messages_failed) for this group.
        """
        messages_sent = 0
        messages_failed = 0

        group_id = group.get("id") if isinstance(group, dict) else group
        group_name = group.get("name", group_id) if isinstance(group, dict) else group_id

        if not group_id:
            return (0, 0)

        # Ensure proper group ID format
        if not group_id.endswith("@g.us"):
            group_id = f"{group_id}@g.us"

        # Wait for the stagger offset before starting this group's conversation
        if start_offset > 0:
            logger.info(f"Group {group_name}: waiting {start_offset:.1f}s before starting conversation")
            await asyncio.sleep(start_offset)

        # Calculate start time for relative delays (after offset wait)
        start_time = datetime.utcnow()

        logger.info(f"Starting conversation in group {group_name}")

        for msg_data in messages_data:
            if not self._running:
                break

            # Calculate when to send this message
            if msg_data["time_type"] == "absolute" and msg_data["absolute_time"]:
                # Parse absolute time and wait until then
                try:
                    hour, minute = map(int, msg_data["absolute_time"].split(":"))
                    target_time = datetime.utcnow().replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )
                    if target_time > datetime.utcnow():
                        wait_seconds = (target_time - datetime.utcnow()).total_seconds()
                        if wait_seconds > 0:
                            logger.info(f"Group {group_name}: waiting {wait_seconds}s until {msg_data['absolute_time']}")
                            await asyncio.sleep(wait_seconds)
                except (ValueError, AttributeError) as e:
                    logger.error(f"Invalid absolute time {msg_data['absolute_time']}: {e}")
            else:
                # Relative delay from start
                delay = msg_data["delay_seconds"] or 0
                elapsed = (datetime.utcnow() - start_time).total_seconds()
                wait_time = delay - elapsed

                if wait_time > 0:
                    logger.info(f"Group {group_name}: waiting {wait_time:.1f}s for message")
                    await asyncio.sleep(wait_time)

            # Send message
            whatsapp_message = convert_to_whatsapp_markdown(msg_data["content"])
            bot_id = msg_data["bot_profile_id"] or hub_bot_ids[0]

            try:
                success = await send_whatsapp_message(
                    bot_profile_id=bot_id,
                    chat_id=group_id,
                    chat_name=group_name,
                    message=whatsapp_message
                )

                if success:
                    messages_sent += 1
                    logger.info(f"Sent message to {group_name}")
                else:
                    messages_failed += 1
                    logger.warning(f"Failed to send message to {group_name}")

            except Exception as e:
                messages_failed += 1
                logger.error(f"Error sending message to {group_name}: {e}")

        logger.info(f"Group {group_name} completed: {messages_sent} sent, {messages_failed} failed")
        return (messages_sent, messages_failed)

    async def _execute_script(self, script_id: int, execution_id: int):
        """Execute a script's messages with staggered group starts."""
        db = SessionLocal()
        messages_sent = 0
        messages_failed = 0

        try:
            script = db.query(ConversationScript).filter(
                ConversationScript.id == script_id
            ).first()

            if not script:
                logger.error(f"Script {script_id} not found")
                return

            # Get messages ordered by sequence
            messages = db.query(ScriptMessage).filter(
                ScriptMessage.script_id == script_id
            ).order_by(ScriptMessage.sequence_order).all()

            if not messages:
                logger.warning(f"Script {script_id} has no messages")
                return

            # Parse target groups
            group_ids = []
            if script.group_ids:
                try:
                    group_ids = json.loads(script.group_ids)
                except:
                    pass

            if not group_ids:
                logger.warning(f"Script {script_id} has no target groups")
                return

            # Get hub's bots for fallback when message doesn't specify a bot
            hub_bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
                HubBotMembership.hub_id == script.hub_id,
                HubBotMembership.is_active == True
            ).all()
            hub_bot_ids = [b[0] for b in hub_bot_ids]

            if not hub_bot_ids:
                logger.warning(f"Script {script_id} hub has no active bots")
                return

            # Prepare message data for passing to group tasks
            messages_data = [
                {
                    "id": msg.id,
                    "content": msg.content,
                    "bot_profile_id": msg.bot_profile_id,
                    "delay_seconds": msg.delay_seconds,
                    "time_type": msg.time_type,
                    "absolute_time": msg.absolute_time,
                }
                for msg in messages
            ]

            # Calculate stagger delay based on script settings and group count
            group_count = len(group_ids)
            sending_speed_mode = script.sending_speed_mode or "auto"
            min_delay, max_delay = calculate_group_stagger_delay(
                group_count=group_count,
                sending_speed_mode=sending_speed_mode,
                stagger_delay_min=script.stagger_delay_min,
                stagger_delay_max=script.stagger_delay_max
            )

            if min_delay > 0:
                logger.info(
                    f"Executing script {script_id} with {len(messages)} messages to {group_count} groups "
                    f"(mode: {sending_speed_mode}, stagger: {min_delay}-{max_delay}s between groups)"
                )
            else:
                logger.info(f"Executing script {script_id} with {len(messages)} messages to {group_count} groups")

            # Create tasks for each group with staggered start times
            group_tasks = []
            cumulative_offset = 0.0

            for i, group in enumerate(group_ids):
                # First group starts immediately, others have cumulative offset
                task = asyncio.create_task(
                    self._execute_script_for_group(
                        script_id=script_id,
                        group=group,
                        messages_data=messages_data,
                        hub_bot_ids=hub_bot_ids,
                        start_offset=cumulative_offset
                    )
                )
                group_tasks.append(task)

                # Add random delay for next group (if not the last group)
                if i < group_count - 1 and min_delay > 0:
                    cumulative_offset += random.uniform(min_delay, max_delay)

            # Wait for all group tasks to complete and aggregate results
            results = await asyncio.gather(*group_tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Group task failed: {result}")
                    messages_failed += 1
                elif isinstance(result, tuple):
                    sent, failed = result
                    messages_sent += sent
                    messages_failed += failed

            # Update message statuses in database
            for msg in messages:
                msg.status = "sent"
                msg.sent_at = datetime.utcnow()
            db.commit()

            # Update execution record
            execution = db.query(ScriptExecution).filter(
                ScriptExecution.id == execution_id
            ).first()

            if execution:
                execution.status = "completed"
                execution.completed_at = datetime.utcnow()
                execution.messages_sent = messages_sent
                execution.messages_failed = messages_failed

            # Update script status
            script.status = "completed"
            script.last_run_at = datetime.utcnow()
            db.commit()

            logger.info(f"Script {script_id} completed: {messages_sent} sent, {messages_failed} failed")

        except asyncio.CancelledError:
            logger.info(f"Script {script_id} execution cancelled")

            # Update status to cancelled
            execution = db.query(ScriptExecution).filter(
                ScriptExecution.id == execution_id
            ).first()
            if execution:
                execution.status = "cancelled"
                execution.completed_at = datetime.utcnow()
                execution.messages_sent = messages_sent
                execution.messages_failed = messages_failed

            script = db.query(ConversationScript).filter(
                ConversationScript.id == script_id
            ).first()
            if script:
                script.status = "cancelled"

            db.commit()
            raise

        except Exception as e:
            logger.error(f"Error executing script {script_id}: {e}")

            # Update execution with error
            execution = db.query(ScriptExecution).filter(
                ScriptExecution.id == execution_id
            ).first()
            if execution:
                execution.status = "failed"
                execution.completed_at = datetime.utcnow()
                execution.messages_sent = messages_sent
                execution.messages_failed = messages_failed
                execution.error_message = str(e)

            script = db.query(ConversationScript).filter(
                ConversationScript.id == script_id
            ).first()
            if script:
                script.status = "failed"

            db.commit()

        finally:
            db.close()

            # Remove from active executions
            if script_id in self._active_executions:
                del self._active_executions[script_id]


# Global scheduler instance
script_scheduler = ScriptScheduler()
