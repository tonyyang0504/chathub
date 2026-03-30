from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.utils import get_current_user_optional
from app.database import AIUsage, BotProfile, Hub, get_db

router = APIRouter(tags=["cost-tracker"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _get_date_range(days: int):
    """Return (start_date, end_date) for the given number of days. 0 = all time."""
    end = datetime.utcnow()
    if days <= 0:
        return None, end
    return end - timedelta(days=days), end


def _base_query(db: Session, user_id: int, days: int, provider: Optional[str] = None):
    """Build a base query filtered by user, date range, and optional provider."""
    q = db.query(AIUsage).filter(AIUsage.user_id == user_id)
    start, _ = _get_date_range(days)
    if start:
        q = q.filter(AIUsage.created_at >= start)
    if provider:
        q = q.filter(AIUsage.provider == provider)
    return q


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})
    return templates.TemplateResponse("cost-tracker.html", {
        "request": request,
        "user": user,
        "active_page": "tools_cost_tracker",
        "page_title": "Cost Tracker",
    })


@router.get("/api/summary")
async def get_summary(
    request: Request,
    days: int = Query(default=30, ge=0, le=365),
    provider: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    q = _base_query(db, user.id, days, provider)

    row = q.with_entities(
        func.count(AIUsage.id).label("total_calls"),
        func.coalesce(func.sum(AIUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(AIUsage.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(AIUsage.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(AIUsage.cost_usd), 0.0).label("total_cost"),
    ).first()

    total_calls = row.total_calls or 0
    return {
        "total_calls": total_calls,
        "prompt_tokens": int(row.prompt_tokens),
        "completion_tokens": int(row.completion_tokens),
        "total_tokens": int(row.total_tokens),
        "total_cost": round(float(row.total_cost), 4),
        "avg_cost_per_call": round(float(row.total_cost) / total_calls, 6) if total_calls else 0,
        "avg_tokens_per_call": int(row.total_tokens) // total_calls if total_calls else 0,
    }


@router.get("/api/daily")
async def get_daily(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    provider: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    start, _ = _get_date_range(days)
    q = db.query(
        func.date(AIUsage.created_at).label("day"),
        func.count(AIUsage.id).label("calls"),
        func.coalesce(func.sum(AIUsage.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(AIUsage.cost_usd), 0.0).label("cost"),
    ).filter(
        AIUsage.user_id == user.id,
        AIUsage.created_at >= start,
    )
    if provider:
        q = q.filter(AIUsage.provider == provider)

    rows = q.group_by(func.date(AIUsage.created_at)).order_by(func.date(AIUsage.created_at)).all()

    return {
        "labels": [str(r.day) for r in rows],
        "costs": [round(float(r.cost), 4) for r in rows],
        "tokens": [int(r.tokens) for r in rows],
        "calls": [int(r.calls) for r in rows],
    }


@router.get("/api/breakdown")
async def get_breakdown(
    request: Request,
    days: int = Query(default=30, ge=0, le=365),
    group_by: str = Query(default="provider", pattern="^(provider|model|source|bot|hub)$"),
    provider: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    start, _ = _get_date_range(days)

    group_col_map = {
        "provider": AIUsage.provider,
        "model": AIUsage.model,
        "source": AIUsage.source,
        "bot": AIUsage.bot_id,
        "hub": AIUsage.hub_id,
    }
    group_col = group_col_map[group_by]

    q = db.query(
        group_col.label("key"),
        func.count(AIUsage.id).label("calls"),
        func.coalesce(func.sum(AIUsage.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(AIUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(AIUsage.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(AIUsage.cost_usd), 0.0).label("cost"),
    ).filter(AIUsage.user_id == user.id)

    if start:
        q = q.filter(AIUsage.created_at >= start)
    if provider:
        q = q.filter(AIUsage.provider == provider)

    rows = q.group_by(group_col).order_by(func.sum(AIUsage.cost_usd).desc()).all()

    # Resolve names for bot/hub IDs
    name_map = {}
    if group_by == "bot":
        bot_ids = [r.key for r in rows if r.key]
        if bot_ids:
            bots = db.query(BotProfile.id, BotProfile.name).filter(BotProfile.id.in_(bot_ids)).all()
            name_map = {b.id: b.name for b in bots}
    elif group_by == "hub":
        hub_ids = [r.key for r in rows if r.key]
        if hub_ids:
            hubs = db.query(Hub.id, Hub.name).filter(Hub.id.in_(hub_ids)).all()
            name_map = {h.id: h.name for h in hubs}

    items = []
    for r in rows:
        key = r.key
        label = str(key) if key else "(none)"
        if group_by in ("bot", "hub") and key:
            label = name_map.get(key, f"#{key}")
        items.append({
            "key": key,
            "label": label,
            "calls": int(r.calls),
            "tokens": int(r.tokens),
            "prompt_tokens": int(r.prompt_tokens),
            "completion_tokens": int(r.completion_tokens),
            "cost": round(float(r.cost), 4),
        })

    return {"group_by": group_by, "items": items}


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _sum_cost(since):
        row = db.query(
            func.coalesce(func.sum(AIUsage.cost_usd), 0.0),
            func.count(AIUsage.id),
        ).filter(AIUsage.user_id == user.id, AIUsage.created_at >= since).first()
        return round(float(row[0]), 4), int(row[1])

    today_cost, today_calls = _sum_cost(today_start)
    month_cost, month_calls = _sum_cost(month_start)

    # Top provider this month
    top_provider_row = db.query(
        AIUsage.provider,
        func.sum(AIUsage.cost_usd).label("cost"),
    ).filter(
        AIUsage.user_id == user.id,
        AIUsage.created_at >= month_start,
    ).group_by(AIUsage.provider).order_by(func.sum(AIUsage.cost_usd).desc()).first()

    top_provider = top_provider_row.provider if top_provider_row else None
    top_provider_cost = round(float(top_provider_row.cost), 4) if top_provider_row else 0

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "today_cost": today_cost,
        "today_calls": today_calls,
        "month_cost": month_cost,
        "month_calls": month_calls,
        "top_provider": top_provider,
        "top_provider_cost": top_provider_cost,
    })
