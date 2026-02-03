"""
Anthropic Provider

Implementation for Anthropic's Claude API.
"""

import base64
import logging
from typing import Optional, List, Dict, Any, Union

from app.ai.providers.base import AIProvider, AIResponse, AIMessage, ToolCall

logger = logging.getLogger(__name__)


class AnthropicProvider(AIProvider):
    """Anthropic Claude API provider."""

    provider_name = "anthropic"
    supports_tools = True
    supports_vision = True
    supports_json_mode = False  # Anthropic doesn't have explicit JSON mode

    # Available models
    MODELS = [
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
    ]

    # Model aliases for convenience
    MODEL_ALIASES = {
        "claude-3.5-sonnet": "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet": "claude-3-5-sonnet-20241022",
        "claude-3.5-haiku": "claude-3-5-haiku-20241022",
        "claude-3-5-haiku": "claude-3-5-haiku-20241022",
        "claude-3-opus": "claude-3-opus-20240229",
        "claude-3-sonnet": "claude-3-sonnet-20240229",
        "claude-3-haiku": "claude-3-haiku-20240307",
    }

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        super().__init__(api_key, model or "claude-3-5-sonnet-20241022", base_url, **kwargs)

        try:
            import anthropic
            self._anthropic = anthropic
        except ImportError:
            raise ImportError(
                "anthropic package is required for Anthropic provider. "
                "Install it with: pip install anthropic"
            )

        # Create Anthropic client
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = anthropic.Anthropic(**client_kwargs)
        logger.debug(f"Anthropic provider initialized with model: {self.model}")

    def _resolve_model(self, model: Optional[str]) -> str:
        """Resolve model alias to actual model name."""
        use_model = model or self.model
        return self.MODEL_ALIASES.get(use_model, use_model)

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
        """Generate a chat completion using Anthropic Claude."""
        use_model = self._resolve_model(model)

        # Normalize and convert messages
        normalized = self._normalize_messages(messages)

        # Extract system message (Anthropic handles it separately)
        system_message = None
        anthropic_messages = []

        for msg in normalized:
            if msg.role == "system":
                system_message = msg.content
            else:
                anthropic_messages.append(self._convert_message(msg))

        # Build request parameters
        params = {
            "model": use_model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 4096,  # Anthropic requires max_tokens
            "temperature": temperature,
            "top_p": top_p,
        }

        if system_message:
            params["system"] = system_message

        # Convert tools to Anthropic format
        if tools:
            params["tools"] = self._convert_tools(tools)
            if tool_choice:
                params["tool_choice"] = self._convert_tool_choice(tool_choice)

        logger.debug(f"Anthropic request: model={use_model}, messages={len(anthropic_messages)}")

        # Make the API call
        response = self.client.messages.create(**params)

        # Convert response
        content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {}
                ))

        return AIResponse(
            content=content,
            model=response.model,
            finish_reason=self._convert_stop_reason(response.stop_reason),
            usage={
                "prompt_tokens": response.usage.input_tokens if response.usage else 0,
                "completion_tokens": response.usage.output_tokens if response.usage else 0,
                "total_tokens": (response.usage.input_tokens + response.usage.output_tokens) if response.usage else 0,
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
        """Analyze an image using Claude's vision capabilities."""
        use_model = self._resolve_model(model)

        # Prepare image content
        if isinstance(image_data, bytes):
            b64_data = base64.b64encode(image_data).decode('utf-8')
            media_type = "image/jpeg"
        elif image_data.startswith('data:'):
            # Parse data URL
            parts = image_data.split(',', 1)
            if len(parts) == 2:
                media_type = parts[0].split(':')[1].split(';')[0]
                b64_data = parts[1]
            else:
                media_type = "image/jpeg"
                b64_data = image_data
        else:
            # Assume it's base64 data
            media_type = "image/jpeg"
            b64_data = image_data

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]

        response = self.client.messages.create(
            model=use_model,
            messages=messages,
            max_tokens=kwargs.get("max_tokens", 1000)
        )

        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text

        return AIResponse(
            content=content,
            model=response.model,
            finish_reason=self._convert_stop_reason(response.stop_reason),
            usage={
                "prompt_tokens": response.usage.input_tokens if response.usage else 0,
                "completion_tokens": response.usage.output_tokens if response.usage else 0,
                "total_tokens": (response.usage.input_tokens + response.usage.output_tokens) if response.usage else 0,
            },
            raw_response=response
        )

    def _convert_message(self, msg: AIMessage) -> Dict:
        """Convert AIMessage to Anthropic format."""
        if msg.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content
                    }
                ]
            }

        if msg.tool_calls:
            # Assistant message with tool calls
            content = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": tc.get("function", {}).get("name"),
                    "input": self._parse_json_safe(tc.get("function", {}).get("arguments", "{}"))
                })
            return {"role": "assistant", "content": content}

        return {
            "role": msg.role,
            "content": msg.content
        }

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """Convert OpenAI-style tools to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {})
                })
            else:
                # Already in Anthropic format or custom
                anthropic_tools.append(tool)
        return anthropic_tools

    def _convert_tool_choice(self, tool_choice: Union[str, Dict]) -> Dict:
        """Convert tool_choice to Anthropic format."""
        if isinstance(tool_choice, str):
            if tool_choice == "auto":
                return {"type": "auto"}
            elif tool_choice == "none":
                return {"type": "none"}
            elif tool_choice == "required":
                return {"type": "any"}
        elif isinstance(tool_choice, dict):
            if "function" in tool_choice:
                return {"type": "tool", "name": tool_choice["function"]["name"]}
        return {"type": "auto"}

    def _convert_stop_reason(self, stop_reason: str) -> str:
        """Convert Anthropic stop reason to standard format."""
        mapping = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "stop_sequence": "stop",
        }
        return mapping.get(stop_reason, stop_reason)

    def _parse_json_safe(self, json_str: str) -> Dict[str, Any]:
        """Safely parse JSON string."""
        import json
        try:
            if isinstance(json_str, dict):
                return json_str
            return json.loads(json_str)
        except:
            return {"raw": json_str}

    def get_available_models(self) -> List[str]:
        """Return list of available Anthropic models."""
        return self.MODELS + list(self.MODEL_ALIASES.keys())
