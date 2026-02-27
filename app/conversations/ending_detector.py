"""
Conversation Ending Detector

Uses AI to detect when a conversation has naturally ended (e.g., after farewell exchanges)
to prevent unnecessary bot responses like endless goodbye loops.

CORE PRINCIPLE: Only skip responses when the NEW incoming message is a farewell
or short acknowledgment AND the bot has already said goodbye. Always respond
to questions, greetings, new topics, and any non-farewell message.
"""

import logging
import hashlib
import threading
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Time gap threshold - if last message was more than this long ago, treat as new conversation
NEW_CONVERSATION_THRESHOLD_MINUTES = 60  # 1 hour


# =============================================================================
# ENDING DETECTION CACHE - Prevents duplicate AI calls when multiple bots
# process the same message in a hub-managed group.
# =============================================================================

class EndingDetectionCache:
    """
    Cache for ending detection results with pending lock mechanism.

    When multiple bots process the same message simultaneously:
    - Bot 1: Cache MISS, marks as PENDING, runs AI analysis
    - Bot 2: Sees PENDING, WAITS for Bot 1 to finish
    - Bot 3: Sees PENDING, WAITS for Bot 1 to finish
    - Bot 1: Finishes, caches result, signals waiting bots
    - Bot 2 & 3: Wake up, use cached result

    This ensures only 1 AI call is made even when bots process simultaneously.
    """

    def __init__(self, ttl_seconds: int = 10, wait_timeout: float = 10.0):
        self._cache: Dict[str, Dict[str, any]] = {}
        self._pending: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._wait_timeout = wait_timeout

    def _make_key(self, chat_id: str, message_content: str, sender_id: str) -> str:
        """Create cache key based on chat + message + sender."""
        content_hash = hashlib.md5(message_content.encode()).hexdigest()[:16]
        sender_hash = sender_id[-8:] if sender_id else "unknown"
        return f"ending:{chat_id}:{sender_hash}:{content_hash}"

    def _cleanup_expired(self):
        """Remove expired entries."""
        now = datetime.utcnow()
        expired = [k for k, v in self._cache.items()
                   if (now - v.get('timestamp', now)).total_seconds() > self._ttl]
        for k in expired:
            del self._cache[k]
            self._pending.pop(k, None)

    def get(self, chat_id: str, message_content: str, sender_id: str) -> Optional[str]:
        """
        Get cached AI decision, waiting if another bot is computing.

        Returns:
            - "RESPOND" or "SKIP": Cached decision (hit or waited)
            - None: Cache miss AND this bot should compute (marked as pending)
        """
        key = self._make_key(chat_id, message_content, sender_id or "")
        event_to_wait = None

        with self._lock:
            self._cleanup_expired()

            # Check if result is already cached
            entry = self._cache.get(key)
            if entry and 'decision' in entry:
                logger.debug(f"Ending cache HIT for chat {chat_id}")
                return entry.get('decision')

            # Check if another bot is already computing
            if key in self._pending:
                event_to_wait = self._pending[key]
                logger.debug(f"Ending cache WAIT for chat {chat_id} (another bot computing)")
            else:
                # We're the first - mark as pending
                self._pending[key] = threading.Event()
                logger.debug(f"Ending cache MISS for chat {chat_id} (will compute)")
                return None

        # Wait outside the lock
        if event_to_wait:
            event_to_wait.wait(timeout=self._wait_timeout)
            # Check cache again after waiting
            with self._lock:
                entry = self._cache.get(key)
                if entry and 'decision' in entry:
                    logger.debug(f"Ending cache HIT (after wait) for chat {chat_id}")
                    return entry.get('decision')

                # Timeout - try to become the new computing bot
                if key not in self._pending:
                    # Previous bot finished but no result (error?) - we'll compute
                    self._pending[key] = threading.Event()
                    logger.debug(f"Ending cache TIMEOUT (will compute) for chat {chat_id}")
                    return None
                else:
                    # Another bot is still pending or took over - default to RESPOND
                    # (safer to respond than to skip)
                    logger.debug(f"Ending cache TIMEOUT (default RESPOND) for chat {chat_id}")
                    return "RESPOND"

        return None

    def set(self, chat_id: str, message_content: str, sender_id: str, decision: str):
        """Cache AI decision and signal waiting bots."""
        key = self._make_key(chat_id, message_content, sender_id or "")
        with self._lock:
            self._cache[key] = {
                'decision': decision,
                'timestamp': datetime.utcnow()
            }

            # Signal waiting bots that result is ready
            if key in self._pending:
                self._pending[key].set()
                del self._pending[key]

            logger.debug(f"Ending cache SET for chat {chat_id}: {decision}")


