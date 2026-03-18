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
from app.database import BotProfile, Contact, Conversation, Hub, HubBotMembership, Message, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["dashboard-conversation-snapshot"])

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


def _build_snapshot_payload(db: Session, scoped_hub_ids: list[int], limit: int) -> dict:
    if not scoped_hub_ids:
        return {
            "summary": {
                "conversations_total": 0,
                "messages_today": 0,
                "active_last_24h": 0,
                "contacts_total": 0,
            },
            "conversations": [],
        }

    now = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_24h = now - timedelta(hours=24)

    membership_query = (
        db.query(
            Conversation.id.label("conversation_id"),
            Conversation.chat_name,
            Conversation.display_name,
            Conversation.phone,
            Conversation.message_count,
            Conversation.last_message_at,
            BotProfile.name.label("bot_name"),
            Hub.id.label("hub_id"),
            Hub.name.label("hub_name"),
        )
        .join(BotProfile, BotProfile.id == Conversation.bot_profile_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == BotProfile.id)
        .join(Hub, Hub.id == HubBotMembership.hub_id)
        .filter(HubBotMembership.hub_id.in_(scoped_hub_ids), HubBotMembership.is_active.is_(True))
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(limit * 4)
        .all()
    )

    deduped = {}
    for row in membership_query:
        if row.conversation_id not in deduped:
            deduped[row.conversation_id] = {
                "id": row.conversation_id,
                "chat_name": row.chat_name or row.display_name or row.phone or "Untitled chat",
                "bot_name": row.bot_name,
                "hub_id": row.hub_id,
                "hub_name": row.hub_name,
                "message_count": row.message_count or 0,
                "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
            }
        if len(deduped) >= limit:
            break

    total_conversations = (
        db.query(func.count(distinct(Conversation.id)))
        .join(BotProfile, BotProfile.id == Conversation.bot_profile_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == BotProfile.id)
        .filter(HubBotMembership.hub_id.in_(scoped_hub_ids), HubBotMembership.is_active.is_(True))
        .scalar()
    )

    messages_today = (
        db.query(func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(BotProfile, BotProfile.id == Conversation.bot_profile_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == BotProfile.id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= day_start,
        )
        .scalar()
    )

    active_last_24h = (
        db.query(func.count(distinct(Conversation.id)))
        .join(BotProfile, BotProfile.id == Conversation.bot_profile_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == BotProfile.id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Conversation.last_message_at >= last_24h,
        )
        .scalar()
    )

    contacts_total = db.query(func.count(Contact.id)).filter(Contact.hub_id.in_(scoped_hub_ids)).scalar()

    return {
        "summary": {
            "conversations_total": total_conversations or 0,
            "messages_today": messages_today or 0,
            "active_last_24h": active_last_24h or 0,
            "contacts_total": contacts_total or 0,
        },
        "conversations": list(deduped.values()),
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs, _ = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("dashboard-conversation-snapshot.html", {
        "request": request,
        "user": user,
        "active_page": "tools_dashboard_conversation_snapshot",
        "page_title": "Conversation Snapshot",
        "hubs": hubs,
    })


@router.get("/api/snapshot")
async def get_snapshot(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, selected_hub = _resolve_hub_scope(user, db, hub_id)
    payload = _build_snapshot_payload(db, scoped_hub_ids, limit)

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
        tool_type="dashboard-conversation-snapshot",
        operation="fetch_snapshot",
        hub_id=hub_id,
        input_data={"limit": limit},
        output_data={"count": len(payload["conversations"]), "provider_check": provider_check},
        user_id=user.id,
    )

    return {
        "summary": payload["summary"],
        "conversations": payload["conversations"],
        "count": len(payload["conversations"]),
        "provider_check": provider_check,
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, _ = _resolve_hub_scope(user, db, None)
    payload = _build_snapshot_payload(db, scoped_hub_ids, limit=5)

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-conversation-snapshot",
        operation="fetch_dashboard_widget",
        input_data={"limit": 5},
        output_data={"count": len(payload["conversations"])},
        user_id=user.id,
    )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "summary": payload["summary"],
        "conversations": payload["conversations"],
    })
