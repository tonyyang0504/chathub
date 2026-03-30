from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Contact, ContactTag, Hub, Message, Conversation, HubBotMembership, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["contact-insights"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _get_user_hub_scope(user, db: Session, hub_id: Optional[int] = None):
    """Return scoped hub IDs and all user hubs for selector."""
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], []
    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)
    scoped = [hub_id] if hub_id else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name).all()
    return scoped, hubs


def _build_stats(db: Session, hub_ids: list[int]) -> dict:
    """Build contact analytics for given hub IDs."""
    if not hub_ids:
        return {
            "totals": {"contacts": 0, "follow_up": 0, "high_urgency": 0, "negative": 0, "tags": 0},
            "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
            "urgency": {"high": 0, "medium": 0, "low": 0},
            "top_engaged": [],
            "needs_attention": [],
            "recent_contacts": [],
            "top_tags": [],
            "engagement_distribution": {"high": 0, "medium": 0, "low": 0, "none": 0},
        }

    contacts = db.query(Contact).filter(Contact.hub_id.in_(hub_ids)).all()
    total = len(contacts)

    # Sentiment counts
    sentiment = {"positive": 0, "neutral": 0, "negative": 0}
    urgency = {"high": 0, "medium": 0, "low": 0}
    follow_up = 0
    high_urgency = 0
    negative = 0
    engagement_dist = {"high": 0, "medium": 0, "low": 0, "none": 0}

    for c in contacts:
        s = (c.sentiment or "").lower()
        if s in sentiment:
            sentiment[s] += 1
        if s == "negative":
            negative += 1

        u = (c.urgency or "").lower()
        if u in urgency:
            urgency[u] += 1
        if u == "high":
            high_urgency += 1

        if c.follow_up_needed:
            follow_up += 1

        score = c.engagement_score or 0
        if score >= 70:
            engagement_dist["high"] += 1
        elif score >= 40:
            engagement_dist["medium"] += 1
        elif score > 0:
            engagement_dist["low"] += 1
        else:
            engagement_dist["none"] += 1

    # Tag count
    tag_count = (
        db.query(func.count(ContactTag.id))
        .join(Contact, ContactTag.contact_id == Contact.id)
        .filter(Contact.hub_id.in_(hub_ids))
        .scalar()
    ) or 0

    # Top tags
    top_tags = (
        db.query(ContactTag.tag, func.count(ContactTag.id).label("cnt"))
        .join(Contact, ContactTag.contact_id == Contact.id)
        .filter(Contact.hub_id.in_(hub_ids))
        .group_by(ContactTag.tag)
        .order_by(func.count(ContactTag.id).desc())
        .limit(8)
        .all()
    )

    # Top engaged contacts
    top_engaged = sorted(
        [c for c in contacts if (c.engagement_score or 0) > 0],
        key=lambda c: c.engagement_score or 0,
        reverse=True,
    )[:6]

    # Contacts needing attention: follow_up_needed or high urgency or negative sentiment
    needs_attention = [
        c for c in contacts
        if c.follow_up_needed or (c.urgency or "").lower() == "high" or (c.sentiment or "").lower() == "negative"
    ]
    # Sort: high urgency first, then negative sentiment, then follow-up
    def attention_priority(c):
        score = 0
        if (c.urgency or "").lower() == "high":
            score += 3
        if (c.sentiment or "").lower() == "negative":
            score += 2
        if c.follow_up_needed:
            score += 1
        return -score
    needs_attention.sort(key=attention_priority)
    needs_attention = needs_attention[:8]

    # Recent contacts (most recently seen)
    recent_contacts = sorted(
        [c for c in contacts if c.last_interaction_at],
        key=lambda c: c.last_interaction_at,
        reverse=True,
    )[:6]

    # Hub name lookup
    hub_map = {h.id: h.name for h in db.query(Hub).filter(Hub.id.in_(hub_ids)).all()}

    def serialize_contact(c):
        return {
            "id": c.id,
            "name": c.display_name or c.phone or "Unknown",
            "phone": c.phone,
            "hub_name": hub_map.get(c.hub_id, ""),
            "sentiment": c.sentiment,
            "urgency": c.urgency,
            "engagement_score": round(c.engagement_score or 0, 1),
            "follow_up_needed": c.follow_up_needed,
            "follow_up_reason": c.follow_up_reason,
            "predicted_intent": c.predicted_intent,
            "last_interaction": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
        }

    return {
        "totals": {
            "contacts": total,
            "follow_up": follow_up,
            "high_urgency": high_urgency,
            "negative": negative,
            "tags": tag_count,
        },
        "sentiment": sentiment,
        "urgency": urgency,
        "engagement_distribution": engagement_dist,
        "top_engaged": [serialize_contact(c) for c in top_engaged],
        "needs_attention": [serialize_contact(c) for c in needs_attention],
        "recent_contacts": [serialize_contact(c) for c in recent_contacts],
        "top_tags": [{"tag": t.tag, "count": t.cnt} for t in top_tags],
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _get_user_hub_scope(user, db)
    return templates.TemplateResponse("contact-insights.html", {
        "request": request,
        "user": user,
        "active_page": "tools_contact_insights",
        "page_title": "Contact Insights",
        "hubs": hubs,
    })


@router.get("/api/stats")
async def get_stats(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db, hub_id)
    stats = _build_stats(db, scoped)

    ToolMonitor.log_execution(
        db=db,
        tool_type="contact-insights",
        operation="fetch_stats",
        hub_id=hub_id,
        input_data={},
        output_data={
            "contacts": stats["totals"]["contacts"],
            "follow_up": stats["totals"]["follow_up"],
            "negative": stats["totals"]["negative"],
        },
        user_id=user.id,
    )

    return stats


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db)
    stats = _build_stats(db, scoped)

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "totals": stats["totals"],
        "sentiment": stats["sentiment"],
        "needs_attention": stats["needs_attention"][:3],
    })
