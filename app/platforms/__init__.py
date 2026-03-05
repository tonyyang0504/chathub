"""
Platforms Module
Multi-platform messaging adapter abstraction layer.
"""

from app.platforms.base import PlatformAdapter, PlatformType
from app.platforms.registry import platform_registry
from app.platforms.send import send_message, send_file

__all__ = [
    "PlatformAdapter",
    "PlatformType",
    "platform_registry",
    "send_message",
    "send_file",
]
