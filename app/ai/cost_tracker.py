"""
AI Cost Tracker

Tracks token usage and estimates costs for all AI API calls.
Pricing is per 1M tokens (input/output) in USD.

Usage context is set via thread-local storage so providers can auto-log:
    from app.ai.cost_tracker import usage_context
    usage_context.set(user_id=1, bot_id=5, source='bot_chat')
    response = ai_provider.chat_completion(...)
    # Usage is automatically logged after the call
"""

import logging
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class _UsageContext(threading.local):
    """Thread-local context for AI usage tracking."""

    def __init__(self):
        super().__init__()
        self.user_id = None
        self.bot_id = None
        self.hub_id = None
        self.agent_id = None
        self.source = None
        self.operation = None

    def set(self, user_id=None, bot_id=None, hub_id=None, agent_id=None, source=None, operation=None):
        self.user_id = user_id
        self.bot_id = bot_id
        self.hub_id = hub_id
        self.agent_id = agent_id
        self.source = source
        self.operation = operation

    def clear(self):
        self.user_id = None
        self.bot_id = None
        self.hub_id = None
        self.agent_id = None
        self.source = None
        self.operation = None


usage_context = _UsageContext()

# Pricing per 1M tokens (input_price, output_price) in USD
# Updated pricing as of early 2026
MODEL_PRICING = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1-preview": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (1.10, 4.40),
    "gpt-5": (2.50, 10.00),
    "gpt-5-mini": (0.15, 0.60),
    # Anthropic
    "claude-opus-4-20250514": (15.00, 75.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-3-sonnet-20240229": (3.00, 15.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
    # Google
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
    # DeepSeek
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
    # Qwen
    "qwen-turbo": (0.30, 0.60),
    "qwen-plus": (0.80, 2.00),
    "qwen-max": (2.40, 9.60),
    # xAI Grok
    "grok-2": (2.00, 10.00),
    "grok-2-mini": (0.30, 0.50),
}

# Fallback pricing per provider (conservative estimates)
PROVIDER_FALLBACK_PRICING = {
    "openai": (2.50, 10.00),
    "anthropic": (3.00, 15.00),
    "google": (0.075, 0.30),
    "gemini": (0.075, 0.30),
    "deepseek": (0.14, 0.28),
    "qwen": (0.30, 0.60),
    "dashscope": (0.30, 0.60),
    "grok": (2.00, 10.00),
}


def calculate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate estimated cost in USD for a given API call."""
    # Try exact model match first
    pricing = MODEL_PRICING.get(model)

    # Try prefix match (e.g., "gpt-4o-2024-08-06" matches "gpt-4o")
    if not pricing:
        for model_prefix, price in MODEL_PRICING.items():
            if model.startswith(model_prefix):
                pricing = price
                break

    # Fallback to provider default
    if not pricing:
        pricing = PROVIDER_FALLBACK_PRICING.get(provider.lower(), (1.00, 3.00))

    input_price_per_token = pricing[0] / 1_000_000
    output_price_per_token = pricing[1] / 1_000_000

    return (prompt_tokens * input_price_per_token) + (completion_tokens * output_price_per_token)


def log_ai_usage(
    user_id: int,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    operation: Optional[str] = None,
    source: Optional[str] = None,
    bot_id: Optional[int] = None,
    hub_id: Optional[int] = None,
    agent_id: Optional[int] = None,
):
    """
    Log an AI API call's token usage and estimated cost.
    Runs in a separate thread-safe DB session to avoid conflicts.
    """
    try:
        from app.database import SessionLocal, AIUsage

        cost = calculate_cost(provider, model, prompt_tokens, completion_tokens)

        db = SessionLocal()
        try:
            usage = AIUsage(
                user_id=user_id,
                bot_id=bot_id,
                hub_id=hub_id,
                agent_id=agent_id,
                provider=provider,
                model=model,
                operation=operation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
                source=source,
                created_at=datetime.utcnow(),
            )
            db.add(usage)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to log AI usage: {e}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Failed to log AI usage (outer): {e}")
