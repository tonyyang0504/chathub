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
from app.database import Contact, Conversation, Hub, HubBotMembership, Message, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["conversation-heatmap"])

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
    selected_hub = next((h for h in hubs if h.id == hub_id), None) if hub_id is not None else None
    return scoped_hub_ids, hubs, selected_hub


def _base_message_query(db: Session, scoped_hub_ids: list[int]):
    return (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
        )
    )


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})
    _, hubs, _ = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("conversation-heatmap.html", {
        "request": request,
        "user": user,
        "active_page": "tools_conversation_heatmap",
        "page_title": "Conversation Heatmap",
        "hubs": hubs,
    })


@router.get("/api/heatmap")
async def get_heatmap(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    direction: Optional[str] = Query(default="all"),
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, _ = _resolve_hub_scope(user, db, hub_id)
    if not scoped_hub_ids:
        return {"grid": [], "stats": {"total": 0, "busiest_hour": None, "busiest_day": None, "max_count": 0}}

    now = datetime.utcnow()
    start = now - timedelta(days=days)

    query = (
        db.query(Message.timestamp, Message.direction)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= start,
        )
    )

    if direction == "incoming":
        query = query.filter(Message.direction == "incoming")
    elif direction == "outgoing":
        query = query.filter(Message.direction == "outgoing")

    messages = query.all()

    # Build 7x24 grid (day_of_week x hour)
    grid = [[0] * 24 for _ in range(7)]
    for msg in messages:
        ts = msg.timestamp
        if ts:
            grid[ts.weekday()][ts.hour] += 1

    max_count = max(max(row) for row in grid) if messages else 0
    total = sum(sum(row) for row in grid)

    # Find busiest hour and day
    busiest_hour = None
    busiest_day = None
    if total > 0:
        hour_totals = [sum(grid[d][h] for d in range(7)) for h in range(24)]
        day_totals = [sum(grid[d]) for d in range(7)]
        busiest_hour = hour_totals.index(max(hour_totals))
        busiest_day = day_totals.index(max(day_totals))

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    ToolMonitor.log_execution(
        db=db,
        tool_type="conversation-heatmap",
        operation="fetch_heatmap",
        hub_id=hub_id,
        input_data={"direction": direction, "days": days},
        output_data={"total_messages": total, "max_cell": max_count},
        user_id=user.id,
    )

    return {
        "grid": grid,
        "stats": {
            "total": total,
            "busiest_hour": f"{busiest_hour:02d}:00" if busiest_hour is not None else None,
            "busiest_day": day_names[busiest_day] if busiest_day is not None else None,
            "max_count": max_count,
        },
    }


@router.get("/api/peak-contacts")
async def get_peak_contacts(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, _ = _resolve_hub_scope(user, db, hub_id)
    if not scoped_hub_ids:
        return {"contacts": []}

    now = datetime.utcnow()
    start = now - timedelta(days=days)

    rows = (
        db.query(
            Conversation.chat_name,
            Conversation.phone,
            func.count(Message.id).label("msg_count"),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= start,
            Message.direction == "incoming",
        )
        .group_by(Conversation.id)
        .order_by(func.count(Message.id).desc())
        .limit(5)
        .all()
    )

    contacts = []
    for row in rows:
        name = row.chat_name or row.phone or "Unknown"
        contacts.append({"name": name, "messages": row.msg_count})

    ToolMonitor.log_execution(
        db=db,
        tool_type="conversation-heatmap",
        operation="fetch_peak_contacts",
        hub_id=hub_id,
        input_data={"days": days},
        output_data={"contacts_returned": len(contacts)},
        user_id=user.id,
    )

    return {"contacts": contacts}
