"""
Grok Provider (xAI)

Implementation for xAI's Grok API using OpenAI-compatible interface.
"""

import logging
from typing import Optional, List, Dict, Any, Union

from openai import OpenAI, APIConnectionError, RateLimitError, APITimeoutError

from app.ai.providers.base import AIProvider, AIResponse, AIMessage, ToolCall, create_retry_decorator

logger = logging.getLogger(__name__)


class GrokProvider(AIProvider):
    """xAI Grok API provider (OpenAI-compatible)."""

    provider_name = "grok"
    supports_tools = True
    supports_vision = True
    supports_json_mode = True

    # Grok API base URL
    BASE_URL = "https://api.x.ai/v1"

    # Available models
    MODELS = [
        "grok-4",
        "grok-4-1-fast-reasoning",
        "grok-4-1-fast-non-reasoning",
        "grok-code-fast-1",
        "grok-3",
        "grok-3-fast",
    ]

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        # Use Grok's API URL unless overridden
        actual_base_url = base_url or self.BASE_URL
        super().__init__(api_key, model or "grok-4", actual_base_url, **kwargs)

        # Create OpenAI-compatible client for Grok
        self.client = OpenAI(
            api_key=api_key,
            base_url=actual_base_url
        )
        logger.debug(f"Grok provider initialized with model: {self.model}")

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
        """Generate a chat completion using Grok."""
        use_model = model or self.model

        # Convert messages to OpenAI format
        grok_messages = self._convert_messages(messages)

        # Build request parameters
        params = {
            "model": use_model,
            "messages": grok_messages,
            "temperature": temperature,
            "top_p": top_p,
        }

        # Grok may not support all OpenAI parameters
        if frequency_penalty != 0.0:
            params["frequency_penalty"] = frequency_penalty
        if presence_penalty != 0.0:
            params["presence_penalty"] = presence_penalty

        if max_tokens:
            params["max_tokens"] = max_tokens

        if tools:
            params["tools"] = tools
            if tool_choice:
                params["tool_choice"] = tool_choice

        if json_mode:
            params["response_format"] = {"type": "json_object"}

        # Add any extra kwargs
        params.update(kwargs)

        logger.debug(f"Grok request: model={use_model}, messages={len(grok_messages)}")

        # Make the API call with retry logic
        response = self._make_api_call(params)

        # Convert response
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
        """Convert messages to Grok/OpenAI format."""
        grok_messages = []
        normalized = self._normalize_messages(messages)

        for msg in normalized:
            grok_msg = {
                "role": msg.role,
                "content": msg.content
            }

            if msg.name:
                grok_msg["name"] = msg.name

            if msg.tool_call_id:
                grok_msg["tool_call_id"] = msg.tool_call_id

            if msg.tool_calls:
                grok_msg["tool_calls"] = msg.tool_calls

            grok_messages.append(grok_msg)

        return grok_messages

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
            logger.error(f"Grok rate limit exceeded: {e}")
            raise
        except APIConnectionError as e:
            logger.error(f"Grok connection error: {e}")
            raise ConnectionError(str(e))
        except APITimeoutError as e:
            logger.error(f"Grok timeout error: {e}")
            raise TimeoutError(str(e))

    def get_available_models(self) -> List[str]:
        """Return list of available Grok models."""
        return self.MODELS
