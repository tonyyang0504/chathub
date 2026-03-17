"""
Analytics Routes
"""

from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel

from app.database import get_db, User, BotProfile, Conversation, Message, ActivityLog, Hub, Contact, ScheduledContent, ToolExecution
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


@router.get("/system-health")
async def get_system_health(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get system health metrics for the dashboard."""
    import os
    import time
    from sqlalchemy import text
    from app.bots.manager import bot_manager

    # Database check
    db_status = "healthy"
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_status = "unhealthy"

    # Bot fleet
    user_bots = db.query(BotProfile).filter(BotProfile.user_id == current_user.id).all()
    total_bots = len(user_bots)
    running_bots = sum(1 for b in user_bots if b.is_running)
    needs_recovery = 0
    for bot in user_bots:
        if bot.is_running:
            health = bot_manager.get_health_status(bot.id)
            if health.get("needs_recovery"):
                needs_recovery += 1

    # Hub stats
    hub_ids = get_user_hub_ids(db, current_user.id)
    active_hubs = 0
    total_hubs = 0
    if hub_ids:
        total_hubs = len(hub_ids)
        active_hubs = db.query(func.count(Hub.id)).filter(
            Hub.id.in_(hub_ids), Hub.is_active == True
        ).scalar() or 0

    # Scheduled content
    pending_content = 0
    failed_content = 0
    if hub_ids:
        pending_content = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids),
            ScheduledContent.status == "pending"
        ).scalar() or 0
        failed_content = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids),
            ScheduledContent.status == "failed"
        ).scalar() or 0

    # Recent errors (last 24h)
    yesterday = datetime.utcnow() - timedelta(hours=24)
    bot_ids = [b.id for b in user_bots]
    recent_errors = 0
    if bot_ids:
        recent_errors = db.query(func.count(ActivityLog.id)).filter(
            ActivityLog.bot_profile_id.in_(bot_ids),
            ActivityLog.timestamp >= yesterday,
            ActivityLog.action.in_(["bot_error", "bot_stopped"])
        ).scalar() or 0

    # System resources
    try:
        import psutil
        process = psutil.Process()
        memory_mb = round(process.memory_info().rss / 1024 / 1024, 1)
        cpu_percent = psutil.cpu_percent(interval=0)
        uptime_seconds = int(time.time() - process.create_time())
    except ImportError:
        # Fallback without psutil
        import sys
        if sys.platform != 'win32':
            import resource
            memory_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        else:
            memory_mb = 0
        cpu_percent = 0
        # Approximate uptime from app start
        if not hasattr(get_system_health, '_start_time'):
            get_system_health._start_time = time.time()
        uptime_seconds = int(time.time() - get_system_health._start_time)

    # Contacts
    total_contacts = 0
    if hub_ids:
        total_contacts = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids)
        ).scalar() or 0

    # Overall status
    status = "healthy"
    if db_status != "healthy" or needs_recovery > 0:
        status = "degraded"

    return {
        "status": status,
        "database": db_status,
        "uptime_seconds": uptime_seconds,
        "memory_mb": memory_mb,
        "cpu_percent": cpu_percent,
        "bots": {"running": running_bots, "total": total_bots, "needs_recovery": needs_recovery},
        "hubs": {"active": active_hubs, "total": total_hubs},
        "scheduled_content": {"pending": pending_content, "failed": failed_content},
        "contacts": total_contacts,
        "recent_errors": recent_errors
    }