# Global cache instance
_ending_cache = EndingDetectionCache(ttl_seconds=10)


def check_conversation_ending(
    ai_provider,
    conversation_id: int,
    message_content: str,
    bot_profile_id: int,
    is_group: bool = False,
    sender_name: Optional[str] = None,
    sender_id: Optional[str] = None,
    max_messages: int = 10,
    chat_id: Optional[str] = None,
    skip_for_hub: bool = False,
    use_ai: bool = True
) -> Tuple[bool, str]:
    """
    Detect if a conversation has naturally ended.

    IMPORTANT: This should only skip responses when the NEW incoming message
    is a farewell or short acknowledgment AND the bot has already said goodbye.

    The bot should ALWAYS respond to:
    - Questions
    - Greetings
    - New topics
    - Any non-farewell message

    Args:
        ai_provider: The AI provider instance for making API calls
        conversation_id: Database ID of the conversation
        message_content: The incoming message text
        bot_profile_id: The bot's profile ID (for logging)
        is_group: Whether this is a group conversation
        sender_name: Name of the message sender (for groups)
        sender_id: Unique ID of the message sender (WhatsApp ID or phone)
        max_messages: Maximum recent messages to include in context
        chat_id: Chat/group ID for caching (optional)
        skip_for_hub: If True, skip ending detection (hub router handles it)
        use_ai: If True, use AI for edge cases. If False, only use pattern matching.

    Returns:
        Tuple of (should_skip: bool, reason: str)
        - should_skip=True means the bot should NOT respond
        - reason explains why
    """
    from app.database import get_db_session, Message, Conversation, BotProfile

    # For hub-managed groups, skip ending detection (router handles continuity)
    if skip_for_hub:
        logger.debug(f"Bot {bot_profile_id}: Skipping ending detection (hub-managed)")
        return False, "hub_managed_skip"

    # =================================================================
    # QUICK PATTERN CHECKS - No AI call needed for obvious cases
    # =================================================================
    msg_lower = message_content.lower().strip()

    # Common greetings - always respond
    greeting_patterns = [
        'hi', 'hello', 'hey', 'hola', 'good morning', 'good afternoon',
        'good evening', 'good night', 'morning', 'afternoon', 'evening',
        'how are you', "how's it going", "how's your day", "what's up",
        'sup', 'yo', 'hii', 'hiii', 'hiiii', 'heyyy', 'heyy'
    ]
    for pattern in greeting_patterns:
        if msg_lower.startswith(pattern) or msg_lower == pattern:
            logger.debug(f"Bot {bot_profile_id}: Quick check - greeting detected, will respond")
            return False, "quick_check_greeting"

    # Questions - always respond
    if '?' in message_content:
        logger.debug(f"Bot {bot_profile_id}: Quick check - question detected, will respond")
        return False, "quick_check_question"

    # Farewell patterns - check if bot already said goodbye
    farewell_patterns = [
        'bye', 'goodbye', 'good bye', 'see you', 'see ya', 'take care',
        'goodnight', 'good night', 'later', 'ttyl', 'ciao', 'adios',
        'gotta go', 'have to go', 'peace', "i'm out", 'im out', 'catch you later',
        'talk later', 'until next time', 'farewell', 'so long'
    ]

    # Short acknowledgments that might indicate conversation ending
    acknowledgment_patterns = [
        'ok', 'okay', 'k', 'thanks', 'thank you', 'thx', 'ty',
        'you too', 'u too', 'welcome', 'sure', 'alright', 'cool',
        'np', 'no problem', 'no worries', 'got it', 'understood'
    ]

    is_farewell = any(pattern in msg_lower for pattern in farewell_patterns)
    is_acknowledgment = msg_lower in acknowledgment_patterns or any(msg_lower == pattern for pattern in acknowledgment_patterns)

    # If message is a farewell or acknowledgment, check if bot already said goodbye
    if is_farewell or is_acknowledgment:
        try:
            with get_db_session() as db:
                # Get recent bot messages to check if already said goodbye
                recent_bot_msgs = db.query(Message).filter(
                    Message.conversation_id == conversation_id,
                    Message.role == 'assistant'
                ).order_by(Message.timestamp.desc()).limit(3).all()

                bot_said_goodbye = False
                for msg in recent_bot_msgs:
                    msg_content_lower = msg.content.lower() if msg.content else ""
                    if any(pattern in msg_content_lower for pattern in farewell_patterns):
                        bot_said_goodbye = True
                        break

                if bot_said_goodbye:
                    logger.debug(f"Bot {bot_profile_id}: Quick check - farewell exchange complete, will skip")
                    return True, "quick_check_farewell_complete"

        except Exception as e:
            logger.debug(f"Bot {bot_profile_id}: Error checking bot messages: {e}")

    # If not using AI, respond to everything else (pattern matching only)
    if not use_ai:
        logger.debug(f"Bot {bot_profile_id}: Pattern-only mode - will respond")
        return False, "pattern_check_respond"

    # =================================================================
    # CHECK CACHE FIRST - Avoid duplicate AI calls for same message
    # With pending lock: If another bot is computing, this will WAIT
    # and return the cached result once available.
    # =================================================================
    cache_chat_id = chat_id or str(conversation_id)
    cached_decision = _ending_cache.get(cache_chat_id, message_content, sender_id or "")

    if cached_decision == "RESPOND":
        # Cache says this is not a farewell - respond immediately
        logger.debug(f"Bot {bot_profile_id}: Ending cache hit - RESPOND")
        return False, "cached_respond"
    elif cached_decision == "SKIP":
        # Cache says this is a farewell and bot already said goodbye - skip
        logger.debug(f"Bot {bot_profile_id}: Ending cache hit - SKIP")
        return True, "cached_skip"

    # cached_decision is None - this bot should compute (marked as pending)
    # Other bots that arrive later will wait for our result

    # Get conversation history for AI analysis
    try:
        with get_db_session() as db:
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id
            ).first()

            if not conversation:
                return False, "conversation_not_found"

            # Get the current bot's name
            current_bot = db.query(BotProfile).filter(
                BotProfile.id == bot_profile_id
            ).first()
            current_bot_name = current_bot.name if current_bot else f"Bot {bot_profile_id}"

            # Get recent messages
            recent_messages = db.query(Message).filter(
                Message.conversation_id == conversation_id
            ).order_by(Message.timestamp.desc()).limit(max_messages).all()

            recent_messages = list(reversed(recent_messages))

            if len(recent_messages) < 2:
                return False, "insufficient_history"

            # Check time gap - if last message was long ago, this is a new conversation
            last_message = recent_messages[-1]
            if last_message.timestamp:
                now = datetime.utcnow()
                time_gap_minutes = (now - last_message.timestamp).total_seconds() / 60
                if time_gap_minutes > NEW_CONVERSATION_THRESHOLD_MINUTES:
                    logger.debug(f"Bot {bot_profile_id}: Time gap of {time_gap_minutes:.0f}min, new conversation")
                    # Cache this as RESPOND for other bots
                    _ending_cache.set(cache_chat_id, message_content, sender_id or "", "RESPOND")
                    return False, "time_gap_new_conversation"

            # Format full conversation history for AI analysis
            conversation_history = _format_conversation_history(
                recent_messages,
                is_group,
                conversation.chat_name,
                current_bot_name,
                bot_profile_id
            )

    except Exception as e:
        logger.error(f"Bot {bot_profile_id}: Error fetching messages: {e}")
        return False, f"error: {e}"

    # =================================================================
    # AI CALL - Only if not cached
    # =================================================================
    decision = _analyze_with_ai(
        ai_provider,
        message_content,
        conversation_history,
        current_bot_name,
        sender_name,
        sender_id,
        is_group,
        bot_profile_id
    )

    # Cache the result for other bots
    _ending_cache.set(cache_chat_id, message_content, sender_id or "", decision)

    if decision == "RESPOND":
        logger.debug(f"Bot {bot_profile_id}: AI says respond to: {message_content[:50]}")
        return False, "ai_decision_respond"
    else:
        logger.info(f"Bot {bot_profile_id}: AI says skip - farewell exchange complete")
        return True, "ai_decision_skip"


