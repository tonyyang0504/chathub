from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Contact, ContactTag, Conversation, Hub, Message, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["contact-scoring-engine"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])

# Scoring weights (must sum to 1.0)
WEIGHTS = {
    "sentiment": 0.25,
    "urgency": 0.20,
    "engagement": 0.20,
    "recency": 0.15,
    "frequency": 0.10,
    "followup": 0.10,
}

# Tier thresholds
TIERS = [
    ("Hot", 75, "#ef4444"),
    ("Warm", 50, "#f59e0b"),
    ("Cool", 25, "#3b82f6"),
    ("Cold", 0, "#6b7280"),
]


def _get_tier(score: float) -> dict:
    for name, threshold, color in TIERS:
        if score >= threshold:
            return {"name": name, "color": color}
    return {"name": "Cold", "color": "#6b7280"}


def _get_user_hub_scope(user, db: Session, hub_id: Optional[int] = None):
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], []
    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)
    scoped = [hub_id] if hub_id else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name).all()
    return scoped, hubs


def _score_sentiment(value: Optional[str]) -> float:
    """Positive=100, Neutral=50, Negative=80 (high because needs attention), None=30."""
    mapping = {"positive": 100, "neutral": 50, "negative": 80}
    return mapping.get((value or "").lower(), 30)


def _score_urgency(value: Optional[str]) -> float:
    """High=100, Medium=60, Low=20, None=10."""
    mapping = {"high": 100, "medium": 60, "low": 20}
    return mapping.get((value or "").lower(), 10)


def _score_engagement(value: Optional[float]) -> float:
    """Direct mapping, capped at 100."""
    return min(max(value or 0, 0), 100)


def _score_recency(last_interaction: Optional[datetime]) -> float:
    """More recent = higher score. Today=100, 1d=90, 3d=70, 7d=50, 14d=30, 30d+=10."""
    if not last_interaction:
        return 0
    days = (datetime.utcnow() - last_interaction).days
    if days <= 0:
        return 100
    if days <= 1:
        return 90
    if days <= 3:
        return 70
    if days <= 7:
        return 50
    if days <= 14:
        return 30
    if days <= 30:
        return 15
    return 5


def _score_frequency(msg_count: int) -> float:
    """Message count mapped to 0-100. 50+ msgs = 100."""
    if msg_count <= 0:
        return 0
    if msg_count >= 50:
        return 100
    return min(msg_count * 2, 100)


def _score_followup(needed: bool) -> float:
    """Follow-up needed = 100, not = 0."""
    return 100 if needed else 0


