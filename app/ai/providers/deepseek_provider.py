"""
DeepSeek Provider

Implementation for DeepSeek's API (OpenAI-compatible).
"""

import logging
from typing import Optional, List, Dict, Any, Union

from openai import OpenAI, APIConnectionError, RateLimitError, APITimeoutError

from app.ai.providers.base import AIProvider, AIResponse, AIMessage, ToolCall, create_retry_decorator

logger = logging.getLogger(__name__)


class DeepSeekProvider(AIProvider):
    """DeepSeek API provider (OpenAI-compatible)."""

    provider_name = "deepseek"
    supports_tools = True
    supports_vision = False  # DeepSeek doesn't support vision yet
    supports_json_mode = True

    # Available models
    MODELS = [
        "deepseek-chat",
        "deepseek-coder",
        "deepseek-reasoner",
    ]

    # Default API base URL
    DEFAULT_BASE_URL = "https://api.deepseek.com"

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        super().__init__(
            api_key,
            model or "deepseek-chat",
            base_url or self.DEFAULT_BASE_URL,
            **kwargs
        )

        # Create OpenAI-compatible client
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.base_url
        )
        logger.debug(f"DeepSeek provider initialized with model: {self.model}")

    def _chat_completion_impl(
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
        """Generate a chat completion using DeepSeek."""
        use_model = model or self.model

        # Convert messages to OpenAI format
        openai_messages = self._convert_messages(messages)

        # Build request parameters
        params = {
            "model": use_model,
            "messages": openai_messages,
            "temperature": temperature,
            "top_p": top_p,
            "frequency_penalty": frequency_penalty,
            "presence_penalty": presence_penalty,
        }

        if max_tokens:
            params["max_tokens"] = max_tokens

        if tools:
            params["tools"] = tools
            if tool_choice:
                params["tool_choice"] = tool_choice

        if json_mode:
            params["response_format"] = {"type": "json_object"}

        logger.debug(f"DeepSeek request: model={use_model}, messages={len(openai_messages)}")

        # Make the API call with retry logic
        response = self._make_api_call(params)

        # Convert response (same as OpenAI)
        choice = response.choices[0]
        message = choice.message

        # Extract tool calls if present
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=self._parse_json_safe(tc.function.arguments)
                ))

        return AIResponse(
            content=message.content or "",
            model=response.model,
            finish_reason=choice.finish_reason,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
            tool_calls=tool_calls,
            raw_response=response
        )

    def _convert_messages(
        self,
        messages: List[Union[AIMessage, Dict[str, str]]]
    ) -> List[Dict]:
        """Convert messages to OpenAI format."""
        openai_messages = []
        normalized = self._normalize_messages(messages)

        for msg in normalized:
            openai_msg = {
                "role": msg.role,
                "content": msg.content
            }

            if msg.name:
                openai_msg["name"] = msg.name

            if msg.tool_call_id:
                openai_msg["tool_call_id"] = msg.tool_call_id

            if msg.tool_calls:
                openai_msg["tool_calls"] = msg.tool_calls

            openai_messages.append(openai_msg)

        return openai_messages

    def _parse_json_safe(self, json_str: str) -> Dict[str, Any]:
        """Safely parse JSON string."""
        import json
        try:
            return json.loads(json_str)
        except:
            return {"raw": json_str}

    @create_retry_decorator(max_attempts=3)
    def _make_api_call(self, params: Dict) -> Any:
        """Make API call with retry logic."""
        try:
            return self.client.chat.completions.create(**params)
        except RateLimitError as e:
            logger.error(f"DeepSeek rate limit exceeded: {e}")
            raise
        except APIConnectionError as e:
            logger.error(f"DeepSeek connection error: {e}")
            raise ConnectionError(str(e))
        except APITimeoutError as e:
            logger.error(f"DeepSeek timeout error: {e}")
            raise TimeoutError(str(e))

    def get_available_models(self) -> List[str]:
        """Return list of available DeepSeek models."""
        return self.MODELS