def _format_conversation_history(
    messages: list,
    is_group: bool,
    chat_name: str,
    bot_name: str,
    bot_id: int
) -> str:
    """
    Format the full conversation history with sender names, unique IDs, and timestamps.

    Returns formatted string like:
    [2026-02-13 20:27] [User:abc123] John: Bye for now
    [2026-02-13 20:28] [Bot:1] Assistant: Goodbye John, take care!
    [2026-02-14 10:06] [User:abc123] John: Hi guys, how's your day going?

    Args:
        messages: List of Message objects
        is_group: Whether this is a group chat
        chat_name: Name of the chat/contact
        bot_name: Name of the bot handling this conversation
        bot_id: The bot's profile ID
    """
    lines = []

    for msg in messages:
        # Format timestamp
        timestamp_str = ""
        if msg.timestamp:
            timestamp_str = f"[{msg.timestamp.strftime('%Y-%m-%d %H:%M')}] "

        # Determine the sender label with unique identifier
        if msg.role == "assistant":
            # Bot message - include bot_id to distinguish from other bots
            sender_label = f"[Bot:{bot_id}] {bot_name}"
        else:
            # User message - always include unique identifier
            sender_id = msg.sender_id or msg.sender_phone or "unknown"
            # Use last 8 chars of ID for uniqueness while keeping it readable
            short_id = sender_id[-8:] if len(sender_id) > 8 else sender_id

            if is_group and msg.sender_name:
                sender_label = f"[User:{short_id}] {msg.sender_name}"
            else:
                sender_label = f"[User:{short_id}] {chat_name or 'User'}"

        # Truncate long messages
        content = msg.content[:300] if len(msg.content) > 300 else msg.content

        lines.append(f"{timestamp_str}{sender_label}: {content}")

    return "\n".join(lines)