def _compute_scores(db: Session, hub_ids: list[int]) -> list[dict]:
    if not hub_ids:
        return []

    contacts = db.query(Contact).filter(Contact.hub_id.in_(hub_ids)).all()
    if not contacts:
        return []

    # Pre-fetch message counts per contact phone/chat_id
    # Get all conversations for these hubs' bots
    from app.database import HubBotMembership, BotProfile
    bot_ids = (
        db.query(HubBotMembership.bot_profile_id)
        .filter(HubBotMembership.hub_id.in_(hub_ids))
        .all()
    )
    bot_id_list = [b[0] for b in bot_ids]

    msg_counts = {}
    if bot_id_list:
        rows = (
            db.query(
                Conversation.phone,
                func.count(Message.id).label("cnt"),
            )
            .join(Message, Message.conversation_id == Conversation.id)
            .filter(
                Conversation.bot_profile_id.in_(bot_id_list),
                Conversation.is_group == False,
            )
            .group_by(Conversation.phone)
            .all()
        )
        for phone, cnt in rows:
            if phone:
                msg_counts[phone] = cnt

        # Also count by chat_id for platforms without phone
        rows_chat = (
            db.query(
                Conversation.chat_id,
                func.count(Message.id).label("cnt"),
            )
            .join(Message, Message.conversation_id == Conversation.id)
            .filter(
                Conversation.bot_profile_id.in_(bot_id_list),
                Conversation.is_group == False,
            )
            .group_by(Conversation.chat_id)
            .all()
        )
        for chat_id, cnt in rows_chat:
            if chat_id and chat_id not in msg_counts:
                msg_counts[chat_id] = cnt

    # Hub name lookup
    hub_map = {h.id: h.name for h in db.query(Hub).filter(Hub.id.in_(hub_ids)).all()}

    # Tag lookup
    tag_map = {}
    tags = (
        db.query(ContactTag)
        .join(Contact, ContactTag.contact_id == Contact.id)
        .filter(Contact.hub_id.in_(hub_ids))
        .all()
    )
    for t in tags:
        tag_map.setdefault(t.contact_id, []).append(t.tag)

    scored = []
    for c in contacts:
        freq_count = msg_counts.get(c.phone, msg_counts.get(c.phone, 0))

        factors = {
            "sentiment": _score_sentiment(c.sentiment),
            "urgency": _score_urgency(c.urgency),
            "engagement": _score_engagement(c.engagement_score),
            "recency": _score_recency(c.last_interaction_at),
            "frequency": _score_frequency(freq_count),
            "followup": _score_followup(c.follow_up_needed or False),
        }

        total = sum(factors[k] * WEIGHTS[k] for k in WEIGHTS)
        total = round(min(total, 100), 1)
        tier = _get_tier(total)

        scored.append({
            "id": c.id,
            "name": c.display_name or c.phone or "Unknown",
            "phone": c.phone,
            "hub_name": hub_map.get(c.hub_id, ""),
            "score": total,
            "tier": tier["name"],
            "tier_color": tier["color"],
            "factors": {k: round(v, 1) for k, v in factors.items()},
            "sentiment": c.sentiment,
            "urgency": c.urgency,
            "engagement_score": round(c.engagement_score or 0, 1),
            "follow_up_needed": c.follow_up_needed or False,
            "predicted_intent": c.predicted_intent,
            "last_interaction": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
            "tags": tag_map.get(c.id, []),
            "message_count": freq_count,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _get_user_hub_scope(user, db)
    return templates.TemplateResponse("contact-scoring-engine.html", {
        "request": request,
        "user": user,
        "active_page": "tools_contact_scoring_engine",
        "page_title": "Contact Scoring Engine",
        "hubs": hubs,
    })


@router.get("/api/scores")
async def get_scores(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db, hub_id)
    scored = _compute_scores(db, scoped)

    # Build summary stats
    tier_counts = {"Hot": 0, "Warm": 0, "Cool": 0, "Cold": 0}
    total_score = 0
    for s in scored:
        tier_counts[s["tier"]] = tier_counts.get(s["tier"], 0) + 1
        total_score += s["score"]

    avg_score = round(total_score / len(scored), 1) if scored else 0

    ToolMonitor.log_execution(
        db=db,
        tool_type="contact-scoring-engine",
        operation="compute_scores",
        hub_id=hub_id,
        input_data={"hub_id": hub_id},
        output_data={
            "contacts_scored": len(scored),
            "avg_score": avg_score,
            "tier_counts": tier_counts,
        },
        user_id=user.id,
    )

    return {
        "contacts": scored,
        "summary": {
            "total": len(scored),
            "avg_score": avg_score,
            "tier_counts": tier_counts,
        },
        "weights": WEIGHTS,
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db)
    scored = _compute_scores(db, scoped)

    tier_counts = {"Hot": 0, "Warm": 0, "Cool": 0, "Cold": 0}
    for s in scored:
        tier_counts[s["tier"]] = tier_counts.get(s["tier"], 0) + 1

    avg_score = round(sum(s["score"] for s in scored) / len(scored), 1) if scored else 0
    top_contacts = scored[:3]

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "total": len(scored),
        "avg_score": avg_score,
        "tier_counts": tier_counts,
        "top_contacts": top_contacts,
    })
