from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy.orm import Session

from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional
from app.database import Conversation, Hub, HubBotMembership, Message, get_db
from app.tools.monitoring import ToolMonitor

router = APIRouter(tags=["message-content-analytics"])

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


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})
    _, hubs, _ = _resolve_hub_scope(user, db, None)
    return templates.TemplateResponse("message-content-analytics.html", {
        "request": request,
        "user": user,
        "active_page": "tools_message_content_analytics",
        "page_title": "Message Content Analytics",
        "hubs": hubs,
    })


@router.get("/api/messages")
async def get_messages(
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
        return {"messages": []}

    now = datetime.utcnow()
    start = now - timedelta(days=days)

    query = (
        db.query(Message.content, Message.timestamp, Message.direction)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(scoped_hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= start,
            Message.content.isnot(None),
            Message.content != "",
        )
    )

    if direction == "incoming":
        query = query.filter(Message.direction == "incoming")
    elif direction == "outgoing":
        query = query.filter(Message.direction == "outgoing")

    messages = query.order_by(Message.timestamp.asc()).all()

    result = [
        {
            "content": m.content,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            "direction": m.direction,
        }
        for m in messages
    ]

    ToolMonitor.log_execution(
        db=db,
        tool_type="message-content-analytics",
        operation="fetch_messages",
        hub_id=hub_id,
        input_data={"direction": direction, "days": days},
        output_data={"message_count": len(result)},
        user_id=user.id,
    )

    return {"messages": result}


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return templates.TemplateResponse("widgets/dashboard.html", {
            "request": request,
            "total_messages": 0,
            "top_keywords": [],
        })

    now = datetime.utcnow()
    start = now - timedelta(days=7)

    messages = (
        db.query(Message.content)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(HubBotMembership, HubBotMembership.bot_profile_id == Conversation.bot_profile_id)
        .filter(
            HubBotMembership.hub_id.in_(hub_ids),
            HubBotMembership.is_active.is_(True),
            Message.timestamp >= start,
            Message.content.isnot(None),
            Message.content != "",
            Message.direction == "incoming",
        )
        .all()
    )

    # Simple server-side keyword extraction for widget
    stopwords = _get_stopwords()
    word_counts: dict[str, int] = {}
    for (content,) in messages:
        for word in _extract_words(content):
            if word not in stopwords and len(word) > 2:
                word_counts[word] = word_counts.get(word, 0) + 1

    top_keywords = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "total_messages": len(messages),
        "top_keywords": top_keywords,
    })


def _extract_words(text: str) -> list[str]:
    import re
    return [w.lower() for w in re.findall(r'[a-zA-Z\u0600-\u06FF\u0400-\u04FF\u4e00-\u9fff]+', text) if len(w) > 1]


def _get_stopwords() -> set[str]:
    return {
        "the", "be", "to", "of", "and", "a", "in", "that", "have", "i",
        "it", "for", "not", "on", "with", "he", "as", "you", "do", "at",
        "this", "but", "his", "by", "from", "they", "we", "say", "her",
        "she", "or", "an", "will", "my", "one", "all", "would", "there",
        "their", "what", "so", "up", "out", "if", "about", "who", "get",
        "which", "go", "me", "when", "make", "can", "like", "time", "no",
        "just", "him", "know", "take", "people", "into", "year", "your",
        "good", "some", "could", "them", "see", "other", "than", "then",
        "now", "look", "only", "come", "its", "over", "think", "also",
        "back", "after", "use", "two", "how", "our", "work", "first",
        "well", "way", "even", "new", "want", "because", "any", "these",
        "give", "day", "most", "us", "are", "was", "were", "been", "has",
        "had", "did", "got", "may", "am", "is", "yes", "yeah", "ok",
        "okay", "hi", "hello", "hey", "thanks", "thank", "please", "sorry",
        "sure", "right", "oh", "well", "um", "uh", "hm", "hmm",
    }
