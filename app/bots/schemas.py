"""
Pydantic Schemas for Bot Module
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class BotProfileCreate(BaseModel):
    """Schema for creating a bot profile."""
    name: str = Field(..., min_length=1, max_length=255)
    openai_api_key: str = Field(..., min_length=10)
    openai_model: str = "gpt-4o-mini"
    system_prompt: str = "You are a helpful assistant. Do not use markdown formatting like asterisks (*), underscores (_), or other special characters for emphasis. Write plain text only."
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=1000, ge=100, le=4096)
    # Advanced AI Settings
    top_p: float = Field(default=1.0, ge=0, le=1)
    frequency_penalty: float = Field(default=0.0, ge=-2, le=2)
    presence_penalty: float = Field(default=0.0, ge=-2, le=2)
    max_history: int = Field(default=20, ge=1, le=100)
    response_delay_min: int = Field(default=3, ge=0, le=60)
    response_delay_max: int = Field(default=8, ge=1, le=120)
    group_chat_enabled: bool = True
    respond_to_all_in_group: bool = False
    headless: bool = False  # Run browser in headless mode
    # Proxy Settings
    proxy_enabled: bool = False
    proxy_url: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


class BotProfileUpdate(BaseModel):
    """Schema for updating a bot profile."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    openai_api_key: Optional[str] = Field(None, min_length=10)
    openai_model: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=100, le=4096)
    # Advanced AI Settings
    top_p: Optional[float] = Field(None, ge=0, le=1)
    frequency_penalty: Optional[float] = Field(None, ge=-2, le=2)
    presence_penalty: Optional[float] = Field(None, ge=-2, le=2)
    max_history: Optional[int] = Field(None, ge=1, le=100)
    response_delay_min: Optional[int] = Field(None, ge=0, le=60)
    response_delay_max: Optional[int] = Field(None, ge=1, le=120)
    group_chat_enabled: Optional[bool] = None
    respond_to_all_in_group: Optional[bool] = None
    headless: Optional[bool] = None
    # Proxy Settings
    proxy_enabled: Optional[bool] = None
    proxy_url: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


class BotProfileResponse(BaseModel):
    """Schema for bot profile response."""
    id: int
    name: str
    openai_model: str
    system_prompt: str
    temperature: float = 0.7
    max_tokens: int = 1000
    # Advanced AI Settings
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    max_history: int
    response_delay_min: int
    response_delay_max: int
    group_chat_enabled: bool
    respond_to_all_in_group: bool
    headless: bool = False
    # Proxy Settings
    proxy_enabled: bool = False
    proxy_url: Optional[str] = None
    is_active: bool
    is_running: bool
    whatsapp_connected: bool
    last_active: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    conversation_count: Optional[int] = 0
    message_count: Optional[int] = 0

    # WhatsApp Account Info
    whatsapp_phone: Optional[str] = None
    whatsapp_name: Optional[str] = None
    whatsapp_push_name: Optional[str] = None
    whatsapp_profile_pic: Optional[str] = None
    whatsapp_about: Optional[str] = None
    whatsapp_account_type: Optional[str] = "personal"

    class Config:
        from_attributes = True


class BotStatusResponse(BaseModel):
    """Schema for bot status response."""
    id: int
    name: str
    is_running: bool
    whatsapp_connected: bool
    last_active: Optional[datetime]


class BotListResponse(BaseModel):
    """Schema for list of bots."""
    bots: List[BotProfileResponse]
    total: int