def _analyze_with_ai(
    ai_provider,
    message_content: str,
    conversation_history: str,
    current_bot_name: str,
    sender_name: Optional[str],
    sender_id: Optional[str],
    is_group: bool,
    bot_profile_id: int
) -> str:
    """
    Single AI call to determine if bot should respond.

    Returns: "RESPOND" or "SKIP"
    """
    try:
        from app.ai.providers.base import AIMessage

        sender_display = sender_name or "User"
        # Create unique sender identifier
        short_sender_id = ""
        if sender_id:
            short_sender_id = sender_id[-8:] if len(sender_id) > 8 else sender_id
        sender_label = f"{sender_display} [User:{short_sender_id}]" if short_sender_id else sender_display

        chat_type = "group chat" if is_group else "private chat"
        bot_label = f"{current_bot_name} [Bot:{bot_profile_id}]"

        # Build prompt with full conversation context
        if conversation_history:
            prompt = f"""You are deciding whether "{bot_label}" should RESPOND or SKIP an incoming message.

CONTEXT:
- Chat type: {chat_type}
- Current bot being asked: {bot_label}
- New message sender: {sender_label}

RECENT CONVERSATION HISTORY (format: [timestamp] [Type:ID] Name: message):
{conversation_history}

NEW INCOMING MESSAGE from {sender_label}: "{message_content}"

DECISION RULES (check in order):

1. Is the new message a GREETING or QUESTION?
   - Greetings: hi, hello, hey, good morning/afternoon/evening, how are you, how's it going, etc.
   - Questions: contains "?" or asks something
   → RESPOND (always respond to greetings and questions)

2. TIME GAP RULE - VERY IMPORTANT:
   - Look at the timestamps in the conversation history
   - If there were old farewell messages (bye, goodbye) but NOW the user is sending a NEW greeting or question:
     - This is a NEW CONVERSATION - the old farewells are RESET
     - Example: "Bye" at 8:27 PM yesterday, then "Hi guys" at 10:06 AM today → RESPOND
   - Old farewells do NOT carry over to new conversation sessions
   → RESPOND to any greeting/question even if there were old farewells

3. Is the new message NORMAL conversation content?
   - Requests, statements, topics, anything that needs a reply
   → RESPOND

4. Is the new message a FAREWELL from this sender IN THE CURRENT SESSION?
   - bye, goodbye, see you, take care, goodnight, later, ttyl, ciao, gotta go, peace, I'm out, etc.
   - For this to be a "skip", BOTH must be true:
     a) The bot ALREADY said goodbye to this user RECENTLY (same day/session)
     b) No new greeting/question has been sent since the farewell exchange
   - If the bot hasn't said goodbye yet in this session → RESPOND
   - IMPORTANT: Match by ID, not by name. Users/bots with same name but different IDs are different people.

5. Is the new message a SHORT ACKNOWLEDGMENT from this sender?
   - ok, thanks, you too, welcome, sure, alright, cool, 👍, etc.
   - Check: Did [Bot:{bot_profile_id}] ALREADY say goodbye TO [User:{short_sender_id}] RECENTLY (same session)?
     - If yes AND no new conversation started since → SKIP
     - Otherwise → RESPOND

IMPORTANT RULES:
- When in doubt, choose RESPOND. We prefer to respond rather than miss a message.
- A greeting or question ALWAYS resets any prior farewell exchange and starts fresh.
- MATCH BY ID, NOT BY NAME: Two users named "John" with different IDs are different people
- Only consider farewell exchanges between THIS sender [User:{short_sender_id}] and THIS bot [Bot:{bot_profile_id}]
- Other participants' farewell exchanges (different IDs) do NOT affect this decision
- OLD farewells (from hours/days ago) are RESET when user sends a new greeting or question
- If timestamps show the user is starting a new conversation after old farewells → RESPOND

Respond with ONLY one word: RESPOND or SKIP"""
        else:
            # No conversation history - almost always respond
            prompt = f"""You are deciding whether "{bot_label}" should RESPOND or SKIP an incoming message.

CONTEXT:
- Chat type: {chat_type}
- Current bot: {bot_label}
- New message sender: {sender_label}

(No recent conversation history available)

NEW INCOMING MESSAGE from {sender_label}: "{message_content}"

DECISION RULES:
1. Greetings (hi, hello, hey) → RESPOND
2. Questions (contains ?) → RESPOND
3. Normal conversation → RESPOND
4. Farewells (bye, goodbye, etc.) → RESPOND (bot hasn't said goodbye yet)
5. Short acknowledgments (ok, thanks) → RESPOND

Since there's no conversation history, the bot should almost always RESPOND.

Respond with ONLY one word: RESPOND or SKIP"""

        messages = [
            AIMessage(role="user", content=prompt)
        ]

        response = ai_provider.chat_completion(
            messages=messages,
            temperature=0.1,
            max_tokens=10
        )

        result = response.content.strip().upper()

        if "SKIP" in result:
            return "SKIP"
        else:
            return "RESPOND"  # Default to RESPOND

    except Exception as e:
        logger.error(f"Bot {bot_profile_id}: AI analysis failed: {e}")
        return "RESPOND"  # On error, default to responding


