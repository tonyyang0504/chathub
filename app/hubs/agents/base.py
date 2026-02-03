"""
Base Agent Class

Abstract base class for all AI agents in the hub system.
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Dict, Any

from app.database import AIAgent, AgentExecution, get_db_session
from app.auth.utils import decrypt_string

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Base class for all AI agents."""

    def __init__(
        self,
        agent: AIAgent,
        hub_api_key: Optional[str] = None,
        hub_ai_provider: Optional[str] = None
    ):
        """
        Initialize the agent.

        Args:
            agent: The AIAgent database model
            hub_api_key: Encrypted API key from the hub (fallback)
            hub_ai_provider: AI provider from hub (fallback)
        """
        self.agent = agent
        self.agent_id = agent.id
        self.name = agent.name
        self.agent_type = agent.agent_type

        # Get API key (agent-specific or hub default)
        api_key = None
        if agent.openai_api_key_encrypted:
            api_key = decrypt_string(agent.openai_api_key_encrypted)
        elif hub_api_key:
            api_key = decrypt_string(hub_api_key)

        if not api_key:
            raise ValueError(f"No API key available for agent {self.name}")

        # Determine AI provider (agent-specific or hub default or 'openai')
        ai_provider = agent.ai_provider or hub_ai_provider or "openai"
        self.model = agent.openai_model or "gpt-4o-mini"

        # Create AI provider instance
        try:
            from app.ai import get_ai_provider
            self.provider = get_ai_provider(
                provider_name=ai_provider,
                api_key=api_key,
                model=self.model
            )
            logger.debug(f"Agent {self.name} initialized with {ai_provider} provider")
        except Exception as e:
            logger.warning(f"Failed to create {ai_provider} provider for agent {self.name}: {e}. Falling back to OpenAI.")
            # Fallback to OpenAI if provider fails
            from openai import OpenAI
            self._fallback_client = OpenAI(api_key=api_key)
            self.provider = None

        # Load config
        self.config = {}
        if agent.config:
            try:
                self.config = json.loads(agent.config)
            except:
                pass

        # System prompt
        self.system_prompt = agent.system_prompt or self._default_system_prompt()

    @abstractmethod
    def _default_system_prompt(self) -> str:
        """Return the default system prompt for this agent type."""
        pass

    @abstractmethod
    def process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process input and return result.

        Args:
            input_data: Input data for the agent

        Returns:
            Agent output/decision
        """
        pass

    def run(self, input_data: Dict[str, Any], trigger_type: str = "manual") -> Dict[str, Any]:
        """
        Run the agent and log execution.

        Args:
            input_data: Input data for the agent
            trigger_type: What triggered this run ('message', 'schedule', 'manual')

        Returns:
            Agent output/decision
        """
        start_time = time.time()
        tokens_used = 0
        output_data = {}
        status = "success"
        error_message = None

        try:
            output_data = self.process(input_data)
        except Exception as e:
            status = "error"
            error_message = str(e)
            output_data = {"error": str(e)}

        execution_time_ms = int((time.time() - start_time) * 1000)

        # Log execution
        self._log_execution(
            trigger_type=trigger_type,
            input_data=input_data,
            output_data=output_data,
            tokens_used=tokens_used,
            execution_time_ms=execution_time_ms,
            status=status,
            error_message=error_message
        )

        # Update agent last_run_at
        self._update_last_run()

        return output_data

    def _call_ai(
        self,
        messages: list,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        response_format: Optional[Dict] = None
    ) -> tuple[str, int]:
        """
        Call AI provider (multi-provider support).

        Args:
            messages: List of message dicts
            temperature: Sampling temperature
            max_tokens: Max response tokens
            response_format: Optional response format (e.g., {"type": "json_object"})

        Returns:
            Tuple of (response content, tokens used)
        """
        json_mode = response_format and response_format.get("type") == "json_object"

        if self.provider:
            # Use the AI provider abstraction
            response = self.provider.chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode
            )
            return response.content, response.usage.get("total_tokens", 0)
        else:
            # Fallback to direct OpenAI client
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens
            }

            if response_format:
                kwargs["response_format"] = response_format

            response = self._fallback_client.chat.completions.create(**kwargs)

            content = response.choices[0].message.content
            tokens = response.usage.total_tokens if response.usage else 0

            return content, tokens

    # Alias for backward compatibility
    def _call_openai(
        self,
        messages: list,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        response_format: Optional[Dict] = None
    ) -> tuple[str, int]:
        """Alias for _call_ai for backward compatibility."""
        return self._call_ai(messages, temperature, max_tokens, response_format)

    def _log_execution(
        self,
        trigger_type: str,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        tokens_used: int,
        execution_time_ms: int,
        status: str,
        error_message: Optional[str]
    ):
        """Log agent execution to database."""
        try:
            with get_db_session() as db:
                execution = AgentExecution(
                    agent_id=self.agent_id,
                    trigger_type=trigger_type,
                    input_data=json.dumps(input_data),
                    output_data=json.dumps(output_data),
                    tokens_used=tokens_used,
                    execution_time_ms=execution_time_ms,
                    status=status,
                    error_message=error_message
                )
                db.add(execution)
        except Exception as e:
            print(f"Failed to log agent execution: {e}")

    def _update_last_run(self):
        """Update agent's last_run_at timestamp."""
        try:
            with get_db_session() as db:
                agent = db.query(AIAgent).filter(AIAgent.id == self.agent_id).first()
                if agent:
                    agent.last_run_at = datetime.utcnow()
        except Exception as e:
            print(f"Failed to update agent last_run_at: {e}")
