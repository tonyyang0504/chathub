import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy.orm import Session

from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional, decrypt_string
from app.database import (
    Contact, Conversation, Hub, HubBotMembership, Message, BotProfile,
    AIAgent, get_db,
)
from app.ai.factory import get_ai_provider
from app.tools.monitoring import ToolMonitor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["escalation-detector"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])

# ---------------------------------------------------------------------------
# Escalation levels and colors
# ---------------------------------------------------------------------------
ESCALATION_LEVELS = {
    "critical": {"label": "Critical", "color": "#ef4444", "order": 0},
    "high": {"label": "High", "color": "#f97316", "order": 1},
    "medium": {"label": "Medium", "color": "#f59e0b", "order": 2},
    "low": {"label": "Low", "color": "#22c55e", "order": 3},
    "none": {"label": "None", "color": "#94a3b8", "order": 4},
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_hub_scope(user, db: Session, hub_id: Optional[int] = None):
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], []
    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)
    scoped = [hub_id] if hub_id else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name).all()
    return scoped, hubs


def _fetch_recent_messages(db: Session, conv_id: int, limit: int = 30) -> list[dict]:
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv_id)
        .order_by(Message.timestamp.desc())
        .limit(limit)
        .all()
    )
    msgs.reverse()
    return [
        {"role": m.role or "user", "content": m.content or "", "ts": m.timestamp or datetime.utcnow()}
        for m in msgs
    ]


def _rule_based_escalation(messages: list[dict]) -> dict:
    """Fast rule-based escalation detection (no AI needed)."""
    if not messages:
        return {"level": "none", "score": 0, "signals": [], "reason": "No messages"}

    signals = []
    score = 0

    # Analyze user messages only
    user_msgs = [m for m in messages if m["role"] == "user"]
    bot_msgs = [m for m in messages if m["role"] == "assistant"]

    if not user_msgs:
        return {"level": "none", "score": 0, "signals": [], "reason": "No user messages"}

    all_user_text = " ".join(m["content"].lower() for m in user_msgs)
    recent_user_text = " ".join(m["content"].lower() for m in user_msgs[-5:])

    # --- Signal 1: Anger / frustration keywords ---
    anger_keywords = [
        "angry", "furious", "terrible", "worst", "horrible", "disgusting",
        "unacceptable", "ridiculous", "outrageous", "pathetic", "useless",
        "hate", "sick of", "fed up", "pissed", "wtf", "damn", "hell",
        "scam", "fraud", "rip off", "ripoff", "lawsuit", "lawyer", "sue",
    ]
    anger_count = sum(1 for kw in anger_keywords if kw in all_user_text)
    if anger_count >= 3:
        signals.append("Multiple anger expressions")
        score += 35
    elif anger_count >= 1:
        signals.append("Frustration detected")
        score += 20

    # --- Signal 2: Profanity ---
    profanity = ["fuck", "shit", "bullshit", "ass ", "bitch", "crap"]
    if any(w in all_user_text for w in profanity):
        signals.append("Profanity used")
        score += 25

    # --- Signal 3: Explicit help request ---
    help_phrases = [
        "speak to a human", "talk to someone", "real person", "manager",
        "supervisor", "escalate", "complaint", "not helpful", "doesn't help",
        "human agent", "live agent", "customer service", "support team",
    ]
    if any(p in all_user_text for p in help_phrases):
        signals.append("Requesting human agent")
        score += 40

    # --- Signal 4: Repeated questions (user asking the same thing) ---
    if len(user_msgs) >= 3:
        recent_contents = [m["content"].lower().strip() for m in user_msgs[-5:]]
        # Check for similar consecutive messages
        for i in range(1, len(recent_contents)):
            if len(recent_contents[i]) > 10 and (
                recent_contents[i] == recent_contents[i - 1]
                or (len(set(recent_contents[i].split()) & set(recent_contents[i - 1].split())) / max(len(recent_contents[i].split()), 1) > 0.6)
            ):
                signals.append("Repeated question")
                score += 20
                break

    # --- Signal 5: Question marks piling up (unanswered questions) ---
    recent_questions = sum(1 for m in user_msgs[-5:] if "?" in m["content"])
    if recent_questions >= 3:
        signals.append("Multiple unanswered questions")
        score += 15

    # --- Signal 6: Urgency keywords ---
    urgency_words = [
        "urgent", "emergency", "asap", "immediately", "right now",
        "critical", "deadline", "time sensitive", "can't wait",
    ]
    if any(w in all_user_text for w in urgency_words):
        signals.append("Urgency expressed")
        score += 15

    # --- Signal 7: Negative sentiment in recent messages ---
    negative_phrases = [
        "not working", "broken", "failed", "error", "problem",
        "issue", "bug", "crash", "doesn't work", "can't",
        "unable", "impossible", "still not", "again",
    ]
    neg_count = sum(1 for p in negative_phrases if p in recent_user_text)
    if neg_count >= 3:
        signals.append("Persistent issues reported")
        score += 20
    elif neg_count >= 1:
        signals.append("Issue reported")
        score += 10

    # --- Signal 8: Bot not responding or very short responses ---
    if bot_msgs:
        recent_bot = bot_msgs[-3:] if len(bot_msgs) >= 3 else bot_msgs
        avg_bot_len = sum(len(m["content"]) for m in recent_bot) / len(recent_bot)
        if avg_bot_len < 20:
            signals.append("Bot giving very short responses")
            score += 15

    # --- Signal 9: Long time since last bot response ---
    if len(messages) >= 2:
        last_msg = messages[-1]
        second_last = messages[-2]
        if last_msg["role"] == "user" and second_last["role"] == "user":
            signals.append("Multiple user messages without bot reply")
            score += 20

    # Cap at 100
    score = min(score, 100)

    # Determine level
    if score >= 70:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    elif score > 0:
        level = "low"
    else:
        level = "none"

    reason = "; ".join(signals) if signals else "No escalation signals detected"

    return {"level": level, "score": score, "signals": signals, "reason": reason}