# Convenience function for use in whatsapp_bot.py
def should_skip_for_conversation_ending(
    ai_provider,
    conversation_id: int,
    message_content: str,
    bot_profile_id: int,
    is_group: bool = False,
    sender_name: Optional[str] = None,
    sender_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    skip_for_hub: bool = False,
    use_ai: bool = True
) -> bool:
    """
    Simplified interface that returns just True/False.

    Args:
        ai_provider: AI provider for making API calls
        conversation_id: Database conversation ID
        message_content: The message text
        bot_profile_id: The bot's profile ID
        is_group: Whether this is a group chat
        sender_name: Sender's display name
        sender_id: Sender's unique ID
        chat_id: Chat/group ID for caching
        skip_for_hub: If True, skip detection (hub router handles it)
        use_ai: If True, use AI for edge cases. If False, only use pattern matching.

    Returns True if the bot should NOT respond (conversation is ending).
    """
    should_skip, reason = check_conversation_ending(
        ai_provider=ai_provider,
        conversation_id=conversation_id,
        message_content=message_content,
        bot_profile_id=bot_profile_id,
        is_group=is_group,
        sender_name=sender_name,
        sender_id=sender_id,
        chat_id=chat_id,
        skip_for_hub=skip_for_hub,
        use_ai=use_ai
    )

    if should_skip:
        logger.info(f"Bot {bot_profile_id}: Skipping response - {reason}")

    return should_skip


# =============================================================================
# HUB-AWARE CONVERSATION-LEVEL ENDING DETECTION
# For group_management hubs with multiple bots
# =============================================================================

