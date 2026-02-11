"""
AI Providers Package

Contains implementations for various AI providers.
"""

from app.ai.providers.base import AIProvider, AIResponse, AIMessage
from app.ai.providers.openai_provider import OpenAIProvider
from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.providers.google_provider import GoogleProvider
from app.ai.providers.deepseek_provider import DeepSeekProvider
from app.ai.providers.qwen_provider import QwenProvider


def get_ai_provider(provider_name: str, api_key: str, model: str = None) -> AIProvider:
    """
    Factory function to create an AI provider instance.

    Args:
        provider_name: Name of the provider ('openai', 'anthropic', 'google', 'deepseek', 'qwen')
        api_key: API key for the provider
        model: Optional model to use

    Returns:
        An instance of the appropriate AI provider
    """
    providers = {
        'openai': OpenAIProvider,
        'anthropic': AnthropicProvider,
        'google': GoogleProvider,
        'deepseek': DeepSeekProvider,
        'qwen': QwenProvider,
    }

    provider_class = providers.get(provider_name.lower())
    if not provider_class:
        raise ValueError(f"Unknown provider: {provider_name}. Available: {list(providers.keys())}")

    return provider_class(api_key=api_key, model=model)


__all__ = [
    'AIProvider',
    'AIResponse',
    'AIMessage',
    'OpenAIProvider',
    'AnthropicProvider',
    'GoogleProvider',
    'DeepSeekProvider',
    'QwenProvider',
    'get_ai_provider',
]
