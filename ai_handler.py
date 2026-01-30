"""
AI Handler for WhatsApp Bot.
Manages OpenAI API integration and response generation.
"""

import logging
from typing import List, Dict, Optional
from openai import OpenAI

from config import Config

logger = logging.getLogger(__name__)


class AIHandler:
    """Handles AI response generation using OpenAI API."""

    def __init__(
        self,
        system_prompt: str = None,
        api_key: str = None,
        model: str = None
    ):
        """
        Initialize AI handler.

        Args:
            system_prompt: Custom system prompt (role definition)
            api_key: OpenAI API key (defaults to config)
            model: Model to use (defaults to config)
        """
        self.api_key = api_key or Config.OPENAI_API_KEY
        self.model = model or Config.OPENAI_MODEL
        self.system_prompt = system_prompt or Config.SYSTEM_PROMPT
        self.max_tokens = Config.MAX_TOKENS
        self.temperature = Config.TEMPERATURE

        # Initialize OpenAI client
        self.client = OpenAI(api_key=self.api_key)

    def set_system_prompt(self, prompt: str):
        """
        Update the system prompt at runtime.

        Args:
            prompt: New system prompt
        """
        self.system_prompt = prompt
        logger.info("System prompt updated")

    def generate_response(
        self,
        messages: List[Dict],
        system_prompt: str = None
    ) -> str:
        """
        Generate AI response based on conversation history.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional override for system prompt

        Returns:
            Generated response text
        """
        try:
            # Build messages list with system prompt
            prompt = system_prompt or self.system_prompt
            full_messages = [
                {"role": "system", "content": prompt}
            ]
            full_messages.extend(messages)

            logger.debug(f"Sending {len(full_messages)} messages to OpenAI")

            # Call OpenAI API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature
            )

            # Extract response text
            result = response.choices[0].message.content
            logger.debug(f"Received response: {result[:100]}...")

            return result

        except Exception as e:
            logger.error(f"Error generating response: {e}")
            raise

    def generate_response_with_context(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None
    ) -> str:
        """
        Generate response with optional conversation context.

        Args:
            user_message: Current user message
            conversation_history: Previous messages (optional)
            system_prompt: Optional override for system prompt

        Returns:
            Generated response text
        """
        messages = conversation_history or []

        # Add current user message if not already in history
        if not messages or messages[-1].get("content") != user_message:
            messages.append({
                "role": "user",
                "content": user_message
            })

        return self.generate_response(messages, system_prompt)

    def moderate_content(self, text: str) -> Dict:
        """
        Check if content violates OpenAI's usage policies.

        Args:
            text: Text to check

        Returns:
            Moderation result dictionary
        """
        try:
            response = self.client.moderations.create(input=text)
            result = response.results[0]

            return {
                "flagged": result.flagged,
                "categories": result.categories.model_dump(),
                "scores": result.category_scores.model_dump()
            }

        except Exception as e:
            logger.error(f"Error moderating content: {e}")
            return {"flagged": False, "error": str(e)}

    def count_tokens(self, text: str) -> int:
        """
        Estimate token count for text.
        This is a rough estimate - actual count may vary.

        Args:
            text: Text to count tokens for

        Returns:
            Estimated token count
        """
        # Rough estimate: ~4 characters per token for English
        return len(text) // 4


class AIHandlerWithFunctions(AIHandler):
    """Extended AI handler with function calling support."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.functions = []

    def register_function(self, function_def: Dict):
        """
        Register a function for function calling.

        Args:
            function_def: Function definition dictionary
        """
        self.functions.append(function_def)

    def generate_response_with_functions(
        self,
        messages: List[Dict],
        system_prompt: str = None
    ) -> Dict:
        """
        Generate response with function calling support.

        Args:
            messages: Conversation messages
            system_prompt: Optional system prompt override

        Returns:
            Response dict with 'content' and optionally 'function_call'
        """
        try:
            prompt = system_prompt or self.system_prompt
            full_messages = [
                {"role": "system", "content": prompt}
            ]
            full_messages.extend(messages)

            kwargs = {
                "model": self.model,
                "messages": full_messages,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature
            }

            if self.functions:
                kwargs["tools"] = [
                    {"type": "function", "function": f}
                    for f in self.functions
                ]

            response = self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]

            result = {
                "content": choice.message.content,
                "finish_reason": choice.finish_reason
            }

            if choice.message.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in choice.message.tool_calls
                ]

            return result

        except Exception as e:
            logger.error(f"Error generating response with functions: {e}")
            raise
