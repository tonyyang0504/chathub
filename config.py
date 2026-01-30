"""
Configuration settings for WhatsApp Bot.
Load settings from environment variables or use defaults.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Bot configuration settings."""

    # OpenAI Settings
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "2000"))
    TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.7"))

    # System Prompt (Customizable Role)
    SYSTEM_PROMPT: str = os.getenv(
        "SYSTEM_PROMPT",
        "You are a helpful assistant. Do not use markdown formatting like asterisks (*), underscores (_), or other special characters for emphasis. Write plain text only."
    )

    # Conversation Settings
    MAX_HISTORY: int = int(os.getenv("MAX_HISTORY", "20"))  # Messages to keep in context

    # Group Chat Settings
    GROUP_CHAT_ENABLED: bool = os.getenv("GROUP_CHAT_ENABLED", "true").lower() == "true"
    RESPOND_TO_ALL_IN_GROUP: bool = os.getenv("RESPOND_TO_ALL_IN_GROUP", "false").lower() == "true"
    BOT_NAME: str = os.getenv("BOT_NAME", "Bot")  # Name to detect mentions

    # Human-like Behavior
    RESPONSE_DELAY_MIN: int = int(os.getenv("RESPONSE_DELAY_MIN", "3"))  # seconds
    RESPONSE_DELAY_MAX: int = int(os.getenv("RESPONSE_DELAY_MAX", "8"))  # seconds
    SHOW_TYPING_INDICATOR: bool = os.getenv("SHOW_TYPING_INDICATOR", "true").lower() == "true"

    # Database Settings
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/conversations.db")

    # WhatsApp Session
    SESSION_PATH: str = os.getenv("SESSION_PATH", "data/session")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def validate(cls) -> bool:
        """Validate required configuration."""
        if not cls.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required. Set it in .env file.")
        return True

    @classmethod
    def to_dict(cls) -> dict:
        """Return configuration as dictionary (excluding sensitive data)."""
        return {
            "OPENAI_MODEL": cls.OPENAI_MODEL,
            "MAX_TOKENS": cls.MAX_TOKENS,
            "TEMPERATURE": cls.TEMPERATURE,
            "MAX_HISTORY": cls.MAX_HISTORY,
            "GROUP_CHAT_ENABLED": cls.GROUP_CHAT_ENABLED,
            "RESPOND_TO_ALL_IN_GROUP": cls.RESPOND_TO_ALL_IN_GROUP,
            "BOT_NAME": cls.BOT_NAME,
            "RESPONSE_DELAY_MIN": cls.RESPONSE_DELAY_MIN,
            "RESPONSE_DELAY_MAX": cls.RESPONSE_DELAY_MAX,
        }
