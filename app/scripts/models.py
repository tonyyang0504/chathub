"""
Scripted Conversations Pydantic Models (Schemas)
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


# ============== Group Selection ==============

class SelectedGroup(BaseModel):
    """A selected group for the script."""
    id: str  # Group ID (e.g., "123456789@g.us")
    name: str  # Group display name


# ============== Script Message Schemas ==============

class ScriptMessageCreate(BaseModel):
    """Create a new script message."""
    bot_profile_id: Optional[int] = None  # If not set, uses first hub bot
    time_type: str = "relative"  # "absolute" or "relative"
    absolute_time: Optional[str] = None  # "13:01" for absolute time
    delay_seconds: Optional[int] = 0  # seconds after script start for relative
    content: str = Field(..., min_length=1)
    sequence_order: Optional[int] = None  # auto-assigned if not provided


class ScriptMessageUpdate(BaseModel):
    """Update a script message."""
    bot_profile_id: Optional[int] = None
    time_type: Optional[str] = None
    absolute_time: Optional[str] = None
    delay_seconds: Optional[int] = None
    content: Optional[str] = None
    sequence_order: Optional[int] = None


class ScriptMessageResponse(BaseModel):
    """Script message response."""
    id: int
    script_id: int
    bot_profile_id: Optional[int] = None
    bot_name: Optional[str] = None  # Bot display name
    time_type: str
    absolute_time: Optional[str] = None
    delay_seconds: int
    content: str
    sequence_order: int
    status: str
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime


class ScriptMessageReorder(BaseModel):
    """Reorder script messages."""
    message_ids: List[int]  # List of message IDs in new order


# ============== Script Schemas ==============

class ScriptCreate(BaseModel):
    """Create a new conversation script."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    group_ids: Optional[List[SelectedGroup]] = None  # Target groups

    # Schedule settings
    schedule_type: str = "immediate"  # immediate, exact, relative, recurring
    scheduled_for: Optional[datetime] = None  # For exact scheduling

    # Recurring settings
    recurring_frequency: Optional[str] = None  # daily, weekly
    recurring_time: Optional[str] = None  # "09:00"
    recurring_days: Optional[List[str]] = None  # ["mon", "wed", "fri"]
    recurring_start_date: Optional[datetime] = None
    recurring_end_date: Optional[datetime] = None

    # Rate limiting settings for multi-group execution
    sending_speed_mode: str = "auto"  # 'auto', 'custom', 'fast'
    stagger_delay_min: Optional[int] = None  # Custom min seconds between group starts
    stagger_delay_max: Optional[int] = None  # Custom max seconds between group starts

    # Optional: include messages on create
    messages: Optional[List[ScriptMessageCreate]] = None


class ScriptUpdate(BaseModel):
    """Update a conversation script."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    group_ids: Optional[List[SelectedGroup]] = None

    # Schedule settings
    schedule_type: Optional[str] = None
    scheduled_for: Optional[datetime] = None

    # Recurring settings
    recurring_frequency: Optional[str] = None
    recurring_time: Optional[str] = None
    recurring_days: Optional[List[str]] = None
    recurring_start_date: Optional[datetime] = None
    recurring_end_date: Optional[datetime] = None

    # Rate limiting settings for multi-group execution
    sending_speed_mode: Optional[str] = None
    stagger_delay_min: Optional[int] = None
    stagger_delay_max: Optional[int] = None

    # Status
    status: Optional[str] = None


class ScriptResponse(BaseModel):
    """Script response."""
    id: int
    hub_id: int
    name: str
    description: Optional[str] = None
    group_ids: Optional[List[SelectedGroup]] = None
    group_summary: Optional[str] = None  # Human-readable summary (e.g., "3 groups")

    # Schedule settings
    schedule_type: str
    scheduled_for: Optional[datetime] = None

    # Recurring settings
    recurring_frequency: Optional[str] = None
    recurring_time: Optional[str] = None
    recurring_days: Optional[List[str]] = None
    recurring_start_date: Optional[datetime] = None
    recurring_end_date: Optional[datetime] = None

    # Rate limiting settings for multi-group execution
    sending_speed_mode: str = "auto"
    stagger_delay_min: Optional[int] = None
    stagger_delay_max: Optional[int] = None

    # Status
    status: str
    message_count: int = 0
    last_run_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class ScriptDetailResponse(ScriptResponse):
    """Script response with messages."""
    messages: List[ScriptMessageResponse] = []


# ============== Script Execution Schemas ==============

class ScriptExecutionResponse(BaseModel):
    """Script execution response."""
    id: int
    script_id: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    messages_sent: int
    messages_failed: int
    error_message: Optional[str] = None


class ExecuteScriptRequest(BaseModel):
    """Request to execute a script."""
    schedule_type: Optional[str] = None  # Override script schedule_type
    scheduled_for: Optional[datetime] = None  # Override scheduled time


# ============== Bulk Operations ==============

class BulkMessageCreate(BaseModel):
    """Create multiple messages at once."""
    messages: List[ScriptMessageCreate]


class BulkMessageDelete(BaseModel):
    """Delete multiple messages at once."""
    message_ids: List[int]