def check_hub_conversation_ending(
    ai_provider,
    hub_id: int,
    chat_id: str,
    message_content: str,
    sender_id: str,
    sender_name: Optional[str] = None,
    max_messages: int = 15,
    use_ai: bool = True
) -> Tuple[bool, str]:
    """
    Hub-aware ending detection that checks if ANY bot in the hub has said goodbye.

    This is different from per-bot ending detection:
    - Per-bot: "Did THIS specific bot say goodbye?"
    - Hub-level: "Did ANY bot in the hub say goodbye to this user?"

    This ensures consistent decisions across all bots in the hub.

    Args:
        ai_provider: The AI provider instance for making API calls
        hub_id: The hub ID
        chat_id: The chat/group ID
        message_content: The incoming message text
        sender_id: Unique ID of the message sender
        sender_name: Name of the message sender
        max_messages: Maximum recent messages to include in context
        use_ai: If True, use AI for edge cases. If False, only use pattern matching.

    Returns:
        Tuple of (should_skip: bool, reason: str)
    """
    from app.database import get_db_session, Message, Conversation, BotProfile, Hub, HubBotMembership

    # =================================================================
    # QUICK PATTERN CHECKS - No AI call needed for obvious cases
    # =================================================================
    msg_lower = message_content.lower().strip()

    # Common greetings - always respond
    greeting_patterns = [
        'hi', 'hello', 'hey', 'hola', 'good morning', 'good afternoon',
        'good evening', 'good night', 'morning', 'afternoon', 'evening',
        'how are you', "how's it going", "how's your day", "what's up",
        'sup', 'yo', 'hii', 'hiii', 'hiiii', 'heyyy', 'heyy'
    ]
    for pattern in greeting_patterns:
        if msg_lower.startswith(pattern) or msg_lower == pattern:
            logger.debug(f"Hub {hub_id}: Quick check - greeting detected, will respond")
            return False, "quick_check_greeting"

    # Questions - always respond
    if '?' in message_content:
        logger.debug(f"Hub {hub_id}: Quick check - question detected, will respond")
        return False, "quick_check_question"

    # Farewell patterns
    farewell_patterns = [
        'bye', 'goodbye', 'good bye', 'see you', 'see ya', 'take care',
        'goodnight', 'good night', 'later', 'ttyl', 'ciao', 'adios',
        'gotta go', 'have to go', 'peace', "i'm out", 'im out', 'catch you later',
        'talk later', 'until next time', 'farewell', 'so long'
    ]

    # Short acknowledgments
    acknowledgment_patterns = [
        'ok', 'okay', 'k', 'thanks', 'thank you', 'thx', 'ty',
        'you too', 'u too', 'welcome', 'sure', 'alright', 'cool',
        'np', 'no problem', 'no worries', 'got it', 'understood'
    ]

    is_farewell = any(pattern in msg_lower for pattern in farewell_patterns)
    is_acknowledgment = msg_lower in acknowledgment_patterns or any(msg_lower == pattern for pattern in acknowledgment_patterns)

    try:
        with get_db_session() as db:
            # Get the hub and all its active bots
            hub = db.query(Hub).filter(Hub.id == hub_id).first()
            if not hub:
                return False, "hub_not_found"

            # Get all active bot IDs in this hub
            bot_memberships = db.query(HubBotMembership).filter(
                HubBotMembership.hub_id == hub_id,
                HubBotMembership.is_active == True
            ).all()

            bot_ids = [m.bot_profile_id for m in bot_memberships]
            if not bot_ids:
                return False, "no_bots_in_hub"

            # If message is a farewell or acknowledgment, check if any bot already said goodbye
            if is_farewell or is_acknowledgment:
                # Get conversations for this chat from ALL bots in the hub
                conversations = db.query(Conversation).filter(
                    Conversation.chat_id == chat_id,
                    Conversation.bot_profile_id.in_(bot_ids)
                ).all()

                if conversations:
                    conversation_ids = [c.id for c in conversations]

                    # Check recent bot messages for farewells
                    recent_bot_msgs = db.query(Message).filter(
                        Message.conversation_id.in_(conversation_ids),
                        Message.role == 'assistant'
                    ).order_by(Message.timestamp.desc()).limit(5).all()

                    for msg in recent_bot_msgs:
                        msg_content_lower = msg.content.lower() if msg.content else ""
                        if any(pattern in msg_content_lower for pattern in farewell_patterns):
                            logger.debug(f"Hub {hub_id}: Quick check - farewell exchange complete, will skip")
                            return True, "quick_check_farewell_complete"

            # If not using AI, respond to everything else
            if not use_ai:
                logger.debug(f"Hub {hub_id}: Pattern-only mode - will respond")
                return False, "pattern_check_respond"

            # Get bot info for display
            bots = db.query(BotProfile).filter(BotProfile.id.in_(bot_ids)).all()
            bot_names = {b.id: b.name for b in bots}

            # Get conversations for this chat from ALL bots in the hub
            conversations = db.query(Conversation).filter(
                Conversation.chat_id == chat_id,
                Conversation.bot_profile_id.in_(bot_ids)
            ).all()

            if not conversations:
                # No conversation history - respond
                return False, "no_conversation_history"

            conversation_ids = [c.id for c in conversations]

            # Get recent messages from ALL bots' conversations for this chat
            recent_messages = db.query(Message).filter(
                Message.conversation_id.in_(conversation_ids)
            ).order_by(Message.timestamp.desc()).limit(max_messages).all()

            recent_messages = list(reversed(recent_messages))

            if len(recent_messages) < 2:
                return False, "insufficient_history"

            # Check time gap
            last_message = recent_messages[-1]
            if last_message.timestamp:
                now = datetime.utcnow()
                time_gap_minutes = (now - last_message.timestamp).total_seconds() / 60
                if time_gap_minutes > NEW_CONVERSATION_THRESHOLD_MINUTES:
                    logger.debug(f"Hub {hub_id}: Time gap of {time_gap_minutes:.0f}min, new conversation")
                    return False, "time_gap_new_conversation"

            # Build combined conversation history from all bots
            # Need to map conversation_id to bot_id
            conv_to_bot = {c.id: c.bot_profile_id for c in conversations}

            conversation_history = _format_hub_conversation_history(
                messages=recent_messages,
                conv_to_bot=conv_to_bot,
                bot_names=bot_names,
                chat_name=conversations[0].chat_name if conversations else "Group"
            )

            # Build list of bot names for prompt
            bot_list = ", ".join([bot_names.get(bid, f"Bot {bid}") for bid in bot_ids])

    except Exception as e:
        logger.error(f"Hub {hub_id}: Error fetching messages for ending detection: {e}")
        return False, f"error: {e}"

    # Run AI analysis with hub-aware prompt
    decision = _analyze_hub_ending_with_ai(
        ai_provider=ai_provider,
        message_content=message_content,
        conversation_history=conversation_history,
        bot_list=bot_list,
        sender_name=sender_name,
        sender_id=sender_id,
        hub_id=hub_id
    )

    if decision == "SKIP":
        logger.info(f"Hub {hub_id}: AI says skip - conversation naturally ending")
        return True, "hub_conversation_ending"
    else:
        logger.debug(f"Hub {hub_id}: AI says respond to: {message_content[:50]}")
        return False, "hub_conversation_continue"


