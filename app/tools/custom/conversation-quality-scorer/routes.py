import logging
import random
import time
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
from app.auth.utils import get_current_user_optional, decrypt_string
from app.database import (
    Contact, Conversation, Hub, HubBotMembership, Message, BotProfile,
    AIAgent, get_db,
)
from app.ai.factory import get_ai_provider
from app.tools.monitoring import ToolMonitor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation-quality-scorer"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])


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


def _score_conversation(messages: list[dict]) -> dict:
    """Score a conversation on multiple factors. Returns per-factor scores and overall grade."""
    if not messages:
        return {
            "overall": 0, "grade": "F",
            "response_time": 0, "message_balance": 0,
            "depth": 0, "engagement": 0,
            "details": {"total_messages": 0, "user_messages": 0, "bot_messages": 0,
                        "avg_response_sec": None, "duration_min": None},
        }

    user_msgs = [m for m in messages if m["role"] == "user"]
    bot_msgs = [m for m in messages if m["role"] == "assistant"]
    total = len(messages)
    n_user = len(user_msgs)
    n_bot = len(bot_msgs)

    # --- 1. Response Time Score (25%) ---
    response_times = []
    sorted_msgs = sorted(messages, key=lambda m: m["ts"])
    for i in range(1, len(sorted_msgs)):
        prev, cur = sorted_msgs[i - 1], sorted_msgs[i]
        if prev["role"] == "user" and cur["role"] == "assistant":
            diff = (cur["ts"] - prev["ts"]).total_seconds()
            if 0 < diff < 86400:  # ignore gaps > 1 day
                response_times.append(diff)

    if response_times:
        avg_resp = sum(response_times) / len(response_times)
        # <30s = 100, 30-60s = 80, 1-5min = 60, 5-15min = 40, 15-60min = 20, >60min = 10
        if avg_resp < 30:
            rt_score = 100
        elif avg_resp < 60:
            rt_score = 80
        elif avg_resp < 300:
            rt_score = 60
        elif avg_resp < 900:
            rt_score = 40
        elif avg_resp < 3600:
            rt_score = 20
        else:
            rt_score = 10
    else:
        avg_resp = None
        rt_score = 50  # no data — neutral

    # --- 2. Message Balance Score (20%) ---
    if n_user > 0 and n_bot > 0:
        ratio = min(n_user, n_bot) / max(n_user, n_bot)
        balance_score = int(ratio * 100)
    elif total > 0:
        balance_score = 20  # one-sided
    else:
        balance_score = 0

    # --- 3. Depth Score (25%) ---
    if total >= 20:
        depth_score = 100
    elif total >= 10:
        depth_score = 80
    elif total >= 6:
        depth_score = 60
    elif total >= 3:
        depth_score = 40
    elif total >= 1:
        depth_score = 20
    else:
        depth_score = 0

    # --- 4. Engagement Score (30%) ---
    # Based on average message length and continued back-and-forth
    avg_len_user = (sum(len(m["content"]) for m in user_msgs) / n_user) if n_user else 0
    avg_len_bot = (sum(len(m["content"]) for m in bot_msgs) / n_bot) if n_bot else 0

    # Longer messages = higher engagement
    len_score = min(100, int((avg_len_user + avg_len_bot) / 4))  # 200 chars avg = 100

    # Back-and-forth turns
    turns = 0
    for i in range(1, len(sorted_msgs)):
        if sorted_msgs[i]["role"] != sorted_msgs[i - 1]["role"]:
            turns += 1
    turn_score = min(100, turns * 10)  # 10 turns = 100

    engagement_score = int(len_score * 0.4 + turn_score * 0.6)

    # --- Overall ---
    overall = int(
        rt_score * 0.25
        + balance_score * 0.20
        + depth_score * 0.25
        + engagement_score * 0.30
    )

    # Grade
    if overall >= 90:
        grade = "A"
    elif overall >= 75:
        grade = "B"
    elif overall >= 60:
        grade = "C"
    elif overall >= 40:
        grade = "D"
    else:
        grade = "F"

    # Duration
    if len(sorted_msgs) >= 2:
        duration_min = round((sorted_msgs[-1]["ts"] - sorted_msgs[0]["ts"]).total_seconds() / 60, 1)
    else:
        duration_min = 0

    return {
        "overall": overall,
        "grade": grade,
        "response_time": rt_score,
        "message_balance": balance_score,
        "depth": depth_score,
        "engagement": engagement_score,
        "details": {
            "total_messages": total,
            "user_messages": n_user,
            "bot_messages": n_bot,
            "avg_response_sec": round(avg_resp, 1) if avg_resp is not None else None,
            "duration_min": duration_min,
        },
    }


