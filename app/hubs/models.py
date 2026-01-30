"""
Hub Pydantic Models (Schemas)
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


# ============== Working Period ==============

class WorkingPeriod(BaseModel):
    """A single working time period within a day."""
    start: str  # Format: "HH:MM" e.g., "09:00"
    end: str  # Format: "HH:MM" e.g., "17:00"


# ============== Multi-Bot Response Rule ==============

class MultiResponseRule(BaseModel):
    """Per-category multi-bot response rule."""
    category: str  # Message category (e.g., "greeting", "discussion", "support")
    max_bots: int = 1  # Max bots for this category (0 = unlimited)
    delay_min: Optional[int] = None  # Override default min delay in seconds (optional)
    delay_max: Optional[int] = None  # Override default max delay in seconds (optional)


# ============== Selected Group ==============

class SelectedGroup(BaseModel):
    """A selected WhatsApp group for the hub."""
    chat_id: str  # WhatsApp group ID (e.g., "123456789@g.us")
    name: str  # Group display name


# ============== Message Topic Schemas ==============

class MessageTopicCreate(BaseModel):
    """Create a new message topic."""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    is_system: bool = False


class MessageTopicUpdate(BaseModel):
    """Update a message topic."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None


class MessageTopicResponse(BaseModel):
    """Message topic response."""
    id: int
    hub_id: int
    name: str
    description: Optional[str] = None
    is_system: bool
    created_at: datetime


# ============== Hub Schemas ==============

class HubCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    task_type: str = "group_management"  # group_management, scheduled_content, contact_analyzer, message_routing, content_generator
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    # Selected groups for this hub
    selected_groups: Optional[List[SelectedGroup]] = None
    # Multi-bot response settings
    max_responding_bots: int = 1  # Default max bots (0 = unlimited)
    response_delay_min: int = 1  # Default min delay between responses (seconds)
    response_delay_max: int = 3  # Default max delay between responses (seconds)
    multi_response_rules: Optional[List[MultiResponseRule]] = None  # Per-category rules
    # Bot-to-bot conversation settings
    bot_conversation_limit: int = 0  # Max bot-to-bot conversations (0 = none/disabled, -1 = unlimited)
    bot_conversation_interval: str = "hour"  # Interval: minute, hour, day


class HubUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    task_type: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    is_active: Optional[bool] = None
    # Selected groups for this hub
    selected_groups: Optional[List[SelectedGroup]] = None
    # Multi-bot response settings
    max_responding_bots: Optional[int] = None
    response_delay_min: Optional[int] = None
    response_delay_max: Optional[int] = None
    multi_response_rules: Optional[List[MultiResponseRule]] = None
    # Bot-to-bot conversation settings
    bot_conversation_limit: Optional[int] = None
    bot_conversation_interval: Optional[str] = None


class HubBotInfo(BaseModel):
    id: int
    bot_profile_id: int
    bot_name: str
    role: str
    expertise: Optional[List[str]] = None
    priority: int
    can_initiate: bool
    is_active: bool
    is_running: bool = False
    # Working hours
    working_hours_start: Optional[str] = None  # DEPRECATED - use working_periods
    working_hours_end: Optional[str] = None  # DEPRECATED - use working_periods
    working_periods: Optional[List[WorkingPeriod]] = None  # Multiple time periods per day
    working_days: Optional[List[str]] = None  # ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class HubAgentInfo(BaseModel):
    id: int
    name: str
    agent_type: str
    description: Optional[str] = None
    is_active: bool
    last_run_at: Optional[datetime] = None


class HubResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    task_type: str = "group_management"
    openai_model: str
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    bot_count: int = 0
    agent_count: int = 0
    contact_count: int = 0
    content_count: int = 0
    # Selected groups for this hub
    selected_groups: Optional[List[SelectedGroup]] = None
    # Multi-bot response settings
    max_responding_bots: int = 1  # Default max bots (0 = unlimited)
    response_delay_min: int = 1  # Default min delay (seconds)
    response_delay_max: int = 3  # Default max delay (seconds)
    multi_response_rules: Optional[List[MultiResponseRule]] = None  # Per-category rules
    # Bot-to-bot conversation settings
    bot_conversation_limit: int = 0  # Max bot-to-bot conversations (0 = none/disabled, -1 = unlimited)
    bot_conversation_interval: str = "hour"  # Interval: minute, hour, day


class HubDetailResponse(HubResponse):
    bots: List[HubBotInfo] = []
    agents: List[HubAgentInfo] = []


# ============== Bot Membership Schemas ==============

class BotMembershipCreate(BaseModel):
    bot_profile_id: int
    role: str = "member"  # 'primary', 'specialist', 'backup', 'member'
    expertise: Optional[List[str]] = None
    priority: int = 0
    can_initiate: bool = True
    # Working hours
    working_hours_start: Optional[str] = None  # DEPRECATED - use working_periods
    working_hours_end: Optional[str] = None  # DEPRECATED - use working_periods
    working_periods: Optional[List[WorkingPeriod]] = None  # Multiple time periods per day
    working_days: Optional[List[str]] = None  # ["mon", "tue", ...]


class BotMembershipUpdate(BaseModel):
    role: Optional[str] = None
    expertise: Optional[List[str]] = None
    priority: Optional[int] = None
    can_initiate: Optional[bool] = None
    is_active: Optional[bool] = None
    # Working hours
    working_hours_start: Optional[str] = None  # DEPRECATED - use working_periods
    working_hours_end: Optional[str] = None  # DEPRECATED - use working_periods
    working_periods: Optional[List[WorkingPeriod]] = None  # Multiple time periods per day
    working_days: Optional[List[str]] = None