def _format_hub_conversation_history(
    messages: list,
    conv_to_bot: Dict[int, int],
    bot_names: Dict[int, str],
    chat_name: str
) -> str:
    """
    Format conversation history from multiple bots in a hub with timestamps.

    Returns formatted string like:
    [2026-02-13 20:27] [User:abc123] John: Bye for now
    [2026-02-13 20:28] [Bot:SalesBot] Goodbye John!
    [2026-02-14 10:06] [User:abc123] John: Hi guys, how's it going?
    """
    lines = []

    for msg in messages:
        # Format timestamp
        timestamp_str = ""
        if msg.timestamp:
            timestamp_str = f"[{msg.timestamp.strftime('%Y-%m-%d %H:%M')}] "

        if msg.role == "assistant":
            # Bot message - get bot name from conversation mapping
            bot_id = conv_to_bot.get(msg.conversation_id)
            bot_name = bot_names.get(bot_id, f"Bot {bot_id}") if bot_id else "Bot"
            sender_label = f"[Bot:{bot_name}]"
        else:
            # User message
            sender_id = msg.sender_id or msg.sender_phone or "unknown"
            short_id = sender_id[-8:] if len(sender_id) > 8 else sender_id

            if msg.sender_name:
                sender_label = f"[User:{short_id}] {msg.sender_name}"
            else:
                sender_label = f"[User:{short_id}] {chat_name or 'User'}"

        # Truncate long messages
        content = msg.content[:300] if len(msg.content) > 300 else msg.content
        lines.append(f"{timestamp_str}{sender_label}: {content}")

    return "\n".join(lines)