async def _ai_escalation_analysis(messages: list[dict], provider, conv_name: str) -> Optional[dict]:
    """Use AI to analyze escalation. Returns dict or None on failure."""
    transcript_lines = []
    for m in messages[-20:]:
        role_label = "Customer" if m["role"] == "user" else "Bot"
        transcript_lines.append(f"{role_label}: {m['content'][:300]}")
    transcript = "\n".join(transcript_lines)

    prompt = (
        "Analyze this customer-bot conversation for escalation signals. "
        "Rate the escalation level and explain why.\n\n"
        "Respond in this exact JSON format (no markdown):\n"
        '{"level": "critical|high|medium|low|none", "score": 0-100, '
        '"signals": ["signal1", "signal2"], "reason": "Brief explanation"}\n\n'
        "Signals to look for:\n"
        "- Customer anger, frustration, or profanity\n"
        "- Requests to speak with a human/manager\n"
        "- Repeated unanswered questions\n"
        "- Bot confusion or unhelpful responses\n"
        "- Urgent issues (billing, security, outages)\n"
        "- Threats (legal, cancellation, social media)\n\n"
        f"Conversation with {conv_name}:\n{transcript}"
    )

    try:
        response = await provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        content = response.get("content", "") if isinstance(response, dict) else str(response)
        content = content.strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(content)
        # Validate
        if result.get("level") not in ESCALATION_LEVELS:
            result["level"] = "medium"
        result["score"] = max(0, min(100, int(result.get("score", 50))))
        result.setdefault("signals", [])
        result.setdefault("reason", "AI analysis")
        return result
    except Exception as e:
        logger.warning(f"AI escalation analysis failed: {e}")
        return None


def _get_ai_provider_for_hub(hub, db: Session):
    """Get an AI provider from hub config or its agents."""
    if hub.api_key_encrypted:
        try:
            api_key = decrypt_string(hub.api_key_encrypted)
            return get_ai_provider(hub.ai_provider or "openai", api_key, hub.model)
        except Exception:
            pass

    # Try agents
    agent = db.query(AIAgent).filter(AIAgent.hub_id == hub.id, AIAgent.api_key_encrypted.isnot(None)).first()
    if agent and agent.api_key_encrypted:
        try:
            api_key = decrypt_string(agent.api_key_encrypted)
            return get_ai_provider(agent.ai_provider or hub.ai_provider or "openai", api_key, agent.model)
        except Exception:
            pass

    # Try bots in the hub
    memberships = db.query(HubBotMembership).filter(HubBotMembership.hub_id == hub.id).all()
    for mem in memberships:
        bot = db.query(BotProfile).filter(BotProfile.id == mem.bot_profile_id).first()
        if bot and bot.api_key_encrypted:
            try:
                api_key = decrypt_string(bot.api_key_encrypted)
                return get_ai_provider(bot.ai_provider or "openai", api_key, bot.model)
            except Exception:
                continue

    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _get_user_hub_scope(user, db)
    return templates.TemplateResponse("escalation-detector.html", {
        "request": request,
        "user": user,
        "active_page": "tools_escalation_detector",
        "page_title": "Escalation Detector",
        "hubs": hubs,
    })


