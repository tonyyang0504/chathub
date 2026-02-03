"""
Base AI Provider

Abstract base class for all AI providers, defining the common interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Union
from enum import Enum


class MessageRole(str, Enum):
    """Message roles for chat completion."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class AIMessage:
    """Represents a message in a conversation."""
    role: str  # 'system', 'user', 'assistant', 'tool'
    content: str
    name: Optional[str] = None  # For tool messages
    tool_call_id: Optional[str] = None  # For tool responses
    tool_calls: Optional[List[Dict]] = None  # For assistant tool calls


@dataclass
class ToolCall:
    """Represents a tool call made by the AI."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class AIResponse:
    """Unified response from any AI provider."""
    content: str
    model: str
    finish_reason: str  # 'stop', 'length', 'tool_calls', etc.
    usage: Dict[str, int] = field(default_factory=dict)  # {'prompt_tokens': x, 'completion_tokens': y, 'total_tokens': z}
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw_response: Any = None  # Original provider response for debugging


class AIProvider(ABC):
    """
    Abstract base class for AI providers.

    All providers must implement:
    - chat_completion: For text generation with conversation history
    - analyze_image: For vision/image analysis (if supported)
    """

    # Provider metadata
    provider_name: str = "base"
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json_mode: bool = False

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize the provider.

        Args:
            api_key: API key for the provider
            model: Default model to use
            base_url: Optional custom base URL (for proxies or compatible APIs)
            **kwargs: Additional provider-specific configuration
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self._config = kwargs

    @abstractmethod
    def chat_completion(
        self,
        messages: List[Union[AIMessage, Dict[str, str]]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        top_p: float = 1.0,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Union[str, Dict]] = None,
        json_mode: bool = False,
        **kwargs
    ) -> AIResponse:
        """
        Generate a chat completion.

        Args:
            messages: List of messages in the conversation
            model: Model to use (overrides default)
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens in response
            top_p: Nucleus sampling parameter
            frequency_penalty: Reduce repetition of tokens
            presence_penalty: Encourage new topics
            tools: List of tool definitions for function calling
            tool_choice: How to handle tool calls ('auto', 'none', or specific tool)
            json_mode: If True, force JSON output
            **kwargs: Additional provider-specific parameters

        Returns:
            AIResponse with the generated content
        """
        pass

    def analyze_image(
        self,
        image_data: Union[str, bytes],
        prompt: str,
        model: Optional[str] = None,
        detail: str = "auto",
        **kwargs
    ) -> AIResponse:
        """
        Analyze an image with the AI.

        Args:
            image_data: Base64-encoded image data or URL
            prompt: Instructions for analysis
            model: Model to use (overrides default)
            detail: Level of detail ('low', 'high', 'auto')
            **kwargs: Additional parameters

        Returns:
            AIResponse with the analysis

        Raises:
            NotImplementedError if provider doesn't support vision
        """
        if not self.supports_vision:
            raise NotImplementedError(
                f"{self.provider_name} does not support image analysis"
            )
        return self._analyze_image_impl(image_data, prompt, model, detail, **kwargs)

    def _analyze_image_impl(
        self,
        image_data: Union[str, bytes],
        prompt: str,
        model: Optional[str],
        detail: str,
        **kwargs
    ) -> AIResponse:
        """Implementation of image analysis. Override in providers that support vision."""
        raise NotImplementedError()

    def _normalize_messages(
        self,
        messages: List[Union[AIMessage, Dict[str, str]]]
    ) -> List[AIMessage]:
        """Convert messages to AIMessage objects."""
        normalized = []
        for msg in messages:
            if isinstance(msg, AIMessage):
                normalized.append(msg)
            elif isinstance(msg, dict):
                normalized.append(AIMessage(
                    role=msg.get('role', 'user'),
                    content=msg.get('content', ''),
                    name=msg.get('name'),
                    tool_call_id=msg.get('tool_call_id'),
                    tool_calls=msg.get('tool_calls')
                ))
            else:
                raise ValueError(f"Invalid message type: {type(msg)}")
        return normalized

    def get_available_models(self) -> List[str]:
        """Return list of available models for this provider."""
        return []

    def validate_model(self, model: str) -> bool:
        """Check if a model is valid for this provider."""
        available = self.get_available_models()
        if not available:
            return True  # If no list, assume valid
        return model in available
