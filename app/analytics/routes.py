"""
Analytics Routes
"""

from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel

from app.database import get_db, User, BotProfile, Conversation, Message, ActivityLog, Hub, Contact, HubBotMembership
from app.auth.utils import get_current_user
from app.auth.ownership import get_user_hub_ids

router = APIRouter(tags=["Analytics"])


# ============== Schemas ==============

class DailyStats(BaseModel):
    date: str
    messages_sent: int
    messages_received: int


class BotAnalytics(BaseModel):
    bot_id: int
    bot_name: str
    total_conversations: int
    total_messages: int
    messages_sent: int
    messages_received: int
    active_conversations: int
    daily_stats: List[DailyStats]


class OverviewStats(BaseModel):
    total_bots: int
    active_bots: int
    total_conversations: int
    total_messages: int
    messages_today: int
    messages_this_week: int
    avg_response_time: Optional[str] = None


class DailyStatsExtended(BaseModel):
    date: str
    messages_sent: int
    messages_received: int
    active_chats: int
    new_conversations: int


class DailyStatsResponse(BaseModel):
    daily_stats: List[DailyStatsExtended]
    hourly_stats: List[int]
    message_types: dict


class TopConversation(BaseModel):
    id: int
    chat_name: str
    message_count: int
    last_active: Optional[datetime]


# ============== Routes ==============

@router.get("/overview", response_model=OverviewStats)
async def get_overview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get overview statistics for current user."""
    # Get user's bots
    bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return OverviewStats(
            total_bots=0,
            active_bots=0,
            total_conversations=0,
            total_messages=0,
            messages_today=0,
            messages_this_week=0
        )

    # Total bots
    total_bots = len(bot_ids)

    # Active bots
    active_bots = db.query(func.count(BotProfile.id)).filter(
        BotProfile.id.in_(bot_ids),
        BotProfile.is_running == True
    ).scalar() or 0

    # Total conversations
    total_conversations = db.query(func.count(Conversation.id)).filter(
        Conversation.bot_profile_id.in_(bot_ids)
    ).scalar() or 0

    # Total messages
    total_messages = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids)
    ).scalar() or 0

    # Messages today
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    messages_today = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Message.timestamp >= today
    ).scalar() or 0

    # Messages this week
    week_ago = today - timedelta(days=7)
    messages_this_week = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Message.timestamp >= week_ago
    ).scalar() or 0

    return OverviewStats(
        total_bots=total_bots,
        active_bots=active_bots,
        total_conversations=total_conversations,
        total_messages=total_messages,
        messages_today=messages_today,
        messages_this_week=messages_this_week,
        avg_response_time="< 5s"  # Placeholder - would need response time tracking
    )


@router.get("/daily")
async def get_daily_stats(
    days: int = Query(7, ge=1, le=90),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get daily statistics for all user's bots."""
    # Get user's bots
    bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return {
            "daily_stats": [],
            "hourly_stats": [0] * 24,
            "message_types": {"received": 0, "sent": 0}
        }

    # Daily stats - latest date first
    daily_stats = []
    for i in range(days):
        date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        next_date = date + timedelta(days=1)

        sent = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.role == "assistant",
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        received = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.role == "user",
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        # Active chats (conversations with messages on this day)
        active_chats = db.query(func.count(func.distinct(Conversation.id))).join(Message).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        # New conversations created on this day
        new_convs = db.query(func.count(Conversation.id)).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Conversation.created_at >= date,
            Conversation.created_at < next_date
        ).scalar() or 0

        daily_stats.append({
            "date": date.strftime("%Y-%m-%d"),
            "messages_sent": sent,
            "messages_received": received,
            "active_chats": active_chats,
            "new_conversations": new_convs
        })

    # Hourly stats (for today)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    hourly_stats = []
    for hour in range(24):
        hour_start = today + timedelta(hours=hour)
        hour_end = hour_start + timedelta(hours=1)

        count = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= hour_start,
            Message.timestamp < hour_end
        ).scalar() or 0
        hourly_stats.append(count)

    # Message types (total)
    total_received = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Message.role == "user"
    ).scalar() or 0

    total_sent = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Message.role == "assistant"
    ).scalar() or 0

    return {
        "daily_stats": daily_stats,
        "hourly_stats": hourly_stats,
        "message_types": {"received": total_received, "sent": total_sent}
    }


