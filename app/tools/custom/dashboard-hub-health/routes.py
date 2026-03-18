from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import case, distinct, func
from sqlalchemy.orm import Session

from app.ai.factory import get_ai_provider
from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Contact, Conversation, Hub, HubBotMembership, Message, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["dashboard-hub-health"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _resolve_hub_scope(user, db: Session, hub_id: Optional[int]) -> tuple[list[int], list[Hub]]:
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], []

    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)

    scoped_hub_ids = [hub_id] if hub_id is not None else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name.asc()).all()
    return scoped_hub_ids, hubs


def _compute_risk(conversations_24h: int, follow_up_needed: int, negative_contacts: int) -> tuple[int, str, list[str]]:
    score = 0
    reasons = []

    if conversations_24h == 0:
        score += 2
        reasons.append("no_recent_conversations")
    elif conversations_24h < 3:
        score += 1
        reasons.append("low_conversation_activity")

    if follow_up_needed >= 5:
        score += 2
        reasons.append("high_follow_up_backlog")
    elif follow_up_needed >= 2:
        score += 1
        reasons.append("moderate_follow_up_backlog")

    if negative_contacts >= 3:
        score += 1
        reasons.append("negative_sentiment_cluster")

    if score >= 4:
        level = "high"
    elif score >= 2:
        level = "medium"
    else:
        level = "low"

    return score, level, reasons


def _build_health_payload(db: Session, scoped_hub_ids: list[int]) -> dict:
    if not scoped_hub_ids:
        return {
            "summary": {
                "hubs_total": 0,
                "active_hubs": 0,
                "active_bots": 0,
                "conversations_24h": 0,
                "messages_7d": 0,
                "follow_up_needed": 0,
                "provider_issues": 0,
            },
            "hubs": [],
            "at_risk": [],
        }

    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    hub_rows = (
        db.query(Hub.id, Hub.name, Hub.is_active, Hub.ai_provider, Hub.model)
        .filter(Hub.id.in_(scoped_hub_ids))
        .order_by(Hub.name.asc())
        .all()
    )

    active_bot_counts = dict(
        db.query(HubBotMembership.hub_id, func.count(distinct(HubBotMembership.bot_profile_id)))
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
        )
        .group_by(HubBotMembership.hub_id)
        .all()
    )

    conversations_24h_counts = dict(
        db.query(HubBotMembership.hub_id, func.count(distinct(Conversation.id)))
        .join(Conversation, Conversation.bot_profile_id == HubBotMembership.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Conversation.last_message_at >= last_24h,
        )
        .group_by(HubBotMembership.hub_id)
        .all()
    )

    total_conversation_counts = dict(
        db.query(HubBotMembership.hub_id, func.count(distinct(Conversation.id)))
        .join(Conversation, Conversation.bot_profile_id == HubBotMembership.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
        )
        .group_by(HubBotMembership.hub_id)
        .all()
    )

    message_7d_counts = dict(
        db.query(HubBotMembership.hub_id, func.count(Message.id))
        .join(Conversation, Conversation.bot_profile_id == HubBotMembership.bot_profile_id)
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= last_7d,
        )
        .group_by(HubBotMembership.hub_id)
        .all()
    )

    contact_metrics = {
        row.hub_id: {
            "contacts_total": row.contacts_total or 0,
            "follow_up_needed": row.follow_up_needed or 0,
            "negative_contacts": row.negative_contacts or 0,
        }
        for row in (
            db.query(
                Contact.hub_id.label("hub_id"),
                func.count(Contact.id).label("contacts_total"),
                func.sum(case((Contact.follow_up_needed.is_(True), 1), else_=0)).label("follow_up_needed"),
                func.sum(case((Contact.sentiment == "negative", 1), else_=0)).label("negative_contacts"),
            )
            .filter(Contact.hub_id.in_(scoped_hub_ids))
            .group_by(Contact.hub_id)
            .all()
        )
    }

    hub_items = []
    provider_issues = 0
    for row in hub_rows:
        provider_available = True
        if row.is_active:
            try:
                get_ai_provider(row.ai_provider or "openai", api_key="placeholder-key", model=row.model)
            except Exception:
                provider_available = False

        if row.is_active and not provider_available:
            provider_issues += 1

        contacts = contact_metrics.get(row.id, {})
        conversations_24h = conversations_24h_counts.get(row.id, 0)
        follow_up_needed = contacts.get("follow_up_needed", 0)
        negative_contacts = contacts.get("negative_contacts", 0)
        risk_score, risk_level, risk_reasons = _compute_risk(
            conversations_24h=conversations_24h,
            follow_up_needed=follow_up_needed,
            negative_contacts=negative_contacts,
        )

        hub_items.append({
            "hub_id": row.id,
            "hub_name": row.name,
            "hub_active": bool(row.is_active),
            "ai_provider": row.ai_provider,
            "provider_available": provider_available,
            "active_bots": active_bot_counts.get(row.id, 0),
            "conversations_total": total_conversation_counts.get(row.id, 0),
            "conversations_24h": conversations_24h,
            "messages_7d": message_7d_counts.get(row.id, 0),
            "contacts_total": contacts.get("contacts_total", 0),
            "follow_up_needed": follow_up_needed,
            "negative_contacts": negative_contacts,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "risk_reasons": risk_reasons,
        })

    at_risk = [item for item in hub_items if item["risk_score"] >= 3]
    at_risk.sort(key=lambda item: item["risk_score"], reverse=True)

    summary = {
        "hubs_total": len(hub_items),
        "active_hubs": sum(1 for item in hub_items if item["hub_active"]),
        "active_bots": sum(item["active_bots"] for item in hub_items),
        "conversations_24h": sum(item["conversations_24h"] for item in hub_items),
        "messages_7d": sum(item["messages_7d"] for item in hub_items),
        "follow_up_needed": sum(item["follow_up_needed"] for item in hub_items),
        "provider_issues": provider_issues,
    }

    return {
        "summary": summary,
        "hubs": hub_items,
        "at_risk": at_risk,
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("dashboard-hub-health.html", {
        "request": request,
        "user": user,
        "active_page": "tools_dashboard_hub_health",
        "page_title": "Hub Health Snapshot",
        "hubs": hubs,
    })


@router.get("/api/health")
async def get_health(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _ = _resolve_hub_scope(user, db, hub_id)
    payload = _build_health_payload(db, scoped_hub_ids)

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-hub-health",
        operation="fetch_health",
        hub_id=hub_id,
        input_data={},
        output_data={
            "hubs_count": len(payload["hubs"]),
            "at_risk_count": len(payload["at_risk"]),
            "provider_issues": payload["summary"]["provider_issues"],
        },
        user_id=user.id,
    )

    return payload


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _ = _resolve_hub_scope(user, db, None)
    payload = _build_health_payload(db, scoped_hub_ids)

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-hub-health",
        operation="fetch_dashboard_widget",
        input_data={},
        output_data={
            "hubs_count": len(payload["hubs"]),
            "at_risk_count": len(payload["at_risk"]),
        },
        user_id=user.id,
    )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "summary": payload["summary"],
        "at_risk": payload["at_risk"][:3],
    })
