"""
Application Configuration
"""

import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings."""

    # App Settings
    APP_NAME: str = "ChatHub"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Server Settings
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")

    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 24 hours

    # Encryption key for API keys (should be 32 bytes for Fernet)
    ENCRYPTION_KEY: Optional[str] = os.getenv("ENCRYPTION_KEY")

    # Cookie
    COOKIE_NAME: str = os.getenv("COOKIE_NAME", "access_token")

    # Session
    SESSION_PATH: str = os.getenv("SESSION_PATH", os.path.join("data", "sessions"))

    # Bot defaults
    DEFAULT_MODEL: str = "gpt-4o-mini"
    DEFAULT_MAX_HISTORY: int = 20
    DEFAULT_RESPONSE_DELAY_MIN: int = 3
    DEFAULT_RESPONSE_DELAY_MAX: int = 8

    # CORS Settings (comma-separated origins, or "*" for all)
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")

    # Logging Settings
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_MAX_SIZE_MB: int = int(os.getenv("LOG_MAX_SIZE_MB", "10"))  # Max log file size in MB
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "30"))  # Number of backup files
    LOG_MAX_AGE_DAYS: int = int(os.getenv("LOG_MAX_AGE_DAYS", "30"))  # Delete logs older than this
    LOG_JSON_FORMAT: bool = os.getenv("LOG_JSON_FORMAT", "false").lower() == "true"

    @classmethod
    def get_encryption_key(cls) -> bytes:
        """Get or generate encryption key."""
        if cls.ENCRYPTION_KEY:
            return cls.ENCRYPTION_KEY.encode()
        # Generate a key from SECRET_KEY (not ideal for production)
        from hashlib import sha256
        return sha256(cls.SECRET_KEY.encode()).digest()


settings = Settings()
