"""
Tests for AI providers.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from app.ai.providers.base import AIProvider, AIMessage, AIResponse, create_retry_decorator


class TestAIMessage:
    """Tests for AIMessage dataclass."""

    def test_create_message(self):
        """Test creating an AI message."""
        msg = AIMessage(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.name is None

    def test_create_message_with_tool_call(self):
        """Test creating a message with tool call info."""
        msg = AIMessage(
            role="tool",
            content="Result",
            tool_call_id="call_123"
        )
        assert msg.tool_call_id == "call_123"


class TestAIResponse:
    """Tests for AIResponse dataclass."""

    def test_create_response(self):
        """Test creating an AI response."""
        response = AIResponse(
            content="Hello!",
            model="gpt-4",
            finish_reason="stop"
        )
        assert response.content == "Hello!"
        assert response.model == "gpt-4"
        assert response.finish_reason == "stop"
        assert response.usage == {}
        assert response.tool_calls == []


class TestRetryDecorator:
    """Tests for retry decorator."""

    def test_retry_decorator_success(self):
        """Test that successful calls don't retry."""
        call_count = 0

        @create_retry_decorator(max_attempts=3)
        def successful_call():
            nonlocal call_count
            call_count += 1
            return "success"

        result = successful_call()
        assert result == "success"
        assert call_count == 1

    def test_retry_decorator_retries_on_connection_error(self):
        """Test that connection errors trigger retries."""
        call_count = 0

        @create_retry_decorator(max_attempts=3)
        def failing_call():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Connection failed")
            return "success"

        result = failing_call()
        assert result == "success"
        assert call_count == 3

    def test_retry_decorator_max_attempts(self):
        """Test that max attempts is respected."""
        from tenacity import RetryError

        call_count = 0

        @create_retry_decorator(max_attempts=3)
        def always_failing_call():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Connection failed")

        with pytest.raises((ConnectionError, RetryError)):
            always_failing_call()
        assert call_count == 3


class TestOpenAIProvider:
    """Tests for OpenAI provider."""

    def test_openai_provider_initialization(self):
        """Test OpenAI provider can be initialized."""
        from app.ai.providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")
        assert provider.provider_name == "openai"
        assert provider.model == "gpt-4o-mini"
        assert provider.supports_tools is True
        assert provider.supports_vision is True

    def test_openai_provider_available_models(self):
        """Test that available models are returned."""
        from app.ai.providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")
        models = provider.get_available_models()
        assert "gpt-4o" in models
        assert "gpt-4o-mini" in models


class TestAnthropicProvider:
    """Tests for Anthropic provider."""

    def test_anthropic_provider_initialization(self):
        """Test Anthropic provider can be initialized."""
        from app.ai.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")
        assert provider.provider_name == "anthropic"
        assert provider.supports_tools is True
        assert provider.supports_vision is True

    def test_anthropic_model_aliases(self):
        """Test that model aliases are resolved."""
        from app.ai.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")
        resolved = provider._resolve_model("claude-3.5-sonnet")
        assert resolved == "claude-3-5-sonnet-20241022"


class TestGoogleProvider:
    """Tests for Google provider."""

    def test_google_provider_initialization(self):
        """Test Google provider can be initialized."""
        from app.ai.providers.google_provider import GoogleProvider

        provider = GoogleProvider(api_key="test-key")
        assert provider.provider_name == "google"
        assert provider.model == "gemini-1.5-flash"


class TestDeepSeekProvider:
    """Tests for DeepSeek provider."""

    def test_deepseek_provider_initialization(self):
        """Test DeepSeek provider can be initialized."""
        from app.ai.providers.deepseek_provider import DeepSeekProvider

        provider = DeepSeekProvider(api_key="test-key")
        assert provider.provider_name == "deepseek"
        assert provider.model == "deepseek-chat"
        assert provider.base_url == "https://api.deepseek.com"


class TestQwenProvider:
    """Tests for Qwen provider."""

    def test_qwen_provider_initialization(self):
        """Test Qwen provider can be initialized."""
        from app.ai.providers.qwen_provider import QwenProvider

        provider = QwenProvider(api_key="test-key")
        assert provider.provider_name == "qwen"
        assert provider.model == "qwen-turbo"
