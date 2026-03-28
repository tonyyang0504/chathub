"""
Google Provider

Implementation for Google's Gemini API.
"""

import base64
import logging
from typing import Optional, List, Dict, Any, Union

from app.ai.providers.base import AIProvider, AIResponse, AIMessage, ToolCall, create_retry_decorator

logger = logging.getLogger(__name__)


class GoogleProvider(AIProvider):
    """Google Gemini API provider."""

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
        "gemini-1.5-pro", "gemini-1.5-flash",
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
            import google.generativeai as genai
            self._genai = genai
        except ImportError:
            raise ImportError(
                "google-generativeai package is required for Google provider. "
                "Install it with: pip install google-generativeai"
            )

        # Configure the API
        genai.configure(api_key=api_key)
        self._model_instance = None
        logger.debug(f"Google provider initialized with model: {self.model}")

    def _get_model(self, model: Optional[str] = None):
        """Get or create a GenerativeModel instance."""
        use_model = model or self.model
        if self._model_instance is None or self._model_instance.model_name != use_model:
            self._model_instance = self._genai.GenerativeModel(use_model)
        return self._model_instance

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
        genai_model = self._get_model(use_model)

        # Normalize messages
        normalized = self._normalize_messages(messages)

        # Convert messages to Gemini format
        gemini_messages, system_instruction = self._convert_messages(normalized)

        # Build generation config
        generation_config = {
            "temperature": temperature,
            "top_p": top_p,
        }

        if max_tokens:
            generation_config["max_output_tokens"] = max_tokens

        if json_mode:
            generation_config["response_mime_type"] = "application/json"

        # Create model with system instruction if present
        if system_instruction:
            genai_model = self._genai.GenerativeModel(
                use_model,
                system_instruction=system_instruction
            )

        # Convert tools if present
        gemini_tools = None
        if tools:
            gemini_tools = self._convert_tools(tools)

        logger.debug(f"Gemini request: model={use_model}, messages={len(gemini_messages)}")

        # Start chat and send message with retry logic
        chat = genai_model.start_chat(history=gemini_messages[:-1] if len(gemini_messages) > 1 else [])

        response = self._make_api_call(
            chat,
            gemini_messages[-1] if gemini_messages else "",
            generation_config,
            gemini_tools
        )

        # Extract content and tool calls
        content = ""
        tool_calls = []

        for candidate in response.candidates:
            for part in candidate.content.parts:
                if hasattr(part, 'text') and part.text:
                    content += part.text
                if hasattr(part, 'function_call') and part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=f"call_{len(tool_calls)}",  # Gemini doesn't provide IDs
                        name=fc.name,
                        arguments=dict(fc.args) if fc.args else {}
                    ))

        # Estimate token usage (Gemini doesn't always provide this)
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage["prompt_tokens"] = getattr(response.usage_metadata, 'prompt_token_count', 0)
            usage["completion_tokens"] = getattr(response.usage_metadata, 'candidates_token_count', 0)
            usage["total_tokens"] = getattr(response.usage_metadata, 'total_token_count', 0)

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
        genai_model = self._genai.GenerativeModel(use_model)

        # Prepare image
        if isinstance(image_data, bytes):
            image_part = {
                "mime_type": "image/jpeg",
                "data": image_data
            }
        elif image_data.startswith('data:'):
            # Parse data URL
            parts = image_data.split(',', 1)
            if len(parts) == 2:
                mime_type = parts[0].split(':')[1].split(';')[0]
                b64_data = parts[1]
            else:
                mime_type = "image/jpeg"
                b64_data = image_data
            image_part = {
                "mime_type": mime_type,
                "data": base64.b64decode(b64_data)
            }
        else:
            # Assume it's base64 data
            image_part = {
                "mime_type": "image/jpeg",
                "data": base64.b64decode(image_data)
            }

        response = genai_model.generate_content([prompt, image_part])

        content = ""
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if hasattr(part, 'text') and part.text:
                    content += part.text

        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage["prompt_tokens"] = getattr(response.usage_metadata, 'prompt_token_count', 0)
            usage["completion_tokens"] = getattr(response.usage_metadata, 'candidates_token_count', 0)
            usage["total_tokens"] = getattr(response.usage_metadata, 'total_token_count', 0)

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
        gemini_messages = []

        for msg in messages:
            if msg.role == "system":
                system_instruction = msg.content
            elif msg.role == "user":
                gemini_messages.append({"role": "user", "parts": [msg.content]})
            elif msg.role == "assistant":
                gemini_messages.append({"role": "model", "parts": [msg.content]})
            elif msg.role == "tool":
                # Tool responses go as user messages
                gemini_messages.append({
                    "role": "user",
                    "parts": [{"function_response": {"name": msg.name, "response": {"result": msg.content}}}]
                })

        return gemini_messages, system_instruction

    def _convert_tools(self, tools: List[Dict]) -> List:
        """Convert OpenAI-style tools to Gemini format."""
        gemini_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                gemini_tools.append({
                    "function_declarations": [{
                        "name": func.get("name"),
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {})
                    }]
                })
        return gemini_tools if gemini_tools else None

    def _get_finish_reason(self, response) -> str:
        """Get finish reason from Gemini response."""
        if hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'finish_reason'):
                reason = str(candidate.finish_reason)
                if "STOP" in reason:
                    return "stop"
                elif "MAX_TOKENS" in reason:
                    return "length"
                elif "SAFETY" in reason:
                    return "content_filter"
        return "stop"

    @create_retry_decorator(max_attempts=3)
    def _make_api_call(self, chat, message, generation_config, tools) -> Any:
        """Make API call with retry logic."""
        try:
            return chat.send_message(
                message,
                generation_config=self._genai.GenerationConfig(**generation_config),
                tools=tools
            )
        except Exception as e:
            error_str = str(e).lower()
            if "rate" in error_str or "quota" in error_str:
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
