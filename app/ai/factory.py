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
from app.ai.providers.ollama_provider import OllamaProvider

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
    "ollama": OllamaProvider,
}

# Default models for each provider
DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
    "google": "gemini-2.0-flash",
    "gemini": "gemini-2.0-flash",
    "deepseek": "deepseek-chat",
    "qwen": "qwen-turbo",
    "dashscope": "qwen-turbo",
    "grok": "grok-4",
    "xai": "grok-4",
    "ollama": "llama3.2",
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


class FailoverAIProvider:
    """Wraps a primary provider with fallback providers for automatic failover."""

    def __init__(self, primary: AIProvider, fallbacks: list):
        self.primary = primary
        self.fallbacks = fallbacks  # List of AIProvider instances
        self._all = [primary] + fallbacks

    def __getattr__(self, name):
        """Delegate attribute access to primary provider."""
        return getattr(self.primary, name)

    def chat_completion(self, **kwargs):
        """Try primary, then each fallback on failure."""
        last_error = None
        for i, provider in enumerate(self._all):
            try:
                return provider.chat_completion(**kwargs)
            except Exception as e:
                label = "primary" if i == 0 else f"fallback-{i}"
                logger.warning(f"AI {label} ({type(provider).__name__}) failed: {e}")
                last_error = e
        # All providers failed — raise the last error
        logger.error(f"All AI providers failed. Last error: {last_error}")
        raise last_error

    def analyze_image(self, **kwargs):
        """Try primary, then fallbacks for image analysis."""
        last_error = None
        for i, provider in enumerate(self._all):
            if not provider.supports_vision:
                continue
            try:
                return provider.analyze_image(**kwargs)
            except Exception as e:
                last_error = e
        if last_error:
            raise last_error
        return None


def get_ai_provider_with_failover(
    primary_provider: str,
    primary_key: str,
    primary_model: str = None,
    fallback_configs: list = None,
) -> AIProvider:
    """Create an AI provider with optional failover chain.

    Args:
        primary_provider: Primary provider name
        primary_key: Primary API key
        primary_model: Primary model
        fallback_configs: List of dicts with {provider, api_key, model}

    Returns:
        AIProvider (or FailoverAIProvider if fallbacks configured)
    """
    primary = get_ai_provider(primary_provider, primary_key, primary_model)

    if not fallback_configs:
        return primary

    fallbacks = []
    for fb in fallback_configs:
        try:
            provider = get_ai_provider(
                fb.get("provider", "openai"),
                fb.get("api_key", ""),
                fb.get("model"),
            )
            fallbacks.append(provider)
        except Exception as e:
            logger.warning(f"Failed to init fallback provider {fb.get('provider')}: {e}")

    if not fallbacks:
        return primary

    logger.info(f"AI failover chain: {primary_provider} → {', '.join(fb.get('provider', '?') for fb in fallback_configs)}")
    return FailoverAIProvider(primary, fallbacks)


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
