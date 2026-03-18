from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import case, desc
from sqlalchemy.orm import Session

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


def _build_attention_item(contact: Contact, hub_name: str) -> dict:
    reasons = []
    score = 0

    if contact.follow_up_needed:
        reasons.append("follow_up_needed")
        score += 3

    if (contact.sentiment or "").lower() == "negative":
        reasons.append("sentiment_negative")
        score += 2

    urgency = (contact.urgency or "").lower()
    if urgency == "high":
        reasons.append("urgency_high")
        score += 2
    elif urgency == "medium":
        score += 1

    return {
        "id": contact.id,
        "hub_id": contact.hub_id,
        "hub_name": hub_name,
        "display_name": contact.display_name or contact.phone,
        "phone": contact.phone,
        "sentiment": contact.sentiment or "unknown",
        "urgency": contact.urgency or "unknown",
        "follow_up_needed": bool(contact.follow_up_needed),
        "follow_up_reason": contact.follow_up_reason,
        "engagement_score": contact.engagement_score,
        "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
        "updated_at": contact.updated_at.isoformat() if contact.updated_at else None,
        "reasons": reasons,
        "attention_score": score,
    }


def _query_attention_contacts(
    db: Session,
    hub_ids: list[int],
    hub_id: Optional[int],
    issue: str,
    limit: int,
) -> list[Contact]:
    query = db.query(Contact).filter(Contact.hub_id.in_(hub_ids))
    if hub_id:
        query = query.filter(Contact.hub_id == hub_id)

    if issue == "follow_up":
        query = query.filter(Contact.follow_up_needed.is_(True))
    elif issue == "negative":
        query = query.filter(Contact.sentiment == "negative")
    elif issue == "high_urgency":
        query = query.filter(Contact.urgency == "high")
    else:
        query = query.filter(
            (Contact.follow_up_needed.is_(True))
            | (Contact.sentiment == "negative")
            | (Contact.urgency == "high")
        )

    attention_order = (
        case((Contact.follow_up_needed.is_(True), 3), else_=0)
        + case((Contact.sentiment == "negative", 2), else_=0)
        + case((Contact.urgency == "high", 2), (Contact.urgency == "medium", 1), else_=0)
    )

    return query.order_by(desc(attention_order), desc(Contact.updated_at)).limit(limit).all()


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    hub_ids = get_user_hub_ids(user, db)
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name.asc()).all()

    return templates.TemplateResponse("contacts-attention.html", {
        "request": request,
        "user": user,
        "active_page": "tools_contacts_attention",
        "page_title": "Contacts Needing Attention",
        "hubs": hubs,
    })


@router.get("/api/attention")
async def get_attention_contacts(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    issue: str = Query(default="all", pattern="^(all|follow_up|negative|high_urgency)$"),
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return {"contacts": [], "count": 0}
    if hub_id and hub_id not in hub_ids:
        raise HTTPException(status_code=403)

    contacts = _query_attention_contacts(db, hub_ids, hub_id, issue, limit)
    hub_map = {
        item.id: item.name
        for item in db.query(Hub.id, Hub.name).filter(Hub.id.in_({c.hub_id for c in contacts})).all()
    }
    payload = [_build_attention_item(contact, hub_map.get(contact.hub_id, f"Hub {contact.hub_id}")) for contact in contacts]

    ToolMonitor.log_execution(
        db=db,
        tool_type="contacts-attention",
        operation="fetch_attention_contacts",
        hub_id=hub_id,
        input_data={"issue": issue, "limit": limit},
        output_data={"count": len(payload)},
        user_id=user.id,
    )

    return {"contacts": payload, "count": len(payload)}


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return templates.TemplateResponse("widgets/dashboard.html", {
            "request": request,
            "user": user,
            "contacts": [],
            "total": 0,
        })

    contacts = _query_attention_contacts(db, hub_ids, None, "all", 5)
    hub_map = {
        item.id: item.name
        for item in db.query(Hub.id, Hub.name).filter(Hub.id.in_({c.hub_id for c in contacts})).all()
    }
    payload = [_build_attention_item(contact, hub_map.get(contact.hub_id, f"Hub {contact.hub_id}")) for contact in contacts]

    ToolMonitor.log_execution(
        db=db,
        tool_type="contacts-attention",
        operation="fetch_dashboard_widget",
        input_data={"limit": 5},
        output_data={"count": len(payload)},
        user_id=user.id,
    )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "contacts": payload,
        "total": len(payload),
    })
