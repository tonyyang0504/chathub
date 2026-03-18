from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from app.ai.factory import get_ai_provider
from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Contact, Hub, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["contacts-attention"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _contact_reasons(contact: Contact) -> list[str]:
    reasons: list[str] = []
    if contact.follow_up_needed:
        reasons.append("Follow-up Needed")
    if (contact.sentiment or "").lower() == "negative":
        reasons.append("Negative Sentiment")
    if (contact.urgency or "").lower() in {"high", "critical"}:
        reasons.append("High Urgency")
    return reasons


def _contact_priority_score(contact: Contact) -> int:
    score = 0
    if contact.follow_up_needed:
        score += 4
    if (contact.sentiment or "").lower() == "negative":
        score += 3
    urgency = (contact.urgency or "").lower()
    if urgency == "critical":
        score += 4
    elif urgency == "high":
        score += 3
    elif urgency == "medium":
        score += 1
    return score


def _serialize_contact(contact: Contact, hub_name: str | None) -> dict[str, Any]:
    display_name = contact.display_name or contact.phone
    reasons = _contact_reasons(contact)
    return {
        "id": contact.id,
        "display_name": display_name,
        "phone": contact.phone,
        "hub_id": contact.hub_id,
        "hub_name": hub_name,
        "engagement_score": contact.engagement_score,
        "sentiment": (contact.sentiment or "neutral").lower(),
        "urgency": (contact.urgency or "low").lower(),
        "follow_up_needed": bool(contact.follow_up_needed),
        "follow_up_reason": contact.follow_up_reason,
        "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
        "attention_reasons": reasons,
        "priority_score": _contact_priority_score(contact),
    }


def _fetch_attention_contacts(
    db: Session,
    user_hub_ids: list[int],
    hub_id: int | None,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    if hub_id is not None and hub_id not in user_hub_ids:
        raise HTTPException(status_code=403, detail="You do not have access to this hub")

    scoped_hub_ids = [hub_id] if hub_id else user_hub_ids
    if not scoped_hub_ids:
        return [], 0

    base_query = db.query(Contact).filter(
        Contact.hub_id.in_(scoped_hub_ids),
        or_(
            Contact.follow_up_needed == True,
            Contact.sentiment == "negative",
            Contact.urgency.in_(["high", "critical"]),
        ),
    )

    total = base_query.count()

    urgency_rank = case(
        (Contact.urgency == "critical", 0),
        (Contact.urgency == "high", 1),
        (Contact.urgency == "medium", 2),
        (Contact.urgency == "low", 3),
        else_=4,
    )

    contacts = (
        base_query
        .order_by(
            Contact.follow_up_needed.desc(),
            urgency_rank,
            Contact.last_interaction_at.asc().nullsfirst(),
            Contact.updated_at.desc().nullslast(),
        )
        .limit(limit)
        .all()
    )

    hub_rows = db.query(Hub.id, Hub.name).filter(Hub.id.in_(scoped_hub_ids)).all()
    hub_names = {hub_row.id: hub_row.name for hub_row in hub_rows}

    return [_serialize_contact(contact, hub_names.get(contact.hub_id)) for contact in contacts], total


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    user_hub_ids = get_user_hub_ids(user, db)
    hubs = db.query(Hub).filter(Hub.id.in_(user_hub_ids)).order_by(Hub.name.asc()).all() if user_hub_ids else []

    return templates.TemplateResponse(
        "contacts-attention.html",
        {
            "request": request,
            "user": user,
            "hubs": hubs,
            "ai_provider_factory_available": callable(get_ai_provider),
            "active_page": "tools_contacts_attention",
            "page_title": "Contacts Needing Attention",
        },
    )


@router.get("/api/data")
async def get_data(
    request: Request,
    hub_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    safe_limit = max(1, min(limit, 100))
    user_hub_ids = get_user_hub_ids(user, db)
    contacts, total = _fetch_attention_contacts(db, user_hub_ids, hub_id, safe_limit)

    ToolMonitor.log_execution(
        db=db,
        tool_type="contacts_attention",
        operation="list_attention_contacts",
        hub_id=hub_id,
        input_data={"hub_id": hub_id, "limit": safe_limit},
        output_data={"total": total, "returned": len(contacts)},
        status="success",
        triggered_by="api",
        user_id=user.id,
    )

    return {
        "contacts": contacts,
        "total": total,
        "count": len(contacts),
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_hub_ids = get_user_hub_ids(user, db)
    contacts, total = _fetch_attention_contacts(db, user_hub_ids, None, 5)

    ToolMonitor.log_execution(
        db=db,
        tool_type="contacts_attention",
        operation="dashboard_widget",
        input_data={"limit": 5},
        output_data={"total": total, "returned": len(contacts)},
        status="success",
        triggered_by="api",
        user_id=user.id,
    )

    return templates.TemplateResponse(
        "widgets/dashboard.html",
        {
            "request": request,
            "user": user,
            "contacts": contacts,
            "total": total,
            "tool_path": "/tools/contacts-attention/",
        },
    )
