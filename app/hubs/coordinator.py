"""
Hub Coordinator - Orchestrates AI agents for multi-bot coordination
"""

import json
import logging
import random
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
from sqlalchemy import func

from app.database import (
    Hub, HubBotMembership, AIAgent, Contact, ContactTag,
    Message, Conversation, MessageRouting, AgentExecution,
    get_db_session, BotProfile, HubMessageTopic
)
from app.auth.utils import decrypt_string
from app.tools.monitoring import ToolMonitor


class HubCoordinator:
    """
    Coordinates multiple bots in a hub using AI agents.

    This is the main entry point for hub-based message processing.
    When a message arrives at a bot that's part of a hub, the coordinator:
    1. Classifies the message (if classifier agent exists)
    2. Routes to appropriate bot(s) (if router agent exists)
    3. Returns routing decision
    """

    def __init__(self, hub: Hub, db: Session):
        self.hub = hub
        self.hub_id = hub.id
        self.db = db

        # Load agents
        self.agents = {}
        for agent in hub.agents:
            if agent.is_active:
                self.agents[agent.agent_type] = agent

        # Load bot memberships
        self.bot_memberships = {}
        for membership in hub.bot_memberships:
            if membership.is_active:
                self.bot_memberships[membership.bot_profile_id] = membership

        # Load multi-bot response settings
        self.max_responding_bots = hub.max_responding_bots or 1  # Default max (0 = unlimited)
        self.response_delay_min = hub.response_delay_min or 1  # Seconds
        self.response_delay_max = hub.response_delay_max or 3  # Seconds

        # Per-category rules: [{"category": "greeting", "max_bots": 1}, {"category": "discussion", "max_bots": 3}]
        self.multi_response_rules = []
        if hub.multi_response_rules:
            try:
                self.multi_response_rules = json.loads(hub.multi_response_rules)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Hub {hub.id}: Failed to parse multi_response_rules: {e}")

        # Bot-to-bot conversation settings
        self.bot_conversation_limit = hub.bot_conversation_limit if hub.bot_conversation_limit is not None else 0  # 0 = disabled (safe default)
        self.bot_conversation_interval = hub.bot_conversation_interval or "hour"  # minute, hour, day

        # Load selected groups for this hub
        self.selected_groups = []
        self.selected_group_ids = set()  # For fast lookup
        if hub.selected_groups:
            try:
                self.selected_groups = json.loads(hub.selected_groups)
                # Extract chat_ids for fast lookup
                for group in self.selected_groups:
                    if isinstance(group, dict) and group.get('chat_id'):
                        self.selected_group_ids.add(group['chat_id'])
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Hub {hub.id}: Failed to parse selected_groups: {e}")

        # Load working hours for each bot
        self.bot_working_hours = {}
        for membership in hub.bot_memberships:
            if membership.is_active:
                working_days = None
                if membership.working_days:
                    try:
                        working_days = json.loads(membership.working_days)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Hub {hub.id}: Failed to parse working_days for bot {membership.bot_profile_id}: {e}")
                working_periods = None
                if membership.working_periods:
                    try:
                        working_periods = json.loads(membership.working_periods)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Hub {hub.id}: Failed to parse working_periods for bot {membership.bot_profile_id}: {e}")
                self.bot_working_hours[membership.bot_profile_id] = {
                    'start': membership.working_hours_start,  # DEPRECATED
                    'end': membership.working_hours_end,  # DEPRECATED
                    'periods': working_periods,  # New: list of {start, end} periods
                    'days': working_days
                }

    def is_chat_in_selected_groups(self, chat_id: str) -> bool:
        """
        Check if a chat_id is in the hub's selected groups.

        Args:
            chat_id: The chat/group ID to check

        Returns:
            True if chat is in selected groups OR if no groups are selected (all allowed)
        """
        # If no selected groups, all chats are allowed
        if not self.selected_group_ids:
            return True
        return chat_id in self.selected_group_ids

    def _get_bot_response_counts(self, hours: int = 1) -> Dict[int, int]:
        """
        Get the number of responses each bot has sent in the last N hours.
        Used for least-busy routing.

        Args:
            hours: Time window in hours (default: 1)

        Returns:
            Dict mapping bot_id to response count
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)

        # Query response counts for bots in this hub
        # Join Message with Conversation to get bot_profile_id
        bot_ids = list(self.bot_memberships.keys())

        counts = self.db.query(
            Conversation.bot_profile_id,
            func.count(Message.id).label('count')
        ).join(
            Message, Message.conversation_id == Conversation.id
        ).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.role == 'assistant',  # Only count bot responses
            Message.timestamp >= cutoff_time
        ).group_by(Conversation.bot_profile_id).all()

        # Build result dict, defaulting to 0 for bots with no responses
        result = {bot_id: 0 for bot_id in bot_ids}
        for bot_id, count in counts:
            result[bot_id] = count

        return result

    def _is_time_in_period(self, current_time: str, start_time: str, end_time: str) -> bool:
        """
        Check if current_time falls within start_time and end_time.
        Handles overnight shifts (e.g., 22:00 - 06:00).

        Args:
            current_time: Current time in "HH:MM" format
            start_time: Period start time in "HH:MM" format
            end_time: Period end time in "HH:MM" format

        Returns:
            True if current_time is within the period
        """
        if start_time <= end_time:
            # Normal shift (e.g., 09:00 - 17:00)
            return start_time <= current_time <= end_time
        else:
            # Overnight shift (e.g., 22:00 - 06:00)
            return current_time >= start_time or current_time <= end_time

    def _is_bot_within_working_hours(self, bot_id: int) -> bool:
        """
        Check if a bot is currently within its working hours.
        Supports multiple working periods per day.

        Args:
            bot_id: The bot profile ID to check

        Returns:
            True if the bot is within working hours (or has no restrictions), False otherwise
        """
        working_hours = self.bot_working_hours.get(bot_id)
        if not working_hours:
            return True  # No restrictions

        working_periods = working_hours.get('periods')
        working_days = working_hours.get('days')

        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_day = now.strftime("%a").lower()[:3]  # mon, tue, wed, etc.

        # Check working days if specified
        if working_days and current_day not in working_days:
            return False

        # Check working periods (new format)
        if working_periods and len(working_periods) > 0:
            for period in working_periods:
                start_time = period.get('start')
                end_time = period.get('end')
                if start_time and end_time:
                    if self._is_time_in_period(current_time, start_time, end_time):
                        return True
            return False  # Not within any period

        # Fall back to deprecated single start/end times
        start_time = working_hours.get('start')
        end_time = working_hours.get('end')

        # If no start/end times specified, bot is always available
        if not start_time or not end_time:
            return True

        return self._is_time_in_period(current_time, start_time, end_time)

    def _get_bot_to_bot_conversation_count(self, interval: str = "hour") -> int:
        """
        Count bot-to-bot conversations within the specified interval.

        Args:
            interval: Time interval - 'minute', 'hour', or 'day'

        Returns:
            Number of bot-to-bot message exchanges in the interval
        """
        if interval == "minute":
            cutoff_time = datetime.utcnow() - timedelta(minutes=1)
        elif interval == "hour":
            cutoff_time = datetime.utcnow() - timedelta(hours=1)
        else:  # day
            cutoff_time = datetime.utcnow() - timedelta(days=1)

        bot_ids = list(self.bot_memberships.keys())

        # Count messages where both sender and recipient are bots in this hub
        # A bot-to-bot conversation is when a bot responds to another bot's message
        count = self.db.query(func.count(Message.id)).join(
            Conversation, Message.conversation_id == Conversation.id
        ).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.role == 'assistant',
            Message.timestamp >= cutoff_time
        ).scalar() or 0

        return count

    def _can_bot_respond_to_bot(self) -> bool:
        """
        Check if bot-to-bot conversation limit allows another response.

        Returns:
            True if bots can still respond to each other, False if limit reached
        """
        if self.bot_conversation_limit == -1:
            return True  # Unlimited
        if self.bot_conversation_limit == 0:
            return False  # No bot-to-bot conversations allowed

        current_count = self._get_bot_to_bot_conversation_count(self.bot_conversation_interval)
        return current_count < self.bot_conversation_limit

    def _is_message_from_hub_bot(self, message_content: str, chat_id: Optional[str] = None) -> tuple:
        """
        Check if the incoming message content matches a recent assistant response
        from any bot in this hub. This is the most reliable way to detect bot-to-bot
        messages in groups where sender phone/name may not match.

        Args:
            message_content: The incoming message text
            chat_id: The chat/group ID (optional, for more precise matching)

        Returns:
            Tuple of (is_bot_message: bool, bot_name: Optional[str])
        """
        if not message_content:
            return False, None

        # Check messages from last 5 minutes from any bot in this hub
        cutoff_time = datetime.utcnow() - timedelta(minutes=5)
        bot_ids = list(self.bot_memberships.keys())

        # Normalize message for comparison (first 100 chars, lowercase, stripped)
        message_normalized = message_content[:100].lower().strip()

        # Query recent assistant messages from bots in this hub
        query = self.db.query(Message, Conversation, BotProfile).join(
            Conversation, Message.conversation_id == Conversation.id
        ).join(
            BotProfile, Conversation.bot_profile_id == BotProfile.id
        ).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.role == 'assistant',
            Message.timestamp >= cutoff_time
        )

        # If chat_id provided, prefer messages from same group/chat
        if chat_id:
            # Check both group conversations and private conversations that might be in the same context
            query = query.filter(
                (Conversation.chat_id == chat_id) |
                (Conversation.is_group == True)
            )

        recent_messages = query.order_by(Message.timestamp.desc()).limit(50).all()

        for msg, conv, bot in recent_messages:
            if msg.content:
                # Normalize stored message for comparison
                stored_normalized = msg.content[:100].lower().strip()
                # Check if messages are similar (exact match or one contains the other)
                if message_normalized == stored_normalized:
                    return True, bot.name
                # Also check partial match (80% of shorter string matches)
                min_len = min(len(message_normalized), len(stored_normalized))
                if min_len > 20:  # Only for substantial messages
                    if message_normalized[:min_len] == stored_normalized[:min_len]:
                        return True, bot.name

        return False, None

    def _select_least_busy_bot(self, candidate_bot_ids: List[int], message_hash: str = None) -> int:
        """
        Select the least busy bot from a list of candidates.
        Uses response count in last hour, with bot_id as tie-breaker.

        If message_hash is provided, uses deterministic selection to ensure
        all bots in the hub select the SAME bot for the same message.

        Args:
            candidate_bot_ids: List of bot IDs that are eligible to respond
            message_hash: Optional hash of message content for deterministic selection

        Returns:
            The bot_id of the least busy bot
        """
        if not candidate_bot_ids:
            return None

        if len(candidate_bot_ids) == 1:
            return candidate_bot_ids[0]

        response_counts = self._get_bot_response_counts(hours=1)

        # Sort by: (response_count ASC, bot_id ASC)
        # Least responses wins, lowest bot_id breaks ties
        sorted_bots = sorted(
            candidate_bot_ids,
            key=lambda bid: (response_counts.get(bid, 0), bid)
        )

        # If message_hash provided and multiple bots have same response count,
        # use deterministic selection based on hash
        if message_hash and len(sorted_bots) > 1:
            # Get bots with the same (lowest) response count
            min_count = response_counts.get(sorted_bots[0], 0)
            tied_bots = [bid for bid in sorted_bots if response_counts.get(bid, 0) == min_count]

            if len(tied_bots) > 1:
                # Use hash to deterministically select among tied bots
                hash_value = hash(message_hash) % len(tied_bots)
                return tied_bots[hash_value]

        return sorted_bots[0]

    def _get_available_bots(self, exclude_backup: bool = False, only_backup: bool = False) -> List[int]:
        """
        Get list of bot IDs that are currently available (within working hours).

        Args:
            exclude_backup: If True, exclude bots with role='backup'
            only_backup: If True, only return bots with role='backup'

        Returns:
            List of available bot IDs
        """
        available = []
        for bot_id, membership in self.bot_memberships.items():
            # Check working hours
            if not self._is_bot_within_working_hours(bot_id):
                continue

            # Filter by backup role
            is_backup = membership.role == 'backup'
            if exclude_backup and is_backup:
                continue
            if only_backup and not is_backup:
                continue

            available.append(bot_id)

        return available

    def should_bot_respond(
        self,
        bot_id: int,
        message_content: str,
        sender_phone: Optional[str],
        chat_id: str,
        is_group: bool = False,
        sender_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Determine if a bot should respond to a message.

        Args:
            bot_id: The bot that received the message
            message_content: The message text
            sender_phone: Phone number of the sender (can be None if not extractable)
            chat_id: The chat/conversation ID
            is_group: Whether this is a group chat
            sender_name: Display name of the sender

        Returns:
            Dict with:
            - should_respond: bool
            - reason: str explanation
            - delay_ms: optional delay before responding
            - modified_context: optional additional context for the response
            - process_steps: list of steps showing decision flow
        """
        # Initialize process steps tracking
        process_steps = []
        chat_type = "group" if is_group else "private"
        msg_preview = message_content[:30] + "..." if message_content and len(message_content) > 30 else message_content
        process_steps.append(f"Message Received (from: {sender_name or sender_phone or 'Unknown'}, chat: {chat_type})")

        # Check if bot is in this hub
        if bot_id not in self.bot_memberships:
            process_steps.append("→ Hub Membership: ✗ Bot not in hub")
            process_steps.append("→ Final: RESPONDED (not managed by hub)")
            return {
                "should_respond": True,
                "reason": "Bot not in hub membership",
                "process_steps": process_steps
            }

        membership = self.bot_memberships[bot_id]
        bot_profile = self.db.query(BotProfile).filter(BotProfile.id == bot_id).first()
        bot_display = bot_profile.name if bot_profile else f"Bot {bot_id}"

        # Get all active bots in this hub for display
        all_active_bots = []
        for mem_bot_id in self.bot_memberships.keys():
            mem_bot = self.db.query(BotProfile).filter(BotProfile.id == mem_bot_id).first()
            if mem_bot:
                bot_label = f"{mem_bot.name} ({self.bot_memberships[mem_bot_id].role})"
                all_active_bots.append(bot_label)

        hub_name = self.hub.name if self.hub else f"Hub {self.hub_id}"
        bots_list = ", ".join(all_active_bots) if all_active_bots else "none"
        process_steps.append(f"→ Hub Membership: ✓ Active in '{hub_name}' with {len(all_active_bots)} bots: [{bots_list}]")

        # Check working hours for all bots and build status
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_day = now.strftime("%a").lower()[:3]

        working_bots = []
        blocked_bots = []
        for mem_bot_id in self.bot_memberships.keys():
            mem_bot = self.db.query(BotProfile).filter(BotProfile.id == mem_bot_id).first()
            bot_label = mem_bot.name if mem_bot else f"Bot {mem_bot_id}"
            if self._is_bot_within_working_hours(mem_bot_id):
                working_bots.append(bot_label)
            else:
                blocked_bots.append(bot_label)

        working_list = ", ".join(working_bots) if working_bots else "none"
        blocked_list = ", ".join(blocked_bots) if blocked_bots else "none"

        if not self._is_bot_within_working_hours(bot_id):
            working_hours = self.bot_working_hours.get(bot_id, {})
            working_periods = working_hours.get('periods', [])
            working_days = working_hours.get('days', [])
            periods_str = ", ".join([f"{p.get('start')}-{p.get('end')}" for p in working_periods]) if working_periods else "not set"
            days_str = ", ".join(working_days) if working_days else "all days"
            process_steps.append(f"→ Working Hours: ✗ {bot_display} BLOCKED (allowed: {periods_str} on {days_str})")
            process_steps.append(f"   Working: [{working_list}] | Blocked: [{blocked_list}]")
            process_steps.append("→ Final: NOT RESPONDED")
            return {
                "should_respond": False,
                "reason": "Bot is outside working hours",
                "process_steps": process_steps
            }

        process_steps.append(f"→ Working Hours: ✓ Working: [{working_list}] | Blocked: [{blocked_list}]")

        # Check if message is from another bot (bot-to-bot conversation)
        # Use multiple detection methods for reliability
        is_bot_to_bot = False
        detected_bot_name = None
        detection_method = None

        # Method 1: Check by phone number (most reliable if available)
        for mem in self.hub.bot_memberships:
            if mem.is_active:
                bot = self.db.query(BotProfile).filter(BotProfile.id == mem.bot_profile_id).first()
                if not bot:
                    continue

                if sender_phone and bot.whatsapp_phone:
                    sender_normalized = sender_phone.replace('+', '').replace(' ', '').replace('-', '')
                    bot_phone_normalized = bot.whatsapp_phone.replace('+', '').replace(' ', '').replace('-', '')
                    if sender_normalized in bot_phone_normalized or bot_phone_normalized in sender_normalized:
                        is_bot_to_bot = True
                        detected_bot_name = bot.name
                        detection_method = "phone"
                        break

        # Method 2: Check by sender name (for groups where phone not available)
        # Compare against WhatsApp push_name and name, NOT bot profile name
        if not is_bot_to_bot and sender_name:
            sender_name_lower = sender_name.lower().strip()
            for mem in self.hub.bot_memberships:
                if mem.is_active:
                    bot = self.db.query(BotProfile).filter(BotProfile.id == mem.bot_profile_id).first()
                    if not bot:
                        continue

                    # Check against whatsapp_push_name (primary - what appears in groups)
                    if bot.whatsapp_push_name:
                        bot_push_name_lower = bot.whatsapp_push_name.lower().strip()
                        if sender_name_lower == bot_push_name_lower or sender_name_lower in bot_push_name_lower or bot_push_name_lower in sender_name_lower:
                            is_bot_to_bot = True
                            detected_bot_name = bot.whatsapp_push_name
                            detection_method = "push_name"
                            break

                    # Check against whatsapp_name (fallback)
                    if bot.whatsapp_name:
                        bot_wa_name_lower = bot.whatsapp_name.lower().strip()
                        if sender_name_lower == bot_wa_name_lower or sender_name_lower in bot_wa_name_lower or bot_wa_name_lower in sender_name_lower:
                            is_bot_to_bot = True
                            detected_bot_name = bot.whatsapp_name
                            detection_method = "wa_name"
                            break

                    # Also check bot profile name as last resort
                    if bot.name:
                        bot_name_lower = bot.name.lower().strip()
                        if sender_name_lower == bot_name_lower or sender_name_lower in bot_name_lower or bot_name_lower in sender_name_lower:
                            is_bot_to_bot = True
                            detected_bot_name = bot.name
                            detection_method = "profile_name"
                            break

        # Method 3: Check by message content (most reliable for groups)
        # If the incoming message matches a recent bot response, it's from a bot
        if not is_bot_to_bot and message_content:
            content_is_bot, content_bot_name = self._is_message_from_hub_bot(message_content, chat_id)
            if content_is_bot:
                is_bot_to_bot = True
                detected_bot_name = content_bot_name
                detection_method = "content"

        # Add bot-to-bot detection step
        if is_bot_to_bot:
            process_steps.append(f"→ Bot-to-Bot Detection: ✓ Detected (method: {detection_method}, sender: {detected_bot_name})")
        else:
            process_steps.append("→ Bot-to-Bot Detection: ✓ Not a bot message")

        # Check bot-to-bot conversation limit
        if is_bot_to_bot:
            limit_desc = "disabled" if self.bot_conversation_limit == 0 else ("unlimited" if self.bot_conversation_limit == -1 else f"max {self.bot_conversation_limit}/{self.bot_conversation_interval}")
            if not self._can_bot_respond_to_bot():
                process_steps.append(f"→ Bot-to-Bot Limit: ✗ BLOCKED (limit: {limit_desc})")
                process_steps.append("→ Final: NOT RESPONDED")
                reason = f"Bot-to-bot conversation disabled (limit: {self.bot_conversation_limit})"
                if detected_bot_name:
                    reason = f"Message from bot '{detected_bot_name}' (detected by {detection_method}) - {reason}"
                return {
                    "should_respond": False,
                    "reason": reason,
                    "process_steps": process_steps
                }
            else:
                process_steps.append(f"→ Bot-to-Bot Limit: ✓ Allowed (limit: {limit_desc})")

        # If no agents configured, use simple rules
        if not self.agents:
            process_steps.append("→ Routing: Using simple rules (no AI agents)")
            result = self._simple_routing(bot_id, membership, is_group)
            result["process_steps"] = process_steps
            # Post-process reason to replace any bot IDs with names
            no_agent_reason = self._replace_bot_ids_in_reason(result.get('reason', 'simple routing'))
            if result.get("should_respond"):
                process_steps.append(f"→ Final: RESPONDED ({no_agent_reason})")
            else:
                process_steps.append(f"→ Final: NOT RESPONDED ({no_agent_reason})")
            return result

        # Get or create contact (only if we have a valid phone number)
        contact = None
        if sender_phone:
            contact = self._get_or_create_contact(sender_phone, sender_name)

        # Run classifier if available
        classification = None
        if 'classifier' in self.agents:
            classification = self._run_classifier(message_content, contact, is_group)
            if classification:
                category = classification.get('category', 'unknown')
                urgency = classification.get('urgency', 'unknown')
                expertise = classification.get('suggested_expertise', [])
                expertise_str = ", ".join(expertise) if expertise else "none"
                process_steps.append(f"→ Classifier: category={category}, urgency={urgency}, expertise=[{expertise_str}]")
            else:
                process_steps.append("→ Classifier: ✗ Failed to classify")

        # Run router if available
        if 'router' in self.agents and classification:
            process_steps.append("→ Router: AI routing decision...")
            routing = self._run_router(bot_id, classification, is_group)
            routing["process_steps"] = process_steps
            responding_bots = routing.get("responding_bots", [])

            # Convert bot IDs to names for display
            responding_bot_names = []
            for rb_id in responding_bots:
                rb = self.db.query(BotProfile).filter(BotProfile.id == rb_id).first()
                responding_bot_names.append(rb.name if rb else f"Bot {rb_id}")

            # Post-process reason to replace any bot IDs with names
            router_reason = self._replace_bot_ids_in_reason(routing.get('reason', 'router decision'))

            if routing.get("should_respond"):
                delay = routing.get("delay_s", 0)
                if responding_bot_names:
                    bots_str = ", ".join(responding_bot_names)
                    process_steps.append(f"→ Router Result: Selected [{bots_str}], {bot_display} responding" + (f" (delay: {delay}s)" if delay else ""))
                process_steps.append(f"→ Final: RESPONDED ({router_reason})")
            else:
                if responding_bot_names:
                    bots_str = ", ".join(responding_bot_names)
                    process_steps.append(f"→ Router Result: Selected [{bots_str}], {bot_display} NOT selected")
                process_steps.append(f"→ Final: NOT RESPONDED ({router_reason})")
            return routing

        # Default: use simple rules with classification hints
        process_steps.append("→ Routing: Using simple rules with classification hints")
        result = self._simple_routing(bot_id, membership, is_group, classification)
        result["process_steps"] = process_steps
        # Post-process reason to replace any bot IDs with names
        simple_reason = self._replace_bot_ids_in_reason(result.get('reason', 'simple routing'))
        if result.get("should_respond"):
            process_steps.append(f"→ Final: RESPONDED ({simple_reason})")
        else:
            process_steps.append(f"→ Final: NOT RESPONDED ({simple_reason})")
        return result

    def _get_bot_name(self, bot_id: int) -> str:
        """Get bot name by ID, with fallback to 'Bot {id}'."""
        bot = self.db.query(BotProfile).filter(BotProfile.id == bot_id).first()
        return bot.name if bot else f"Bot {bot_id}"

    def _replace_bot_ids_in_reason(self, reason: str) -> str:
        """Replace any bot IDs in a reason string with bot names."""
        import re
        if not reason:
            return reason

        # Find patterns like "bot 5", "Bot 5", "bot_id: 5", "bot_id 5", etc.
        # Also handle patterns like "responding_bot_id: 5"
        for bot_id in self.bot_memberships.keys():
            bot_name = self._get_bot_name(bot_id)
            # Replace various patterns
            patterns = [
                (rf'\bbot[_\s]?{bot_id}\b', bot_name, re.IGNORECASE),
                (rf'\bBot\s+{bot_id}\b', bot_name, 0),
                (rf'responding_bot_id[:\s]+{bot_id}\b', f'responding bot: {bot_name}', re.IGNORECASE),
            ]
            for pattern, replacement, flags in patterns:
                reason = re.sub(pattern, replacement, reason, flags=flags)

        return reason

    def _get_bots_with_matching_expertise(
        self,
        bot_ids: List[int],
        suggested_expertise: List[str]
    ) -> List[int]:
        """
        Find bots with matching expertise from a list of candidates.

        Args:
            bot_ids: List of bot IDs to check
            suggested_expertise: List of expertise topics to match

        Returns:
            List of bot IDs that have matching expertise
        """
        matching_bots = []
        for bid in bot_ids:
            m = self.bot_memberships[bid]
            if m.expertise:
                try:
                    expertise_list = json.loads(m.expertise) if isinstance(m.expertise, str) else m.expertise
                    if any(e.lower() in [exp.lower() for exp in expertise_list] for e in suggested_expertise):
                        matching_bots.append(bid)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Hub {self.hub_id}: Failed to parse expertise for bot {bid}: {e}")
        return matching_bots

    def _simple_routing(
        self,
        bot_id: int,
        membership: HubBotMembership,
        is_group: bool,
        classification: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Simple routing rules when no router agent is configured.
        Uses LEAST BUSY routing when multiple bots are tied.

        Enhanced Rules:
        1. In direct chats: Bot that received the message responds
        2. In group chats (priority order):
           a. Specialist + expertise match (specialists are for specific topics)
           b. Primary bots (for general queries when no specialist matches)
           c. Member/Custom + expertise match (prefer expertise among peers)
           d. Priority-based selection (highest priority, least busy)
           e. Backup bots (only when no other bots are available)
        """
        # For direct chats, the receiving bot responds
        if not is_group:
            return {
                "should_respond": True,
                "reason": "Direct chat - receiving bot responds"
            }

        # For group chats, use enhanced role -> expertise -> priority selection
        # Get available bots (within working hours), excluding backup bots first
        available_bots = self._get_available_bots(exclude_backup=True)

        # If no non-backup bots are available, try backup bots
        if not available_bots:
            backup_bots = self._get_available_bots(only_backup=True)
            if backup_bots:
                selected = self._select_least_busy_bot(backup_bots)
                selected_name = self._get_bot_name(selected)
                if bot_id == selected:
                    reason = "Backup bot (fallback)" if len(backup_bots) == 1 else "Least busy backup bot (fallback)"
                    return {"should_respond": True, "reason": reason}
                else:
                    return {
                        "should_respond": False,
                        "reason": f"{selected_name} selected (backup, fallback)"
                    }
            # No bots available at all
            return {
                "should_respond": False,
                "reason": "No available bots within working hours"
            }

        # If current bot is a backup bot, it should not respond (non-backup bots are available)
        if membership.role == 'backup':
            return {
                "should_respond": False,
                "reason": "Backup bot - non-backup bots are available"
            }

        # Get suggested expertise from classification
        suggested_expertise = []
        if classification:
            suggested_expertise = classification.get('suggested_expertise', [])

        # Step 1: Check SPECIALIST bots with matching expertise (highest priority)
        specialist_bots = [
            bid for bid in available_bots
            if self.bot_memberships[bid].role == 'specialist'
        ]

        if specialist_bots and suggested_expertise:
            matching_specialists = self._get_bots_with_matching_expertise(specialist_bots, suggested_expertise)
            if matching_specialists:
                selected = self._select_least_busy_bot(matching_specialists)
                selected_name = self._get_bot_name(selected)
                if bot_id == selected:
                    reason = f"Specialist with expertise: {suggested_expertise}" if len(matching_specialists) == 1 else f"Least busy specialist with expertise: {suggested_expertise}"
                    return {"should_respond": True, "reason": reason}
                else:
                    return {
                        "should_respond": False,
                        "reason": f"{selected_name} selected (specialist, expertise: {suggested_expertise})"
                    }

        # Step 2: Check PRIMARY bots (for general queries when no specialist matches)
        primary_bots = [
            bid for bid in available_bots
            if self.bot_memberships[bid].role == 'primary'
        ]

        if primary_bots:
            selected = self._select_least_busy_bot(primary_bots)
            selected_name = self._get_bot_name(selected)
            if bot_id == selected:
                reason = "Primary bot" if len(primary_bots) == 1 else "Least busy primary bot"
                return {"should_respond": True, "reason": reason}
            else:
                return {
                    "should_respond": False,
                    "reason": f"{selected_name} selected (primary, least busy)"
                }

        # Step 3: Check MEMBER/CUSTOM bots with matching expertise
        # (non-primary, non-specialist, non-backup bots)
        member_bots = [
            bid for bid in available_bots
            if self.bot_memberships[bid].role not in ['primary', 'specialist', 'backup']
        ]

        if member_bots and suggested_expertise:
            matching_members = self._get_bots_with_matching_expertise(member_bots, suggested_expertise)
            if matching_members:
                selected = self._select_least_busy_bot(matching_members)
                selected_name = self._get_bot_name(selected)
                if bot_id == selected:
                    reason = f"Member with expertise: {suggested_expertise}" if len(matching_members) == 1 else f"Least busy member with expertise: {suggested_expertise}"
                    return {"should_respond": True, "reason": reason}
                else:
                    return {
                        "should_respond": False,
                        "reason": f"{selected_name} selected (member, expertise: {suggested_expertise})"
                    }

        # Step 4: Priority-based selection among remaining bots
        # (no specialist match, no primary, no member expertise match)
        if member_bots:
            # Group by priority, then select least busy within highest priority group
            member_memberships = {bid: self.bot_memberships[bid] for bid in member_bots}
            max_priority = max(m.priority for m in member_memberships.values())
            highest_priority_bots = [
                bid for bid, m in member_memberships.items()
                if m.priority == max_priority
            ]

            selected = self._select_least_busy_bot(highest_priority_bots)
            selected_name = self._get_bot_name(selected)
            if bot_id == selected:
                reason = "Highest priority member" if len(highest_priority_bots) == 1 else "Least busy among highest priority members"
                return {"should_respond": True, "reason": reason}

            return {
                "should_respond": False,
                "reason": f"{selected_name} selected (member, highest priority)"
            }

        # No suitable bot found (shouldn't normally reach here)
        return {
            "should_respond": False,
            "reason": "No suitable bot found for routing"
        }

    def _get_or_create_contact(self, phone: str, display_name: Optional[str] = None) -> Contact:
        """Get or create a contact for this phone number."""
        contact = self.db.query(Contact).filter(
            Contact.hub_id == self.hub_id,
            Contact.phone == phone
        ).first()

        if not contact:
            contact = Contact(
                hub_id=self.hub_id,
                phone=phone,
                display_name=display_name,
                first_seen_at=datetime.utcnow()
            )
            self.db.add(contact)
            self.db.commit()
            self.db.refresh(contact)
        else:
            # Update display_name if we have a new one and the current one is empty
            if display_name and not contact.display_name:
                contact.display_name = display_name

        # Update last interaction
        contact.last_interaction_at = datetime.utcnow()
        self.db.commit()

        return contact

    def _get_ai_provider(self, agent: AIAgent):
        """Get AI provider for an agent, with fallback to hub settings."""
        try:
            from app.ai import get_ai_provider

            # Get API key (agent-specific or hub default)
            api_key = None
            if agent.openai_api_key_encrypted:
                api_key = decrypt_string(agent.openai_api_key_encrypted)
            elif self.hub.openai_api_key_encrypted:
                api_key = decrypt_string(self.hub.openai_api_key_encrypted)

            if not api_key:
                return None

            # Determine provider (agent > hub > default)
            provider_name = agent.ai_provider or self.hub.ai_provider or "openai"
            model = agent.openai_model or self.hub.openai_model or "gpt-4o-mini"

            return get_ai_provider(
                provider_name=provider_name,
                api_key=api_key,
                model=model
            )
        except Exception as e:
            print(f"Failed to create AI provider: {e}")
            # Fallback to OpenAI
            from openai import OpenAI
            api_key = None
            if agent.openai_api_key_encrypted:
                api_key = decrypt_string(agent.openai_api_key_encrypted)
            elif self.hub.openai_api_key_encrypted:
                api_key = decrypt_string(self.hub.openai_api_key_encrypted)
            if api_key:
                return OpenAI(api_key=api_key)
            return None

    def _run_classifier(
        self,
        message_content: str,
        contact: Optional[Contact],
        is_group: bool
    ) -> Optional[Dict]:
        """Run the classifier agent on a message."""
        agent = self.agents.get('classifier')
        if not agent:
            return None

        try:
            # Get AI provider (supports multiple providers)
            provider = self._get_ai_provider(agent)
            if not provider:
                print(f"Hub {self.hub_id}: No API key for classifier")
                return None

            # Build context
            contact_info = "Unknown sender"
            tags = []
            if contact:
                contact_info = f"Phone: {contact.phone}"
                if contact.display_name:
                    contact_info += f", Name: {contact.display_name}"
                if contact.description:
                    contact_info += f", Profile: {contact.description}"

                # Get contact tags
                tags = self.db.query(ContactTag).filter(
                    ContactTag.contact_id == contact.id
                ).all()

            if tags:
                contact_info += f", Tags: {[t.tag for t in tags]}"

            # Load hub topics for dynamic classification
            hub_topics = self.db.query(HubMessageTopic).filter(
                HubMessageTopic.hub_id == self.hub_id
            ).all()
            topic_names = [t.name for t in hub_topics]

            # Also collect all expertise from bot memberships
            all_expertise = set()
            for membership in self.hub.bot_memberships:
                if membership.is_active and membership.expertise:
                    try:
                        expertise_list = json.loads(membership.expertise)
                        all_expertise.update(e.lower() for e in expertise_list)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"Hub {self.hub_id}: Failed to parse expertise in classifier: {e}")

            # Combine topics and expertise
            available_categories = list(set(topic_names) | all_expertise)
            if not available_categories:
                available_categories = ["sales", "support", "billing", "general", "greeting", "feedback", "complaint"]

            # Build dynamic system prompt - ALWAYS use default with dynamic categories
            # Classifier MUST have access to current topics and expertise for accurate classification
            categories_str = ", ".join(available_categories)
            system_prompt = f"""You are a message classifier. Analyze the message and return JSON with:
- category: main topic (one of: {categories_str})
- urgency: low, medium, high
- sentiment: positive, negative, neutral
- suggested_expertise: list of relevant expertise tags from this list: {categories_str}
- requires_single_bot: true if only one bot should respond

Return ONLY valid JSON, no other text."""

            # Append additional instructions if provided (without replacing core logic)
            if agent.additional_instructions:
                system_prompt += f"\n\nAdditional Instructions:\n{agent.additional_instructions}"

            # Use provider (supports OpenAI and other providers)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Contact: {contact_info}\nIs Group Chat: {is_group}\n\nMessage: {message_content}"}
            ]

            # Check if provider is our abstraction or direct OpenAI client
            if hasattr(provider, 'chat_completion'):
                # Using AI provider abstraction
                response = provider.chat_completion(
                    messages=messages,
                    temperature=0.3,
                    max_tokens=500,
                    json_mode=True
                )
                result = json.loads(response.content)
                tokens_used = response.usage.get("total_tokens", 0)
            else:
                # Direct OpenAI client (fallback)
                response = provider.chat.completions.create(
                    model=agent.openai_model or "gpt-4o-mini",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=500,
                    response_format={"type": "json_object"}
                )
                result = json.loads(response.choices[0].message.content)
                tokens_used = response.usage.total_tokens if response.usage else 0

            # Log execution
            self._log_agent_execution(
                agent.id,
                "message",
                {"message": message_content[:200], "contact": contact.phone if contact else "unknown"},
                result,
                tokens_used
            )

            # Update agent last run
            agent.last_run_at = datetime.utcnow()
            self.db.commit()

            return result

        except Exception as e:
            print(f"Hub {self.hub_id}: Classifier error: {e}")
            self._log_agent_execution(
                agent.id,
                "message",
                {"message": message_content[:200]},
                {"error": str(e)},
                0,
                status="error",
                error_message=str(e)
            )
            return None

    def _run_router(
        self,
        requesting_bot_id: int,
        classification: Dict,
        is_group: bool
    ) -> Dict[str, Any]:
        """Run the router agent to decide which bot should respond."""
        agent = self.agents.get('router')
        if not agent:
            return {"should_respond": True, "reason": "No router agent"}

        try:
            # Get AI provider (supports multiple providers)
            provider = self._get_ai_provider(agent)
            if not provider:
                return {"should_respond": True, "reason": "No API key for router"}

            # Get response counts for least-busy routing
            response_counts = self._get_bot_response_counts(hours=1)

            # Build bot info with response counts
            bots_info = []
            for bot_id, membership in self.bot_memberships.items():
                expertise = []
                if membership.expertise:
                    try:
                        expertise = json.loads(membership.expertise) if isinstance(membership.expertise, str) else membership.expertise
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"Hub {self.hub_id}: Failed to parse expertise for bot {bot_id} in router: {e}")

                bots_info.append({
                    "bot_id": bot_id,
                    "role": membership.role,
                    "expertise": expertise,
                    "priority": membership.priority,
                    "can_initiate": membership.can_initiate,
                    "recent_responses": response_counts.get(bot_id, 0)  # For least-busy routing
                })

            # Load hub topics for dynamic routing context
            hub_topics = self.db.query(HubMessageTopic).filter(
                HubMessageTopic.hub_id == self.hub_id
            ).all()
            topic_names = [t.name for t in hub_topics]

            # Collect all expertise for routing context
            all_expertise = set()
            for membership in self.hub.bot_memberships:
                if membership.is_active and membership.expertise:
                    try:
                        expertise_list = json.loads(membership.expertise)
                        all_expertise.update(e.lower() for e in expertise_list)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"Hub {self.hub_id}: Failed to parse expertise in router: {e}")

            # Combine topics and expertise
            available_categories = list(set(topic_names) | all_expertise)
            if not available_categories:
                available_categories = ["sales", "support", "billing", "general", "greeting", "feedback", "complaint"]

            categories_str = ", ".join(available_categories)

            # Build dynamic system prompt - ALWAYS use default with dynamic categories
            # Router MUST have access to current topics and expertise for accurate routing
            system_prompt = f"""You are a response router for a multi-bot WhatsApp system. Based on the message classification and available bots, decide which bot(s) should respond.

Available message categories/topics: {categories_str}

Return JSON with EITHER:
1. Single bot: {{"responding_bot_id": <bot_id>, "reason": "..."}}
2. Multiple bots: {{"responding_bots": [{{"bot_id": <id>, "order": 1}}, {{"bot_id": <id>, "order": 2}}], "reason": "..."}}

Use responding_bot_id: -1 or responding_bots: [] if NO bot should respond.

ROUTING RULES:
1. Match bot expertise to message category when possible (expertise should match one of: {categories_str})
2. Primary role bots handle general/unmatched queries
3. Specialist bots should be preferred when their expertise matches
4. Backup role bots only respond when no other suitable bot is available
5. When max_responding_bots > 1, select bots that would create natural conversation
6. Use recent_responses count for load balancing (prefer less busy bots)
7. In groups, multiple bots can create engaging discussion on topics like brainstorming, debate, etc.

Return ONLY valid JSON, no other text."""

            # Append additional instructions if provided (without replacing core logic)
            if agent.additional_instructions:
                system_prompt += f"\n\nAdditional Instructions:\n{agent.additional_instructions}"

            # Get category-specific settings or use defaults
            category = classification.get('category', 'general')
            max_bots_for_category = self.max_responding_bots  # Default
            delay_min = self.response_delay_min  # Seconds
            delay_max = self.response_delay_max  # Seconds

            # Check per-category rules
            for rule in self.multi_response_rules:
                if rule.get('category') == category:
                    max_bots_for_category = rule.get('max_bots', self.max_responding_bots)
                    # Use category-specific delay if specified, otherwise use hub defaults
                    if rule.get('delay_min') is not None:
                        delay_min = rule['delay_min']
                    if rule.get('delay_max') is not None:
                        delay_max = rule['delay_max']
                    break

            # 0 means unlimited (use all available bots)
            if max_bots_for_category == 0:
                max_bots_for_category = len(bots_info)

            # Calculate random delay within range for this response (seconds)
            response_delay = random.randint(delay_min, delay_max) if delay_min < delay_max else delay_min

            # Build user prompt with multi-bot settings
            user_prompt = f"""Classification: {json.dumps(classification)}
Available Bots: {json.dumps(bots_info)}
Requesting Bot ID: {requesting_bot_id}
Is Group Chat: {is_group}
Max Responding Bots for "{category}": {max_bots_for_category}
Response Delay Range: {delay_min}s - {delay_max}s

Which bot(s) should respond? Consider:
- For max_bots > 1, multiple bots CAN respond to create natural conversation
- Prioritize bots with matching expertise
- Use least busy (recent_responses) for tie-breaking

Which bot(s) should respond?"""

            # Use provider (supports OpenAI and other providers)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            # Check if provider is our abstraction or direct OpenAI client
            if hasattr(provider, 'chat_completion'):
                # Using AI provider abstraction
                response = provider.chat_completion(
                    messages=messages,
                    temperature=0.3,
                    max_tokens=500,
                    json_mode=True
                )
                result = json.loads(response.content)
                tokens_used = response.usage.get("total_tokens", 0)
            else:
                # Direct OpenAI client (fallback)
                response = provider.chat.completions.create(
                    model=agent.openai_model or "gpt-4o-mini",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=500,
                    response_format={"type": "json_object"}
                )
                result = json.loads(response.choices[0].message.content)
                tokens_used = response.usage.total_tokens if response.usage else 0

            # Log execution
            self._log_agent_execution(
                agent.id,
                "message",
                {"classification": classification, "requesting_bot": requesting_bot_id, "max_bots": max_bots_for_category},
                result,
                tokens_used
            )

            # Update agent last run
            agent.last_run_at = datetime.utcnow()
            self.db.commit()

            # Handle response - support both single bot and multi-bot formats
            # Format 1: responding_bot_id (single bot)
            # Format 2: responding_bots (array of bot_ids or {bot_id, order} objects)
            responding_bots_list = []

            if 'responding_bots' in result:
                # Multi-bot format
                for item in result.get('responding_bots', []):
                    if isinstance(item, dict):
                        responding_bots_list.append({
                            'bot_id': item.get('bot_id'),
                            'order': item.get('order', 0)
                        })
                    else:
                        responding_bots_list.append({'bot_id': item, 'order': 0})
                # Sort by order
                responding_bots_list.sort(key=lambda x: x.get('order', 0))
            elif result.get('responding_bot_id') is not None:
                # Single bot format
                bot_id = result.get('responding_bot_id')
                if bot_id != -1:  # -1 means no response
                    responding_bots_list.append({'bot_id': bot_id, 'order': 0})

            # Determine if this bot should respond and calculate delay
            should_respond = False
            delay_s = 0  # Delay in seconds
            reason = result.get('reason') or result.get('coordination_note', 'Router decision')

            if not responding_bots_list:
                # No bots should respond
                should_respond = False
                reason = reason or "Router decided no bot should respond"
            else:
                # Check if requesting bot is in the list
                for idx, bot_info in enumerate(responding_bots_list):
                    if bot_info['bot_id'] == requesting_bot_id:
                        should_respond = True
                        # First bot responds immediately, subsequent bots get random delays
                        if idx == 0:
                            delay_s = 0
                        else:
                            # Each subsequent bot gets a cumulative random delay (seconds)
                            delay_s = idx * random.randint(delay_min, delay_max)
                            # Get bot name for display
                            req_bot = self.db.query(BotProfile).filter(BotProfile.id == requesting_bot_id).first()
                            req_bot_name = req_bot.name if req_bot else f"Bot {requesting_bot_id}"
                            reason = f"{reason} ({req_bot_name} responding #{idx + 1}, delay: {delay_s}s)"
                        break

            return {
                "should_respond": should_respond,
                "reason": reason,
                "delay_s": delay_s,
                "classification": classification,
                "responding_bots": [b['bot_id'] for b in responding_bots_list]
            }

        except Exception as e:
            print(f"Hub {self.hub_id}: Router error: {e}")
            import traceback
            traceback.print_exc()
            # On error, fall back to simple routing instead of allowing all bots
            # This respects max_responding_bots setting
            membership = self.bot_memberships.get(requesting_bot_id)
            if membership:
                return self._simple_routing(requesting_bot_id, membership, is_group, classification)
            return {"should_respond": False, "reason": f"Router error: {e}"}

    def _log_agent_execution(
        self,
        agent_id: int,
        trigger_type: str,
        input_data: Dict,
        output_data: Dict,
        tokens_used: int,
        status: str = "success",
        error_message: str = None
    ):
        """Log an agent execution."""
        try:
            execution = AgentExecution(
                agent_id=agent_id,
                trigger_type=trigger_type,
                input_data=json.dumps(input_data),
                output_data=json.dumps(output_data),
                tokens_used=tokens_used,
                status=status,
                error_message=error_message
            )
            self.db.add(execution)
            self.db.commit()
        except Exception as e:
            print(f"Failed to log agent execution: {e}")


def update_hub_execution_response(execution_id: int, response_message: str) -> bool:
    """
    Update a hub execution record with the bot's response message.

    Args:
        execution_id: The tool execution ID from check_hub_routing result
        response_message: The bot's response message

    Returns:
        True if updated successfully, False otherwise
    """
    if not execution_id:
        return False

    try:
        with get_db_session() as db:
            return ToolMonitor.update_execution_response(db, execution_id, response_message)
    except Exception as e:
        print(f"Failed to update hub execution response: {e}")
        return False


def get_hub_for_bot(bot_id: int, db: Session) -> Optional[Hub]:
    """
    Get the active hub that a bot belongs to.

    Returns None if bot is not in any active hub.
    """
    membership = db.query(HubBotMembership).filter(
        HubBotMembership.bot_profile_id == bot_id,
        HubBotMembership.is_active == True
    ).first()

    if not membership:
        return None

    hub = db.query(Hub).filter(
        Hub.id == membership.hub_id,
        Hub.is_active == True
    ).first()

    return hub


def check_hub_routing(
    bot_id: int,
    message_content: str,
    sender_phone: Optional[str],
    chat_id: str,
    is_group: bool = False,
    sender_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Check if a bot should respond to a message based on hub routing rules.

    This is the main entry point called from whatsapp_bot.py.

    Args:
        bot_id: The bot that received the message
        message_content: The message text
        sender_phone: Phone number of the sender (can be None)
        chat_id: The chat/conversation ID
        is_group: Whether this is a group chat
        sender_name: Display name of the sender

    Returns:
        Dict with should_respond, reason, and optional delay_ms
    """
    import time
    start_time = time.time()

    try:
        with get_db_session() as db:
            hub = get_hub_for_bot(bot_id, db)

            if not hub:
                # Bot not in any hub, respond normally
                return {
                    "should_respond": True,
                    "reason": "Bot not in any hub"
                }

            coordinator = HubCoordinator(hub, db)

            # For group_management hubs, only process messages from selected groups
            if hub.task_type == 'group_management':
                # If not a group message, skip hub coordination (respond normally)
                if not is_group:
                    return {
                        "should_respond": True,
                        "reason": "Private message - not managed by group_management hub"
                    }

                # Check if this group is in the selected groups
                if not coordinator.is_chat_in_selected_groups(chat_id):
                    return {
                        "should_respond": True,
                        "reason": "Group not in hub's selected groups - responding normally"
                    }

            result = coordinator.should_bot_respond(
                bot_id=bot_id,
                message_content=message_content,
                sender_phone=sender_phone,
                chat_id=chat_id,
                is_group=is_group,
                sender_name=sender_name
            )

            # Log to ToolExecution for all decisions
            execution_time_ms = int((time.time() - start_time) * 1000)
            tool_type = hub.task_type or 'group_management'  # Default to group_management
            execution_id = None

            # Get names for display
            bot_profile = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
            bot_name = bot_profile.name if bot_profile else f"Bot {bot_id}"
            hub_name = hub.name if hub else f"Hub {hub.id}"

            # Get group name from selected_groups if available
            group_name = None
            if is_group and coordinator.selected_groups:
                for group in coordinator.selected_groups:
                    if isinstance(group, dict) and group.get('chat_id') == chat_id:
                        group_name = group.get('name')
                        break

            try:
                execution = ToolMonitor.log_execution(
                    db=db,
                    tool_type=tool_type,
                    operation='message_received',
                    hub_id=hub.id,
                    input_data={
                        'message': message_content[:200] if message_content else '',
                        'sender_phone': sender_phone,
                        'sender_name': sender_name,
                        'chat_id': chat_id,
                        'is_group': is_group,
                        'bot_id': bot_id,
                        'bot_name': bot_name,
                        'hub_name': hub_name,
                        'group_name': group_name
                    },
                    output_data={
                        'should_respond': result.get('should_respond'),
                        'reason': result.get('reason'),
                        'delay_ms': result.get('delay_ms', 0),
                        'process_steps': result.get('process_steps', [])
                    },
                    status='success',
                    execution_time_ms=execution_time_ms,
                    triggered_by='bot',
                    related_entity_type='bot',
                    related_entity_id=bot_id,
                    user_id=hub.user_id
                )
                execution_id = execution.id if execution else None
            except Exception as log_err:
                print(f"Failed to log tool execution: {log_err}")

            # Add execution_id to result for response logging
            result['execution_id'] = execution_id
            return result
    except Exception as e:
        print(f"Hub routing check error: {e}")
        # On error, allow response
        return {
            "should_respond": True,
            "reason": f"Hub check error: {e}"
        }