def _analyze_hub_ending_with_ai(
    ai_provider,
    message_content: str,
    conversation_history: str,
    bot_list: str,
    sender_name: Optional[str],
    sender_id: Optional[str],
    hub_id: int
) -> str:
    """
    AI analysis for hub-level conversation ending.

    Uses a prompt that asks about ANY bot, not a specific bot.
    """
    try:
        from app.ai.providers.base import AIMessage

        sender_display = sender_name or "User"
        short_sender_id = sender_id[-8:] if sender_id and len(sender_id) > 8 else (sender_id or "unknown")
        sender_label = f"{sender_display} [User:{short_sender_id}]"

        prompt = f"""You are deciding whether ANY bot should respond to an incoming message in a multi-bot group chat.

CONTEXT:
- Chat type: group chat (hub-managed with multiple bots)
- Bots in this hub: {bot_list}
- New message sender: {sender_label}

RECENT CONVERSATION HISTORY (format: [timestamp] [Type:ID] Name: message):
{conversation_history}

NEW INCOMING MESSAGE from {sender_label}: "{message_content}"

DECISION RULES (check in order):

1. Is the new message a GREETING or QUESTION?
   - Greetings: hi, hello, hey, good morning/afternoon/evening, how are you, how's it going, etc.
   - Questions: contains "?" or asks something
   → RESPOND (always respond to greetings and questions)

2. TIME GAP RULE - VERY IMPORTANT:
   - Look at the timestamps in the conversation history
   - If there were old farewell messages (bye, goodbye) but NOW the user is sending a NEW greeting or question:
     - This is a NEW CONVERSATION - the old farewells are RESET
     - Example: "Bye" at 8:27 PM yesterday, then "Hi guys" at 10:06 AM today → RESPOND
   - Old farewells do NOT carry over to new conversation sessions
   → RESPOND to any greeting/question even if there were old farewells

3. Is the new message NORMAL conversation content?
   - Requests, statements, topics, anything that needs a reply
   → RESPOND

4. Is the new message a FAREWELL from this sender IN THE CURRENT SESSION?
   - bye, goodbye, see you, take care, goodnight, later, ttyl, ciao, gotta go, peace, I'm out, etc.
   - For this to be a "skip", BOTH must be true:
     a) ANY bot ALREADY said goodbye to this user RECENTLY (same day/session)
     b) No new greeting/question has been sent since the farewell exchange
   - If no bot has said goodbye yet in this session → RESPOND
   - IMPORTANT: Match by ID, not by name.

5. Is the new message a SHORT ACKNOWLEDGMENT from this sender?
   - ok, thanks, you too, welcome, sure, alright, cool, 👍, etc.
   - Check: Did ANY bot ALREADY say goodbye to [User:{short_sender_id}] RECENTLY (same session)?
     - If yes AND no new conversation started since → SKIP
     - Otherwise → RESPOND

IMPORTANT:
- This is a MULTI-BOT hub. Check if ANY bot (not just one specific bot) has said goodbye.
- When in doubt, choose RESPOND. We prefer to respond rather than miss a message.
- Match users by their ID [User:XXXXXXXX], not by name.
- OLD farewells (from hours/days ago) are RESET when user sends a new greeting or question.
- A greeting or question ALWAYS resets any prior farewell exchange and starts fresh.

Respond with ONLY one word: RESPOND or SKIP"""

        messages = [
            AIMessage(role="user", content=prompt)
        ]

        response = ai_provider.chat_completion(
            messages=messages,
            temperature=0.1,
            max_tokens=10
        )

        result = response.content.strip().upper()

        if "SKIP" in result:
            return "SKIP"
        else:
            return "RESPOND"

    except Exception as e:
        logger.error(f"Hub {hub_id}: AI analysis failed: {e}")
        return "RESPOND"  # On error, default to responding
