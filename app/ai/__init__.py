"""
AI Provider Module

Provides a unified interface for multiple AI providers:
- OpenAI (GPT-4, GPT-4o, etc.)
- Anthropic (Claude)
- Google (Gemini)
- DeepSeek
- Qwen

Usage:
    from app.ai import get_ai_provider

    provider = get_ai_provider("openai", api_key="...")
    response = provider.chat_completion(messages=[...])
"""

from app.ai.factory import get_ai_provider, AIProviderError
from app.ai.providers.base import AIProvider, AIResponse, AIMessage

__all__ = [
    'get_ai_provider',
    'AIProvider',
    'AIResponse',
    'AIMessage',
    'AIProviderError',
]