@router.get("/api/scan")
async def scan_conversations(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    use_ai: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """Scan conversations for escalation signals."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    t0 = time.time()
    scoped, _ = _get_user_hub_scope(user, db, hub_id)

    if not scoped:
        return {"conversations": [], "stats": {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0, "escalation_rate": 0}}

    # Get bot IDs from hubs
    bot_ids = [b[0] for b in db.query(HubBotMembership.bot_profile_id).filter(HubBotMembership.hub_id.in_(scoped)).all()]

    if not bot_ids:
        return {"conversations": [], "stats": {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0, "escalation_rate": 0}}

    # Get recent conversations with messages
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Conversation.last_message_at.isnot(None),
        )
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(100)
        .all()
    )

    # Get AI provider if requested
    ai_provider = None
    if use_ai and scoped:
        hub = db.query(Hub).filter(Hub.id == scoped[0]).first()
        if hub:
            ai_provider = _get_ai_provider_for_hub(hub, db)

    results = []
    stats = {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0}

    for conv in convs:
        msgs = _fetch_recent_messages(db, conv.id, limit=30)
        if len(msgs) < 2:
            continue

        # Rule-based detection
        escalation = _rule_based_escalation(msgs)

        # AI enhancement (only for medium+ or if explicitly requested)
        if ai_provider and (use_ai or escalation["level"] in ("critical", "high", "medium")):
            ai_result = await _ai_escalation_analysis(msgs, ai_provider, conv.chat_name or "Unknown")
            if ai_result:
                # Merge: take the higher score between rule-based and AI
                if ai_result["score"] > escalation["score"]:
                    escalation = ai_result
                else:
                    # Add AI signals we didn't catch
                    for sig in ai_result.get("signals", []):
                        if sig not in escalation["signals"]:
                            escalation["signals"].append(sig)

        # Get bot name
        bot = db.query(BotProfile).filter(BotProfile.id == conv.bot_profile_id).first()

        # Get contact info
        contact = None
        if conv.phone:
            contact = db.query(Contact).filter(Contact.phone == conv.phone, Contact.hub_id.in_(scoped)).first()

        last_user_msg = ""
        for m in reversed(msgs):
            if m["role"] == "user":
                last_user_msg = m["content"][:120]
                break

        stats["total"] += 1
        stats[escalation["level"]] = stats.get(escalation["level"], 0) + 1

        results.append({
            "conversation_id": conv.id,
            "name": conv.chat_name or conv.phone or conv.chat_id or "Unknown",
            "phone": conv.phone,
            "bot_name": bot.name if bot else "Unknown",
            "is_group": conv.is_group or False,
            "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "last_user_message": last_user_msg,
            "human_takeover": conv.human_takeover or False,
            "contact_sentiment": contact.sentiment if contact else None,
            "level": escalation["level"],
            "score": escalation["score"],
            "signals": escalation["signals"],
            "reason": escalation["reason"],
        })

    # Sort by escalation score descending
    results.sort(key=lambda r: r["score"], reverse=True)

    escalated = stats["critical"] + stats["high"] + stats["medium"]
    stats["escalation_rate"] = round(escalated / max(stats["total"], 1) * 100, 1)

    elapsed = int((time.time() - t0) * 1000)

    ToolMonitor.log_execution(
        db=db,
        tool_type="escalation-detector",
        operation="scan",
        hub_id=hub_id,
        input_data={"hub_ids": scoped, "use_ai": use_ai, "conversations_checked": stats["total"]},
        output_data={"critical": stats["critical"], "high": stats["high"], "medium": stats["medium"], "escalation_rate": stats["escalation_rate"]},
        execution_time_ms=elapsed,
        user_id=user.id,
    )

    return {"conversations": results, "stats": stats}


@router.get("/api/conversation/{conv_id}/messages")
async def get_conversation_messages(
    request: Request,
    conv_id: int,
    db: Session = Depends(get_db),
):
    """Get messages for a specific conversation for the detail modal."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404)

    bot = db.query(BotProfile).filter(BotProfile.id == conv.bot_profile_id).first()
    if not bot or bot.user_id != user.id:
        raise HTTPException(status_code=403)

    msgs = _fetch_recent_messages(db, conv.id, limit=50)
    return {
        "messages": [
            {
                "role": m["role"],
                "content": m["content"],
                "timestamp": m["ts"].isoformat() if m["ts"] else None,
            }
            for m in msgs
        ],
        "conversation_name": conv.chat_name or conv.phone or "Unknown",
    }


@router.get("/api/activity")
async def get_activity(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    limit: int = Query(default=10),
    db: Session = Depends(get_db),
):
    """Get recent scan activity log."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    executions = ToolMonitor.get_recent_executions(
        db=db,
        tool_type="escalation-detector",
        hub_id=hub_id,
        limit=limit,
    )

    return {
        "activity": [
            {
                "id": e.id,
                "operation": e.operation,
                "input_data": json.loads(e.input_data) if e.input_data else {},
                "output_data": json.loads(e.output_data) if e.output_data else {},
                "status": e.status,
                "execution_time_ms": e.execution_time_ms,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in executions
        ]
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db)
    critical = 0
    high = 0
    total_scanned = 0

    if scoped:
        bot_ids = [b[0] for b in db.query(HubBotMembership.bot_profile_id).filter(HubBotMembership.hub_id.in_(scoped)).all()]
        if bot_ids:
            convs = (
                db.query(Conversation)
                .filter(
                    Conversation.bot_profile_id.in_(bot_ids),
                    Conversation.last_message_at.isnot(None),
                )
                .order_by(Conversation.last_message_at.desc().nullslast())
                .limit(30)
                .all()
            )
            for conv in convs:
                msgs = _fetch_recent_messages(db, conv.id, limit=20)
                if len(msgs) < 2:
                    continue
                total_scanned += 1
                esc = _rule_based_escalation(msgs)
                if esc["level"] == "critical":
                    critical += 1
                elif esc["level"] == "high":
                    high += 1

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "critical": critical,
        "high": high,
        "total_scanned": total_scanned,
    })
