"""
Platform Adapter Base Class
Abstract interface that all messaging platform adapters must implement.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Dict, Any, List, Callable


class PlatformType(str, Enum):
    """Supported messaging platforms."""
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    MESSENGER = "messenger"
    LINE = "line"
    LINKEDIN = "linkedin"
    TINDER = "tinder"
    BUMBLE = "bumble"
    DISCORD = "discord"
    SLACK = "slack"
    SIGNAL = "signal"
    WECHAT = "wechat"


class AuthMethod(str, Enum):
    """How the platform authenticates."""
    QR_CODE = "qr_code"          # WhatsApp, WeChat, Line
    API_TOKEN = "api_token"       # Messenger, Discord
    PHONE_CODE = "phone_code"    # Telegram (phone + SMS code)
    OAUTH = "oauth"               # Instagram, LinkedIn
    CREDENTIALS = "credentials"   # Tinder, Bumble


class PlatformCapabilities:
    """Declares what a platform adapter supports."""

    def __init__(
        self,
        *,
        supports_groups: bool = False,
        supports_media: bool = True,
        supports_file_send: bool = True,
        supports_reactions: bool = False,
        supports_read_receipts: bool = False,
        supports_typing_indicator: bool = False,
        supports_history_sync: bool = False,
        supports_contacts_list: bool = False,
        supports_groups_list: bool = False,
        supports_profile_pic: bool = False,
        auth_method: AuthMethod = AuthMethod.QR_CODE,
        max_message_length: Optional[int] = None,
        supported_media_types: Optional[List[str]] = None,
    ):
        self.supports_groups = supports_groups
        self.supports_media = supports_media
        self.supports_file_send = supports_file_send
        self.supports_reactions = supports_reactions
        self.supports_read_receipts = supports_read_receipts
        self.supports_typing_indicator = supports_typing_indicator
        self.supports_history_sync = supports_history_sync
        self.supports_contacts_list = supports_contacts_list
        self.supports_groups_list = supports_groups_list
        self.supports_profile_pic = supports_profile_pic
        self.auth_method = auth_method
        self.max_message_length = max_message_length
        self.supported_media_types = supported_media_types or [
            "image/jpeg", "image/png", "image/gif",
            "video/mp4", "audio/mpeg", "audio/ogg",
            "application/pdf",
        ]


class PlatformAdapter(ABC):
    """Abstract base class for messaging platform adapters.

    Each platform (WhatsApp, Telegram, etc.) implements this interface.
    The adapter is responsible for:
    - Running the platform connection (browser automation, API polling, webhooks)
    - Sending messages and files
    - Notifying the BotInstance of incoming messages and status changes via callbacks

    Lifecycle:
        1. Adapter is created by the registry with bot config
        2. BotManager calls run(instance) which starts the platform connection
        3. Platform notifies instance via instance.notify_status() / instance.notify_qr()
        4. External code calls send_message() / send_file() to send outbound messages
        5. BotManager calls stop() to shut down
    """

    @property
    @abstractmethod
    def platform_type(self) -> PlatformType:
        """The platform this adapter handles."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> PlatformCapabilities:
        """What this platform supports."""
        ...

    @abstractmethod
    async def run(self, instance) -> None:
        """Start the platform connection and enter the main loop.

        This is called by BotManager as an asyncio task. It should:
        - Initialize the platform connection (browser, API client, etc.)
        - Notify instance of auth events (QR codes, tokens, etc.)
        - Enter a message processing loop
        - Handle incoming messages and route to AI/DB
        - Process outbound message queue
        - Handle errors and recovery

        Args:
            instance: BotInstance with config, callbacks, and outbound queue
        """
        ...

    @abstractmethod
    async def send_message(
        self,
        bot_profile_id: int,
        chat_id: str,
        chat_name: str,
        message: str,
    ) -> bool:
        """Send a text message to a chat.

        Args:
            bot_profile_id: The bot's database ID
            chat_id: Platform-specific chat identifier
            chat_name: Display name of the chat (used for finding chat in UI)
            message: Text content to send

        Returns:
            True if message was sent successfully
        """
        ...

    @abstractmethod
    async def send_file(
        self,
        bot_profile_id: int,
        chat_id: str,
        file_path: str,
        caption: str = "",
        file_type: str = "",
        chat_name: str = "",
    ) -> bool:
        """Send a file/media to a chat.

        Args:
            bot_profile_id: The bot's database ID
            chat_id: Platform-specific chat identifier
            file_path: Absolute path to the file
            caption: Optional caption/description
            file_type: MIME type of the file
            chat_name: Display name of the chat

        Returns:
            True if file was sent successfully
        """
        ...

    def cleanup(self, bot_profile_id: int) -> None:
        """Clean up resources for a bot (queues, temp files, etc.).

        Called when a bot instance is removed. Override if the adapter
        maintains per-bot resources that need cleanup.

        Args:
            bot_profile_id: The bot's database ID
        """
        pass

    def get_contacts(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Get the contact list for a bot. Override if platform supports it.

        Returns:
            List of contact dicts with at minimum 'chat_id' and 'name' keys
        """
        return []

    def get_groups(self, bot_profile_id: int) -> List[Dict[str, Any]]:
        """Get the group list for a bot. Override if platform supports it.

        Returns:
            List of group dicts with at minimum 'chat_id' and 'name' keys
        """
        return []
