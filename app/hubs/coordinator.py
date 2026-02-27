"""
Hub Coordinator - Orchestrates AI agents for multi-bot coordination
"""

import json
import logging
import random
import hashlib
import threading
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


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_group_name_from_db(db: Session, chat_id: str) -> Optional[str]:
    """
    Look up the actual group name from the Conversation table in database.

    Returns the chat_name if found, or None if not found.
    """
    try:
        # Query Conversation table for the chat_name
        conversation = db.query(Conversation).filter(
            Conversation.chat_id == chat_id
        ).first()
        if conversation and conversation.chat_name:
            return conversation.chat_name
    except Exception as e:
        logger.debug(f"Failed to get group name from database: {e}")
    return None


# =============================================================================
# ROUTING CACHE - Prevents duplicate AI calls when multiple bots process
# the same message. Cache TTL is short (10 seconds) since all bots process
# messages almost simultaneously.
# =============================================================================

class RoutingCache:
    """
    Thread-safe cache for classifier and router results with pending lock mechanism.

    When a message arrives in a hub-managed group with 3 bots:
    - Bot 1 processes first: marks as PENDING, runs classifier + router, caches results
    - Bot 2 processes: sees PENDING, WAITS for Bot 1 to finish, uses cached results
    - Bot 3 processes: sees PENDING, WAITS for Bot 1 to finish, uses cached results

    This reduces AI calls from 6 (3 classifier + 3 router) to 2 (1 classifier + 1 router),
    even when all bots process the message simultaneously.

    OPTIMIZATION: Combined waiting - Bot 2 waits ONCE for both classifier AND router
    to complete, rather than waking up after each step. This reduces context switches.
    """

    def __init__(self, ttl_seconds: int = 10, wait_timeout: float = 10.0):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._pending_classifier: Dict[str, threading.Event] = {}
        self._pending_router: Dict[str, threading.Event] = {}
        self._pending_full: Dict[str, threading.Event] = {}  # Combined classifier+router
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._wait_timeout = wait_timeout

    def _make_key(self, hub_id: int, chat_id: str, message_content: str) -> str:
        """Create a unique cache key for this message."""
        content_hash = hashlib.md5(message_content.encode()).hexdigest()[:16]
        return f"hub:{hub_id}:chat:{chat_id}:msg:{content_hash}"

    def _cleanup_expired(self):
        """Remove expired entries (called within lock)."""
        now = datetime.utcnow()
        expired_keys = [
            key for key, entry in self._cache.items()
            if (now - entry.get('timestamp', now)).total_seconds() > self._ttl
        ]
        for key in expired_keys:
            del self._cache[key]
            # Also clean up any stale pending entries
            self._pending_classifier.pop(key, None)
            self._pending_router.pop(key, None)
            self._pending_full.pop(key, None)

    def get_classifier_result(self, hub_id: int, chat_id: str, message_content: str) -> Optional[Dict]:
        """
        Get cached classifier result, waiting if another bot is computing.

        Returns:
            - Dict: Cached result (cache hit or waited for result)
            - None: Cache miss AND this bot should compute (marked as pending)
        """
        key = self._make_key(hub_id, chat_id, message_content)
        event_to_wait = None

        with self._lock:
            self._cleanup_expired()

            # Check if result is already cached
            entry = self._cache.get(key)
            if entry and 'classifier' in entry:
                logger.debug(f"Cache HIT: classifier for hub {hub_id}, chat {chat_id}")
                return entry['classifier']

            # Check if another bot is already computing
            if key in self._pending_classifier:
                event_to_wait = self._pending_classifier[key]
                logger.debug(f"Cache WAIT: classifier for hub {hub_id}, chat {chat_id} (another bot computing)")
            else:
                # We're the first - mark as pending
                self._pending_classifier[key] = threading.Event()
                logger.debug(f"Cache MISS: classifier for hub {hub_id}, chat {chat_id} (will compute)")
                return None

        # Wait outside the lock
        if event_to_wait:
            event_to_wait.wait(timeout=self._wait_timeout)
            # Check cache again after waiting
            with self._lock:
                entry = self._cache.get(key)
                if entry and 'classifier' in entry:
                    logger.debug(f"Cache HIT (after wait): classifier for hub {hub_id}, chat {chat_id}")
                    return entry['classifier']

                # Timeout - try to become the new computing bot
                if key not in self._pending_classifier:
                    # Previous bot finished but no result (error?) - we'll compute
                    self._pending_classifier[key] = threading.Event()
                    logger.debug(f"Cache TIMEOUT (will compute): classifier for hub {hub_id}, chat {chat_id}")
                    return None
                else:
                    # Another bot is still pending or took over - skip to avoid duplicate
                    logger.debug(f"Cache TIMEOUT (skip): classifier for hub {hub_id}, chat {chat_id}")
                    return {'category': 'general', 'urgency': 'normal', 'timeout_fallback': True}

        return None

    def set_classifier_result(self, hub_id: int, chat_id: str, message_content: str, result: Dict):
        """Cache classifier result and signal waiting bots."""
        key = self._make_key(hub_id, chat_id, message_content)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = {'timestamp': datetime.utcnow()}
            self._cache[key]['classifier'] = result

            # Signal waiting bots that result is ready
            if key in self._pending_classifier:
                self._pending_classifier[key].set()
                del self._pending_classifier[key]

            logger.debug(f"Cache SET: classifier for hub {hub_id}, chat {chat_id}")

    def get_router_result(self, hub_id: int, chat_id: str, message_content: str, bot_id: int) -> Optional[Dict]:
        """
        Get cached router result, waiting if another bot is computing.

        Returns adjusted result for the specific bot_id (should_respond flag).
        """
        key = self._make_key(hub_id, chat_id, message_content)
        event_to_wait = None

        with self._lock:
            self._cleanup_expired()

            # Check if result is already cached
            entry = self._cache.get(key)
            if entry and 'router' in entry:
                result = self._adjust_router_result_for_bot(entry['router'], bot_id, hub_id, chat_id)
                return result

            # Check if another bot is already computing
            if key in self._pending_router:
                event_to_wait = self._pending_router[key]
                logger.debug(f"Cache WAIT: router for hub {hub_id}, chat {chat_id}, bot {bot_id}")
            else:
                # We're the first - mark as pending
                self._pending_router[key] = threading.Event()
                logger.debug(f"Cache MISS: router for hub {hub_id}, chat {chat_id}, bot {bot_id} (will compute)")
                return None

        # Wait outside the lock
        if event_to_wait:
            event_to_wait.wait(timeout=self._wait_timeout)
            # Check cache again after waiting
            with self._lock:
                entry = self._cache.get(key)
                if entry and 'router' in entry:
                    logger.debug(f"Cache HIT (after wait): router for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                    return self._adjust_router_result_for_bot(entry['router'], bot_id, hub_id, chat_id)

                # Timeout - try to become the new computing bot
                if key not in self._pending_router:
                    # Previous bot finished but no result (error?) - we'll compute
                    self._pending_router[key] = threading.Event()
                    logger.debug(f"Cache TIMEOUT (will compute): router for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                    return None
                else:
                    # Another bot is still pending or took over - this bot should skip
                    logger.debug(f"Cache TIMEOUT (skip): router for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                    return {
                        'should_respond': False,
                        'reason': 'Router timeout - another bot handling',
                        'timeout_fallback': True
                    }

        return None

    def _adjust_router_result_for_bot(self, cached_result: Dict, bot_id: int, hub_id: int, chat_id: str) -> Dict:
        """Adjust cached router result for the specific bot asking."""
        result = cached_result.copy()

        # Get the original selected bot(s)
        responding_bots = result.get('responding_bots', [])
        responding_bot_id = result.get('responding_bot_id')

        # Determine if THIS bot should respond
        if responding_bot_id is not None:
            should_respond = (bot_id == responding_bot_id)
        elif responding_bots:
            should_respond = bot_id in [rb.get('bot_id') if isinstance(rb, dict) else rb for rb in responding_bots]
        else:
            should_respond = False

        # Adjust result for this bot
        result['should_respond'] = should_respond
        if not should_respond:
            result['deferred'] = True
            result['reason'] = result.get('reason', '') + ' (cached decision)'

        logger.debug(f"Cache HIT: router for hub {hub_id}, chat {chat_id}, bot {bot_id} -> respond={should_respond}")
        return result

    def set_router_result(self, hub_id: int, chat_id: str, message_content: str, result: Dict):
        """Cache router result and signal waiting bots."""
        key = self._make_key(hub_id, chat_id, message_content)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = {'timestamp': datetime.utcnow()}
            self._cache[key]['router'] = result

            # Signal waiting bots that result is ready (individual router wait)
            if key in self._pending_router:
                self._pending_router[key].set()
                del self._pending_router[key]

            # Also signal the combined wait (classifier+router complete)
            # Router is always last, so when router is set, full chain is complete
            if key in self._pending_full:
                self._pending_full[key].set()
                del self._pending_full[key]

            logger.debug(f"Cache SET: router for hub {hub_id}, chat {chat_id}")

    def get_full_routing_result(
        self,
        hub_id: int,
        chat_id: str,
        message_content: str,
        bot_id: int
    ) -> tuple:
        """
        Get both classifier and router results with a SINGLE wait.

        This is an optimization over separate get_classifier_result + get_router_result
        calls. Instead of waking up after classifier and waiting again for router,
        bots wait ONCE for both to complete.

        Returns:
            - (classifier_dict, router_dict): Both cached (hit or waited)
            - (None, None): Cache miss - this bot should compute both
        """
        key = self._make_key(hub_id, chat_id, message_content)
        event_to_wait = None

        with self._lock:
            self._cleanup_expired()

            entry = self._cache.get(key)
            # Check if BOTH classifier AND router are cached
            if entry and 'classifier' in entry and 'router' in entry:
                classifier = entry['classifier']
                router = self._adjust_router_result_for_bot(entry['router'], bot_id, hub_id, chat_id)
                logger.debug(f"Cache HIT: full routing for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                return classifier, router

            # Check if another bot is computing the full chain
            if key in self._pending_full:
                event_to_wait = self._pending_full[key]
                logger.debug(f"Cache WAIT: full routing for hub {hub_id}, chat {chat_id}, bot {bot_id} (another bot computing)")
            else:
                # We're the first - mark as pending for full chain
                self._pending_full[key] = threading.Event()
                logger.debug(f"Cache MISS: full routing for hub {hub_id}, chat {chat_id}, bot {bot_id} (will compute)")
                return None, None

        # Wait outside the lock for the full chain to complete
        if event_to_wait:
            event_to_wait.wait(timeout=self._wait_timeout)
            # Check cache again after waiting
            with self._lock:
                entry = self._cache.get(key)
                if entry and 'classifier' in entry and 'router' in entry:
                    classifier = entry['classifier']
                    router = self._adjust_router_result_for_bot(entry['router'], bot_id, hub_id, chat_id)
                    logger.debug(f"Cache HIT (after wait): full routing for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                    return classifier, router

                # Timeout - try to become the new computing bot
                if key not in self._pending_full:
                    # Previous bot finished but no result (error?) - we'll compute
                    self._pending_full[key] = threading.Event()
                    logger.debug(f"Cache TIMEOUT (will compute): full routing for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                    return None, None
                else:
                    # Another bot is still pending or took over - return fallback
                    logger.debug(f"Cache TIMEOUT (skip): full routing for hub {hub_id}, chat {chat_id}, bot {bot_id}")
                    # Return fallback values - general classification and skip response
                    fallback_classifier = {'category': 'general', 'urgency': 'normal', 'timeout_fallback': True}
                    fallback_router = {
                        'should_respond': False,
                        'reason': 'Full routing timeout - another bot handling',
                        'timeout_fallback': True
                    }
                    return fallback_classifier, fallback_router

        return None, None


# Global cache instance
_routing_cache = RoutingCache(ttl_seconds=10)


# =============================================================================
# HUB MESSAGE PROCESSOR - Centralized processing for group management hubs
# Runs AI calls ONCE per message, distributes decisions to all bots.
# =============================================================================

class HubMessageProcessor:
    """
    Centralized message processor for group management hubs.

    Instead of each bot running AI calls independently (with caching to avoid
    duplicates), this processor runs ALL AI calls ONCE and stores decisions
    for all bots.

    Flow:
    1. First bot to receive message triggers processing
    2. Processor runs: ending detection → classifier → router
    3. Decisions for ALL bots are stored
    4. Other bots just look up their pre-computed decision

    This is cleaner than the caching approach because:
    - No race conditions with pending locks
    - No multiple wait/wake cycles
    - Clear separation: AI runs once, bots just query decisions
    """

    def __init__(self, ttl_seconds: int = 15, wait_timeout: float = 12.0):
        self._decisions: Dict[str, Dict[str, Any]] = {}  # message_key -> {timestamp, bot_decisions}
        self._processing: Dict[str, threading.Event] = {}  # message_key -> event
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._wait_timeout = wait_timeout

    def _make_key(self, hub_id: int, chat_id: str, message_content: str, sender_id: str) -> str:
        """Create unique key for this message."""
        content_hash = hashlib.md5(message_content.encode()).hexdigest()[:16]
        sender_hash = sender_id[-8:] if sender_id else "unknown"
        return f"hubmsg:{hub_id}:{chat_id}:{sender_hash}:{content_hash}"

    def _cleanup_expired(self):
        """Remove expired entries (called within lock)."""
        now = datetime.utcnow()
        expired = [
            key for key, entry in self._decisions.items()
            if (now - entry.get('timestamp', now)).total_seconds() > self._ttl
        ]
        for key in expired:
            del self._decisions[key]
            self._processing.pop(key, None)

    def get_bot_decision(
        self,
        hub_id: int,
        bot_id: int,
        chat_id: str,
        message_content: str,
        sender_id: str,
        sender_name: Optional[str] = None,
        sender_phone: Optional[str] = None,
        is_group: bool = True,
        whatsapp_message_id: Optional[str] = None,
        conversation_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get the routing decision for a specific bot.

        If the message hasn't been processed yet, this bot triggers processing.
        If another bot is processing, this bot waits for the result.

        Args:
            hub_id: The hub ID
            bot_id: The bot requesting the decision
            chat_id: The chat/group ID
            message_content: The message text
            sender_id: Unique sender identifier
            sender_name: Sender's display name
            sender_phone: Sender's phone number
            is_group: Whether this is a group chat
            whatsapp_message_id: WhatsApp message ID for bot detection
            conversation_id: Database conversation ID for ending detection

        Returns:
            Dict with:
            - should_respond: bool
            - reason: str
            - skip_ending_check: bool (True, already done)
            - classification: optional classifier result
            - routing_context: optional context for response
            - delay_ms: optional response delay
            - process_steps: list of decision steps
        """
        message_key = self._make_key(hub_id, chat_id, message_content, sender_id or "")
        event_to_wait = None

        with self._lock:
            self._cleanup_expired()

            # Check if decision already exists
            if message_key in self._decisions:
                decisions = self._decisions[message_key].get('bot_decisions', {})
                if bot_id in decisions:
                    logger.debug(f"HubMessageProcessor: Decision HIT for bot {bot_id} in hub {hub_id}")
                    decision = decisions[bot_id].copy()
                    decision['from_cache'] = True
                    return decision

            # Check if another bot is processing
            if message_key in self._processing:
                event_to_wait = self._processing[message_key]
                logger.debug(f"HubMessageProcessor: Bot {bot_id} waiting for processing in hub {hub_id}")
            else:
                # We're the first - mark as processing and do the work
                self._processing[message_key] = threading.Event()
                logger.info(f"HubMessageProcessor: Bot {bot_id} processing message for hub {hub_id}")

        # If we need to wait, do it outside the lock
        if event_to_wait:
            event_to_wait.wait(timeout=self._wait_timeout)
            # Check for result after waiting
            with self._lock:
                if message_key in self._decisions:
                    decisions = self._decisions[message_key].get('bot_decisions', {})
                    if bot_id in decisions:
                        logger.debug(f"HubMessageProcessor: Decision HIT (after wait) for bot {bot_id}")
                        decision = decisions[bot_id].copy()
                        decision['from_cache'] = True
                        return decision

                # Timeout or no result - try to become processor
                if message_key not in self._processing:
                    self._processing[message_key] = threading.Event()
                    logger.debug(f"HubMessageProcessor: Bot {bot_id} taking over processing after timeout")
                else:
                    # Another bot took over - return default
                    logger.debug(f"HubMessageProcessor: Bot {bot_id} timeout, returning default")
                    return {
                        'should_respond': False,
                        'reason': 'Processing timeout - another bot handling',
                        'skip_ending_check': True,
                        'timeout_fallback': True
                    }

        # This bot is the processor - run all AI calls
        return self._process_message(
            message_key=message_key,
            hub_id=hub_id,
            requesting_bot_id=bot_id,
            chat_id=chat_id,
            message_content=message_content,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_phone=sender_phone,
            is_group=is_group,
            whatsapp_message_id=whatsapp_message_id,
            conversation_id=conversation_id
        )

    def _process_message(
        self,
        message_key: str,
        hub_id: int,
        requesting_bot_id: int,
        chat_id: str,
        message_content: str,
        sender_id: str,
        sender_name: Optional[str],
        sender_phone: Optional[str],
        is_group: bool,
        whatsapp_message_id: Optional[str],
        conversation_id: Optional[int]
    ) -> Dict[str, Any]:
        """
        Process the message and generate decisions for ALL bots in the hub.

        This runs:
        1. Ending detection (should any bot respond?)
        2. Classifier (what category is this message?)
        3. Router (which bot should respond?)

        Then stores decisions for all bots so they can look them up.
        """
        try:
            with get_db_session() as db:
                hub = db.query(Hub).filter(Hub.id == hub_id).first()
                if not hub:
                    return self._finalize_processing(message_key, requesting_bot_id, {
                        requesting_bot_id: {
                            'should_respond': True,
                            'reason': 'Hub not found',
                            'skip_ending_check': True
                        }
                    })

                # Get all active bots in this hub
                bot_ids = [
                    m.bot_profile_id for m in hub.bot_memberships
                    if m.is_active
                ]

                if not bot_ids:
                    return self._finalize_processing(message_key, requesting_bot_id, {
                        requesting_bot_id: {
                            'should_respond': True,
                            'reason': 'No active bots in hub',
                            'skip_ending_check': True
                        }
                    })

                # Get bot names for display
                all_bots = db.query(BotProfile).filter(BotProfile.id.in_(bot_ids)).all()
                all_bot_names = [bot.name for bot in all_bots]
                bot_phones = {bot.id: bot.whatsapp_phone for bot in all_bots if bot.whatsapp_phone}

                # Format bot names with truncation (max 40 chars)
                bot_names_str = ", ".join(all_bot_names)
                if len(bot_names_str) > 40:
                    bot_names_display = bot_names_str[:37] + "..."
                else:
                    bot_names_display = bot_names_str

                process_steps = [f"Message received in hub '{hub.name}' with {len(bot_ids)} bots: [{bot_names_display}]"]

                # =============================================================
                # STEP 0: BOT-TO-BOT DETECTION (check if message is from a bot)
                # =============================================================
                is_bot_to_bot = False
                detected_bot_name = None
                detection_method = None

                # Method 1: Check by WhatsApp Message ID (if message was sent by a bot)
                # Check against ALL bots in the system, not just hub members
                if whatsapp_message_id:
                    existing_bot_msg = db.query(Message, Conversation, BotProfile).join(
                        Conversation, Message.conversation_id == Conversation.id
                    ).join(
                        BotProfile, Conversation.bot_profile_id == BotProfile.id
                    ).filter(
                        Message.whatsapp_message_id == whatsapp_message_id,
                        Message.role == 'assistant'
                    ).first()

                    if existing_bot_msg:
                        msg, conv, bot = existing_bot_msg
                        is_bot_to_bot = True
                        detected_bot_name = bot.name
                        detection_method = "message_id"

                # Method 2: Check by phone number (against hub bots first, then ALL system bots)
                if not is_bot_to_bot and sender_phone:
                    sender_normalized = sender_phone.replace('+', '').replace(' ', '').replace('-', '')

                    # First check against hub member bots
                    for bot_id_check, bot_phone in bot_phones.items():
                        bot_phone_normalized = bot_phone.replace('+', '').replace(' ', '').replace('-', '')
                        if sender_normalized in bot_phone_normalized or bot_phone_normalized in sender_normalized:
                            bot = db.query(BotProfile).filter(BotProfile.id == bot_id_check).first()
                            is_bot_to_bot = True
                            detected_bot_name = bot.name if bot else f"Bot {bot_id_check}"
                            detection_method = "phone"
                            break

                    # If not found, check against ALL bots in the system (not just hub members)
                    if not is_bot_to_bot:
                        all_system_bots = db.query(BotProfile).filter(
                            BotProfile.whatsapp_phone.isnot(None),
                            BotProfile.whatsapp_phone != ''
                        ).all()

                        for system_bot in all_system_bots:
                            if system_bot.id in bot_ids:
                                continue  # Already checked
                            bot_phone_normalized = system_bot.whatsapp_phone.replace('+', '').replace(' ', '').replace('-', '')
                            if sender_normalized in bot_phone_normalized or bot_phone_normalized in sender_normalized:
                                is_bot_to_bot = True
                                detected_bot_name = system_bot.name
                                detection_method = "phone_system"  # Mark as detected from system-wide search
                                break

                # Method 3: Check by message content (matches recent bot responses from ALL bots)
                if not is_bot_to_bot and message_content:
                    # Check if message matches any recent bot response from ANY bot in the system
                    cutoff_time = datetime.utcnow() - timedelta(minutes=5)
                    message_normalized = message_content[:100].lower().strip()

                    recent_bot_msgs = db.query(Message, Conversation, BotProfile).join(
                        Conversation, Message.conversation_id == Conversation.id
                    ).join(
                        BotProfile, Conversation.bot_profile_id == BotProfile.id
                    ).filter(
                        Message.role == 'assistant',
                        Message.timestamp >= cutoff_time
                    ).order_by(Message.timestamp.desc()).limit(100).all()

                    for msg, conv, bot in recent_bot_msgs:
                        if msg.content:
                            stored_normalized = msg.content[:100].lower().strip()
                            if message_normalized == stored_normalized:
                                is_bot_to_bot = True
                                detected_bot_name = bot.name
                                detection_method = "content"
                                break
                            # Partial match for substantial messages
                            min_len = min(len(message_normalized), len(stored_normalized))
                            if min_len > 20 and message_normalized[:min_len] == stored_normalized[:min_len]:
                                is_bot_to_bot = True
                                detected_bot_name = bot.name
                                detection_method = "content"
                                break

                # If bot-to-bot detected, log and return early
                if is_bot_to_bot:
                    # Get bot-to-bot limit info from hub
                    bot_limit = hub.bot_conversation_limit if hub.bot_conversation_limit is not None else 0
                    bot_interval = hub.bot_conversation_interval or 'hour'
                    if bot_limit == 0:
                        limit_desc = "bot-to-bot disabled"
                    elif bot_limit == -1:
                        limit_desc = "bot-to-bot unlimited"
                    else:
                        limit_desc = f"bot-to-bot limit: {bot_limit}/{bot_interval}"

                    process_steps.append(f"→ Bot-to-Bot Detection: BLOCKED (method: {detection_method}, sender: {detected_bot_name})")
                    process_steps.append(f"→ Bot-to-Bot Limit: {limit_desc}")
                    process_steps.append(f"→ Final: NOT RESPONDED (bot message blocked)")

                    bot_decisions = {
                        bid: {
                            'should_respond': False,
                            'reason': f"Bot message from {detected_bot_name} (detected by {detection_method})",
                            'skip_ending_check': True,
                            'process_steps': process_steps.copy()
                        }
                        for bid in bot_ids
                    }

                    # Log bot-to-bot blocked activity
                    try:
                        # Look up actual group name from database
                        group_name = _get_group_name_from_db(db, chat_id)

                        execution = ToolMonitor.log_execution(
                            db=db,
                            tool_type='group_management',
                            operation='bot_to_bot_blocked',
                            hub_id=hub_id,
                            input_data={
                                'message': message_content[:200],
                                'sender_name': detected_bot_name or sender_name or 'Unknown',
                                'sender_phone': sender_phone or sender_id,
                                'chat_id': chat_id,
                                'group_name': group_name,
                                'is_group': is_group,
                                'bot_id': requesting_bot_id,
                                'bot_name': detected_bot_name or 'Bot',
                                'hub_name': hub.name,
                                'all_bot_names': all_bot_names,
                                'bot_count': len(bot_ids),
                                'is_bot_message': True,
                                'detected_bot': detected_bot_name,
                                'detection_method': detection_method
                            },
                            output_data={
                                'should_respond': False,
                                'reason': f"Bot message from {detected_bot_name} blocked",
                                'bot_to_bot_blocked': True,
                                'bot_to_bot_limit': limit_desc,
                                'detected_bot': detected_bot_name,
                                'detection_method': detection_method,
                                'process_steps': process_steps
                            },
                            status='success',
                            triggered_by='agent',
                            related_entity_type='bot',
                            related_entity_id=requesting_bot_id
                        )
                    except Exception as log_err:
                        logger.debug(f"Failed to log bot-to-bot blocked activity: {log_err}")

                    return self._finalize_processing(message_key, requesting_bot_id, bot_decisions)

                process_steps.append("→ Bot-to-Bot Detection: ✓ User message")

                # =============================================================
                # STEP 1: ENDING DETECTION (run once for all bots)
                # =============================================================
                ending_result = self._check_ending_detection(
                    db=db,
                    hub=hub,
                    chat_id=chat_id,
                    message_content=message_content,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    conversation_id=conversation_id,
                    bot_ids=bot_ids
                )

                if ending_result.get('should_skip'):
                    # All bots should skip - conversation is ending
                    process_steps.append(f"→ Ending Detection: SKIP ({ending_result.get('reason', 'farewell detected')})")
                    process_steps.append("→ Final: ALL BOTS SKIP")

                    bot_decisions = {
                        bot_id: {
                            'should_respond': False,
                            'reason': f"Conversation ending: {ending_result.get('reason')}",
                            'skip_ending_check': True,
                            'process_steps': process_steps.copy()
                        }
                        for bot_id in bot_ids
                    }

                    # Log ending detection activity
                    try:
                        # Get bot name for display (use requesting bot)
                        requesting_bot = db.query(BotProfile).filter(BotProfile.id == requesting_bot_id).first()
                        requesting_bot_name = requesting_bot.name if requesting_bot else f"Bot {requesting_bot_id}"

                        # Look up actual group name from database
                        group_name = _get_group_name_from_db(db, chat_id)

                        ToolMonitor.log_execution(
                            db=db,
                            tool_type='group_management',
                            operation='ending_detection',
                            hub_id=hub_id,
                            input_data={
                                'message': message_content[:200],
                                'sender_name': sender_name or 'Unknown',
                                'sender_phone': sender_phone or sender_id,
                                'chat_id': chat_id,
                                'group_name': group_name,
                                'is_group': True,
                                'bot_id': requesting_bot_id,
                                'bot_name': requesting_bot_name,
                                'hub_name': hub.name,
                                'all_bot_names': all_bot_names,
                                'bot_count': len(bot_ids)
                            },
                            output_data={
                                'should_respond': False,
                                'reason': ending_result.get('reason', 'farewell detected'),
                                'ending_reason': ending_result.get('reason', 'farewell detected'),
                                'delay_ms': 0,
                                'process_steps': process_steps
                            },
                            status='success',
                            triggered_by='agent',
                            related_entity_type='bot',
                            related_entity_id=requesting_bot_id
                        )
                    except Exception as log_err:
                        logger.debug(f"Failed to log ending detection activity: {log_err}")

                    return self._finalize_processing(message_key, requesting_bot_id, bot_decisions)

                # Handle ending detection step display
                if ending_result.get('skipped'):
                    process_steps.append("→ Ending Detection: skipped (no AI provider)")
                else:
                    process_steps.append(f"→ Ending Detection: RESPOND ({ending_result.get('reason', 'not a farewell')})")

                # =============================================================
                # STEP 2: CLASSIFIER + ROUTER (using HubCoordinator)
                # =============================================================
                coordinator = HubCoordinator(hub, db)

                # Get contact if we have phone
                contact = None
                if sender_phone:
                    contact = coordinator._get_or_create_contact(sender_phone, sender_name)

                # Run classifier
                classification = None
                if 'classifier' in coordinator.agents:
                    classification = coordinator._run_classifier(message_content, contact, is_group)
                    if classification:
                        category = classification.get('category', 'unknown')
                        urgency = classification.get('urgency', 'unknown')
                        expertise = classification.get('suggested_expertise', [])
                        expertise_str = ", ".join(expertise) if expertise else "none"
                        process_steps.append(f"→ Classifier: category={category}, urgency={urgency}, expertise=[{expertise_str}]")
                    else:
                        process_steps.append("→ Classifier: Failed")
                        classification = {'category': 'general', 'urgency': 'normal'}

                # Run router to decide which bot(s) should respond
                routing_result = None
                selected_bot_ids = []

                if 'router' in coordinator.agents and classification:
                    # Use the first bot to get routing decision (will apply to all)
                    routing_result = coordinator._run_router(
                        requesting_bot_id=bot_ids[0],  # Use first bot for routing context
                        classification=classification,
                        is_group=is_group,
                        chat_id=chat_id,
                        sender_phone=sender_phone,
                        sender_name=sender_name,
                        message_content=message_content
                    )

                    # Extract which bot(s) should respond
                    responding_bot_id = routing_result.get('responding_bot_id')
                    responding_bots = routing_result.get('responding_bots', [])

                    if responding_bot_id == -1:
                        # Router says no bot should respond (ending detected by router)
                        process_steps.append("→ Router: No bot should respond")
                        selected_bot_ids = []
                    elif responding_bot_id is not None and responding_bot_id > 0:
                        selected_bot_ids = [responding_bot_id]
                    elif responding_bots:
                        selected_bot_ids = [
                            rb.get('bot_id') if isinstance(rb, dict) else rb
                            for rb in responding_bots
                        ]

                    # Get bot names for logging
                    selected_names = []
                    for bid in selected_bot_ids:
                        bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
                        selected_names.append(bot.name if bot else f"Bot {bid}")

                    # Get router reason
                    router_reason = routing_result.get('reason', '') if routing_result else ''

                    if selected_names:
                        reason_str = f" - {router_reason}" if router_reason else ""
                        process_steps.append(f"→ Router: Selected [{', '.join(selected_names)}]{reason_str}")
                    else:
                        reason_str = f" ({router_reason})" if router_reason else ""
                        process_steps.append(f"→ Router: No bot selected{reason_str}")
                else:
                    # No router - use simple rules (first available bot)
                    selected_bot_ids = [bot_ids[0]] if bot_ids else []
                    process_steps.append(f"→ Simple Routing: First bot selected")

                # =============================================================
                # STEP 3: Generate decisions for ALL bots
                # =============================================================
                bot_decisions = {}
                for bot_id in bot_ids:
                    should_respond = bot_id in selected_bot_ids

                    # Get delay for this bot if multiple responding
                    delay_ms = 0
                    if should_respond and len(selected_bot_ids) > 1:
                        # Stagger responses
                        position = selected_bot_ids.index(bot_id)
                        delay_ms = position * random.randint(1000, 3000)

                    bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
                    bot_name = bot.name if bot else f"Bot {bot_id}"

                    steps_for_bot = process_steps.copy()
                    if should_respond:
                        steps_for_bot.append(f"→ Final: {bot_name} RESPONDS" + (f" (delay: {delay_ms}ms)" if delay_ms else ""))
                    else:
                        steps_for_bot.append(f"→ Final: {bot_name} SKIPS (not selected)")

                    bot_decisions[bot_id] = {
                        'should_respond': should_respond,
                        'reason': routing_result.get('reason', 'hub routing') if routing_result else 'simple routing',
                        'skip_ending_check': True,  # Already checked
                        'classification': classification,
                        'routing_context': routing_result.get('routing_context') if routing_result else None,
                        'coordination_hint': routing_result.get('coordination_hint', '') if routing_result else '',
                        'delay_ms': delay_ms,
                        'deferred': not should_respond,
                        'process_steps': steps_for_bot
                    }

                # =============================================================
                # STEP 4: Log activity for group_management tool
                # =============================================================
                try:
                    # Get names of selected bots and first responding bot info
                    responding_names = []
                    first_responding_bot_name = None
                    first_responding_bot_id = None
                    for bid in selected_bot_ids:
                        bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
                        if bot:
                            responding_names.append(bot.name)
                            if first_responding_bot_name is None:
                                first_responding_bot_name = bot.name
                                first_responding_bot_id = bot.id

                    # Determine if any bot should respond
                    any_should_respond = len(selected_bot_ids) > 0

                    # Add final step to process_steps for logging
                    if any_should_respond:
                        if len(responding_names) == 1:
                            process_steps.append(f"→ Final: {responding_names[0]} RESPONDS")
                        else:
                            process_steps.append(f"→ Final: {len(responding_names)} bots RESPOND [{', '.join(responding_names)}]")
                    else:
                        reason = routing_result.get('reason', 'no bot selected') if routing_result else 'no bot selected'
                        process_steps.append(f"→ Final: NO RESPONSE ({reason})")

                    # Get router reason
                    router_reason = ''
                    if routing_result:
                        router_reason = routing_result.get('reason', '')
                        if not router_reason and routing_result.get('responding_bot_id'):
                            router_reason = f"Selected bot {first_responding_bot_name or routing_result.get('responding_bot_id')}"

                    # Look up actual group name from database
                    group_name = _get_group_name_from_db(db, chat_id)

                    execution = ToolMonitor.log_execution(
                        db=db,
                        tool_type='group_management',
                        operation='message_routing',
                        hub_id=hub_id,
                        input_data={
                            'message': message_content[:200],
                            'sender_name': sender_name or 'Unknown',
                            'sender_phone': sender_phone or sender_id,
                            'chat_id': chat_id,
                            'group_name': group_name,
                            'is_group': is_group,
                            'bot_id': first_responding_bot_id or requesting_bot_id,
                            'bot_name': first_responding_bot_name or 'None',
                            'hub_name': hub.name,
                            'all_bot_names': all_bot_names,
                            'bot_count': len(bot_ids)
                        },
                        output_data={
                            'should_respond': any_should_respond,
                            'reason': routing_result.get('reason') if routing_result else 'simple routing',
                            'router_reason': router_reason,
                            'ending_reason': None if ending_result and ending_result.get('skipped') else (ending_result.get('reason') if ending_result else None),
                            'delay_ms': 0,
                            'process_steps': process_steps,
                            'classification': classification,
                            'selected_bots': responding_names
                        },
                        status='success',
                        triggered_by='agent',
                        related_entity_type='bot',
                        related_entity_id=first_responding_bot_id or requesting_bot_id,
                        tokens_used=routing_result.get('tokens_used', 0) if routing_result else 0
                    )

                    # Add execution_id to bot decisions for response tracking
                    execution_id = execution.id if execution else None
                    if execution_id:
                        logger.info(f"HubMessageProcessor: Adding execution_id {execution_id} to {len(bot_decisions)} bot decisions")
                        for bid in bot_decisions:
                            bot_decisions[bid]['execution_id'] = execution_id
                    else:
                        logger.warning(f"HubMessageProcessor: No execution_id returned from logging")
                except Exception as log_err:
                    logger.warning(f"HubMessageProcessor: Failed to log group_management activity: {log_err}")

                return self._finalize_processing(message_key, requesting_bot_id, bot_decisions)

        except Exception as e:
            logger.error(f"HubMessageProcessor: Error processing message: {e}", exc_info=True)
            # On error, let the requesting bot respond and signal others to skip
            return self._finalize_processing(message_key, requesting_bot_id, {
                requesting_bot_id: {
                    'should_respond': True,
                    'reason': f'Processing error: {str(e)}',
                    'skip_ending_check': True,
                    'error': True
                }
            })

    def _check_ending_detection(
        self,
        db: Session,
        hub: Hub,
        chat_id: str,
        message_content: str,
        sender_id: str,
        sender_name: Optional[str],
        conversation_id: Optional[int],
        bot_ids: List[int]
    ) -> Dict[str, Any]:
        """
        Check if the conversation is ending (farewell detection).

        Uses HUB-AWARE conversation-level detection that checks if ANY bot
        in the hub has said goodbye, not just one specific bot.

        Returns:
            Dict with 'should_skip' and 'reason'
        """
        try:
            # Find the first bot with ending_detection_enabled=True AND has an API key
            from app.ai.providers import get_ai_provider

            bot_with_ending = db.query(BotProfile).filter(
                BotProfile.id.in_(bot_ids),
                BotProfile.ending_detection_enabled == True,
                BotProfile.api_key_encrypted.isnot(None),
                BotProfile.api_key_encrypted != ''
            ).first()

            use_ai = False
            ai_provider = None

            if bot_with_ending:
                # Use the bot's API key for AI ending detection
                try:
                    decrypted_key = decrypt_string(bot_with_ending.api_key_encrypted)
                    if decrypted_key:
                        ai_provider = get_ai_provider(
                            provider=bot_with_ending.ai_provider or 'openai',
                            api_key=decrypted_key,
                            model=bot_with_ending.model or 'gpt-4o-mini'
                        )
                        use_ai = True
                        logger.debug(f"HubMessageProcessor: Using bot '{bot_with_ending.name}' API key for ending detection")
                except Exception as e:
                    logger.debug(f"HubMessageProcessor: Failed to get bot API key: {e}")

            if not use_ai:
                # No bot with ending detection + API key - use pattern matching only
                logger.debug(f"HubMessageProcessor: No bot with ending detection + API key, using pattern matching")

            # Use the HUB-AWARE ending detector (checks ANY bot said goodbye)
            from app.conversations.ending_detector import check_hub_conversation_ending

            should_skip, reason = check_hub_conversation_ending(
                ai_provider=ai_provider,
                hub_id=hub.id,
                chat_id=chat_id,
                message_content=message_content,
                sender_id=sender_id,
                sender_name=sender_name,
                use_ai=use_ai
            )

            return {'should_skip': should_skip, 'reason': reason}

        except Exception as e:
            logger.error(f"HubMessageProcessor: Ending detection error: {e}")
            return {'should_skip': False, 'reason': f'error: {str(e)}'}

    def _finalize_processing(
        self,
        message_key: str,
        requesting_bot_id: int,
        bot_decisions: Dict[int, Dict]
    ) -> Dict[str, Any]:
        """Store decisions and signal waiting bots."""
        with self._lock:
            self._decisions[message_key] = {
                'timestamp': datetime.utcnow(),
                'bot_decisions': bot_decisions
            }

            # Signal any waiting bots
            if message_key in self._processing:
                self._processing[message_key].set()
                del self._processing[message_key]

        # Return the decision for the requesting bot
        if requesting_bot_id in bot_decisions:
            return bot_decisions[requesting_bot_id]
        else:
            # Bot not in hub? Let it respond
            return {
                'should_respond': True,
                'reason': 'Bot not found in hub decisions',
                'skip_ending_check': True
            }


# Global processor instance
_hub_message_processor = HubMessageProcessor(ttl_seconds=15, wait_timeout=12.0)


def get_hub_message_decision(
    hub_id: int,
    bot_id: int,
    chat_id: str,
    message_content: str,
    sender_id: str,
    sender_name: Optional[str] = None,
    sender_phone: Optional[str] = None,
    is_group: bool = True,
    whatsapp_message_id: Optional[str] = None,
    conversation_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Get the hub routing decision for a bot.

    This is the main entry point for the centralized hub message processor.
    Call this instead of separate ending detection + hub routing for
    group management hubs.

    Returns:
        Dict with should_respond, reason, skip_ending_check, etc.
    """
    return _hub_message_processor.get_bot_decision(
        hub_id=hub_id,
        bot_id=bot_id,
        chat_id=chat_id,
        message_content=message_content,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_phone=sender_phone,
        is_group=is_group,
        whatsapp_message_id=whatsapp_message_id,
        conversation_id=conversation_id
    )


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
        sender_name: Optional[str] = None,
        whatsapp_message_id: Optional[str] = None
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
            whatsapp_message_id: WhatsApp's unique message ID for bot detection

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

        # Method 1: Check by WhatsApp Message ID + Role (MOST reliable)
        # If this message_id exists in DB as role='assistant' from any hub bot,
        # it means a bot sent this message (we're seeing another bot's response)
        if not is_bot_to_bot and whatsapp_message_id:
            bot_ids = list(self.bot_memberships.keys())
            # Query for any message with same whatsapp_message_id and role='assistant'
            existing_bot_msg = self.db.query(Message, Conversation, BotProfile).join(
                Conversation, Message.conversation_id == Conversation.id
            ).join(
                BotProfile, Conversation.bot_profile_id == BotProfile.id
            ).filter(
                Conversation.bot_profile_id.in_(bot_ids),
                Message.whatsapp_message_id == whatsapp_message_id,
                Message.role == 'assistant'
            ).first()

            if existing_bot_msg:
                msg, conv, bot = existing_bot_msg
                is_bot_to_bot = True
                detected_bot_name = bot.name
                detection_method = "message_id"

        # Method 2: Check by phone number (reliable if phone is available)
        if not is_bot_to_bot:
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

        # NOTE: Name-based detection was REMOVED because:
        # - WhatsApp names are NOT unique identifiers
        # - Multiple accounts can have the same display name
        # - A real user "Tony" and a bot named "Tony" are different accounts
        # - Name matching causes false positives, blocking legitimate user messages

        # Method 3: Check by message content (fallback - matches recent bot responses)
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
                logger.info(f"Hub {self.hub_id}: Bot-to-bot BLOCKED - {reason} (should be logged to Recent Activity)")
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

        # =================================================================
        # COMBINED CLASSIFIER + ROUTER CACHE (Single Wait Optimization)
        # When both classifier and router are configured, wait for BOTH
        # to complete in a single wait, reducing context switches.
        # =================================================================
        classification = None
        cache_chat_id = chat_id or "unknown"

        if 'classifier' in self.agents and 'router' in self.agents:
            # Use combined wait for both classifier and router
            cached_classification, cached_routing = _routing_cache.get_full_routing_result(
                self.hub_id, cache_chat_id, message_content, bot_id
            )

            if cached_classification is not None and cached_routing is not None:
                # Cache HIT - both classifier and router results are available
                classification = cached_classification
                routing = cached_routing

                # Log classifier result
                category = classification.get('category', 'unknown')
                urgency = classification.get('urgency', 'unknown')
                expertise = classification.get('suggested_expertise', [])
                expertise_str = ", ".join(expertise) if expertise else "none"
                process_steps.append(f"→ Classifier (cached): category={category}, urgency={urgency}, expertise=[{expertise_str}]")
                process_steps.append("→ Router (cached): Using cached routing decision")

                # Process routing result
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
                    delay_ms = routing.get("delay_ms", 0)
                    delay_s = delay_ms / 1000 if delay_ms else 0
                    if responding_bot_names:
                        bots_str = ", ".join(responding_bot_names)
                        process_steps.append(f"→ Router Result: Selected [{bots_str}], {bot_display} responding" + (f" (delay: {delay_s}s)" if delay_s else ""))
                    process_steps.append(f"→ Final: RESPONDED ({router_reason})")
                else:
                    if responding_bot_names:
                        bots_str = ", ".join(responding_bot_names)
                        process_steps.append(f"→ Router Result: Selected [{bots_str}], {bot_display} NOT selected")
                    process_steps.append(f"→ Final: NOT RESPONDED ({router_reason})")
                return routing

            else:
                # Cache MISS - this bot computes both classifier and router
                # Run classifier
                classification = self._run_classifier(message_content, contact, is_group)
                if classification:
                    _routing_cache.set_classifier_result(
                        self.hub_id, cache_chat_id, message_content, classification
                    )
                    category = classification.get('category', 'unknown')
                    urgency = classification.get('urgency', 'unknown')
                    expertise = classification.get('suggested_expertise', [])
                    expertise_str = ", ".join(expertise) if expertise else "none"
                    process_steps.append(f"→ Classifier: category={category}, urgency={urgency}, expertise=[{expertise_str}]")

                    # Run router
                    process_steps.append("→ Router: AI routing decision...")
                    routing = self._run_router(
                        bot_id,
                        classification,
                        is_group,
                        chat_id=chat_id,
                        sender_phone=sender_phone,
                        sender_name=sender_name,
                        message_content=message_content
                    )
                    # Cache router result (this also signals the combined wait event)
                    _routing_cache.set_router_result(
                        self.hub_id, cache_chat_id, message_content, routing
                    )

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
                        delay_ms = routing.get("delay_ms", 0)
                        delay_s = delay_ms / 1000 if delay_ms else 0
                        if responding_bot_names:
                            bots_str = ", ".join(responding_bot_names)
                            process_steps.append(f"→ Router Result: Selected [{bots_str}], {bot_display} responding" + (f" (delay: {delay_s}s)" if delay_s else ""))
                        process_steps.append(f"→ Final: RESPONDED ({router_reason})")
                    else:
                        if responding_bot_names:
                            bots_str = ", ".join(responding_bot_names)
                            process_steps.append(f"→ Router Result: Selected [{bots_str}], {bot_display} NOT selected")
                        process_steps.append(f"→ Final: NOT RESPONDED ({router_reason})")
                    return routing
                else:
                    process_steps.append("→ Classifier: ✗ Failed to classify")

        elif 'classifier' in self.agents:
            # Only classifier configured (no router) - use original flow
            classification = _routing_cache.get_classifier_result(
                self.hub_id, cache_chat_id, message_content
            )

            if classification:
                category = classification.get('category', 'unknown')
                urgency = classification.get('urgency', 'unknown')
                expertise = classification.get('suggested_expertise', [])
                expertise_str = ", ".join(expertise) if expertise else "none"
                process_steps.append(f"→ Classifier (cached): category={category}, urgency={urgency}, expertise=[{expertise_str}]")
            else:
                classification = self._run_classifier(message_content, contact, is_group)
                if classification:
                    _routing_cache.set_classifier_result(
                        self.hub_id, cache_chat_id, message_content, classification
                    )
                    category = classification.get('category', 'unknown')
                    urgency = classification.get('urgency', 'unknown')
                    expertise = classification.get('suggested_expertise', [])
                    expertise_str = ", ".join(expertise) if expertise else "none"
                    process_steps.append(f"→ Classifier: category={category}, urgency={urgency}, expertise=[{expertise_str}]")
                else:
                    process_steps.append("→ Classifier: ✗ Failed to classify")

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
                        "reason": f"{selected_name} selected (backup, fallback)",
                        "deferred": True
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
                        "reason": f"{selected_name} selected (specialist, expertise: {suggested_expertise})",
                        "deferred": True
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
                    "reason": f"{selected_name} selected (primary, least busy)",
                    "deferred": True
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
                        "reason": f"{selected_name} selected (member, expertise: {suggested_expertise})",
                        "deferred": True
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
                "reason": f"{selected_name} selected (member, highest priority)",
                "deferred": True
            }

        # No suitable bot found (shouldn't normally reach here)
        return {
            "should_respond": False,
            "reason": "No suitable bot found for routing"
        }

    def _get_or_create_contact(self, phone: str, display_name: Optional[str] = None, profile_pic: Optional[str] = None) -> Contact:
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
                profile_pic=profile_pic,
                first_seen_at=datetime.utcnow()
            )
            self.db.add(contact)
            self.db.commit()
            self.db.refresh(contact)
        else:
            # Update display_name if we have a new one and the current one is empty
            if display_name and not contact.display_name:
                contact.display_name = display_name
            # Update profile_pic if we have a new one and the current one is empty
            if profile_pic and not contact.profile_pic:
                contact.profile_pic = profile_pic

        # Update last interaction
        contact.last_interaction_at = datetime.utcnow()
        self.db.commit()

        return contact

    def _get_conversation_history(
        self,
        chat_id: str,
        sender_phone: Optional[str] = None,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Get recent conversation history for context.

        Returns conversation context including:
        - Recent messages with sender/bot info
        - Last responding bot
        - Conversation depth
        - Topics discussed
        """
        try:
            # Find conversations in this chat for any bot in the hub
            bot_ids = list(self.bot_memberships.keys())
            if not bot_ids:
                return {"messages": [], "last_responding_bot": None, "depth": 0}

            # Get recent messages from conversations matching this chat
            conversations = self.db.query(Conversation).filter(
                Conversation.bot_profile_id.in_(bot_ids),
                Conversation.chat_id == chat_id
            ).all()

            if not conversations:
                # Try matching by chat_name as fallback
                conversations = self.db.query(Conversation).filter(
                    Conversation.bot_profile_id.in_(bot_ids),
                    Conversation.chat_name.contains(chat_id.split('@')[0] if '@' in chat_id else chat_id)
                ).all()

            if not conversations:
                return {"messages": [], "last_responding_bot": None, "depth": 0}

            conv_ids = [c.id for c in conversations]

            # Get recent messages
            messages = self.db.query(Message).filter(
                Message.conversation_id.in_(conv_ids)
            ).order_by(Message.timestamp.desc()).limit(limit).all()

            messages = list(reversed(messages))  # Chronological order

            if not messages:
                return {"messages": [], "last_responding_bot": None, "depth": 0}

            # Format messages for context
            formatted_messages = []
            last_responding_bot = None
            last_responding_bot_id = None
            user_message_count = 0

            for msg in messages:
                # Get bot name for assistant messages
                bot_name = None
                if msg.role == "assistant":
                    # Find which bot sent this
                    conv = self.db.query(Conversation).filter(
                        Conversation.id == msg.conversation_id
                    ).first()
                    if conv:
                        bot = self.db.query(BotProfile).filter(
                            BotProfile.id == conv.bot_profile_id
                        ).first()
                        if bot:
                            bot_name = bot.name
                            last_responding_bot = bot_name
                            last_responding_bot_id = bot.id

                formatted_messages.append({
                    "role": msg.role,
                    "content": msg.content[:200] if msg.content else "",  # Truncate for context
                    "sender_name": msg.sender_name if msg.role == "user" else None,
                    "bot_name": bot_name,
                    "timestamp": msg.timestamp.strftime("%H:%M") if msg.timestamp else None
                })

                if msg.role == "user":
                    user_message_count += 1

            # Calculate conversation depth (user messages = exchanges)
            depth = user_message_count

            # Get last responding bot's expertise
            last_bot_expertise = []
            if last_responding_bot_id and last_responding_bot_id in self.bot_memberships:
                membership = self.bot_memberships[last_responding_bot_id]
                if membership.expertise:
                    try:
                        last_bot_expertise = json.loads(membership.expertise) if isinstance(membership.expertise, str) else membership.expertise
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Calculate time since last message
            time_gap_minutes = None
            if messages:
                last_msg_time = messages[-1].timestamp
                if last_msg_time:
                    time_gap = datetime.utcnow() - last_msg_time
                    time_gap_minutes = int(time_gap.total_seconds() / 60)

            return {
                "messages": formatted_messages,
                "last_responding_bot": last_responding_bot,
                "last_responding_bot_id": last_responding_bot_id,
                "last_bot_expertise": last_bot_expertise,
                "depth": depth,
                "time_gap_minutes": time_gap_minutes
            }

        except Exception as e:
            logger.warning(f"Hub {self.hub_id}: Failed to get conversation history: {e}")
            return {"messages": [], "last_responding_bot": None, "depth": 0}

    def _format_conversation_for_router(self, history: Dict[str, Any]) -> str:
        """Format conversation history for the router prompt."""
        if not history.get("messages"):
            return "No previous conversation in this chat."

        lines = []
        for msg in history["messages"]:
            timestamp = msg.get("timestamp", "")
            if msg["role"] == "user":
                sender = msg.get("sender_name", "User")
                lines.append(f"[{timestamp}] {sender}: {msg['content']}")
            else:
                bot = msg.get("bot_name", "Bot")
                lines.append(f"[{timestamp}] {bot} (bot): {msg['content']}")

        return "\n".join(lines)

    def _get_ai_provider(self, agent: AIAgent):
        """Get AI provider for an agent, with fallback to hub settings."""
        try:
            from app.ai import get_ai_provider

            # Get API key (agent-specific or hub default)
            api_key = None
            if agent.api_key_encrypted:
                api_key = decrypt_string(agent.api_key_encrypted)
            elif self.hub.api_key_encrypted:
                api_key = decrypt_string(self.hub.api_key_encrypted)

            if not api_key:
                return None

            # Determine provider (agent > hub > default)
            provider_name = agent.ai_provider or self.hub.ai_provider or "openai"
            model = agent.model or self.hub.model or "gpt-4o-mini"

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
            if agent.api_key_encrypted:
                api_key = decrypt_string(agent.api_key_encrypted)
            elif self.hub.api_key_encrypted:
                api_key = decrypt_string(self.hub.api_key_encrypted)
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

            # Load hub topics for dynamic classification (with descriptions)
            hub_topics = self.db.query(HubMessageTopic).filter(
                HubMessageTopic.hub_id == self.hub_id
            ).all()

            # Build topic info with descriptions for better AI understanding
            topic_names = [t.name for t in hub_topics]
            topics_with_desc = []
            for t in hub_topics:
                if t.description:
                    topics_with_desc.append(f"- {t.name}: {t.description}")
                else:
                    topics_with_desc.append(f"- {t.name}")

            # Also collect all expertise from bot memberships
            all_expertise = set()
            for membership in self.hub.bot_memberships:
                if membership.is_active and membership.expertise:
                    try:
                        expertise_list = json.loads(membership.expertise)
                        all_expertise.update(e.lower() for e in expertise_list)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"Hub {self.hub_id}: Failed to parse expertise in classifier: {e}")

            # Combine topics and expertise for category list
            available_categories = list(set(topic_names) | all_expertise)
            if not available_categories:
                available_categories = ["sales", "support", "billing", "general", "greeting", "feedback", "complaint"]

            # Build dynamic system prompt - ALWAYS use default with dynamic categories
            # Classifier MUST have access to current topics and expertise for accurate classification
            categories_str = ", ".join(available_categories)

            # Build categories description section (includes descriptions for better classification)
            if topics_with_desc:
                categories_desc = "\n".join(topics_with_desc)
                categories_section = f"""Available categories and their meanings:
{categories_desc}

Additional expertise tags: {", ".join(all_expertise) if all_expertise else "none"}"""
            else:
                categories_section = f"Available categories: {categories_str}"

            system_prompt = f"""You are a message classifier. Analyze the message and classify it.

{categories_section}

Return JSON with:
- category: main topic (one of: {categories_str})
- urgency: low, medium, high
- sentiment: positive, negative, neutral
- suggested_expertise: list of relevant expertise tags from: {categories_str}
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
                    model=agent.model or "gpt-4o-mini",
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
        is_group: bool,
        chat_id: Optional[str] = None,
        sender_phone: Optional[str] = None,
        sender_name: Optional[str] = None,
        message_content: Optional[str] = None
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

            # Get conversation history for context
            conversation_history = {}
            if chat_id:
                conversation_history = self._get_conversation_history(chat_id, sender_phone, limit=10)

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
            system_prompt = f"""You are a response router for a multi-bot WhatsApp system. Based on the message classification, conversation history, and available bots, decide which bot(s) should respond.

Available message categories/topics: {categories_str}

Return JSON with:
1. Routing decision (EITHER format):
   - Single bot: {{"responding_bot_id": <bot_id>, "reason": "...", "coordination_hint": "..."}}
   - Multiple bots: {{"responding_bots": [{{"bot_id": <id>, "order": 1, "role_hint": "..."}}, ...], "reason": "...", "coordination_hint": "..."}}
2. Use responding_bot_id: -1 or responding_bots: [] if NO bot should respond.

CONVERSATION ENDING DETECTION (CHECK FIRST - HIGHEST PRIORITY):
Before routing, check if the conversation has naturally ended:

1. Is the new message a FAREWELL? (bye, goodbye, see you, take care, goodnight, later, ttyl, ciao, gotta go, peace, I'm out, etc.)
   - Check conversation history: Did a bot ALREADY say goodbye to this sender?
   - If bot already said goodbye AND new message is farewell → responding_bot_id: -1 (no response needed)
   - If bot has NOT said goodbye yet → route to the bot that was helping (let them say goodbye)

2. Is the new message a SHORT ACKNOWLEDGMENT after farewell? (ok, thanks, you too, welcome, sure, 👍, etc.)
   - If there was already a farewell exchange in history → responding_bot_id: -1 (no response needed)
   - Short acknowledgments after goodbye don't need a response

3. Is the new message a GREETING or QUESTION?
   - Greetings (hi, hello, hey) → ALWAYS route to a bot (new conversation starting)
   - Questions (contains ?) → ALWAYS route to a bot

4. Time gap > 60 minutes since last message?
   - Treat as NEW conversation - route normally, ignore previous farewells

CONVERSATION CONTINUITY GUIDELINES:
1. If the sender was recently talking to a specific bot, STRONGLY PREFER continuing with that bot
2. Conversation depth matters: After 3+ exchanges with a bot, maintain continuity unless absolutely necessary to switch
3. Only switch bots if:
   - The topic CLEARLY requires specialist expertise the current bot lacks AND is urgent
   - The current bot is unavailable (not in available bots list)
   - The user explicitly asks for different help
4. "Related" questions (e.g., asking about support while discussing pricing) are usually part of the same conversation - continue with the same bot
5. When switching IS necessary, prefer adding a specialist alongside the current bot rather than replacing

ROUTING RULES:
1. FIRST: Check conversation ending (see above) - return -1 if conversation has ended
2. SECOND: Check conversation history - prefer the bot who was already helping this sender
3. If new conversation or switch needed: Match bot expertise to message category
4. Primary role bots handle general/unmatched queries
5. Specialist bots should be preferred for new conversations when their expertise matches
6. Backup role bots only respond when no other suitable bot is available
7. When max_responding_bots > 1, select bots that would create natural conversation flow
8. Use recent_responses count for load balancing (prefer less busy bots) - but continuity trumps load balancing
9. In coordination_hint, provide guidance for how the bot should respond (brief, detailed, handoff, etc.)

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

            # Format conversation history for prompt
            history_text = self._format_conversation_for_router(conversation_history)
            last_bot = conversation_history.get("last_responding_bot")
            last_bot_id = conversation_history.get("last_responding_bot_id")
            last_bot_expertise = conversation_history.get("last_bot_expertise", [])
            conv_depth = conversation_history.get("depth", 0)
            time_gap = conversation_history.get("time_gap_minutes")

            # Build conversation context summary
            conv_context = "New conversation (no history)"
            if conv_depth > 0:
                time_str = f"{time_gap} minutes ago" if time_gap else "recently"
                conv_context = f"Ongoing conversation: {conv_depth} exchanges with {last_bot or 'unknown bot'}, last message {time_str}"
                if last_bot_expertise:
                    conv_context += f" (expertise: {', '.join(last_bot_expertise)})"

            # Build user prompt with conversation context
            user_prompt = f"""CONVERSATION HISTORY:
{history_text}

CONVERSATION CONTEXT:
- {conv_context}
- Last responding bot: {last_bot or 'None'} (ID: {last_bot_id or 'N/A'})
- Conversation depth: {conv_depth} exchanges
- Time since last message: {time_gap} minutes

CURRENT MESSAGE:
- Sender: {sender_name or 'Unknown'}
- Content: "{message_content[:200] if message_content else 'N/A'}"
- Classification: {json.dumps(classification)}

AVAILABLE BOTS:
{json.dumps(bots_info, indent=2)}

SETTINGS:
- Requesting Bot ID: {requesting_bot_id}
- Is Group Chat: {is_group}
- Max Responding Bots for "{category}": {max_bots_for_category}
- Response Delay Range: {delay_min}s - {delay_max}s

DECISION REQUIRED:
1. Is this a continuation of the ongoing conversation? If yes, prefer the last responding bot ({last_bot or 'N/A'}).
2. If switching bots is necessary, explain why in the reason.
3. Provide coordination_hint for how the selected bot(s) should respond.

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
                    model=agent.model or "gpt-4o-mini",
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

            # Mark as "deferred" if this bot should NOT respond but other bots will
            # (used to skip logging redundant "not responded" records)
            deferred_to_other_bot = (not should_respond and len(responding_bots_list) > 0)

            # Convert delay to milliseconds
            delay_ms = delay_s * 1000

            # Build routing context for the responding bot's prompt
            routing_context = None
            if should_respond:
                # Determine why this bot was selected
                is_continuation = (
                    conversation_history.get("last_responding_bot_id") == requesting_bot_id
                    and conversation_history.get("depth", 0) > 0
                )

                why_selected = reason
                if is_continuation:
                    why_selected = f"Continuing conversation ({conversation_history.get('depth', 0)} exchanges)"

                routing_context = {
                    "why_selected": why_selected,
                    "classification": classification,
                    "is_continuation": is_continuation,
                    "conversation_depth": conversation_history.get("depth", 0),
                    "coordination_hint": result.get("coordination_hint", "")
                }

            # Build other_responders list for coordination
            other_responders = []
            if should_respond and len(responding_bots_list) > 1:
                for bot_info in responding_bots_list:
                    if bot_info['bot_id'] != requesting_bot_id:
                        other_bot = self.db.query(BotProfile).filter(
                            BotProfile.id == bot_info['bot_id']
                        ).first()
                        other_bot_name = other_bot.name if other_bot else f"Bot {bot_info['bot_id']}"

                        # Get role hint from result if available
                        role_hint = ""
                        if 'responding_bots' in result:
                            for rb in result.get('responding_bots', []):
                                if isinstance(rb, dict) and rb.get('bot_id') == bot_info['bot_id']:
                                    role_hint = rb.get('role_hint', '')
                                    break

                        other_responders.append({
                            "bot_id": bot_info['bot_id'],
                            "bot_name": other_bot_name,
                            "order": bot_info.get('order', 0),
                            "role_hint": role_hint
                        })

            return {
                "should_respond": should_respond,
                "reason": reason,
                "delay_ms": delay_ms,  # Changed from delay_s to delay_ms
                "classification": classification,
                "responding_bots": [b['bot_id'] for b in responding_bots_list],
                "deferred": deferred_to_other_bot,
                # New fields for bot prompt enhancement
                "routing_context": routing_context,
                "other_responders": other_responders,
                "coordination_hint": result.get("coordination_hint", "")
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

    def _run_analyzer(
        self,
        contact: Contact,
        messages: List[Dict],
        bot_names: Optional[List[str]] = None
    ) -> Optional[Dict]:
        """
        Run the analyzer agent on a contact to generate insights.

        Args:
            contact: The Contact to analyze
            messages: List of message dicts with role/content/timestamp
            bot_names: Optional list of bot names the contact interacted with

        Returns:
            Analysis result with tags, engagement_score, predicted_intent, etc.
        """
        agent = self.agents.get('analyzer')
        if not agent:
            return None

        try:
            # Get AI provider
            provider = self._get_ai_provider(agent)
            if not provider:
                print(f"Hub {self.hub_id}: No API key for analyzer")
                return None

            # Get existing tags
            existing_tags = self.db.query(ContactTag).filter(
                ContactTag.contact_id == contact.id
            ).all()
            existing_tag_names = [t.tag for t in existing_tags]

            # Build input data
            input_data = {
                "contact": {
                    "phone": contact.phone,
                    "display_name": contact.display_name,
                    "existing_tags": existing_tag_names,
                    "engagement_score": contact.engagement_score,
                    "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None
                },
                "messages": messages,
                "context": {
                    "hub_name": self.hub.name,
                    "bot_names": bot_names or []
                }
            }

            # Format messages for prompt
            message_history = "\n".join([
                f"[{m.get('timestamp', '')}] {m['role'].upper()}: {m['content']}"
                for m in messages[-50:]  # Last 50 messages
            ])

            # Build system prompt (use custom if available)
            system_prompt = agent.system_prompt or """You are a contact analyzer AI. Analyze conversation history to build a comprehensive contact profile.

Return a JSON object with:
{
    "tags": [{"tag": "tag_name", "confidence": 0.85, "value": null}],
    "engagement_score": 75,
    "predicted_intent": "buyer",
    "sentiment": "positive",
    "urgency": "medium",
    "follow_up_needed": true,
    "follow_up_reason": "Reason for follow-up",
    "description": "Brief profile summary",
    "key_topics": ["topic1", "topic2"]
}

Return ONLY valid JSON."""

            # Append additional instructions if provided
            if agent.additional_instructions:
                system_prompt += f"\n\nAdditional Instructions:\n{agent.additional_instructions}"

            user_message = f"""Analyze this contact's conversation history and return your analysis as JSON:

Phone: {contact.phone}
Name: {contact.display_name or 'Unknown'}
Existing Tags: {', '.join(existing_tag_names) if existing_tag_names else 'None'}
Current Engagement Score: {contact.engagement_score}

Conversation History:
{message_history if message_history else 'No messages available'}

Provide a comprehensive analysis in JSON format."""

            messages_for_ai = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]

            # Call AI provider
            if hasattr(provider, 'chat_completion'):
                response = provider.chat_completion(
                    messages=messages_for_ai,
                    temperature=0.3,
                    max_tokens=1000,
                    json_mode=True
                )
                result = json.loads(response.content)
                tokens_used = response.usage.get("total_tokens", 0)
            else:
                # Fallback to direct OpenAI client
                response = provider.chat.completions.create(
                    model=agent.model or "gpt-4o-mini",
                    messages=messages_for_ai,
                    temperature=0.3,
                    max_tokens=1000,
                    response_format={"type": "json_object"}
                )
                result = json.loads(response.choices[0].message.content)
                tokens_used = response.usage.total_tokens if response.usage else 0

            # Log execution
            self._log_agent_execution(
                agent.id,
                "analysis",
                {"contact_phone": contact.phone, "message_count": len(messages)},
                result,
                tokens_used
            )

            # Update agent last run
            agent.last_run_at = datetime.utcnow()
            self.db.commit()

            return result

        except Exception as e:
            print(f"Hub {self.hub_id}: Analyzer error: {e}")
            import traceback
            traceback.print_exc()
            self._log_agent_execution(
                agent.id,
                "analysis",
                {"contact_phone": contact.phone},
                {"error": str(e)},
                0,
                status="error",
                error_message=str(e)
            )
            return None

    def analyze_contact(self, contact_id: int) -> Optional[Dict]:
        """
        Analyze a specific contact and update their profile.

        This is a public method that can be called to analyze a contact
        on demand (e.g., from a scheduled task or API endpoint).

        Args:
            contact_id: The ID of the contact to analyze

        Returns:
            Analysis result if successful, None otherwise
        """
        contact = self.db.query(Contact).filter(
            Contact.id == contact_id,
            Contact.hub_id == self.hub_id
        ).first()

        if not contact:
            return None

        # Get conversation history for this contact
        conversations = self.db.query(Conversation).filter(
            Conversation.phone == contact.phone
        ).all()

        # Collect messages
        messages = []
        bot_names = set()
        for conv in conversations:
            if conv.bot_profile:
                bot_names.add(conv.bot_profile.name)

            conv_messages = self.db.query(Message).filter(
                Message.conversation_id == conv.id
            ).order_by(Message.timestamp.asc()).limit(100).all()

            for msg in conv_messages:
                messages.append({
                    "role": msg.role or "user",
                    "content": msg.content or "",
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None
                })

        # Run analyzer
        result = self._run_analyzer(contact, messages, list(bot_names))

        if not result or result.get("error"):
            return result

        # Update contact with analysis results
        if result.get("description"):
            desc = result["description"]
            # Handle if description is a dict
            if isinstance(desc, dict):
                desc = desc.get("text") or desc.get("summary") or str(desc)
            contact.description = str(desc) if desc else None

        if result.get("predicted_intent"):
            intent = result["predicted_intent"]
            # Handle if predicted_intent is a dict (e.g., {'intent': '...', 'likelihood_score': 0.75})
            if isinstance(intent, dict):
                intent = intent.get("intent") or intent.get("type") or intent.get("value") or list(intent.values())[0]
            contact.predicted_intent = str(intent) if intent else None

        if result.get("engagement_score") is not None:
            score = result["engagement_score"]
            # Handle if score is a dict
            if isinstance(score, dict):
                score = score.get("score") or score.get("value") or 50
            if score > 1:
                score = score / 100.0
            contact.engagement_score = min(1.0, max(0.0, float(score)))

        contact.updated_at = datetime.utcnow()

        # Add new tags
        if result.get("tags"):
            for tag_data in result["tags"]:
                tag_name = tag_data.get("tag") if isinstance(tag_data, dict) else str(tag_data)
                if not tag_name:
                    continue

                existing = self.db.query(ContactTag).filter(
                    ContactTag.contact_id == contact_id,
                    ContactTag.tag == tag_name
                ).first()

                if not existing:
                    new_tag = ContactTag(
                        contact_id=contact_id,
                        tag=tag_name,
                        value=tag_data.get("value") if isinstance(tag_data, dict) else None,
                        confidence=tag_data.get("confidence", 1.0) if isinstance(tag_data, dict) else 1.0,
                        source="ai_analyzer"
                    )
                    self.db.add(new_tag)

        self.db.commit()
        return result

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

    DEPRECATED: Use get_hub_for_bot_and_group() instead for accurate hub matching
    when a bot belongs to multiple hubs managing different groups.
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


def get_hub_for_bot_and_group(bot_id: int, chat_id: str, db: Session) -> Optional[Hub]:
    """
    Get the hub that manages this specific bot AND group combination.

    This function handles the case where a bot belongs to multiple hubs,
    each managing different groups. It finds the correct hub based on
    which hub has this specific group in its selected_groups.

    Args:
        bot_id: The bot profile ID
        chat_id: The chat/group ID the message came from
        db: Database session

    Returns:
        The Hub that manages this bot+group combination, or None if not found.
    """
    # Step 1: Find ALL hubs this bot belongs to
    memberships = db.query(HubBotMembership).filter(
        HubBotMembership.bot_profile_id == bot_id,
        HubBotMembership.is_active == True
    ).all()

    if not memberships:
        return None

    # Step 2: For each hub, check if this group is in its selected_groups
    for membership in memberships:
        hub = db.query(Hub).filter(
            Hub.id == membership.hub_id,
            Hub.is_active == True
        ).first()

        if not hub:
            continue

        # Only process group_management hubs for message routing
        if hub.task_type != 'group_management':
            continue

        # Parse selected groups
        selected_group_ids = set()
        if hub.selected_groups:
            try:
                selected_groups = json.loads(hub.selected_groups)
                for group in selected_groups:
                    if isinstance(group, dict) and group.get('chat_id'):
                        selected_group_ids.add(group['chat_id'])
            except (json.JSONDecodeError, TypeError):
                pass

        # If no groups selected, hub manages all groups for this bot
        if not selected_group_ids:
            return hub

        # Check if this chat_id is in the hub's selected groups
        if chat_id in selected_group_ids:
            return hub

    # No matching hub found - return None (bot will use standard response)
    return None


def check_hub_routing(
    bot_id: int,
    message_content: str,
    sender_phone: Optional[str],
    chat_id: str,
    is_group: bool = False,
    sender_name: Optional[str] = None,
    whatsapp_message_id: Optional[str] = None
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
        whatsapp_message_id: WhatsApp's unique message ID for bot detection

    Returns:
        Dict with should_respond, reason, and optional delay_ms
    """
    import time
    start_time = time.time()

    try:
        with get_db_session() as db:
            # For group messages, find the hub that manages this specific bot+group combination
            # This handles bots that belong to multiple hubs managing different groups
            if is_group:
                hub = get_hub_for_bot_and_group(bot_id, chat_id, db)
            else:
                # For private messages, use the old function (first hub found)
                hub = get_hub_for_bot(bot_id, db)

            if not hub:
                # Bot not in any hub (or no hub manages this group), respond normally
                return {
                    "should_respond": True,
                    "reason": "Bot not in any hub for this group"
                }

            coordinator = HubCoordinator(hub, db)

            # For group_management hubs with private messages, skip hub coordination
            if hub.task_type == 'group_management' and not is_group:
                return {
                    "should_respond": True,
                    "reason": "Private message - not managed by group_management hub"
                }

            result = coordinator.should_bot_respond(
                bot_id=bot_id,
                message_content=message_content,
                sender_phone=sender_phone,
                chat_id=chat_id,
                is_group=is_group,
                sender_name=sender_name,
                whatsapp_message_id=whatsapp_message_id
            )

            # Log to ToolExecution - but skip logging when another bot was allocated
            # (to avoid redundant "not responded" records when router selected a different bot)
            execution_time_ms = int((time.time() - start_time) * 1000)
            tool_type = hub.task_type or 'group_management'  # Default to group_management
            execution_id = None

            # Check if this is a "deferred to other bot" case - don't log these
            # The router sets 'deferred: True' when this bot should not respond but other bots will
            if result.get('deferred', False):
                # Skip logging - another bot was allocated to respond, no need to log this bot's "not responded"
                logger.debug(f"Hub routing: Skipping log for deferred case (bot {bot_id})")
                result['execution_id'] = None
                return result

            # Log this execution (including bot-to-bot blocked, working hours blocked, etc.)
            logger.debug(f"Hub routing: Logging execution for bot {bot_id}, should_respond={result.get('should_respond')}, reason={result.get('reason', '')[:50]}")

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
                if execution_id:
                    logger.info(f"Hub routing: Logged execution {execution_id} for bot {bot_id}, should_respond={result.get('should_respond')}")
            except Exception as log_err:
                logger.error(f"Failed to log tool execution: {log_err}")

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
