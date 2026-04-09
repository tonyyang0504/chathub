"""
Scheduled Report Generator
Auto-generates daily/weekly summary reports and delivers them via connected bots.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import (
    SessionLocal, Hub, BotProfile, Conversation, Message,
    Contact, HubBotMembership, ActivityLog,
)

logger = logging.getLogger(__name__)


def generate_hub_report(hub_id: int, period: str = "daily") -> dict:
    """Generate a summary report for a hub.

    Args:
        hub_id: Hub to report on
        period: "daily" or "weekly"

    Returns:
        Report data dict
    """
    db = SessionLocal()
    try:
        hub = db.query(Hub).filter(Hub.id == hub_id).first()
        if not hub:
            return {"error": "Hub not found"}

        # Time range
        now = datetime.utcnow()
        if period == "weekly":
            start = now - timedelta(days=7)
        else:
            start = now - timedelta(days=1)

        # Get hub bot IDs
        bot_ids = [m.bot_profile_id for m in hub.bot_memberships if m.is_active]
        if not bot_ids:
            return {"error": "No bots in hub"}

        # Message stats
        total_messages = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= start,
        ).scalar() or 0

        incoming = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= start,
            Message.role == "user",
        ).scalar() or 0

        outgoing = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= start,
            Message.role == "assistant",
        ).scalar() or 0

        # New conversations
        new_conversations = db.query(func.count(Conversation.id)).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Conversation.created_at >= start,
        ).scalar() or 0

        # Contact stats
        total_contacts = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub_id,
        ).scalar() or 0

        new_contacts = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub_id,
            Contact.first_seen_at >= start,
        ).scalar() or 0

        follow_up_needed = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub_id,
            Contact.follow_up_needed == True,
        ).scalar() or 0

        # Sentiment breakdown
        positive = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub_id, Contact.sentiment == "positive"
        ).scalar() or 0
        negative = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub_id, Contact.sentiment == "negative"
        ).scalar() or 0
        neutral = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub_id, Contact.sentiment == "neutral"
        ).scalar() or 0

        # Active bots
        active_bots = db.query(func.count(BotProfile.id)).filter(
            BotProfile.id.in_(bot_ids), BotProfile.is_running == True
        ).scalar() or 0

        report = {
            "hub_name": hub.name,
            "period": period,
            "start": start.isoformat() + "Z",
            "end": now.isoformat() + "Z",
            "messages": {
                "total": total_messages,
                "incoming": incoming,
                "outgoing": outgoing,
            },
            "conversations": {
                "new": new_conversations,
            },
            "contacts": {
                "total": total_contacts,
                "new": new_contacts,
                "follow_up_needed": follow_up_needed,
                "sentiment": {
                    "positive": positive,
                    "negative": negative,
                    "neutral": neutral,
                },
            },
            "bots": {
                "total": len(bot_ids),
                "active": active_bots,
            },
        }

        return report
    finally:
        db.close()


def format_report_text(report: dict) -> str:
    """Format report data as a human-readable text message."""
    if "error" in report:
        return f"Report error: {report['error']}"

    period_label = "Daily" if report["period"] == "daily" else "Weekly"
    m = report["messages"]
    c = report["contacts"]
    b = report["bots"]

    lines = [
        f"📊 *{period_label} Report — {report['hub_name']}*",
        "",
        f"💬 Messages: {m['total']} ({m['incoming']} in, {m['outgoing']} out)",
        f"🆕 New conversations: {report['conversations']['new']}",
        "",
        f"👥 Contacts: {c['total']} total, {c['new']} new",
        f"📞 Need follow-up: {c['follow_up_needed']}",
        f"😊 Sentiment: +{c['sentiment']['positive']} / ={c['sentiment']['neutral']} / -{c['sentiment']['negative']}",
        "",
        f"🤖 Bots: {b['active']}/{b['total']} active",
    ]

    return "\n".join(lines)
