from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func, and_, case
from sqlalchemy.orm import Session, aliased

from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import (
    BotProfile,
    Conversation,
    Hub,
    HubBotMembership,
    Message,
    get_db,
)
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["response-time-tracker"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


def _get_user_bot_ids(user, db: Session, hub_id: Optional[int] = None) -> tuple[list[int], list[int]]:
    """Return (hub_ids, bot_ids) the user owns, optionally filtered by hub."""
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        # Fallback: bots not in any hub
        bots = db.query(BotProfile.id).filter(BotProfile.user_id == user.id).all()
        return [], [b.id for b in bots]

    if hub_id is not None:
        if hub_id not in hub_ids:
            raise HTTPException(status_code=403)
        hub_ids = [hub_id]

    bot_ids = [
        row.bot_profile_id
        for row in db.query(HubBotMembership.bot_profile_id)
        .filter(HubBotMembership.hub_id.in_(hub_ids))
        .distinct()
        .all()
    ]

    # Also include bots not in any hub
    standalone_bots = (
        db.query(BotProfile.id)
        .filter(
            BotProfile.user_id == user.id,
            ~BotProfile.id.in_(
                db.query(HubBotMembership.bot_profile_id)
                .filter(HubBotMembership.hub_id.in_(get_user_hub_ids(user, db)))
            ),
        )
        .all()
    )
    if hub_id is None:
        bot_ids.extend([b.id for b in standalone_bots])

    return hub_ids, bot_ids


def _compute_response_times(db: Session, bot_ids: list[int], days: int = 7) -> dict:
    """Compute response time metrics from message pairs (user → assistant)."""
    if not bot_ids:
        return {
            "overall": {"avg": 0, "median": 0, "min": 0, "max": 0, "count": 0},
            "per_bot": [],
            "hourly": [0] * 24,
            "daily": [],
            "slow_responses": [],
        }

    since = datetime.utcnow() - timedelta(days=days)

    # Get all messages in scope, ordered by conversation and timestamp
    messages = (
        db.query(
            Message.id,
            Message.conversation_id,
            Message.role,
            Message.timestamp,
            Message.content,
            Conversation.bot_profile_id,
            Conversation.chat_name,
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= since,
            Message.role.in_(["user", "assistant"]),
        )
        .order_by(Message.conversation_id, Message.timestamp)
        .all()
    )

    # Group by conversation and find user→assistant pairs
    response_times = []  # (bot_id, seconds, hour, date, chat_name, content_preview)
    prev_msg = None

    for msg in messages:
        if prev_msg and prev_msg.conversation_id == msg.conversation_id:
            if prev_msg.role == "user" and msg.role == "assistant":
                delta = (msg.timestamp - prev_msg.timestamp).total_seconds()
                # Filter out unreasonably long gaps (>1 hour = probably not a direct response)
                if 0 < delta <= 3600:
                    response_times.append({
                        "bot_id": msg.bot_profile_id,
                        "seconds": delta,
                        "hour": msg.timestamp.hour,
                        "date": msg.timestamp.strftime("%Y-%m-%d"),
                        "chat_name": msg.chat_name or "Unknown",
                        "content_preview": (msg.content or "")[:80],
                        "timestamp": msg.timestamp.isoformat(),
                    })
        prev_msg = msg

    if not response_times:
        return {
            "overall": {"avg": 0, "median": 0, "min": 0, "max": 0, "count": 0},
            "per_bot": [],
            "hourly": [0] * 24,
            "daily": [],
            "slow_responses": [],
        }

    # Overall stats
    all_seconds = sorted([r["seconds"] for r in response_times])
    count = len(all_seconds)
    avg = sum(all_seconds) / count
    median = all_seconds[count // 2] if count % 2 == 1 else (all_seconds[count // 2 - 1] + all_seconds[count // 2]) / 2

    overall = {
        "avg": round(avg, 1),
        "median": round(median, 1),
        "min": round(all_seconds[0], 1),
        "max": round(all_seconds[-1], 1),
        "count": count,
    }

    # Per-bot stats
    bot_map = {}
    for r in response_times:
        bot_map.setdefault(r["bot_id"], []).append(r["seconds"])

    bot_names = {
        row.id: row.name
        for row in db.query(BotProfile.id, BotProfile.name)
        .filter(BotProfile.id.in_(bot_ids))
        .all()
    }

    per_bot = []
    for bid, times in bot_map.items():
        times_sorted = sorted(times)
        n = len(times_sorted)
        bot_avg = sum(times_sorted) / n
        bot_median = times_sorted[n // 2] if n % 2 == 1 else (times_sorted[n // 2 - 1] + times_sorted[n // 2]) / 2
        per_bot.append({
            "bot_id": bid,
            "bot_name": bot_names.get(bid, f"Bot {bid}"),
            "avg": round(bot_avg, 1),
            "median": round(bot_median, 1),
            "min": round(times_sorted[0], 1),
            "max": round(times_sorted[-1], 1),
            "count": n,
        })
    per_bot.sort(key=lambda x: x["avg"])

    # Hourly distribution (average response time per hour)
    hourly_map = {}
    for r in response_times:
        hourly_map.setdefault(r["hour"], []).append(r["seconds"])
    hourly = [
        round(sum(hourly_map.get(h, [0])) / max(len(hourly_map.get(h, [1])), 1), 1)
        for h in range(24)
    ]

    # Daily trend
    daily_map = {}
    for r in response_times:
        daily_map.setdefault(r["date"], []).append(r["seconds"])
    daily = [
        {"date": d, "avg": round(sum(times) / len(times), 1), "count": len(times)}
        for d, times in sorted(daily_map.items())
    ]

    # Slow responses (>60s)
    slow = sorted(
        [r for r in response_times if r["seconds"] > 60],
        key=lambda x: x["seconds"],
        reverse=True,
    )[:10]

    return {
        "overall": overall,
        "per_bot": per_bot,
        "hourly": hourly,
        "daily": daily,
        "slow_responses": slow,
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    hub_ids = get_user_hub_ids(user, db)
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name).all() if hub_ids else []

    return templates.TemplateResponse("response-time-tracker.html", {
        "request": request,
        "user": user,
        "active_page": "tools_response_time_tracker",
        "page_title": "Response Time Tracker",
        "hubs": hubs,
    })


@router.get("/api/stats")
async def get_stats(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    hub_ids, bot_ids = _get_user_bot_ids(user, db, hub_id)
    stats = _compute_response_times(db, bot_ids, days)

    ToolMonitor.log_execution(
        db=db,
        tool_type="response-time-tracker",
        operation="fetch_stats",
        hub_id=hub_id,
        input_data={"days": days, "bot_count": len(bot_ids)},
        output_data={
            "response_count": stats["overall"]["count"],
            "avg_seconds": stats["overall"]["avg"],
        },
        user_id=user.id,
    )

    return stats


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    _, bot_ids = _get_user_bot_ids(user, db)
    stats = _compute_response_times(db, bot_ids, days=7)

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "overall": stats["overall"],
        "per_bot": stats["per_bot"][:5],
    })