@router.get("/bot/{bot_id}", response_model=BotAnalytics)
async def get_bot_analytics(
    bot_id: int,
    days: int = Query(7, ge=1, le=90),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get analytics for a specific bot."""
    # Verify ownership
    bot = db.query(BotProfile).filter(
        BotProfile.id == bot_id,
        BotProfile.user_id == current_user.id
    ).first()

    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Total conversations
    total_conversations = db.query(func.count(Conversation.id)).filter(
        Conversation.bot_profile_id == bot_id
    ).scalar() or 0

    # Total messages
    total_messages = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id == bot_id
    ).scalar() or 0

    # Messages by role
    messages_sent = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
        Message.role == "assistant"
    ).scalar() or 0

    messages_received = db.query(func.count(Message.id)).join(Conversation).filter(
        Conversation.bot_profile_id == bot_id,
        Message.role == "user"
    ).scalar() or 0

    # Active conversations (last 24 hours)
    yesterday = datetime.utcnow() - timedelta(days=1)
    active_conversations = db.query(func.count(Conversation.id)).filter(
        Conversation.bot_profile_id == bot_id,
        Conversation.last_message_at >= yesterday
    ).scalar() or 0

    # Daily stats - latest date first
    daily_stats = []
    for i in range(days):
        date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        next_date = date + timedelta(days=1)

        sent = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            Message.role == "assistant",
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        received = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id == bot_id,
            Message.role == "user",
            Message.timestamp >= date,
            Message.timestamp < next_date
        ).scalar() or 0

        daily_stats.append(DailyStats(
            date=date.strftime("%Y-%m-%d"),
            messages_sent=sent,
            messages_received=received
        ))

    return BotAnalytics(
        bot_id=bot_id,
        bot_name=bot.name,
        total_conversations=total_conversations,
        total_messages=total_messages,
        messages_sent=messages_sent,
        messages_received=messages_received,
        active_conversations=active_conversations,
        daily_stats=daily_stats
    )


@router.get("/bot/{bot_id}/top-conversations", response_model=List[TopConversation])
async def get_top_conversations(
    bot_id: int,
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get top conversations by message count."""
    # Verify ownership
    bot = db.query(BotProfile).filter(
        BotProfile.id == bot_id,
        BotProfile.user_id == current_user.id
    ).first()

    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id == bot_id
    ).order_by(desc(Conversation.message_count)).limit(limit).all()

    return [
        TopConversation(
            id=c.id,
            chat_name=c.chat_name or c.chat_id,
            message_count=c.message_count or 0,
            last_active=c.last_message_at
        )
        for c in conversations
    ]


@router.get("/activity")
async def get_activity_log(
    bot_id: Optional[int] = None,
    action: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get activity log with filtering and pagination."""
    # Get user's bot IDs
    bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return {"activities": [], "total": 0}

    query = db.query(ActivityLog).filter(ActivityLog.bot_profile_id.in_(bot_ids))

    if bot_id:
        if bot_id not in bot_ids:
            raise HTTPException(status_code=404, detail="Bot not found")
        query = query.filter(ActivityLog.bot_profile_id == bot_id)

    if action:
        query = query.filter(ActivityLog.action == action)

    if start_date:
        query = query.filter(ActivityLog.timestamp >= start_date)

    if end_date:
        query = query.filter(ActivityLog.timestamp <= end_date)

    if search:
        query = query.filter(ActivityLog.details.ilike(f"%{search}%"))

    total = query.count()
    
    # Calculate offset for pagination
    offset = (page - 1) * limit
    
    activities = query.order_by(desc(ActivityLog.timestamp)).offset(offset).limit(limit).all()

    return {
        "activities": [
            {
                "id": a.id,
                "bot_profile_id": a.bot_profile_id,
                "action": a.action,
                "details": a.details,
                "timestamp": a.timestamp
            }
            for a in activities
        ],
        "total": total,
        "page": page,
        "limit": limit
    }


@router.get("/conversation-analytics")
async def get_conversation_analytics(
    days: int = Query(7, ge=1, le=90),
    bot_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get conversation-level analytics: volume trends, response times, platform breakdown."""
    from sqlalchemy import case, extract

    start = datetime.utcnow() - timedelta(days=days)

    # Get user's bot IDs (filtered by bot_id if provided)
    if bot_id:
        bot_ids = [b.id for b in db.query(BotProfile.id).filter(BotProfile.user_id == current_user.id, BotProfile.id == bot_id).all()]
    else:
        bot_ids = [b.id for b in db.query(BotProfile.id).filter(BotProfile.user_id == current_user.id).all()]
    if not bot_ids:
        return {"daily_volume": [], "platform_breakdown": [], "top_conversations": []}

    # Daily message volume
    daily_volume = []
    for i in range(days):
        day_start = (datetime.utcnow() - timedelta(days=days - i - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        incoming = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= day_start, Message.timestamp < day_end,
            Message.role == "user",
        ).scalar() or 0
        outgoing = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= day_start, Message.timestamp < day_end,
            Message.role == "assistant",
        ).scalar() or 0
        daily_volume.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "incoming": incoming,
            "outgoing": outgoing,
        })

    # Platform breakdown
    platform_stats = db.query(
        BotProfile.platform_type,
        func.count(Conversation.id),
        func.sum(Conversation.message_count),
    ).join(Conversation).filter(
        BotProfile.id.in_(bot_ids),
    ).group_by(BotProfile.platform_type).all()

    platform_breakdown = [
        {"platform": p[0] or "whatsapp", "conversations": p[1], "messages": p[2] or 0}
        for p in platform_stats
    ]

    # Top conversations by message count
    top_convs = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
    ).order_by(desc(Conversation.message_count)).limit(10).all()

    top_conversations = [
        {
            "id": c.id,
            "chat_name": c.chat_name or c.chat_id,
            "message_count": c.message_count or 0,
            "last_message_at": c.last_message_at.isoformat() + "Z" if c.last_message_at else None,
            "is_group": c.is_group,
        }
        for c in top_convs
    ]

    # Contact sentiment summary (from hubs, filtered by bot if selected)
    if bot_id:
        hub_ids = [m.hub_id for m in db.query(HubBotMembership.hub_id).filter(HubBotMembership.bot_profile_id == bot_id).all()]
    else:
        hub_ids = get_user_hub_ids(current_user, db)
    sentiment_data = {"positive": 0, "negative": 0, "neutral": 0}
    if hub_ids:
        for s in ["positive", "negative", "neutral"]:
            sentiment_data[s] = db.query(func.count(Contact.id)).filter(
                Contact.hub_id.in_(hub_ids), Contact.sentiment == s
            ).scalar() or 0

    return {
        "daily_volume": daily_volume,
        "top_conversations": top_conversations,
        "sentiment": sentiment_data,
        "period_days": days,
    }