def _fetch_messages_for_conversation(db: Session, conv_id: int, limit: int = 200) -> list[dict]:
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv_id)
        .order_by(Message.timestamp.asc())
        .limit(limit)
        .all()
    )
    return [
        {"role": m.role or "user", "content": m.content or "", "ts": m.timestamp or datetime.utcnow()}
        for m in msgs
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _get_user_hub_scope(user, db)
    return templates.TemplateResponse("conversation-quality-scorer.html", {
        "request": request,
        "user": user,
        "active_page": "tools_conversation_quality_scorer",
        "page_title": "Conversation Quality Scorer",
        "hubs": hubs,
    })


@router.get("/api/stats")
async def get_stats(
    request: Request,
    hub_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Aggregate quality stats across conversations."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    t0 = time.time()
    scoped, _ = _get_user_hub_scope(user, db, hub_id)

    if not scoped:
        return {"totals": {"conversations": 0, "scored": 0, "avg_score": 0, "grade_dist": {}},
                "grade_distribution": {}, "top_conversations": [], "bottom_conversations": []}

    # Get conversations via hub → bot membership
    bot_ids = (
        db.query(HubBotMembership.bot_profile_id)
        .filter(HubBotMembership.hub_id.in_(scoped))
        .all()
    )
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return {"totals": {"conversations": 0, "scored": 0, "avg_score": 0, "grade_dist": {}},
                "grade_distribution": {}, "top_conversations": [], "bottom_conversations": []}

    # Get recent conversations with at least 2 messages
    convs = (
        db.query(Conversation)
        .filter(Conversation.bot_profile_id.in_(bot_ids))
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(100)
        .all()
    )

    scored_conversations = []
    grade_dist = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}

    for conv in convs:
        msgs = _fetch_messages_for_conversation(db, conv.id, limit=100)
        if len(msgs) < 2:
            continue
        score_data = _score_conversation(msgs)
        grade_dist[score_data["grade"]] = grade_dist.get(score_data["grade"], 0) + 1

        # Get bot name
        bot = db.query(BotProfile).filter(BotProfile.id == conv.bot_profile_id).first()

        scored_conversations.append({
            "id": conv.id,
            "name": conv.chat_name or conv.chat_id or "Unknown",
            "bot_name": bot.name if bot else "Unknown",
            "last_message": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "is_group": conv.is_group or False,
            **score_data,
        })

    # Sort by score
    scored_conversations.sort(key=lambda c: c["overall"], reverse=True)

    total_scored = len(scored_conversations)
    avg_score = round(sum(c["overall"] for c in scored_conversations) / total_scored, 1) if total_scored else 0

    elapsed = int((time.time() - t0) * 1000)

    ToolMonitor.log_execution(
        db=db,
        tool_type="conversation-quality-scorer",
        operation="fetch_stats",
        hub_id=hub_id,
        input_data={"hub_ids": scoped},
        output_data={"scored": total_scored, "avg_score": avg_score, "grade_dist": grade_dist},
        execution_time_ms=elapsed,
        user_id=user.id,
    )

    return {
        "totals": {
            "conversations": len(convs),
            "scored": total_scored,
            "avg_score": avg_score,
            "avg_grade": _score_to_grade(avg_score),
        },
        "grade_distribution": grade_dist,
        "top_conversations": scored_conversations[:10],
        "bottom_conversations": list(reversed(scored_conversations[-5:])) if total_scored > 5 else [],
    }


@router.post("/api/analyze")
async def ai_analyze(
    request: Request,
    db: Session = Depends(get_db),
):
    """AI-powered deep analysis of a single conversation."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    conv_id = body.get("conversation_id")
    if not conv_id:
        raise HTTPException(status_code=400, detail="conversation_id required")

    # Verify ownership
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404)

    bot = db.query(BotProfile).filter(BotProfile.id == conv.bot_profile_id).first()
    if not bot or bot.user_id != user.id:
        raise HTTPException(status_code=403)

    t0 = time.time()

    msgs = _fetch_messages_for_conversation(db, conv.id, limit=50)
    score_data = _score_conversation(msgs)

    # Build conversation transcript
    transcript_lines = []
    for m in msgs[-30:]:  # last 30 messages
        role_label = "Customer" if m["role"] == "user" else "Bot"
        transcript_lines.append(f"{role_label}: {m['content'][:300]}")
    transcript = "\n".join(transcript_lines)

    # Try to get AI provider from the bot
    ai_analysis = None
    try:
        if bot.api_key_encrypted:
            api_key = decrypt_string(bot.api_key_encrypted)
            provider = get_ai_provider(
                bot.ai_provider or "openai",
                api_key,
                bot.model or None,
            )
            prompt = (
                "Analyze this conversation between a bot and a customer. "
                "Provide a brief analysis (3-5 sentences) covering:\n"
                "1. Overall conversation quality and effectiveness\n"
                "2. Whether the customer's needs were addressed\n"
                "3. Any areas for improvement\n"
                "4. Notable patterns (positive or negative)\n\n"
                f"Quality Score: {score_data['overall']}/100 (Grade: {score_data['grade']})\n"
                f"Response Time Score: {score_data['response_time']}/100\n"
                f"Message Balance: {score_data['message_balance']}/100\n"
                f"Depth: {score_data['depth']}/100\n"
                f"Engagement: {score_data['engagement']}/100\n\n"
                f"Transcript:\n{transcript}"
            )
            response = await provider.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=300,
            )
            ai_analysis = response.get("content", "").strip() if isinstance(response, dict) else str(response).strip()
    except Exception as e:
        logger.warning(f"AI analysis failed for conv {conv_id}: {e}")
        ai_analysis = None

    elapsed = int((time.time() - t0) * 1000)

    ToolMonitor.log_execution(
        db=db,
        tool_type="conversation-quality-scorer",
        operation="ai_analyze",
        hub_id=None,
        input_data={"conversation_id": conv_id, "conversation_name": conv.chat_name},
        output_data={"score": score_data["overall"], "grade": score_data["grade"], "ai_analysis": bool(ai_analysis)},
        execution_time_ms=elapsed,
        user_id=user.id,
    )

    return {
        **score_data,
        "ai_analysis": ai_analysis,
        "conversation_name": conv.chat_name or conv.chat_id or "Unknown",
    }


@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db)
    if not scoped:
        return templates.TemplateResponse("widgets/dashboard.html", {
            "request": request, "user": user,
            "avg_score": 0, "avg_grade": "—", "total_scored": 0, "grade_dist": {},
        })

    bot_ids = [b[0] for b in db.query(HubBotMembership.bot_profile_id).filter(HubBotMembership.hub_id.in_(scoped)).all()]

    if not bot_ids:
        return templates.TemplateResponse("widgets/dashboard.html", {
            "request": request, "user": user,
            "avg_score": 0, "avg_grade": "—", "total_scored": 0, "grade_dist": {},
        })

    convs = (
        db.query(Conversation)
        .filter(Conversation.bot_profile_id.in_(bot_ids))
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(30)
        .all()
    )

    scores = []
    grade_dist = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    for conv in convs:
        msgs = _fetch_messages_for_conversation(db, conv.id, limit=50)
        if len(msgs) < 2:
            continue
        sd = _score_conversation(msgs)
        scores.append(sd["overall"])
        grade_dist[sd["grade"]] = grade_dist.get(sd["grade"], 0) + 1

    avg_score = round(sum(scores) / len(scores), 1) if scores else 0

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request, "user": user,
        "avg_score": avg_score,
        "avg_grade": _score_to_grade(avg_score),
        "total_scored": len(scores),
        "grade_dist": grade_dist,
    })


def _score_to_grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Demo Data
# ---------------------------------------------------------------------------

_DEMO_CONVERSATIONS = [
    {
        "name": "Sarah Johnson",
        "messages": [
            ("user", "Hi, I'm interested in your premium plan. Can you tell me more?"),
            ("assistant", "Hello Sarah! Our premium plan includes unlimited messaging, priority support, and advanced analytics. It's $29/month. Would you like me to walk you through the features?"),
            ("user", "Yes please! What kind of analytics do you offer?"),
            ("assistant", "Great question! Our analytics dashboard shows message volume trends, response time metrics, customer sentiment analysis, and engagement scores. You also get weekly email reports with key insights."),
            ("user", "That sounds perfect. How do I upgrade?"),
            ("assistant", "I can help you upgrade right now! Just go to Settings > Billing > Upgrade Plan. You'll see the premium option there. Would you like a 7-day free trial first?"),
            ("user", "A free trial would be great!"),
            ("assistant", "Done! I've activated your 7-day premium trial. You'll have full access to all features starting now. Is there anything else I can help you with?"),
            ("user", "No that's everything, thank you so much!"),
            ("assistant", "You're welcome, Sarah! Enjoy the premium features. Don't hesitate to reach out if you need anything. Have a great day!"),
        ],
    },
    {
        "name": "Mike Chen",
        "messages": [
            ("user", "my order hasn't arrived yet and it's been 2 weeks"),
            ("assistant", "I'm sorry to hear about the delay, Mike. Let me look into your order right away. Could you provide your order number?"),
            ("user", "it's ORDER-4829"),
            ("assistant", "Thank you. I can see your order ORDER-4829 was shipped on March 25th. According to tracking, it appears to be held at the local distribution center. I'll escalate this with our shipping partner immediately."),
            ("user", "this is ridiculous, I paid for express shipping"),
            ("assistant", "I completely understand your frustration, and you're right to be upset. Since you paid for express shipping, I'm going to issue a full refund for the shipping cost right now, and I'll also add a 15% discount code for your next order as an apology."),
            ("user", "ok fine, when will i actually get my package?"),
            ("assistant", "Based on the escalation, your package should arrive within 1-2 business days. I've also set up a priority alert so you'll get a notification the moment it's out for delivery. I'll personally follow up tomorrow to make sure everything is on track."),
            ("user", "alright thanks"),
        ],
    },
    {
        "name": "Pizza Express Group",
        "messages": [
            ("user", "what time do you close tonight?"),
            ("assistant", "We're open until 11 PM tonight! Would you like to place an order?"),
            ("user", "ya can I get a large pepperoni"),
            ("assistant", "One large pepperoni pizza coming up! That'll be $18.99. Would you like delivery or pickup?"),
            ("user", "delivery to 45 Main St"),
            ("assistant", "Got it! Delivery to 45 Main St. Your estimated delivery time is 30-40 minutes. Your order total with delivery fee is $23.99. Shall I confirm the order?"),
            ("user", "yes confirm"),
            ("assistant", "Order confirmed! You'll receive a confirmation text shortly with tracking info. Enjoy your pizza! 🍕"),
        ],
    },
    {
        "name": "Alex Rivera",
        "messages": [
            ("user", "hello"),
            ("assistant", "Hi there! How can I help you today?"),
            ("user", "nothing really just browsing"),
        ],
    },
    {
        "name": "Tech Support - Emma W",
        "messages": [
            ("user", "I can't log into my account, it keeps saying invalid password"),
            ("assistant", "I'm sorry you're having trouble logging in, Emma. Let me help you resolve this. Have you tried the 'Forgot Password' option on the login page?"),
            ("user", "yes but I never received the reset email"),
            ("assistant", "That can happen sometimes. Let me check a few things: 1) Can you confirm the email address associated with your account? 2) Have you checked your spam/junk folder?"),
            ("user", "email is emma.w@gmail.com and yes I checked spam"),
            ("assistant", "Thank you. I can see your account and I've just sent a manual password reset link to emma.w@gmail.com. This one bypasses the normal queue so it should arrive within 1-2 minutes. Please check your inbox."),
            ("user", "got it! resetting now"),
            ("assistant", "Great! Let me know once you've successfully logged in."),
            ("user", "I'm in! it worked, thank you"),
            ("assistant", "Wonderful! Glad we got that sorted. As a security tip, I'd recommend enabling two-factor authentication in your account settings to add an extra layer of protection. Is there anything else I can help with?"),
            ("user", "no that's all, you were very helpful"),
            ("assistant", "Thank you, Emma! Happy to help. Have a great rest of your day!"),
        ],
    },
    {
        "name": "David Park",
        "messages": [
            ("user", "I want to cancel my subscription"),
            ("assistant", "I'm sorry to hear you'd like to cancel, David. Before I process that, may I ask what's prompting the cancellation? I'd like to see if there's anything we can do to improve your experience."),
            ("user", "it's too expensive for what I get"),
            ("assistant", "I understand budget concerns. We actually have a few options that might work better for you: 1) Our basic plan at $9/month, 2) An annual plan that saves 30%, or 3) I can apply a 3-month 50% discount to your current plan. Would any of these interest you?"),
            ("user", "the 50% discount sounds good actually"),
            ("assistant", "I've applied the 50% discount to your account for the next 3 months. Your new rate is $14.50/month instead of $29. The discount will show on your next billing cycle. Is there anything else I can help with?"),
            ("user", "nope that works, thanks for offering that"),
            ("assistant", "Of course! We value you as a customer and want to make sure you're getting great value. Enjoy the savings, and feel free to reach out anytime!"),
        ],
    },
    {
        "name": "Group - Marketing Team",
        "messages": [
            ("user", "Can the bot schedule our social media posts?"),
            ("assistant", "Yes! I can help schedule social media posts. I support Facebook, Twitter, Instagram, and LinkedIn. Would you like me to set up a posting schedule?"),
            ("user", "yes for twitter and instagram"),
            ("assistant", "I'll set up scheduling for Twitter and Instagram. To get started, I'll need: 1) Your preferred posting times, 2) How many posts per day, 3) Any content themes or hashtags you'd like to include. What works best for your team?"),
            ("user", "twice daily, 9am and 6pm, focus on product launches"),
            ("assistant", "Perfect! I've configured the schedule: Twitter and Instagram, twice daily at 9:00 AM and 6:00 PM, focusing on product launch content. I'll generate draft posts for your review each morning. You can approve or edit them before they go live."),
        ],
    },
    {
        "name": "Lisa Thompson",
        "messages": [
            ("user", "hi"),
            ("assistant", "Hello! How can I assist you today?"),
            ("user", "do you have any discounts"),
            ("assistant", "Yes! We currently have a spring sale with 20% off all plans. Would you like more details?"),
            ("user", "maybe later bye"),
        ],
    },
]


@router.post("/api/generate-demo")
async def generate_demo_data(request: Request, db: Session = Depends(get_db)):
    """Generate sample conversations for demo/testing purposes."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    from app.auth.utils import encrypt_string

    # Check if user already has a demo hub
    existing = db.query(Hub).filter(Hub.user_id == user.id, Hub.name == "Demo Hub (Quality Scorer)").first()
    if existing:
        return {"status": "exists", "message": "Demo data already exists. Score your conversations!"}

    # Create a demo bot
    bot = BotProfile(
        user_id=user.id,
        name="Demo Support Bot",
        platform_type="whatsapp",
        ai_provider="openai",
        api_key_encrypted=encrypt_string("demo-key-not-real"),
        model="gpt-4o-mini",
    )
    db.add(bot)
    db.flush()

    # Create a demo hub
    hub = Hub(
        user_id=user.id,
        name="Demo Hub (Quality Scorer)",
        task_type="support",
        ai_provider="openai",
    )
    db.add(hub)
    db.flush()

    # Link bot to hub
    membership = HubBotMembership(hub_id=hub.id, bot_profile_id=bot.id, role="responder")
    db.add(membership)
    db.flush()

    # Create conversations with varying quality
    now = datetime.utcnow()
    created = 0

    for i, demo in enumerate(_DEMO_CONVERSATIONS):
        conv_start = now - timedelta(hours=random.randint(1, 72))
        is_group = "group" in demo["name"].lower()

        conv = Conversation(
            bot_profile_id=bot.id,
            chat_id=f"demo_{i}_{int(now.timestamp())}",
            chat_name=demo["name"],
            is_group=is_group,
            last_message_at=conv_start + timedelta(seconds=len(demo["messages"]) * random.randint(15, 120)),
        )
        db.add(conv)
        db.flush()

        msg_time = conv_start
        for role, content in demo["messages"]:
            # Vary response times to create different scores
            if role == "assistant":
                delay = random.randint(5, 180)  # 5s to 3min
            else:
                delay = random.randint(10, 300)  # 10s to 5min
            msg_time += timedelta(seconds=delay)

            msg = Message(
                conversation_id=conv.id,
                content=content,
                role=role,
                sender_name=demo["name"] if role == "user" else "Bot",
                timestamp=msg_time,
            )
            db.add(msg)

        conv.last_message_at = msg_time
        created += 1

    db.commit()

    return {"status": "created", "message": f"Created {created} demo conversations in 'Demo Hub (Quality Scorer)'. Hit Score to see results!"}
