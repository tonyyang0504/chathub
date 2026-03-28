"""
Google Provider

Implementation for Google's Gemini API using the google-genai SDK.
"""

import base64
import logging
from typing import Optional, List, Dict, Any, Union

from app.ai.providers.base import AIProvider, AIResponse, AIMessage, ToolCall, create_retry_decorator

logger = logging.getLogger(__name__)


class GoogleProvider(AIProvider):
    """Google Gemini API provider using google-genai SDK."""

    provider_name = "google"
    supports_tools = True
    supports_vision = True
    supports_json_mode = True

    # Available models
    MODELS = [
        "gemini-3.1-pro-preview", "gemini-3.1-flash-lite-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro", "gemini-2.5-flash",
        "gemini-2.0-flash", "gemini-2.0-flash-lite",
    ]

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        super().__init__(api_key, model or "gemini-2.0-flash", base_url, **kwargs)

        try:
            from google import genai
            from google.genai import types
            self._genai = genai
            self._types = types
            self._client = genai.Client(api_key=api_key)
        except ImportError:
            raise ImportError(
                "google-genai package is required for Google provider. "
                "Install it with: pip install google-genai"
            )

        logger.debug(f"Google provider initialized with model: {self.model}")

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
        """Generate a chat completion using Google Gemini."""
        use_model = model or self.model

        # Normalize messages
        normalized = self._normalize_messages(messages)

        # Convert messages to Gemini format
        gemini_contents, system_instruction = self._convert_messages(normalized)

        # Build generation config
        config_kwargs = {
            "temperature": temperature,
            "top_p": top_p,
        }

        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens

        if frequency_penalty:
            config_kwargs["frequency_penalty"] = frequency_penalty

        if presence_penalty:
            config_kwargs["presence_penalty"] = presence_penalty

        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        # Convert tools if present
        if tools:
            gemini_tools = self._convert_tools(tools)
            if gemini_tools:
                config_kwargs["tools"] = gemini_tools

        config = self._types.GenerateContentConfig(**config_kwargs)

        logger.debug(f"Gemini request: model={use_model}, messages={len(gemini_contents)}")

        response = self._make_api_call(use_model, gemini_contents, config)

        # Extract content and tool calls
        content = ""
        tool_calls = []

        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.text:
                    content += part.text
                if part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=f"call_{len(tool_calls)}",
                        name=fc.name,
                        arguments=dict(fc.args) if fc.args else {}
                    ))

        # Token usage
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        if response.usage_metadata:
            usage["prompt_tokens"] = response.usage_metadata.prompt_token_count or 0
            usage["completion_tokens"] = response.usage_metadata.candidates_token_count or 0
            usage["total_tokens"] = response.usage_metadata.total_token_count or 0

        return AIResponse(
            content=content,
            model=use_model,
            finish_reason=self._get_finish_reason(response),
            usage=usage,
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
        """Analyze an image using Gemini's vision capabilities."""
        use_model = model or "gemini-2.0-flash"

        # Prepare image part
        if isinstance(image_data, bytes):
            image_part = self._types.Part.from_bytes(data=image_data, mime_type="image/jpeg")
        elif image_data.startswith('data:'):
            parts = image_data.split(',', 1)
            if len(parts) == 2:
                mime_type = parts[0].split(':')[1].split(';')[0]
                b64_data = parts[1]
            else:
                mime_type = "image/jpeg"
                b64_data = image_data
            image_part = self._types.Part.from_bytes(
                data=base64.b64decode(b64_data), mime_type=mime_type
            )
        else:
            image_part = self._types.Part.from_bytes(
                data=base64.b64decode(image_data), mime_type="image/jpeg"
            )

        response = self._client.models.generate_content(
            model=use_model,
            contents=[prompt, image_part],
        )

        content = ""
        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.text:
                    content += part.text

        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        if response.usage_metadata:
            usage["prompt_tokens"] = response.usage_metadata.prompt_token_count or 0
            usage["completion_tokens"] = response.usage_metadata.candidates_token_count or 0
            usage["total_tokens"] = response.usage_metadata.total_token_count or 0

        return AIResponse(
            content=content,
            model=use_model,
            finish_reason=self._get_finish_reason(response),
            usage=usage,
            raw_response=response
        )

    def _convert_messages(self, messages: List[AIMessage]) -> tuple:
        """Convert messages to Gemini format and extract system instruction."""
        system_instruction = None
        gemini_contents = []

        for msg in messages:
            if msg.role == "system":
                system_instruction = msg.content
            elif msg.role == "user":
                gemini_contents.append(
                    self._types.Content(role="user", parts=[self._types.Part.from_text(text=msg.content)])
                )
            elif msg.role == "assistant":
                gemini_contents.append(
                    self._types.Content(role="model", parts=[self._types.Part.from_text(text=msg.content)])
                )
            elif msg.role == "tool":
                gemini_contents.append(
                    self._types.Content(
                        role="user",
                        parts=[self._types.Part(function_response=self._types.FunctionResponse(
                            name=msg.name or "tool",
                            response={"result": msg.content}
                        ))]
                    )
                )

        return gemini_contents, system_instruction

    def _convert_tools(self, tools: List[Dict]) -> List:
        """Convert OpenAI-style tools to Gemini format."""
        declarations = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                declarations.append(self._types.FunctionDeclaration(
                    name=func.get("name"),
                    description=func.get("description", ""),
                    parameters=func.get("parameters", {}),
                ))
        if declarations:
            return [self._types.Tool(function_declarations=declarations)]
        return None

    def _get_finish_reason(self, response) -> str:
        """Get finish reason from Gemini response."""
        if response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                reason = str(candidate.finish_reason)
                if "STOP" in reason:
                    return "stop"
                elif "MAX_TOKENS" in reason:
                    return "length"
                elif "SAFETY" in reason:
                    return "content_filter"
        return "stop"

    @create_retry_decorator(max_attempts=3)
    def _make_api_call(self, model: str, contents, config) -> Any:
        """Make API call with retry logic."""
        try:
            return self._client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            error_str = str(e).lower()
            if "rate" in error_str or "quota" in error_str or "capacity" in error_str:
                logger.error(f"Google rate limit exceeded: {e}")
                raise
            elif "connection" in error_str or "network" in error_str:
                logger.error(f"Google connection error: {e}")
                raise ConnectionError(str(e))
            elif "timeout" in error_str:
                logger.error(f"Google timeout error: {e}")
                raise TimeoutError(str(e))
            raise

    def get_available_models(self) -> List[str]:
        """Return list of available Google models."""
        return self.MODELS
