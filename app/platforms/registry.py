"""
Platform Registry
Maps platform types to their adapter implementations.
"""

import logging
from typing import Dict, Type

from app.platforms.base import PlatformAdapter, PlatformType

logger = logging.getLogger(__name__)


class PlatformRegistry:
    """Registry of available platform adapters.

    Usage:
        # Register an adapter class
        platform_registry.register(PlatformType.WHATSAPP, WhatsAppAdapter)

        # Get an adapter instance
        adapter = platform_registry.get_adapter(PlatformType.WHATSAPP)
    """

    def __init__(self):
        self._adapters: Dict[PlatformType, Type[PlatformAdapter]] = {}
        self._instances: Dict[PlatformType, PlatformAdapter] = {}

    def register(self, platform_type: PlatformType, adapter_class: Type[PlatformAdapter]):
        """Register a platform adapter class.

        Args:
            platform_type: The platform this adapter handles
            adapter_class: The adapter class (not instance)
        """
        self._adapters[platform_type] = adapter_class
        logger.info(f"Registered platform adapter: {platform_type.value}")

    def get_adapter(self, platform_type: PlatformType) -> PlatformAdapter:
        """Get a singleton adapter instance for a platform.

        Args:
            platform_type: The platform to get an adapter for

        Returns:
            PlatformAdapter instance

        Raises:
            ValueError: If no adapter is registered for the platform
        """
        if platform_type not in self._adapters:
            available = [p.value for p in self._adapters.keys()]
            raise ValueError(
                f"No adapter registered for platform '{platform_type.value}'. "
                f"Available: {available}"
            )

        if platform_type not in self._instances:
            self._instances[platform_type] = self._adapters[platform_type]()

        return self._instances[platform_type]

    def is_registered(self, platform_type: PlatformType) -> bool:
        """Check if an adapter is registered for a platform."""
        return platform_type in self._adapters

    def get_available_platforms(self) -> list:
        """Get list of registered platform types."""
        return list(self._adapters.keys())


# Global registry singleton
platform_registry = PlatformRegistry()
