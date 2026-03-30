from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy.orm import Session

from app.ai.factory import get_ai_provider
from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Contact, Hub, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["dashboard-contacts-attention"])

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


def _score_contact(contact: Contact, stale_cutoff: datetime) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    if contact.follow_up_needed:
        score += 3
        reasons.append("follow_up")

    if contact.sentiment == "negative":
        score += 2
        reasons.append("sentiment")

    if contact.urgency == "high":
        score += 3
        reasons.append("urgency")
    elif contact.urgency == "medium":
        score += 1

    if not contact.last_interaction_at or contact.last_interaction_at < stale_cutoff:
        score += 1
        reasons.append("inactivity")

    return score, reasons


def _provider_check(hub: Optional[Hub]) -> dict:
    if not hub:
        return {"provider": None, "available": None}

    check = {"provider": hub.ai_provider, "available": True}
    try:
        get_ai_provider(hub.ai_provider or "openai", api_key="placeholder-key", model=hub.model)
    except Exception:
        check["available"] = False
    return check


def _build_attention_payload(db: Session, scoped_hub_ids: list[int], limit: int = 50) -> dict:
    if not scoped_hub_ids:
        return {
            "summary": {
                "contacts_in_scope": 0,
                "needs_attention": 0,
                "follow_up_needed": 0,
                "negative_sentiment": 0,
                "high_urgency": 0,
            },
            "contacts": [],
        }

    stale_cutoff = datetime.utcnow() - timedelta(days=7)
    hub_map = {
        hub.id: hub.name
        for hub in db.query(Hub).filter(Hub.id.in_(scoped_hub_ids)).all()
    }

    contacts = (
        db.query(Contact)
        .filter(Contact.hub_id.in_(scoped_hub_ids))
        .order_by(Contact.last_interaction_at.asc().nullsfirst(), Contact.updated_at.desc())
        .all()
    )

    rows = []
    for contact in contacts:
        score, reasons = _score_contact(contact, stale_cutoff)
        if not reasons:
            continue

        rows.append({
            "contact_id": contact.id,
            "contact_name": contact.display_name or contact.phone or f"Contact #{contact.id}",
            "phone": contact.phone,
            "hub_id": contact.hub_id,
            "hub_name": hub_map.get(contact.hub_id, "Unknown hub"),
            "sentiment": contact.sentiment,
            "urgency": contact.urgency,
            "follow_up_needed": bool(contact.follow_up_needed),
            "follow_up_reason": contact.follow_up_reason,
            "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
            "score": score,
            "reasons": reasons,
        })

    rows.sort(
        key=lambda item: (
            item["score"],
            item["last_interaction_at"] or "",
        ),
        reverse=True,
    )

    total = len(contacts)
    limited_rows = rows[:limit]
    summary = {
        "contacts_in_scope": total,
        "needs_attention": len(rows),
        "follow_up_needed": sum(1 for contact in contacts if contact.follow_up_needed),
        "negative_sentiment": sum(1 for contact in contacts if contact.sentiment == "negative"),
        "high_urgency": sum(1 for contact in contacts if contact.urgency == "high"),
    }

    return {
        "summary": summary,
        "contacts": limited_rows,
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs, _ = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("dashboard-contacts-attention.html", {
        "request": request,
        "user": user,
        "active_page": "tools_dashboard_contacts_attention",
        "page_title": "Contacts Needing Attention",
        "hubs": hubs,
    })


@router.get("/api/attention")
async def get_attention(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, selected_hub = _resolve_hub_scope(user, db, hub_id)
    payload = _build_attention_payload(db, scoped_hub_ids, limit=limit)
    provider_check = _provider_check(selected_hub)

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-contacts-attention",
        operation="fetch_attention",
        hub_id=hub_id,
        input_data={"limit": limit},
        output_data={
            "contacts_in_scope": payload["summary"]["contacts_in_scope"],
            "needs_attention": payload["summary"]["needs_attention"],
            "provider_check": provider_check,
        },
        user_id=user.id,
    )

    return {
        "summary": payload["summary"],
        "contacts": payload["contacts"],
        "provider_check": provider_check,
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped_hub_ids, _, _ = _resolve_hub_scope(user, db, None)
    payload = _build_attention_payload(db, scoped_hub_ids, limit=3)

    ToolMonitor.log_execution(
        db=db,
        tool_type="dashboard-contacts-attention",
        operation="fetch_dashboard_widget",
        input_data={},
        output_data={
            "contacts_in_scope": payload["summary"]["contacts_in_scope"],
            "needs_attention": payload["summary"]["needs_attention"],
            "widget_items": len(payload["contacts"]),
        },
        user_id=user.id,
    )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "summary": payload["summary"],
        "contacts": payload["contacts"],
    })