class BotMembershipResponse(BaseModel):
    id: int
    hub_id: int
    bot_profile_id: int
    bot_name: str
    role: str
    expertise: Optional[List[str]] = None
    priority: int
    can_initiate: bool
    is_active: bool
    created_at: datetime
    # Working hours
    working_hours_start: Optional[str] = None  # DEPRECATED - use working_periods
    working_hours_end: Optional[str] = None  # DEPRECATED - use working_periods
    working_periods: Optional[List[WorkingPeriod]] = None  # Multiple time periods per day
    working_days: Optional[List[str]] = None


# ============== AI Agent Schemas ==============

class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    agent_type: str  # 'classifier', 'router', 'generator', 'scheduler', 'analyzer', 'followup'
    description: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    system_prompt: Optional[str] = None  # Custom prompt for generator, analyzer, followup
    additional_instructions: Optional[str] = None  # Extra instructions for classifier, router (appended to default)
    config: Optional[dict] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    system_prompt: Optional[str] = None  # Custom prompt for generator, analyzer, followup
    additional_instructions: Optional[str] = None  # Extra instructions for classifier, router
    config: Optional[dict] = None
    is_active: Optional[bool] = None


class AgentResponse(BaseModel):
    id: int
    hub_id: int
    name: str
    agent_type: str
    description: Optional[str] = None
    openai_model: str
    is_active: bool
    last_run_at: Optional[datetime] = None
    created_at: datetime


class AgentDetailResponse(AgentResponse):
    system_prompt: Optional[str] = None  # For generator, analyzer, followup
    additional_instructions: Optional[str] = None  # For classifier, router
    config: Optional[dict] = None


# ============== Contact Schemas ==============

class ContactCreate(BaseModel):
    phone: str = Field(..., min_length=1, max_length=50)
    display_name: Optional[str] = None
    profile_pic: Optional[str] = None
    extra_data: Optional[dict] = None


class ContactUpdate(BaseModel):
    display_name: Optional[str] = None
    profile_pic: Optional[str] = None
    description: Optional[str] = None
    predicted_intent: Optional[str] = None
    engagement_score: Optional[float] = None
    extra_data: Optional[dict] = None


class ContactTagInfo(BaseModel):
    id: int
    tag: str
    value: Optional[str] = None
    confidence: float
    source: str
    created_at: datetime


class ContactResponse(BaseModel):
    id: int
    hub_id: int
    phone: str
    display_name: Optional[str] = None
    profile_pic: Optional[str] = None
    description: Optional[str] = None
    predicted_intent: Optional[str] = None
    engagement_score: float
    last_interaction_at: Optional[datetime] = None
    first_seen_at: datetime
    created_at: datetime
    tags: List[ContactTagInfo] = []


class ContactListResponse(BaseModel):
    id: int
    phone: str
    display_name: Optional[str] = None
    profile_pic: Optional[str] = None
    engagement_score: float
    last_interaction_at: Optional[datetime] = None
    tag_count: int = 0


# ============== Tag Schemas ==============

class TagCreate(BaseModel):
    tag: str = Field(..., min_length=1, max_length=100)
    value: Optional[str] = None
    confidence: float = 1.0
    source: str = "manual"


class TagUpdate(BaseModel):
    value: Optional[str] = None
    confidence: Optional[float] = None


# ============== Scheduled Content Schemas ==============

class ScheduledContentCreate(BaseModel):
    content: str
    content_type: str = "message"  # 'message', 'followup', 'promo'
    topic: Optional[str] = None
    bot_profile_id: Optional[int] = None
    contact_id: Optional[int] = None  # Legacy single contact
    scheduled_for: Optional[datetime] = None
    recipient_type: str = "broadcast"  # 'broadcast', 'all_groups', 'contacts', 'groups'
    group_id: Optional[str] = None  # Legacy single group
    group_name: Optional[str] = None  # Legacy single group name
    contact_ids: Optional[List[int]] = None  # Multi-select contacts
    group_ids: Optional[List[dict]] = None  # Multi-select groups [{"id": "...", "name": "..."}]


class ScheduledContentUpdate(BaseModel):
    content: Optional[str] = None
    content_type: Optional[str] = None
    topic: Optional[str] = None
    bot_profile_id: Optional[int] = None
    contact_id: Optional[int] = None
    scheduled_for: Optional[datetime] = None
    status: Optional[str] = None
    recipient_type: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    contact_ids: Optional[List[int]] = None
    group_ids: Optional[List[dict]] = None


class ScheduledContentResponse(BaseModel):
    id: int
    hub_id: int
    bot_profile_id: Optional[int] = None
    bot_name: Optional[str] = None
    contact_id: Optional[int] = None
    contact_name: Optional[str] = None
    content: str
    content_type: str
    topic: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    status: str
    created_at: datetime
    recipient_type: str = "broadcast"
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    contact_ids: Optional[List[int]] = None
    group_ids: Optional[List[dict]] = None
    recipient_summary: Optional[str] = None  # Human-readable summary


# ============== Agent Execution Schemas ==============

class AgentExecutionResponse(BaseModel):
    id: int
    agent_id: int
    agent_name: str
    trigger_type: Optional[str] = None
    input_data: Optional[dict] = None
    output_data: Optional[dict] = None
    tokens_used: Optional[int] = None
    execution_time_ms: Optional[int] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime


# ============== Message Routing Schemas ==============

class MessageRoutingResponse(BaseModel):
    id: int
    hub_id: int
    message_id: Optional[int] = None
    contact_id: Optional[int] = None
    classification: Optional[dict] = None
    assigned_bots: Optional[List[int]] = None
    routing_reason: Optional[str] = None
    created_at: datetime
