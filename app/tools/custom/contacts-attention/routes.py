from datetime import datetime
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

router = APIRouter(tags=["contacts-attention"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _normalize_phone(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(char for char in value if char.isdigit())


def _resolve_hub_scope(user, db: Session, hub_id: Optional[int]) -> tuple[list[int], list[Hub]]:
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], []
    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)

    scoped_hub_ids = [hub_id] if hub_id is not None else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name.asc()).all()
    return scoped_hub_ids, hubs


def _compute_attention_score(contact: Contact, days_idle: int, awaiting_reply: bool) -> int:
    score = 0
    urgency = (contact.urgency or "").lower()
    sentiment = (contact.sentiment or "").lower()

    if contact.follow_up_needed:
        score += 38
    if urgency == "high":
        score += 30
    elif urgency == "medium":
        score += 18
    if sentiment == "negative":
        score += 22

    score += min(days_idle, 21) * 2
    if awaiting_reply:
        score += 12
    if (contact.followup_status or "").lower() == "pending":
        score += 8

    return score


def _build_attention_payload(db: Session, scoped_hub_ids: list[int], idle_days: int, limit: int) -> dict:
    if not scoped_hub_ids:
        return {
            "summary": {
                "contacts_scanned": 0,
                "attention_count": 0,
                "overdue_count": 0,
                "awaiting_reply_count": 0,
                "high_priority_count": 0,
            },
            "contacts": [],
        }

    hubs = db.query(Hub).filter(Hub.id.in_(scoped_hub_ids)).all()
    hub_names = {hub.id: hub.name for hub in hubs}
    contacts = db.query(Contact).filter(Contact.hub_id.in_(scoped_hub_ids)).all()

    latest_conversation_by_phone: dict[tuple[int, str], dict] = {}
    conversation_rows = (
        db.query(
            HubBotMembership.hub_id,
            Conversation.id.label("conversation_id"),
            Conversation.phone,
            Conversation.last_message_at,
        )
        .join(Conversation, Conversation.bot_profile_id == HubBotMembership.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Conversation.phone.isnot(None),
        )
        .order_by(HubBotMembership.hub_id.asc(), Conversation.last_message_at.desc().nullslast())
        .all()
    )

    for row in conversation_rows:
        key = (row.hub_id, _normalize_phone(row.phone))
        if key[1] and key not in latest_conversation_by_phone:
            latest_conversation_by_phone[key] = {
                "conversation_id": row.conversation_id,
                "last_message_at": row.last_message_at,
            }

    conversation_ids = [item["conversation_id"] for item in latest_conversation_by_phone.values()]
    role_timestamps: dict[int, dict[str, datetime]] = {}
    if conversation_ids:
        role_rows = (
            db.query(
                Message.conversation_id,
                Message.role,
                func.max(Message.timestamp).label("last_timestamp"),
            )
            .filter(Message.conversation_id.in_(conversation_ids))
            .group_by(Message.conversation_id, Message.role)
            .all()
        )
        for row in role_rows:
            role_timestamps.setdefault(row.conversation_id, {})[row.role] = row.last_timestamp

    now = datetime.utcnow()
    contacts_attention = []
    for contact in contacts:
        contact_key = (contact.hub_id, _normalize_phone(contact.phone))
        conversation_info = latest_conversation_by_phone.get(contact_key)

        latest_touch = contact.last_interaction_at
        if conversation_info and conversation_info["last_message_at"]:
            latest_touch = max(
                latest_touch,
                conversation_info["last_message_at"],
            ) if latest_touch else conversation_info["last_message_at"]

        days_idle = (now - latest_touch).days if latest_touch else 999
        last_user_message_at = None
        last_assistant_message_at = None
        awaiting_reply = False

        if conversation_info:
            timestamps = role_timestamps.get(conversation_info["conversation_id"], {})
            last_user_message_at = timestamps.get("user")
            last_assistant_message_at = timestamps.get("assistant")
            awaiting_reply = bool(
                last_user_message_at and (
                    not last_assistant_message_at or last_user_message_at > last_assistant_message_at
                )
            )

        score = _compute_attention_score(contact, days_idle, awaiting_reply)
        qualifies = any([
            contact.follow_up_needed,
            (contact.urgency or "").lower() == "high",
            (contact.sentiment or "").lower() == "negative",
            awaiting_reply,
            days_idle >= idle_days,
        ])

        if not qualifies:
            continue

        contacts_attention.append({
            "id": contact.id,
            "name": contact.display_name or contact.phone or "Unknown",
            "phone": contact.phone,
            "hub_id": contact.hub_id,
            "hub_name": hub_names.get(contact.hub_id, ""),
            "sentiment": (contact.sentiment or "unknown").lower(),
            "urgency": (contact.urgency or "unknown").lower(),
            "follow_up_needed": bool(contact.follow_up_needed),
            "follow_up_reason": contact.follow_up_reason,
            "followup_status": contact.followup_status,
            "awaiting_reply": awaiting_reply,
            "last_interaction_at": latest_touch.isoformat() if latest_touch else None,
            "days_idle": days_idle,
            "attention_score": score,
            "predicted_intent": contact.predicted_intent,
            "conversation_id": conversation_info["conversation_id"] if conversation_info else None,
            "last_user_message_at": last_user_message_at.isoformat() if last_user_message_at else None,
            "last_assistant_message_at": (
                last_assistant_message_at.isoformat() if last_assistant_message_at else None
            ),
        })

    contacts_attention.sort(
        key=lambda item: (item["attention_score"], item["days_idle"]),
        reverse=True,
    )

    trimmed = contacts_attention[:limit]
    summary = {
        "contacts_scanned": len(contacts),
        "attention_count": len(contacts_attention),
        "overdue_count": len([item for item in contacts_attention if item["days_idle"] >= idle_days]),
        "awaiting_reply_count": len([item for item in contacts_attention if item["awaiting_reply"]]),
        "high_priority_count": len([item for item in contacts_attention if item["attention_score"] >= 70]),
    }
    return {"summary": summary, "contacts": trimmed}


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("contacts-attention.html", {
        "request": request,
        "user": user,
        "active_page": "tools_contacts_attention",
        "page_title": "Contacts Needing Attention",
        "hubs": hubs,
    })


@router.get("/api/attention")
async def get_attention(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    idle_days: int = Query(default=3, ge=1, le=30),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _ = _resolve_hub_scope(user, db, hub_id)
    payload = _build_attention_payload(db, scoped_hub_ids, idle_days, limit)

    ToolMonitor.log_execution(
        db=db,
        tool_type="contacts-attention",
        operation="fetch_attention",
        hub_id=hub_id,
        input_data={"idle_days": idle_days, "limit": limit},
        output_data={
            "attention_count": payload["summary"]["attention_count"],
            "high_priority_count": payload["summary"]["high_priority_count"],
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
    payload = _build_attention_payload(db, scoped_hub_ids, idle_days=3, limit=4)

    ToolMonitor.log_execution(
        db=db,
        tool_type="contacts-attention",
        operation="fetch_dashboard_widget",
        input_data={"idle_days": 3, "limit": 4},
        output_data={"attention_count": payload["summary"]["attention_count"]},
        user_id=user.id,
    )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "summary": payload["summary"],
        "contacts": payload["contacts"],
    })
