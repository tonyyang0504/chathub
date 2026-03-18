from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from app.ai.factory import get_ai_provider
from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Conversation, Hub, HubBotMembership, Message, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["dashboard-message-trends"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _resolve_hub_scope(user, db: Session, hub_id: Optional[int]) -> tuple[list[int], list[Hub], Optional[Hub]]:
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], [], None

    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)

    scoped_hub_ids = [hub_id] if hub_id is not None else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name.asc()).all()
    selected_hub = next((hub for hub in hubs if hub.id == hub_id), None) if hub_id is not None else None
    return scoped_hub_ids, hubs, selected_hub


def _build_trends_payload(db: Session, scoped_hub_ids: list[int]) -> dict:
    if not scoped_hub_ids:
        return {
            "summary": {
                "messages_today": 0,
                "messages_yesterday": 0,
                "messages_7d": 0,
                "active_conversations_7d": 0,
                "delta": 0,
                "delta_percent": 0,
            },
            "daily_points": [],
            "top_hubs": [],
        }

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=6)

    base_query = (
        db.query(Message.timestamp, Hub.id.label("hub_id"), Hub.name.label("hub_name"))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .join(Hub, Hub.id == HubBotMembership.hub_id)
        .filter(HubBotMembership.hub_id.in_(scoped_hub_ids), HubBotMembership.is_active.is_(True))
    )

    messages_today = base_query.filter(Message.timestamp >= today_start).count()
    messages_yesterday = base_query.filter(
        Message.timestamp >= yesterday_start,
        Message.timestamp < today_start,
    ).count()
    messages_7d = base_query.filter(Message.timestamp >= week_start).count()

    active_conversations_7d = (
        db.query(func.count(distinct(Conversation.id)))
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= week_start,
        )
        .scalar()
    )

    daily_rows = (
        db.query(func.date(Message.timestamp).label("day"), func.count(Message.id).label("count"))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= week_start,
        )
        .group_by(func.date(Message.timestamp))
        .all()
    )

    counts_by_day = {str(row.day): row.count for row in daily_rows}
    daily_points = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        day_key = day.date().isoformat()
        daily_points.append({
            "date": day_key,
            "label": day.strftime("%a"),
            "count": counts_by_day.get(day_key, 0),
        })

    top_hubs_rows = (
        db.query(Hub.id, Hub.name, func.count(Message.id).label("message_count"))
        .join(HubBotMembership, HubBotMembership.hub_id == Hub.id)
        .join(Conversation, Conversation.bot_profile_id == HubBotMembership.bot_profile_id)
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            Hub.id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= week_start,
        )
        .group_by(Hub.id, Hub.name)
        .order_by(func.count(Message.id).desc())
        .limit(3)
        .all()
    )

    delta = messages_today - messages_yesterday
    delta_percent = int((delta / messages_yesterday) * 100) if messages_yesterday > 0 else (100 if messages_today > 0 else 0)

    return {
        "summary": {
            "messages_today": messages_today,
            "messages_yesterday": messages_yesterday,
            "messages_7d": messages_7d,
            "active_conversations_7d": active_conversations_7d or 0,
            "delta": delta,
            "delta_percent": delta_percent,
        },
        "daily_points": daily_points,
        "top_hubs": [
            {"hub_id": row.id, "hub_name": row.name, "message_count": row.message_count}
            for row in top_hubs_rows
        ],
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs, _ = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("dashboard-message-trends.html", {
        "request": request,
        "user": user,
        "active_page": "tools_dashboard_message_trends",
        "page_title": "Message Trends",
        "hubs": hubs,
    })


@router.get("/api/trends")
async def get_trends(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, selected_hub = _resolve_hub_scope(user, db, hub_id)
    payload = _build_trends_payload(db, scoped_hub_ids)

    provider_check = {"provider": None, "available": None}
    if selected_hub:
        provider_check["provider"] = selected_hub.ai_provider
        try:
            get_ai_provider(selected_hub.ai_provider or "openai", api_key="placeholder-key", model=selected_hub.model)
            provider_check["available"] = True
        except Exception:
            provider_check["available"] = False

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-message-trends",
        operation="fetch_trends",
        hub_id=hub_id,
        input_data={},
        output_data={
            "messages_today": payload["summary"]["messages_today"],
            "messages_7d": payload["summary"]["messages_7d"],
            "top_hubs": len(payload["top_hubs"]),
            "provider_check": provider_check,
        },
        user_id=user.id,
    )

    return {
        "summary": payload["summary"],
        "daily_points": payload["daily_points"],
        "top_hubs": payload["top_hubs"],
        "provider_check": provider_check,
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, _ = _resolve_hub_scope(user, db, None)
    payload = _build_trends_payload(db, scoped_hub_ids)

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-message-trends",
        operation="fetch_dashboard_widget",
        input_data={},
        output_data={
            "messages_today": payload["summary"]["messages_today"],
            "top_hubs": len(payload["top_hubs"]),
        },
        user_id=user.id,
    )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "summary": payload["summary"],
        "top_hubs": payload["top_hubs"],
    })
