"""
Pydantic schemas for ChatHub Agent request/response models.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


# ============== Settings ==============

class SettingsUpdate(BaseModel):
    ai_provider: Optional[str] = None
    api_key: Optional[str] = None
    default_model: Optional[str] = None
    approval_mode: Optional[str] = None
    auto_commit: Optional[bool] = None
    auto_backup_db: Optional[bool] = None
    workspace_path: Optional[str] = None


class SettingsResponse(BaseModel):
    id: int
    user_id: int
    ai_provider: str
    default_model: str
    auto_commit: bool
    auto_backup_db: bool
    workspace_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============== Sessions ==============

class SessionCreate(BaseModel):
    prompt: str
    model: Optional[str] = None


class SessionResponse(BaseModel):
    id: int
    user_id: int
    parent_session_id: Optional[int] = None
    status: str
    prompt: str
    ai_provider: Optional[str] = None
    model: Optional[str] = None
    total_turns: int = 0
    total_tool_calls: int = 0
    total_tokens: int = 0
    rolled_back: bool = False
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: Optional[str] = None
    message_type: Optional[str] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tokens_used: int = 0
    execution_time_ms: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class SessionDetailResponse(SessionResponse):
    messages: List[MessageResponse] = []
    git_commit_hash: Optional[str] = None
    db_backup_path: Optional[str] = None
    rolled_back_at: Optional[datetime] = None


# ============== Messages ==============

class MessageCreate(BaseModel):
    content: str


class ApprovalRequest(BaseModel):
    tool_call_id: str
    approved: bool


# ============== Skills ==============

class SkillResponse(BaseModel):
    id: int
    user_id: int
    name: str
    tier: str
    path: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    metadata_json: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SkillToggle(BaseModel):
    is_active: bool


# ============== WebSocket Events ==============

class WSEvent(BaseModel):
    type: str
    session_id: Optional[int] = None
    data: Optional[Dict[str, Any]] = None


class WSTextEvent(WSEvent):
    type: str = "text"
    content: str = ""


class WSToolCallEvent(WSEvent):
    type: str = "tool_call"
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)


class WSToolResultEvent(WSEvent):
    type: str = "tool_result"
    tool_call_id: str = ""
    result: str = ""
    success: bool = True


class WSApprovalEvent(WSEvent):
    type: str = "approval_required"
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)


class WSStatusEvent(WSEvent):
    type: str = "status"
    status: str = ""


class WSErrorEvent(WSEvent):
    type: str = "error"
    error: str = ""
