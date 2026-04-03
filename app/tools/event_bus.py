"""
Tool Event Bus

A publish/subscribe system that connects system events to custom tool hook functions.
Custom tools declare event subscriptions in their TOOL.md frontmatter. The core system
emits events at key points (message received, bot started, contact created, etc.).
The event bus routes events to subscribed tool handlers.

Usage:
    # Core code emits events (one line):
    await tool_event_bus.emit("message.received", db=db, bot_profile_id=1, conversation=conv, message=msg)

    # Custom tools subscribe via TOOL.md:
    # events:
    #   message.received: check_keywords
    #   contact.created: welcome_contact
"""

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Dict, List, Any

logger = logging.getLogger(__name__)


class ToolEventBus:
    """Lightweight async event bus for custom tool hooks."""

    def __init__(self):
        self._subscribers: Dict[str, List[dict]] = defaultdict(list)
        # Each subscriber: {"tool_name": str, "handler": Callable, "module_path": str}

    def subscribe(self, event: str, tool_name: str, handler: Callable, module_path: str = ""):
        """Register a tool's handler function for an event.

        Args:
            event: Event name (e.g., "message.received")
            tool_name: Tool's kebab-case name (for logging)
            handler: Async function to call when event fires
            module_path: Module path for debugging
        """
        self._subscribers[event].append({
            "tool_name": tool_name,
            "handler": handler,
            "module_path": module_path,
        })
        logger.info(f"Tool '{tool_name}' subscribed to event '{event}'")

    def unsubscribe_tool(self, tool_name: str):
        """Remove all subscriptions for a tool (used when tool is deactivated)."""
        for event in list(self._subscribers.keys()):
            self._subscribers[event] = [
                s for s in self._subscribers[event] if s["tool_name"] != tool_name
            ]

    async def emit(self, event: str, **kwargs):
        """Emit an event to all subscribed handlers.

        Catches all exceptions — tool errors never break core system flow.
        Runs handlers sequentially to avoid DB session conflicts.

        Args:
            event: Event name (e.g., "message.received")
            **kwargs: Event data passed to handlers (db, bot_profile_id, etc.)
        """
        subscribers = self._subscribers.get(event, [])
        if not subscribers:
            return

        for sub in subscribers:
            try:
                handler = sub["handler"]
                if asyncio.iscoroutinefunction(handler):
                    await handler(**kwargs)
                else:
                    handler(**kwargs)
            except Exception as e:
                logger.error(
                    f"Tool '{sub['tool_name']}' handler for '{event}' failed: {e}",
                    exc_info=True
                )
                # Log to ToolMonitor if db is available
                try:
                    db = kwargs.get("db")
                    if db:
                        from app.tools.monitoring import ToolMonitor
                        ToolMonitor.log_execution(
                            db=db,
                            tool_type=sub["tool_name"],
                            operation=f"hook:{event}",
                            status="error",
                            error_message=str(e)[:500],
                        )
                except Exception:
                    pass  # Don't let logging errors cascade

    def get_subscribers(self, event: str) -> List[dict]:
        """Get all subscribers for an event (for debugging/admin)."""
        return self._subscribers.get(event, [])

    def get_all_events(self) -> List[str]:
        """Get all events that have subscribers."""
        return [e for e, subs in self._subscribers.items() if subs]

    def get_stats(self) -> dict:
        """Get subscriber statistics."""
        return {
            "total_events": len(self._subscribers),
            "total_subscribers": sum(len(s) for s in self._subscribers.values()),
            "events": {e: len(s) for e, s in self._subscribers.items() if s},
        }


# Singleton instance
tool_event_bus = ToolEventBus()
