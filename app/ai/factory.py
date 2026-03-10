"""
AI Provider Factory

Factory function for creating AI provider instances.
"""

import logging
from typing import Optional, Dict, Type

from app.ai.providers.base import AIProvider
from app.ai.providers.openai_provider import OpenAIProvider
from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.providers.google_provider import GoogleProvider
from app.ai.providers.deepseek_provider import DeepSeekProvider
from app.ai.providers.qwen_provider import QwenProvider
from app.ai.providers.grok_provider import GrokProvider

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    """Exception raised for AI provider errors."""
    pass


# Registry of available providers
PROVIDERS: Dict[str, Type[AIProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "gemini": GoogleProvider,  # Alias
    "deepseek": DeepSeekProvider,
    "qwen": QwenProvider,
    "dashscope": QwenProvider,  # Alias
    "grok": GrokProvider,
    "xai": GrokProvider,  # Alias
}

# Default models for each provider
DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
    "google": "gemini-1.5-flash",
    "gemini": "gemini-1.5-flash",
    "deepseek": "deepseek-chat",
    "qwen": "qwen-turbo",
    "dashscope": "qwen-turbo",
    "grok": "grok-4",
    "xai": "grok-4",
}


def get_ai_provider(
    provider_name: str,
    api_key: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    **kwargs
) -> AIProvider:
    """
    Create an AI provider instance.

    Args:
        provider_name: Name of the provider ('openai', 'anthropic', 'google', 'deepseek', 'qwen')
        api_key: API key for the provider
        model: Model to use (optional, will use provider default)
        base_url: Custom base URL (optional)
        **kwargs: Additional provider-specific configuration

    Returns:
        AIProvider instance

    Raises:
        AIProviderError: If provider is not supported or initialization fails
    """
    provider_name = provider_name.lower()

    if provider_name not in PROVIDERS:
        available = ", ".join(PROVIDERS.keys())
        raise AIProviderError(
            f"Unknown provider: {provider_name}. Available: {available}"
        )

    provider_class = PROVIDERS[provider_name]

    # Use default model if not specified
    if not model:
        model = DEFAULT_MODELS.get(provider_name)

    try:
        logger.debug(f"Creating {provider_name} provider with model {model}")
        return provider_class(
            api_key=api_key,
            model=model,
            base_url=base_url,
            **kwargs
        )
    except ImportError as e:
        raise AIProviderError(
            f"Failed to initialize {provider_name} provider: {e}. "
            f"Make sure the required package is installed."
        )
    except Exception as e:
        raise AIProviderError(
            f"Failed to initialize {provider_name} provider: {e}"
        )


def get_available_providers() -> Dict[str, Dict]:
    """
    Get information about available providers.

    Returns:
        Dict with provider info including capabilities
    """
    info = {}
    for name, provider_class in PROVIDERS.items():
        if name in ["gemini", "dashscope", "xai"]:  # Skip aliases
            continue
        info[name] = {
            "supports_tools": provider_class.supports_tools,
            "supports_vision": provider_class.supports_vision,
            "supports_json_mode": provider_class.supports_json_mode,
            "default_model": DEFAULT_MODELS.get(name),
        }
    return info


def is_provider_available(provider_name: str) -> bool:
    """Check if a provider is available (has required dependencies)."""
    provider_name = provider_name.lower()

    if provider_name not in PROVIDERS:
        return False

    # Check if required packages are installed
    if provider_name in ["anthropic"]:
        try:
            import anthropic
            return True
        except ImportError:
            return False

    if provider_name in ["google", "gemini"]:
        try:
            import google.generativeai
            return True
        except ImportError:
            return False

    # OpenAI-compatible providers just need openai package
    try:
        import openai
        return True
    except ImportError:
        return False
