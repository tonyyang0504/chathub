"""
Database Models and Connection
"""

import os
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Float, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session
from sqlalchemy.pool import NullPool, QueuePool
from contextlib import contextmanager

from app.config import settings

# Ensure data directory exists for SQLite
db_path = settings.DATABASE_URL.replace("sqlite:///", "").replace("sqlite:", "")
if db_path and os.path.dirname(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

# Create engine with proper pool configuration
# For SQLite: use NullPool to avoid connection pool exhaustion during long operations
# For other databases: use QueuePool with larger size
if "sqlite" in settings.DATABASE_URL:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=NullPool  # No pooling - each connection is fresh
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        poolclass=QueuePool,
        pool_size=20,
        max_overflow=30,
        pool_timeout=60,
        pool_pre_ping=True
    )

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


# ============== Models ==============

class User(Base):
    """User account model."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(255))
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    bot_profiles = relationship("BotProfile", back_populates="user", cascade="all, delete-orphan")


class BotProfile(Base):
    """Bot profile/configuration model."""
    __tablename__ = "bot_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)

    # Platform type (whatsapp, telegram, instagram, messenger, wechat, line, linkedin, tinder, bumble)
    platform_type = Column(String(50), default="whatsapp")

    # AI Provider Settings
    ai_provider = Column(String(50), default="openai")  # 'openai', 'anthropic', 'google', 'deepseek', 'qwen'

    # API Settings (API key encrypted - used for any provider)
    api_key_encrypted = Column(Text, nullable=False)
    model = Column(String(50), default="gpt-4o-mini")
    system_prompt = Column(Text, default="You are a helpful assistant. Do not use markdown formatting like asterisks (*), underscores (_), or other special characters for emphasis. Write plain text only.")
    temperature = Column(Float, default=0.7)  # AI creativity (0.0-2.0)
    max_tokens = Column(Integer, default=1000)  # Max response length
    # Advanced AI Settings
    top_p = Column(Float, default=1.0)  # Nucleus sampling (0.0-1.0)
    frequency_penalty = Column(Float, default=0.0)  # Reduce repetition (-2.0 to 2.0)
    presence_penalty = Column(Float, default=0.0)  # Encourage new topics (-2.0 to 2.0)

    # Bot Settings
    max_history = Column(Integer, default=20)
    response_delay_min = Column(Integer, default=3)
    response_delay_max = Column(Integer, default=8)
    group_chat_enabled = Column(Boolean, default=True)
    respond_to_all_in_group = Column(Boolean, default=False)
    ending_detection_enabled = Column(Boolean, default=False)  # Enable AI-based ending detection (False = pattern matching only)
    headless = Column(Boolean, default=False)  # Run browser in headless mode

    # Proxy Settings
    proxy_enabled = Column(Boolean, default=False)  # Enable proxy for this bot
    proxy_url = Column(String(500))  # Proxy URL (e.g., http://proxy.example.com:8080)
    proxy_username = Column(String(255))  # Proxy username (optional, encrypted)
    proxy_password = Column(String(255))  # Proxy password (optional, encrypted)

    # Browser Timezone Setting (IANA timezone ID for Playwright)
    # Default 'UTC' means WhatsApp shows UTC timestamps - simplest, no conversion needed
    # Can be set to match proxy location (e.g., 'America/New_York', 'Europe/London', 'Asia/Dubai')
    browser_timezone = Column(String(100), default='UTC')

    # Platform-specific configuration (JSON string for OAuth tokens, API keys, etc.)
    platform_config = Column(Text, default="{}")

    # Status
    is_active = Column(Boolean, default=False)
    is_running = Column(Boolean, default=False)
    whatsapp_connected = Column(Boolean, default=False)
    last_active = Column(DateTime)

    # WhatsApp Account Info (populated when connected)
    whatsapp_phone = Column(String(50))  # Phone number
    whatsapp_name = Column(String(255))  # Display name
    whatsapp_push_name = Column(String(255))  # Push name
    whatsapp_profile_pic = Column(Text)  # Profile picture URL
    whatsapp_about = Column(Text)  # About/status text
    whatsapp_account_type = Column(String(50), default="personal")  # 'personal' or 'business'

    # Timezone offset detected from WhatsApp (hours from UTC, e.g., -5 for UTC-5)
    # This is auto-detected by comparing WhatsApp timestamps with actual UTC time
    whatsapp_timezone_offset = Column(Integer, nullable=True)  # None means not yet detected

    # Session data (encrypted)
    session_data = Column(Text)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="bot_profiles")
    conversations = relationship("Conversation", back_populates="bot_profile", cascade="all, delete-orphan")
    scheduled_messages = relationship("ScheduledMessage", back_populates="bot_profile", cascade="all, delete-orphan")


class Conversation(Base):
    """Conversation/chat model."""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    bot_profile_id = Column(Integer, ForeignKey("bot_profiles.id"), nullable=False, index=True)
    chat_id = Column(String(255), nullable=False, index=True)  # Unique identifier (name or data-id)
    chat_name = Column(String(255))  # Display name shown in sidebar (contact name or phone)
    display_name = Column(String(255))  # WhatsApp saved contact name (if available)
    phone = Column(String(50))  # Phone number extracted from message data-ids
    is_group = Column(Boolean, default=False)
    profile_pic = Column(Text)  # Contact/group profile picture URL or base64
    message_count = Column(Integer, default=0)
    last_message_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Sync status - track if full history has been fetched
    history_synced = Column(Boolean, default=False)  # True if full history fetched
    last_synced_at = Column(DateTime)  # When messages were last synced

    # Human takeover - when human replies, AI bot pauses
    human_takeover = Column(Boolean, default=False)  # True if human has taken over
    human_takeover_at = Column(DateTime)  # When human takeover started

    # Relationships
    bot_profile = relationship("BotProfile", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    """Message model."""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # 'user', 'assistant', 'system'
    content = Column(Text, nullable=False)
    sender_name = Column(String(255))
    sender_id = Column(String(255))  # WhatsApp ID (could be LID or phone)
    sender_phone = Column(String(50))  # Extracted phone number (distinct from LID)
    sender_profile_pic = Column(Text)  # Sender's profile picture for group messages
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    # WhatsApp message tracking
    whatsapp_message_id = Column(String(255))  # Message ID from WhatsApp data-id

    # File attachment fields
    file_url = Column(Text)  # URL or path to the uploaded file
    file_name = Column(String(500))  # Original filename
    file_type = Column(String(100))  # MIME type (image/png, application/pdf, etc.)
    file_size = Column(Integer)  # File size in bytes
    file_pages = Column(Integer)  # Number of pages (for PDFs)
    media_analysis = Column(Text)  # AI-generated analysis of media (images, documents, videos)

    # Relationships
    conversation = relationship("Conversation", back_populates="messages")


class ScheduledMessage(Base):
    """Scheduled message model."""
    __tablename__ = "scheduled_messages"

    id = Column(Integer, primary_key=True, index=True)
    bot_profile_id = Column(Integer, ForeignKey("bot_profiles.id"), nullable=False)
    chat_id = Column(String(255), nullable=False)
    chat_name = Column(String(255))
    message = Column(Text, nullable=False)
    scheduled_time = Column(DateTime, nullable=False)
    repeat_type = Column(String(20))  # 'once', 'daily', 'weekly'
    is_sent = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    bot_profile = relationship("BotProfile", back_populates="scheduled_messages")


class ActivityLog(Base):
    """Activity log for analytics."""
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    bot_profile_id = Column(Integer, ForeignKey("bot_profiles.id"), nullable=False)
    action = Column(String(50), nullable=False)  # 'message_sent', 'message_received', 'bot_started', etc.
    details = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)


# ============== Hub Models ==============

class Hub(Base):
    """Hub - Coordination center for multi-bot AI orchestration."""
    __tablename__ = "hubs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    task_type = Column(String(50), nullable=False, default="group_management")  # group_management, scheduled_content, contact_analyzer, message_routing, content_generator

    # AI Provider Settings
    ai_provider = Column(String(50), default="openai")  # 'openai', 'anthropic', 'google', 'deepseek', 'qwen'
    api_key_encrypted = Column(Text)  # Default API key for hub agents (used for any provider)
    model = Column(String(50), default="gpt-4o-mini")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Multi-bot response settings
    max_responding_bots = Column(Integer, default=1)  # Default max bots (0 = unlimited, use all available bots)
    response_delay_min = Column(Integer, default=1)  # Minimum delay between bot responses (seconds)
    response_delay_max = Column(Integer, default=3)  # Maximum delay between bot responses (seconds, randomized)
    multi_response_rules = Column(Text)  # JSON: per-category rules [{"category": "greeting", "max_bots": 1, "delay_min": 1, "delay_max": 3}, ...]

    # Bot-to-bot conversation settings
    bot_conversation_limit = Column(Integer, default=0)  # Max bot-to-bot conversations (0 = none/disabled, -1 = unlimited)
    bot_conversation_interval = Column(String(20), default="hour")  # Interval: minute, hour, day

    # Selected groups for this hub
    selected_groups = Column(Text)  # JSON: [{"chat_id": "123@g.us", "name": "Group 1"}, ...]

    # Legacy fields - kept for backward compatibility
    multi_response_categories = Column(Text)  # Deprecated: use multi_response_rules instead
    response_delay_min_ms = Column(Integer, default=1000)  # Deprecated: use response_delay_min instead
    response_delay_max_ms = Column(Integer, default=3000)  # Deprecated: use response_delay_max instead
    response_delay_ms = Column(Integer, default=2000)  # Deprecated: use min/max instead

    # Auto-analysis settings (for contact_analyzer hubs)
    auto_analysis_enabled = Column(Boolean, default=False)
    auto_analysis_interval_hours = Column(Integer, default=24)  # How often to check for new messages
    auto_analysis_min_new_messages = Column(Integer, default=5)  # Min new messages to trigger re-analysis
    auto_analysis_last_run = Column(DateTime)  # Last time auto-analysis ran

    # Auto-send follow-up settings (for contact_followup hubs)
    auto_send_enabled = Column(Boolean, default=False)
    auto_send_interval_minutes = Column(Integer, default=15)  # How often to check for pending contacts
    auto_send_tone = Column(String(20), default="friendly")  # friendly, professional, casual
    auto_send_speed_mode = Column(String(20), default="auto")  # auto, custom, fast
    auto_send_delay_min = Column(Integer, default=5)  # Min seconds between sends
    auto_send_delay_max = Column(Integer, default=15)  # Max seconds between sends
    auto_send_batch_size = Column(Integer, default=20)  # Messages per batch (0 = no batching)
    auto_send_batch_pause = Column(Integer, default=180)  # Seconds to pause between batches
    auto_send_bot_ids = Column(Text)  # JSON array of bot profile IDs, null = any available
    auto_send_filters = Column(Text)  # JSON: {urgency, sentiment, engagement, date, custom_days, custom_days_direction}
    auto_send_last_run = Column(DateTime)

    # Relationships
    user = relationship("User", backref="hubs")
    bot_memberships = relationship("HubBotMembership", back_populates="hub", cascade="all, delete-orphan")
    agents = relationship("AIAgent", back_populates="hub", cascade="all, delete-orphan")
    contacts = relationship("Contact", back_populates="hub", cascade="all, delete-orphan")
    scheduled_contents = relationship("ScheduledContent", back_populates="hub", cascade="all, delete-orphan")
    message_topics = relationship("HubMessageTopic", back_populates="hub", cascade="all, delete-orphan")


class HubBotMembership(Base):
    """Hub-Bot membership - Assigns bots to hubs with roles."""
    __tablename__ = "hub_bot_memberships"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False)
    bot_profile_id = Column(Integer, ForeignKey("bot_profiles.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), default="member")  # 'primary', 'specialist', 'backup', 'member'
    expertise = Column(Text)  # JSON: ["sales", "support", "billing"]
    priority = Column(Integer, default=0)
    can_initiate = Column(Boolean, default=True)  # Can start conversations
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Working hours settings
    working_hours_start = Column(String(5))  # DEPRECATED - use working_periods instead
    working_hours_end = Column(String(5))  # DEPRECATED - use working_periods instead
    working_periods = Column(Text)  # JSON: [{"start": "09:00", "end": "12:00"}, {"start": "14:00", "end": "18:00"}]
    working_days = Column(Text)  # JSON: ["mon", "tue", "wed", "thu", "fri"] (null = all days)

    # Relationships
    hub = relationship("Hub", back_populates="bot_memberships")
    bot_profile = relationship("BotProfile", backref="hub_memberships")


class HubMessageTopic(Base):
    """Hub Message Topic - Centralized list of message categories/topics for a hub."""
    __tablename__ = "hub_message_topics"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)  # e.g., "sales", "billing", "support"
    description = Column(Text)  # Optional description of the topic
    is_system = Column(Boolean, default=False)  # True for predefined topics, False for user-created
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    hub = relationship("Hub", back_populates="message_topics")

    # Unique constraint: each topic name should be unique within a hub
    __table_args__ = (
        UniqueConstraint('hub_id', 'name', name='uq_hub_topic_name'),
    )


class AgentTemplate(Base):
    """Agent Template - Predefined agent configurations for quick creation."""
    __tablename__ = "agent_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    agent_type = Column(String(50), nullable=False)  # 'classifier', 'router', 'generator', 'scheduler', 'analyzer', 'followup'
    description = Column(Text)
    system_prompt = Column(Text)
    default_config = Column(Text)  # JSON: default configuration
    is_system = Column(Boolean, default=False)  # System-provided vs user-created
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # NULL for system templates
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="agent_templates")


class AIAgent(Base):
    """AI Agent - Coordinating agents for hubs."""
    __tablename__ = "ai_agents"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    agent_type = Column(String(50), nullable=False)  # 'classifier', 'router', 'generator', 'scheduler', 'analyzer', 'followup'
    description = Column(Text)

    # AI Provider Settings
    ai_provider = Column(String(50), default="openai")  # 'openai', 'anthropic', 'google', 'deepseek', 'qwen'
    api_key_encrypted = Column(Text)  # Override hub default if set (used for any provider)
    model = Column(String(50), default="gpt-4o-mini")
    system_prompt = Column(Text)  # Custom system prompt (for generator, analyzer, followup agents)
    additional_instructions = Column(Text)  # Extra instructions appended to default prompt (for classifier, router)
    config = Column(Text)  # JSON: agent-specific settings
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    # New fields for global agent management and monitoring
    is_global = Column(Boolean, default=False)  # Can be viewed across hubs
    template_id = Column(Integer, ForeignKey("agent_templates.id"), nullable=True)
    status = Column(String(20), default="idle")  # 'idle', 'running', 'error'
    last_error = Column(Text)
    total_executions = Column(Integer, default=0)
    successful_executions = Column(Integer, default=0)
    total_tokens_used = Column(Integer, default=0)

    # Relationships
    hub = relationship("Hub", back_populates="agents")
    executions = relationship("AgentExecution", back_populates="agent", cascade="all, delete-orphan")
    template = relationship("AgentTemplate", backref="agents")


class Contact(Base):
    """Contact - Unified contact profiles across hub bots."""
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False)
    phone = Column(String(50), nullable=False, index=True)
    display_name = Column(String(255))
    profile_pic = Column(Text)
    description = Column(Text)  # AI-generated profile summary
    predicted_intent = Column(Text)  # AI prediction of what they want
    engagement_score = Column(Float, default=0.0)
    last_interaction_at = Column(DateTime)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    extra_data = Column(Text)  # JSON: additional data
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # AI Analysis fields
    sentiment = Column(String(50))  # positive, neutral, negative
    urgency = Column(String(50))  # low, medium, high
    follow_up_needed = Column(Boolean, default=False)
    follow_up_reason = Column(Text)
    key_topics = Column(Text)  # JSON array of topics

    # Analysis queue fields
    analysis_status = Column(String(20))  # pending, analyzing, completed, failed, cancelled
    analysis_queued_at = Column(DateTime)  # When queued for analysis

    # Follow-up tracking fields
    followup_status = Column(String(20))  # pending, sent, responded, dismissed
    followup_sent_at = Column(DateTime)
    followup_message = Column(Text)  # Last sent follow-up message
    followup_attempts = Column(Integer, default=0)
    followup_last_attempt_at = Column(DateTime)

    # Relationships
    hub = relationship("Hub", back_populates="contacts")
    tags = relationship("ContactTag", back_populates="contact", cascade="all, delete-orphan")


class ContactTag(Base):
    """Contact tags - Labels/tags for contacts."""
    __tablename__ = "contact_tags"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    tag = Column(String(100), nullable=False, index=True)
    value = Column(Text)  # Optional value (e.g., "interest_level": "high")
    confidence = Column(Float, default=1.0)
    source = Column(String(50), default="manual")  # 'manual', 'ai_analyzer', 'rule'
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    contact = relationship("Contact", back_populates="tags")


class ScheduledContent(Base):
    """Scheduled content - Generated content queue for proactive outreach."""
    __tablename__ = "scheduled_contents"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False, index=True)
    bot_profile_id = Column(Integer, ForeignKey("bot_profiles.id"), nullable=True)  # Legacy: single bot
    # Multi-bot support
    bot_profile_ids = Column(Text, nullable=True)  # JSON array of bot IDs: [1, 2, 3]
    bot_send_mode = Column(String(20), default="any")  # 'any', 'selected', 'all'
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)  # NULL = broadcast
    content = Column(Text, nullable=False)
    content_type = Column(String(50), default="message")  # 'message', 'followup', 'promo'
    topic = Column(String(255))
    scheduled_for = Column(DateTime, index=True)
    sent_at = Column(DateTime)
    status = Column(String(50), default="pending", index=True)  # 'pending', 'assigned', 'sent', 'failed', 'cancelled'
    created_at = Column(DateTime, default=datetime.utcnow)

    # Schedule type: 'immediate', 'exact', 'relative', 'recurring'
    schedule_type = Column(String(20), default="immediate")
    # Recurring schedule fields
    recurring_frequency = Column(String(20), nullable=True)  # 'daily', 'weekly', 'monthly'
    recurring_time = Column(String(10), nullable=True)  # '09:00' format
    recurring_start_date = Column(DateTime, nullable=True)
    recurring_end_date = Column(DateTime, nullable=True)

    # Recipient type: 'broadcast' (all contacts), 'all_groups', 'contacts' (selected), 'groups' (selected)
    recipient_type = Column(String(20), default="broadcast")
    # WhatsApp group ID (e.g., "123456789@g.us") - for single group (legacy)
    group_id = Column(String(100), nullable=True)
    # Group display name for UI (legacy)
    group_name = Column(String(255), nullable=True)
    # JSON array of contact IDs for multi-select: [1, 2, 3]
    contact_ids = Column(Text, nullable=True)
    # JSON array of group objects for multi-select: [{"id": "123@g.us", "name": "Group 1"}, ...]
    group_ids = Column(Text, nullable=True)

    # Rate limiting settings to avoid WhatsApp spam detection
    sending_speed_mode = Column(String(20), default="auto")  # 'auto', 'custom', 'fast'
    delay_min = Column(Integer, default=5)  # Minimum seconds between messages
    delay_max = Column(Integer, default=15)  # Maximum seconds between messages
    batch_size = Column(Integer, default=20)  # Messages per batch before pause
    batch_pause = Column(Integer, default=180)  # Seconds to pause between batches

    # Relationships
    hub = relationship("Hub", back_populates="scheduled_contents")
    bot_profile = relationship("BotProfile", backref="scheduled_contents")
    contact = relationship("Contact", backref="scheduled_contents")


class AgentExecution(Base):
    """Agent execution log - Audit trail for AI agent runs."""
    __tablename__ = "agent_executions"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("ai_agents.id", ondelete="CASCADE"), nullable=False)
    trigger_type = Column(String(50))  # 'message', 'schedule', 'manual'
    input_data = Column(Text)  # JSON: what was analyzed
    output_data = Column(Text)  # JSON: result/decision
    tokens_used = Column(Integer)
    execution_time_ms = Column(Integer)
    status = Column(String(50), default="success")  # 'success', 'error'
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    agent = relationship("AIAgent", back_populates="executions")


class MessageRouting(Base):
    """Message routing - Tracks routing decisions for messages."""
    __tablename__ = "message_routings"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    classification = Column(Text)  # JSON: classifier result
    assigned_bots = Column(Text)  # JSON: [bot_id, bot_id]
    routing_reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    hub = relationship("Hub", backref="message_routings")
    message = relationship("Message", backref="routings")
    contact = relationship("Contact", backref="message_routings")


# ============== Scripted Conversations Models ==============

class ConversationScript(Base):
    """Conversation Script - Pre-planned scripted conversations for groups."""
    __tablename__ = "conversation_scripts"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)

    # Target groups (JSON array like scheduled content)
    group_ids = Column(Text)  # [{"id": "xxx@g.us", "name": "Group 1"}, ...]

    # Schedule settings (same pattern as scheduled content)
    schedule_type = Column(String(50), default="immediate")  # immediate, exact, relative, recurring
    scheduled_for = Column(DateTime, nullable=True)
    recurring_frequency = Column(String(50))  # daily, weekly
    recurring_time = Column(String(10))  # "09:00"
    recurring_days = Column(Text)  # JSON: ["mon", "wed", "fri"]
    recurring_start_date = Column(DateTime)
    recurring_end_date = Column(DateTime)

    status = Column(String(50), default="draft", index=True)  # draft, scheduled, running, completed, cancelled
    last_run_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Rate limiting settings for multi-group execution
    sending_speed_mode = Column(String(20), default="auto")  # 'auto', 'custom', 'fast'
    stagger_delay_min = Column(Integer, default=None)  # Custom min seconds between group starts
    stagger_delay_max = Column(Integer, default=None)  # Custom max seconds between group starts

    # Relationships
    hub = relationship("Hub", backref="conversation_scripts")
    messages = relationship("ScriptMessage", back_populates="script", cascade="all, delete-orphan", order_by="ScriptMessage.sequence_order")
    executions = relationship("ScriptExecution", back_populates="script", cascade="all, delete-orphan")


class ScriptMessage(Base):
    """Script Message - Individual messages in a conversation script."""
    __tablename__ = "script_messages"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("conversation_scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    bot_profile_id = Column(Integer, ForeignKey("bot_profiles.id"), nullable=False)

    # Time settings (both options)
    time_type = Column(String(20), default="relative")  # "absolute" or "relative"
    absolute_time = Column(String(10))  # "13:01" for absolute time
    delay_seconds = Column(Integer, default=0)  # seconds after script start for relative

    content = Column(Text, nullable=False)
    sequence_order = Column(Integer, default=0)  # for ordering messages

    status = Column(String(50), default="pending")  # pending, sent, failed, skipped
    sent_at = Column(DateTime)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    script = relationship("ConversationScript", back_populates="messages")
    bot_profile = relationship("BotProfile", backref="script_messages")


class ScriptExecution(Base):
    """Script Execution - Track each run of a script (especially for recurring)."""
    __tablename__ = "script_executions"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("conversation_scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    status = Column(String(50), default="running")  # running, completed, failed, cancelled
    messages_sent = Column(Integer, default=0)
    messages_failed = Column(Integer, default=0)
    error_message = Column(Text)

    # Relationships
    script = relationship("ConversationScript", back_populates="executions")


class ToolExecution(Base):
    """Tool Execution - Universal monitoring table for all tool operations."""
    __tablename__ = "tool_executions"

    id = Column(Integer, primary_key=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="CASCADE"), nullable=True)
    tool_type = Column(String(50), nullable=False)  # 'group_management', 'content_generator', 'contact_analyzer', 'scheduled_content', 'message_routing'
    operation = Column(String(100), nullable=False)  # Specific operation performed
    input_data = Column(Text)  # JSON: input parameters
    output_data = Column(Text)  # JSON: results
    status = Column(String(20), default="success")  # 'pending', 'running', 'success', 'error'
    error_message = Column(Text)
    execution_time_ms = Column(Integer)
    tokens_used = Column(Integer)  # For AI operations
    triggered_by = Column(String(50))  # 'user', 'scheduled', 'api', 'agent'
    related_entity_type = Column(String(50))  # 'contact', 'content', 'bot', 'agent'
    related_entity_id = Column(Integer)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    hub = relationship("Hub", backref="tool_executions")
    user = relationship("User", backref="tool_executions")


# ============== Claude Code Models ==============

class ClaudeCodeSession(Base):
    """Claude Code CLI session - tracks each spawned CLI subprocess."""
    __tablename__ = "claude_code_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), default="pending", index=True)  # pending, running, completed, failed, stopped
    prompt = Column(Text, nullable=False)
    git_commit_hash = Column(String(40))  # Safety commit before session
    db_backup_path = Column(String(500))  # Backup file path
    pid = Column(Integer)  # OS process ID
    model = Column(String(100))
    rolled_back = Column(Boolean, default=False)
    rolled_back_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)

    # Relationships
    user = relationship("User", backref="claude_code_sessions")
    messages = relationship("ClaudeCodeMessage", back_populates="session", cascade="all, delete-orphan", order_by="ClaudeCodeMessage.created_at")


class ClaudeCodeMessage(Base):
    """Claude Code message - individual stream events from CLI."""
    __tablename__ = "claude_code_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("claude_code_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # user, assistant, system, tool_use, tool_result
    content = Column(Text)
    message_type = Column(String(50))  # text, tool_use, tool_result, error, system
    event_data = Column(Text)  # JSON of raw stream event
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    session = relationship("ClaudeCodeSession", back_populates="messages")


class ClaudeCodeSettings(Base):
    """Claude Code settings - per-user configuration."""
    __tablename__ = "claude_code_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    anthropic_api_key_encrypted = Column(Text)  # Fernet-encrypted
    default_model = Column(String(100), default="sonnet")
    auto_commit = Column(Boolean, default=True)
    auto_backup_db = Column(Boolean, default=True)
    max_session_minutes = Column(Integer, default=30)
    auth_method = Column(String(20), default="api_key")  # "api_key" or "membership"
    oauth_token_encrypted = Column(Text)  # Fernet-encrypted setup-token for membership
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="claude_code_settings")


# ============== ChatHub Agent Models ==============

class AIUsage(Base):
    """AI Usage - tracks token usage and cost for every AI API call."""
    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bot_id = Column(Integer, ForeignKey("bot_profiles.id", ondelete="SET NULL"), nullable=True, index=True)
    hub_id = Column(Integer, ForeignKey("hubs.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_id = Column(Integer, ForeignKey("ai_agents.id", ondelete="SET NULL"), nullable=True)
    provider = Column(String(50), nullable=False)  # 'openai', 'anthropic', 'google', etc.
    model = Column(String(100), nullable=False)  # 'gpt-4o-mini', 'claude-3-5-sonnet', etc.
    operation = Column(String(100))  # 'chat', 'image_analysis', 'classification', 'routing', 'content_generation', etc.
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)  # Estimated cost in USD
    source = Column(String(50))  # 'bot_chat', 'hub_routing', 'tool', 'agent', 'scheduler'
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    user = relationship("User", backref="ai_usage_records")
    bot = relationship("BotProfile", backref="ai_usage_records")
    hub = relationship("Hub", backref="ai_usage_records")
    agent = relationship("AIAgent", backref="ai_usage_records")


class ChatHubAgentSettings(Base):
    """ChatHub Agent settings - per-user configuration."""
    __tablename__ = "chathub_agent_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    ai_provider = Column(String(50), default="openai")
    api_key_encrypted = Column(Text)
    default_model = Column(String(100), default="gpt-4o")
    default_agent_id = Column(Integer, ForeignKey("chathub_agent_configs.id", ondelete="SET NULL"), nullable=True)
    auto_commit = Column(Boolean, default=True)
    auto_backup_db = Column(Boolean, default=True)
    max_session_minutes = Column(Integer, default=60)
    queue_mode = Column(String(20), default="collect")
    workspace_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="chathub_agent_settings")
    default_agent = relationship("ChatHubAgentConfig", foreign_keys=[default_agent_id])


class ChatHubAgentConfig(Base):
    """ChatHub Agent config - reusable agent configurations."""
    __tablename__ = "chathub_agent_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    ai_provider = Column(String(50), default="openai")
    api_key_encrypted = Column(Text)
    model = Column(String(100), default="gpt-4o")
    system_prompt = Column(Text)
    temperature = Column(Float, default=0.3)
    max_tokens = Column(Integer, default=8192)
    allowed_tools = Column(Text)  # JSON list
    dangerous_tools = Column(Text, default='["exec_command"]')  # JSON list
    auto_approve_read = Column(Boolean, default=True)
    workspace_path = Column(String(500))
    enabled_skills = Column(Text)  # JSON list
    routing_rules = Column(Text)  # JSON object
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="chathub_agent_configs")
    sessions = relationship("ChatHubAgentSession", back_populates="agent_config")


class ChatHubAgentSession(Base):
    """ChatHub Agent session - tracks each agent execution."""
    __tablename__ = "chathub_agent_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_config_id = Column(Integer, ForeignKey("chathub_agent_configs.id", ondelete="SET NULL"), nullable=True)
    parent_session_id = Column(Integer, ForeignKey("chathub_agent_sessions.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), default="pending", index=True)  # pending, running, completed, failed, stopped
    prompt = Column(Text, nullable=False)
    git_commit_hash = Column(String(40))
    db_backup_path = Column(String(500))
    ai_provider = Column(String(50))
    model = Column(String(100))
    total_turns = Column(Integer, default=0)
    total_tool_calls = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    rolled_back = Column(Boolean, default=False)
    rolled_back_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)

    # Relationships
    user = relationship("User", backref="chathub_agent_sessions")
    agent_config = relationship("ChatHubAgentConfig", back_populates="sessions")
    parent_session = relationship("ChatHubAgentSession", remote_side="ChatHubAgentSession.id", backref="child_sessions")
    messages = relationship("ChatHubAgentMessage", back_populates="session", cascade="all, delete-orphan", order_by="ChatHubAgentMessage.created_at")


class ChatHubAgentMessage(Base):
    """ChatHub Agent message - individual events from agent execution."""
    __tablename__ = "chathub_agent_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chathub_agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # user, assistant, system, tool_call, tool_result, approval_request
    content = Column(Text)
    message_type = Column(String(50))  # text, tool_use, tool_result, error, system
    tool_name = Column(String(100))
    tool_call_id = Column(String(100))
    event_data = Column(Text)  # JSON of raw event data
    tokens_used = Column(Integer, default=0)
    execution_time_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    session = relationship("ChatHubAgentSession", back_populates="messages")


class ChatHubAgentSkill(Base):
    """ChatHub Agent skill - managed or workspace skill definitions."""
    __tablename__ = "chathub_agent_skills"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    tier = Column(String(20), nullable=False)  # managed, workspace
    path = Column(String(500))
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    metadata_json = Column(Text)  # JSON metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="chathub_agent_skills")


# ============== Database Functions ==============

def create_tables():
    """Create all database tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session (dependency injection)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_session():
    """Context manager for database session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# Create tables on import
create_tables()


# ============== Database Migrations ==============

def run_migrations():
    """Run database migrations to add new columns to existing tables."""
    from sqlalchemy import text, inspect

    with engine.connect() as conn:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        # Create tool_executions table if not exists
        if 'tool_executions' not in existing_tables:
            try:
                conn.execute(text('''
                    CREATE TABLE tool_executions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        hub_id INTEGER REFERENCES hubs(id) ON DELETE CASCADE,
                        tool_type VARCHAR(50) NOT NULL,
                        operation VARCHAR(100) NOT NULL,
                        input_data TEXT,
                        output_data TEXT,
                        status VARCHAR(20) DEFAULT 'success',
                        error_message TEXT,
                        execution_time_ms INTEGER,
                        tokens_used INTEGER,
                        triggered_by VARCHAR(50),
                        related_entity_type VARCHAR(50),
                        related_entity_id INTEGER,
                        user_id INTEGER REFERENCES users(id),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                '''))
                conn.execute(text('CREATE INDEX ix_tool_executions_created_at ON tool_executions(created_at)'))
                conn.execute(text('CREATE INDEX ix_tool_executions_tool_type ON tool_executions(tool_type)'))
                conn.execute(text('CREATE INDEX ix_tool_executions_hub_id ON tool_executions(hub_id)'))
                conn.commit()
                print("Created tool_executions table")
            except Exception as e:
                print(f"Could not create tool_executions table: {e}")

        # Check if messages table exists
        if 'messages' in existing_tables:
            # Get existing columns
            existing_columns = [col['name'] for col in inspector.get_columns('messages')]

            # Add file_url column if not exists
            if 'file_url' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE messages ADD COLUMN file_url TEXT'))
                    conn.commit()
                    print("Added file_url column to messages table")
                except Exception as e:
                    print(f"Could not add file_url column: {e}")

            # Add file_name column if not exists
            if 'file_name' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE messages ADD COLUMN file_name VARCHAR(500)'))
                    conn.commit()
                    print("Added file_name column to messages table")
                except Exception as e:
                    print(f"Could not add file_name column: {e}")

            # Add file_type column if not exists
            if 'file_type' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE messages ADD COLUMN file_type VARCHAR(100)'))
                    conn.commit()
                    print("Added file_type column to messages table")
                except Exception as e:
                    print(f"Could not add file_type column: {e}")

            # Add file_size column if not exists
            if 'file_size' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE messages ADD COLUMN file_size INTEGER'))
                    conn.commit()
                    print("Added file_size column to messages table")
                except Exception as e:
                    print(f"Could not add file_size column: {e}")

            # Add file_pages column if not exists
            if 'file_pages' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE messages ADD COLUMN file_pages INTEGER'))
                    conn.commit()
                    print("Added file_pages column to messages table")
                except Exception as e:
                    print(f"Could not add file_pages column: {e}")

        # Check if bot_profiles table exists
        if 'bot_profiles' in existing_tables:
            # Get existing columns
            existing_columns = [col['name'] for col in inspector.get_columns('bot_profiles')]

            # Add whatsapp_timezone_offset column if not exists
            if 'whatsapp_timezone_offset' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE bot_profiles ADD COLUMN whatsapp_timezone_offset INTEGER'))
                    conn.commit()
                    print("Added whatsapp_timezone_offset column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add whatsapp_timezone_offset column: {e}")

            # Add browser_timezone column if not exists
            if 'browser_timezone' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN browser_timezone VARCHAR(100) DEFAULT 'UTC'"))
                    conn.commit()
                    print("Added browser_timezone column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add browser_timezone column: {e}")

            # Add top_p column if not exists
            if 'top_p' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE bot_profiles ADD COLUMN top_p FLOAT DEFAULT 1.0'))
                    conn.commit()
                    print("Added top_p column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add top_p column: {e}")

            # Add frequency_penalty column if not exists
            if 'frequency_penalty' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE bot_profiles ADD COLUMN frequency_penalty FLOAT DEFAULT 0.0'))
                    conn.commit()
                    print("Added frequency_penalty column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add frequency_penalty column: {e}")

            # Add presence_penalty column if not exists
            if 'presence_penalty' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE bot_profiles ADD COLUMN presence_penalty FLOAT DEFAULT 0.0'))
                    conn.commit()
                    print("Added presence_penalty column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add presence_penalty column: {e}")

            # Add platform_config column if not exists
            if 'platform_config' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN platform_config TEXT DEFAULT '{}'"))
                    conn.commit()
                    print("Added platform_config column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add platform_config column: {e}")

            # Add platform_type column if not exists
            if 'platform_type' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN platform_type VARCHAR(50) DEFAULT 'whatsapp'"))
                    conn.commit()
                    print("Added platform_type column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add platform_type column: {e}")

            # Add ending_detection_enabled column if not exists
            if 'ending_detection_enabled' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE bot_profiles ADD COLUMN ending_detection_enabled BOOLEAN DEFAULT 0'))
                    conn.commit()
                    print("Added ending_detection_enabled column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add ending_detection_enabled column: {e}")

        # Check if scheduled_contents table exists
        if 'scheduled_contents' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('scheduled_contents')]

            # Add recipient_type column if not exists
            if 'recipient_type' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE scheduled_contents ADD COLUMN recipient_type VARCHAR(20) DEFAULT 'contact'"))
                    conn.commit()
                    print("Added recipient_type column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add recipient_type column: {e}")

            # Add group_id column if not exists
            if 'group_id' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE scheduled_contents ADD COLUMN group_id VARCHAR(100)'))
                    conn.commit()
                    print("Added group_id column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add group_id column: {e}")

            # Add group_name column if not exists
            if 'group_name' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE scheduled_contents ADD COLUMN group_name VARCHAR(255)'))
                    conn.commit()
                    print("Added group_name column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add group_name column: {e}")

            # Add contact_ids column if not exists (JSON array for multi-select)
            if 'contact_ids' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE scheduled_contents ADD COLUMN contact_ids TEXT'))
                    conn.commit()
                    print("Added contact_ids column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add contact_ids column: {e}")

            # Add group_ids column if not exists (JSON array for multi-select)
            if 'group_ids' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE scheduled_contents ADD COLUMN group_ids TEXT'))
                    conn.commit()
                    print("Added group_ids column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add group_ids column: {e}")

            # Add bot_profile_ids column if not exists (JSON array for multi-bot)
            if 'bot_profile_ids' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE scheduled_contents ADD COLUMN bot_profile_ids TEXT'))
                    conn.commit()
                    print("Added bot_profile_ids column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add bot_profile_ids column: {e}")

            # Add bot_send_mode column if not exists ('any', 'all', 'selected')
            if 'bot_send_mode' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE scheduled_contents ADD COLUMN bot_send_mode VARCHAR(20) DEFAULT 'any'"))
                    conn.commit()
                    print("Added bot_send_mode column to scheduled_contents table")
                except Exception as e:
                    print(f"Could not add bot_send_mode column: {e}")

        # Check if ai_agents table exists and add new columns
        if 'ai_agents' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('ai_agents')]

            # Add is_global column if not exists
            if 'is_global' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN is_global BOOLEAN DEFAULT 0'))
                    conn.commit()
                    print("Added is_global column to ai_agents table")
                except Exception as e:
                    print(f"Could not add is_global column: {e}")

            # Add template_id column if not exists
            if 'template_id' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN template_id INTEGER'))
                    conn.commit()
                    print("Added template_id column to ai_agents table")
                except Exception as e:
                    print(f"Could not add template_id column: {e}")

            # Add status column if not exists
            if 'status' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE ai_agents ADD COLUMN status VARCHAR(20) DEFAULT 'idle'"))
                    conn.commit()
                    print("Added status column to ai_agents table")
                except Exception as e:
                    print(f"Could not add status column: {e}")

            # Add last_error column if not exists
            if 'last_error' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN last_error TEXT'))
                    conn.commit()
                    print("Added last_error column to ai_agents table")
                except Exception as e:
                    print(f"Could not add last_error column: {e}")

            # Add total_executions column if not exists
            if 'total_executions' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN total_executions INTEGER DEFAULT 0'))
                    conn.commit()
                    print("Added total_executions column to ai_agents table")
                except Exception as e:
                    print(f"Could not add total_executions column: {e}")

            # Add successful_executions column if not exists
            if 'successful_executions' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN successful_executions INTEGER DEFAULT 0'))
                    conn.commit()
                    print("Added successful_executions column to ai_agents table")
                except Exception as e:
                    print(f"Could not add successful_executions column: {e}")

            # Add total_tokens_used column if not exists
            if 'total_tokens_used' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN total_tokens_used INTEGER DEFAULT 0'))
                    conn.commit()
                    print("Added total_tokens_used column to ai_agents table")
                except Exception as e:
                    print(f"Could not add total_tokens_used column: {e}")

            # Add additional_instructions column if not exists
            if 'additional_instructions' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE ai_agents ADD COLUMN additional_instructions TEXT'))
                    conn.commit()
                    print("Added additional_instructions column to ai_agents table")
                except Exception as e:
                    print(f"Could not add additional_instructions column: {e}")

            # Add ai_provider column if not exists
            if 'ai_provider' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE ai_agents ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'openai'"))
                    conn.commit()
                    print("Added ai_provider column to ai_agents table")
                except Exception as e:
                    print(f"Could not add ai_provider column: {e}")

        # Check if hubs table exists and add new columns
        if 'hubs' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('hubs')]

            # Add task_type column if not exists
            if 'task_type' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE hubs ADD COLUMN task_type VARCHAR(50) DEFAULT 'group_management'"))
                    conn.commit()
                    print("Added task_type column to hubs table")
                except Exception as e:
                    print(f"Could not add task_type column: {e}")

            # Add selected_groups column if not exists
            if 'selected_groups' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE hubs ADD COLUMN selected_groups TEXT"))
                    conn.commit()
                    print("Added selected_groups column to hubs table")
                except Exception as e:
                    print(f"Could not add selected_groups column: {e}")

            # Add ai_provider column if not exists
            if 'ai_provider' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE hubs ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'openai'"))
                    conn.commit()
                    print("Added ai_provider column to hubs table")
                except Exception as e:
                    print(f"Could not add ai_provider column: {e}")

        # Add ai_provider to bot_profiles if needed
        if 'bot_profiles' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('bot_profiles')]

            if 'ai_provider' not in existing_columns:
                try:
                    conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'openai'"))
                    conn.commit()
                    print("Added ai_provider column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add ai_provider column to bot_profiles: {e}")

        # ============== Rename openai_* columns to generic names ==============
        # These fields are used for ALL AI providers, not just OpenAI
        tables_to_migrate = ['bot_profiles', 'hubs', 'ai_agents']
        for table in tables_to_migrate:
            if table in existing_tables:
                columns = [col['name'] for col in inspector.get_columns(table)]

                # Rename openai_api_key_encrypted -> api_key_encrypted
                if 'openai_api_key_encrypted' in columns and 'api_key_encrypted' not in columns:
                    try:
                        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN openai_api_key_encrypted TO api_key_encrypted"))
                        conn.commit()
                        print(f"Renamed openai_api_key_encrypted to api_key_encrypted in {table}")
                    except Exception as e:
                        print(f"Could not rename openai_api_key_encrypted in {table}: {e}")

                # Rename openai_model -> model
                if 'openai_model' in columns and 'model' not in columns:
                    try:
                        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN openai_model TO model"))
                        conn.commit()
                        print(f"Renamed openai_model to model in {table}")
                    except Exception as e:
                        print(f"Could not rename openai_model in {table}: {e}")

        # ============== Create Scripted Conversations tables ==============

        # Create conversation_scripts table if not exists
        if 'conversation_scripts' not in existing_tables:
            try:
                conn.execute(text('''
                    CREATE TABLE conversation_scripts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        hub_id INTEGER NOT NULL REFERENCES hubs(id) ON DELETE CASCADE,
                        name VARCHAR(255) NOT NULL,
                        description TEXT,
                        group_ids TEXT,
                        schedule_type VARCHAR(50) DEFAULT 'immediate',
                        scheduled_for DATETIME,
                        recurring_frequency VARCHAR(50),
                        recurring_time VARCHAR(10),
                        recurring_days TEXT,
                        recurring_start_date DATETIME,
                        recurring_end_date DATETIME,
                        status VARCHAR(50) DEFAULT 'draft',
                        last_run_at DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                '''))
                conn.execute(text('CREATE INDEX ix_conversation_scripts_hub_id ON conversation_scripts(hub_id)'))
                conn.execute(text('CREATE INDEX ix_conversation_scripts_status ON conversation_scripts(status)'))
                conn.commit()
                print("Created conversation_scripts table")
            except Exception as e:
                print(f"Could not create conversation_scripts table: {e}")

        # Create script_messages table if not exists
        if 'script_messages' not in existing_tables:
            try:
                conn.execute(text('''
                    CREATE TABLE script_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        script_id INTEGER NOT NULL REFERENCES conversation_scripts(id) ON DELETE CASCADE,
                        bot_profile_id INTEGER NOT NULL REFERENCES bot_profiles(id),
                        time_type VARCHAR(20) DEFAULT 'relative',
                        absolute_time VARCHAR(10),
                        delay_seconds INTEGER DEFAULT 0,
                        content TEXT NOT NULL,
                        sequence_order INTEGER DEFAULT 0,
                        status VARCHAR(50) DEFAULT 'pending',
                        sent_at DATETIME,
                        error_message TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                '''))
                conn.execute(text('CREATE INDEX ix_script_messages_script_id ON script_messages(script_id)'))
                conn.commit()
                print("Created script_messages table")
            except Exception as e:
                print(f"Could not create script_messages table: {e}")

        # Create script_executions table if not exists
        if 'script_executions' not in existing_tables:
            try:
                conn.execute(text('''
                    CREATE TABLE script_executions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        script_id INTEGER NOT NULL REFERENCES conversation_scripts(id) ON DELETE CASCADE,
                        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        completed_at DATETIME,
                        status VARCHAR(50) DEFAULT 'running',
                        messages_sent INTEGER DEFAULT 0,
                        messages_failed INTEGER DEFAULT 0,
                        error_message TEXT
                    )
                '''))
                conn.execute(text('CREATE INDEX ix_script_executions_script_id ON script_executions(script_id)'))
                conn.commit()
                print("Created script_executions table")
            except Exception as e:
                print(f"Could not create script_executions table: {e}")

        # ============== Create ai_usage table ==============
        if 'ai_usage' not in existing_tables:
            try:
                conn.execute(text('''
                    CREATE TABLE ai_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        bot_id INTEGER REFERENCES bot_profiles(id) ON DELETE SET NULL,
                        hub_id INTEGER REFERENCES hubs(id) ON DELETE SET NULL,
                        agent_id INTEGER REFERENCES ai_agents(id) ON DELETE SET NULL,
                        provider VARCHAR(50) NOT NULL,
                        model VARCHAR(100) NOT NULL,
                        operation VARCHAR(100),
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        cost_usd REAL DEFAULT 0.0,
                        source VARCHAR(50),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                '''))
                conn.execute(text('CREATE INDEX ix_ai_usage_user_id ON ai_usage(user_id)'))
                conn.execute(text('CREATE INDEX ix_ai_usage_bot_id ON ai_usage(bot_id)'))
                conn.execute(text('CREATE INDEX ix_ai_usage_hub_id ON ai_usage(hub_id)'))
                conn.execute(text('CREATE INDEX ix_ai_usage_created_at ON ai_usage(created_at)'))
                conn.execute(text('CREATE INDEX ix_ai_usage_provider ON ai_usage(provider)'))
                conn.commit()
                print("Created ai_usage table")
            except Exception as e:
                print(f"Could not create ai_usage table: {e}")

        # ============== Add AI analysis fields to contacts table ==============
        if 'contacts' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('contacts')]

            # Add sentiment column if not exists
            if 'sentiment' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN sentiment VARCHAR(50)'))
                    conn.commit()
                    print("Added sentiment column to contacts table")
                except Exception as e:
                    print(f"Could not add sentiment column: {e}")

            # Add urgency column if not exists
            if 'urgency' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN urgency VARCHAR(50)'))
                    conn.commit()
                    print("Added urgency column to contacts table")
                except Exception as e:
                    print(f"Could not add urgency column: {e}")

            # Add follow_up_needed column if not exists
            if 'follow_up_needed' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN follow_up_needed BOOLEAN DEFAULT 0'))
                    conn.commit()
                    print("Added follow_up_needed column to contacts table")
                except Exception as e:
                    print(f"Could not add follow_up_needed column: {e}")

            # Add follow_up_reason column if not exists
            if 'follow_up_reason' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN follow_up_reason TEXT'))
                    conn.commit()
                    print("Added follow_up_reason column to contacts table")
                except Exception as e:
                    print(f"Could not add follow_up_reason column: {e}")

            # Add key_topics column if not exists
            if 'key_topics' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN key_topics TEXT'))
                    conn.commit()
                    print("Added key_topics column to contacts table")
                except Exception as e:
                    print(f"Could not add key_topics column: {e}")

            # ============== Add follow-up tracking fields to contacts table ==============
            # Add followup_status column if not exists
            if 'followup_status' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN followup_status VARCHAR(20)'))
                    conn.commit()
                    print("Added followup_status column to contacts table")
                except Exception as e:
                    print(f"Could not add followup_status column: {e}")

            # Add followup_sent_at column if not exists
            if 'followup_sent_at' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN followup_sent_at DATETIME'))
                    conn.commit()
                    print("Added followup_sent_at column to contacts table")
                except Exception as e:
                    print(f"Could not add followup_sent_at column: {e}")

            # Add followup_message column if not exists
            if 'followup_message' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN followup_message TEXT'))
                    conn.commit()
                    print("Added followup_message column to contacts table")
                except Exception as e:
                    print(f"Could not add followup_message column: {e}")

            # Add followup_attempts column if not exists
            if 'followup_attempts' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN followup_attempts INTEGER DEFAULT 0'))
                    conn.commit()
                    print("Added followup_attempts column to contacts table")
                except Exception as e:
                    print(f"Could not add followup_attempts column: {e}")

            # Add followup_last_attempt_at column if not exists
            if 'followup_last_attempt_at' not in existing_columns:
                try:
                    conn.execute(text('ALTER TABLE contacts ADD COLUMN followup_last_attempt_at DATETIME'))
                    conn.commit()
                    print("Added followup_last_attempt_at column to contacts table")
                except Exception as e:
                    print(f"Could not add followup_last_attempt_at column: {e}")


# Run migrations on import
run_migrations()
