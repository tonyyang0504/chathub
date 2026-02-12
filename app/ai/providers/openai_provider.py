"""
OpenAI Provider

Implementation for OpenAI's API (GPT-4, GPT-4o, etc.)
"""

import base64
import logging
from typing import Optional, List, Dict, Any, Union

from openai import OpenAI, APIConnectionError, RateLimitError, APITimeoutError

from app.ai.providers.base import AIProvider, AIResponse, AIMessage, ToolCall, create_retry_decorator

logger = logging.getLogger(__name__)


class OpenAIProvider(AIProvider):
    """OpenAI API provider."""

    provider_name = "openai"
    supports_tools = True
    supports_vision = True
    supports_json_mode = True

    # Available models
    MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-4",
        "gpt-3.5-turbo",
        "o1-preview",
        "o1-mini",
    ]

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        super().__init__(api_key, model or "gpt-4o-mini", base_url, **kwargs)

        # Create OpenAI client
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = OpenAI(**client_kwargs)
        logger.debug(f"OpenAI provider initialized with model: {self.model}")

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
        """Generate a chat completion using OpenAI."""
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
            # Newer models (GPT-5.x, o1, etc.) use max_completion_tokens instead of max_tokens
            if use_model.startswith(('gpt-5', 'o1', 'o3')):
                params["max_completion_tokens"] = max_tokens
            else:
                params["max_tokens"] = max_tokens

        if tools:
            params["tools"] = tools
            if tool_choice:
                params["tool_choice"] = tool_choice

        if json_mode:
            params["response_format"] = {"type": "json_object"}

        # Add any extra kwargs
        params.update(kwargs)

        logger.debug(f"OpenAI request: model={use_model}, messages={len(openai_messages)}")

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

    def _analyze_image_impl(
        self,
        image_data: Union[str, bytes],
        prompt: str,
        model: Optional[str],
        detail: str,
        **kwargs
    ) -> AIResponse:
        """Analyze an image using OpenAI's vision capabilities."""
        use_model = model or "gpt-4o"  # Vision requires gpt-4o or gpt-4-turbo

        # Prepare image content
        if isinstance(image_data, bytes):
            # Convert bytes to base64
            b64_data = base64.b64encode(image_data).decode('utf-8')
            image_url = f"data:image/jpeg;base64,{b64_data}"
        elif image_data.startswith('data:'):
            image_url = image_data
        elif image_data.startswith('http'):
            image_url = image_data
        else:
            # Assume it's base64 data
            image_url = f"data:image/jpeg;base64,{image_data}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": detail
                        }
                    }
                ]
            }
        ]

        response = self.client.chat.completions.create(
            model=use_model,
            messages=messages,
            max_tokens=kwargs.get("max_tokens", 1000)
        )

        choice = response.choices[0]

        return AIResponse(
            content=choice.message.content or "",
            model=response.model,
            finish_reason=choice.finish_reason,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
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
            logger.error(f"OpenAI rate limit exceeded: {e}")
            raise
        except APIConnectionError as e:
            logger.error(f"OpenAI connection error: {e}")
            raise ConnectionError(str(e))
        except APITimeoutError as e:
            logger.error(f"OpenAI timeout error: {e}")
            raise TimeoutError(str(e))

    def get_available_models(self) -> List[str]:
        """Return list of available OpenAI models."""
        return self.MODELS
