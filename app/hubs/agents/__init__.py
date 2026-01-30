"""
AI Agents Package

This package contains AI agents for hub coordination:
- classifier: Message classification agent
- router: Response routing agent
- generator: Content generation agent
- scheduler: Message scheduling agent
- analyzer: Contact analysis agent
- followup: Follow-up agent
"""

from app.hubs.agents.base import BaseAgent

__all__ = ["BaseAgent"]
