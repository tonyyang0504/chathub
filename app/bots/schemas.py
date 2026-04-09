"""
Pydantic Schemas for Bot Module
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator

VALID_AI_PROVIDERS = ["openai", "anthropic", "google", "deepseek", "qwen", "grok", "ollama"]
VALID_PLATFORMS = ["whatsapp", "telegram", "instagram", "messenger", "line", "linkedin", "tinder", "bumble", "discord", "slack", "signal", "imessage", "wechat"]

# Platform auth method descriptions for the frontend
PLATFORM_AUTH_INFO = {
    "whatsapp": {"auth_method": "qr_code", "label": "WhatsApp", "icon": "bi-whatsapp", "color": "#25D366", "token_label": None},
    "telegram": {"auth_method": "phone_code", "label": "Telegram", "icon": "bi-telegram", "color": "#26A5E4", "token_label": None, "extra_fields": [{"key": "telegram_api_id", "label": "API ID", "placeholder": "e.g. 12345678", "help": "Get from my.telegram.org → API development tools"}, {"key": "telegram_api_hash", "label": "API Hash", "placeholder": "e.g. abcdef1234567890...", "help": ""}]},
    "instagram": {"auth_method": "api_token", "label": "Instagram", "icon": "bi-instagram", "color": "#E4405F", "token_label": None},
    "messenger": {"auth_method": "api_token", "label": "Facebook Page", "icon": "bi-facebook", "color": "#1877F2", "token_label": None},
    "discord": {"auth_method": "api_token", "label": "Discord", "icon": "bi-discord", "color": "#5865F2", "token_label": None},
    "line": {"auth_method": "api_token", "label": "LINE", "icon": "bi-chat-dots-fill", "color": "#00B900", "token_label": None},
    "linkedin": {"auth_method": "oauth", "label": "LinkedIn", "icon": "bi-linkedin", "color": "#0A66C2", "token_label": None},
    "tinder": {"auth_method": "credentials", "label": "Tinder", "icon": "bi-fire", "color": "#FE3C72", "token_label": None},
    "bumble": {"auth_method": "credentials", "label": "Bumble", "icon": "bi-heart-fill", "color": "#FFC629", "token_label": None},
    "slack": {"auth_method": "api_token", "label": "Slack", "icon": "bi-slack", "color": "#4A154B", "token_label": None},
    "signal": {"auth_method": "credentials", "label": "Signal", "icon": "bi-shield-lock-fill", "color": "#3A76F0", "token_label": None},
    "imessage": {"auth_method": "credentials", "label": "iMessage", "icon": "bi-chat-square-text-fill", "color": "#34C759", "token_label": None},
    "wechat": {"auth_method": "qr_code", "label": "WeChat", "icon": "bi-wechat", "color": "#07C160", "token_label": None},
}


class BotProfileCreate(BaseModel):
    """Schema for creating a bot profile."""
    name: str = Field(..., min_length=1, max_length=255)
    platform_type: str = "whatsapp"
    ai_provider: str = "openai"
    api_key: str = Field(..., min_length=10)
    model: str = "gpt-4o-mini"
    # Fallback AI providers (JSON string: [{"provider": "anthropic", "api_key": "sk-ant-...", "model": "claude-3-5-sonnet"}])
    fallback_providers: Optional[str] = None
    # Platform-specific token (Discord bot token, etc.)
    platform_token: Optional[str] = None
    # Telegram API credentials (from my.telegram.org)
    telegram_api_id: Optional[str] = None
    telegram_api_hash: Optional[str] = None
    app_secret: Optional[str] = None  # Meta App Secret (Messenger/Instagram)
    slack_app_token: Optional[str] = None  # Slack App Token (xapp-) for Socket Mode
    signal_api_url: Optional[str] = None  # Signal CLI REST API URL

    @field_validator('platform_type')
    @classmethod
    def validate_platform_type(cls, v):
        if v not in VALID_PLATFORMS:
            raise ValueError(f"platform_type must be one of {VALID_PLATFORMS}")
        return v

    @field_validator('ai_provider')
    @classmethod
    def validate_ai_provider(cls, v):
        if v not in VALID_AI_PROVIDERS:
            raise ValueError(f"ai_provider must be one of {VALID_AI_PROVIDERS}")
        return v
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
    ending_detection_enabled: bool = False  # Enable AI-based ending detection
    dm_pairing_enabled: bool = False  # Require approval for unknown DM senders
    voice_response_enabled: bool = False  # Send AI responses as voice notes
    headless: bool = False  # Run browser in headless mode
    # Proxy Settings
    proxy_enabled: bool = False
    proxy_url: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


class BotProfileUpdate(BaseModel):
    """Schema for updating a bot profile."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    platform_type: Optional[str] = None
    ai_provider: Optional[str] = None
    api_key: Optional[str] = Field(None, min_length=10)
    model: Optional[str] = None
    fallback_providers: Optional[str] = None
    platform_token: Optional[str] = None
    telegram_api_id: Optional[str] = None
    telegram_api_hash: Optional[str] = None
    app_secret: Optional[str] = None
    slack_app_token: Optional[str] = None

    @field_validator('platform_type')
    @classmethod
    def validate_platform_type(cls, v):
        if v is not None and v not in VALID_PLATFORMS:
            raise ValueError(f"platform_type must be one of {VALID_PLATFORMS}")
        return v

    @field_validator('ai_provider')
    @classmethod
    def validate_ai_provider(cls, v):
        if v is not None and v not in VALID_AI_PROVIDERS:
            raise ValueError(f"ai_provider must be one of {VALID_AI_PROVIDERS}")
        return v
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
    ending_detection_enabled: Optional[bool] = None
    dm_pairing_enabled: Optional[bool] = None
    voice_response_enabled: Optional[bool] = None
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
    platform_type: str = "whatsapp"
    ai_provider: str = "openai"
    api_key_masked: Optional[str] = None  # Masked API key for display (e.g., sk-proj-...gasA)
    model: str
    fallback_providers: Optional[str] = "[]"
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
    ending_detection_enabled: bool = False
    dm_pairing_enabled: bool = False
    headless: bool = False
    # Proxy Settings
    proxy_enabled: bool = False
    proxy_url: Optional[str] = None
    # Platform token masked for display
    platform_token_masked: Optional[str] = None
    is_active: bool
    is_running: bool
    whatsapp_connected: bool
    platform_connected: bool = False
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
