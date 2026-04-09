"""
Analytics Routes
"""

from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel

from app.database import get_db, User, BotProfile, Conversation, Message, ActivityLog, Hub, Contact, HubBotMembership, ContactTag
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


# ============== Platform Stats ==============

PLATFORM_META = {
    "whatsapp": {"label": "WhatsApp", "icon": "bi-whatsapp", "color": "#25D366", "auth_method": "qr_code"},
    "telegram": {"label": "Telegram", "icon": "bi-telegram", "color": "#26A5E4", "auth_method": "phone_code"},
    "discord": {"label": "Discord", "icon": "bi-discord", "color": "#5865F2", "auth_method": "api_token"},
    "messenger": {"label": "Facebook Page", "icon": "bi-facebook", "color": "#1877F2", "auth_method": "oauth"},
    "instagram": {"label": "Instagram", "icon": "bi-instagram", "color": "#E4405F", "auth_method": "oauth"},
    "slack": {"label": "Slack", "icon": "bi-slack", "color": "#4A154B", "auth_method": "api_token"},
    "signal": {"label": "Signal", "icon": "bi-shield-lock-fill", "color": "#3A76F0", "auth_method": "credentials"},
    "line": {"label": "LINE", "icon": "bi-chat-dots-fill", "color": "#00B900", "auth_method": "api_token"},
    "linkedin": {"label": "LinkedIn", "icon": "bi-linkedin", "color": "#0A66C2", "auth_method": "oauth"},
    "tinder": {"label": "Tinder", "icon": "bi-fire", "color": "#FE3C72", "auth_method": "credentials"},
    "bumble": {"label": "Bumble", "icon": "bi-heart-fill", "color": "#FFC629", "auth_method": "credentials"},
    "imessage": {"label": "iMessage", "icon": "bi-chat-square-text", "color": "#34C759", "auth_method": "credentials"},
    "wechat": {"label": "WeChat", "icon": "bi-wechat", "color": "#07C160", "auth_method": "qr_code"},
}

PLATFORM_CAPABILITIES = {
    "whatsapp": {"groups": True, "media": True, "file_send": True, "reactions": False, "read_receipts": True, "typing_indicator": False, "history_sync": True, "contacts_list": True, "groups_list": True, "voice_messages": True},
    "telegram": {"groups": True, "media": True, "file_send": True, "reactions": True, "read_receipts": False, "typing_indicator": True, "history_sync": True, "contacts_list": True, "groups_list": True, "voice_messages": True},
    "discord": {"groups": True, "media": True, "file_send": True, "reactions": True, "read_receipts": False, "typing_indicator": True, "history_sync": False, "contacts_list": True, "groups_list": True, "voice_messages": False},
    "messenger": {"groups": False, "media": True, "file_send": True, "reactions": True, "read_receipts": True, "typing_indicator": True, "history_sync": True, "contacts_list": True, "groups_list": False, "voice_messages": True},
    "instagram": {"groups": False, "media": True, "file_send": False, "reactions": True, "read_receipts": True, "typing_indicator": True, "history_sync": False, "contacts_list": True, "groups_list": False, "voice_messages": False},
    "slack": {"groups": True, "media": True, "file_send": True, "reactions": True, "read_receipts": True, "typing_indicator": True, "history_sync": True, "contacts_list": True, "groups_list": True, "voice_messages": False},
    "signal": {"groups": True, "media": True, "file_send": True, "reactions": True, "read_receipts": True, "typing_indicator": False, "history_sync": False, "contacts_list": False, "groups_list": True, "voice_messages": True},
    "line": {"groups": True, "media": True, "file_send": True, "reactions": False, "read_receipts": True, "typing_indicator": True, "history_sync": False, "contacts_list": False, "groups_list": False, "voice_messages": True},
    "linkedin": {"groups": False, "media": True, "file_send": True, "reactions": False, "read_receipts": True, "typing_indicator": False, "history_sync": False, "contacts_list": True, "groups_list": False, "voice_messages": False},
    "tinder": {"groups": False, "media": True, "file_send": False, "reactions": False, "read_receipts": False, "typing_indicator": False, "history_sync": False, "contacts_list": True, "groups_list": False, "voice_messages": False},
    "bumble": {"groups": False, "media": True, "file_send": False, "reactions": False, "read_receipts": False, "typing_indicator": False, "history_sync": False, "contacts_list": True, "groups_list": False, "voice_messages": False},
    "imessage": {"groups": True, "media": True, "file_send": True, "reactions": True, "read_receipts": True, "typing_indicator": True, "history_sync": True, "contacts_list": True, "groups_list": True, "voice_messages": True},
    "wechat": {"groups": True, "media": True, "file_send": True, "reactions": False, "read_receipts": False, "typing_indicator": False, "history_sync": False, "contacts_list": False, "groups_list": False, "voice_messages": True},
}


@router.get("/platform-stats")
async def get_platform_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get summary stats for all platforms."""
    # Aggregate by platform
    all_bots = db.query(BotProfile).filter(BotProfile.user_id == current_user.id).all()

    platform_bots = {}
    for b in all_bots:
        pt = b.platform_type or "whatsapp"
        if pt not in platform_bots:
            platform_bots[pt] = {"bot_count": 0, "running_bots": 0}
        platform_bots[pt]["bot_count"] += 1
        if b.is_running:
            platform_bots[pt]["running_bots"] += 1

    # Conversation/message counts by platform
    conv_data = db.query(
        BotProfile.platform_type,
        func.count(Conversation.id),
        func.coalesce(func.sum(Conversation.message_count), 0),
    ).join(Conversation, Conversation.bot_profile_id == BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).group_by(BotProfile.platform_type).all()

    conv_lookup = {}
    for row in conv_data:
        pt = row[0] or "whatsapp"
        conv_lookup[pt] = {"conversations": row[1], "messages": row[2] or 0}

    # Build response for all 13 platforms
    platforms = []
    for pt, meta in PLATFORM_META.items():
        bots = platform_bots.get(pt, {"bot_count": 0, "running_bots": 0})
        convs = conv_lookup.get(pt, {"conversations": 0, "messages": 0})
        platforms.append({
            "platform": pt,
            **meta,
            **bots,
            **convs,
        })

    return {"platforms": platforms}


@router.get("/platform-stats/{platform}")
async def get_platform_detail(
    platform: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get detailed stats for a specific platform."""
    if platform not in PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unknown platform")

    meta = PLATFORM_META[platform]

    # Get bots for this platform
    bots = db.query(BotProfile).filter(
        BotProfile.user_id == current_user.id,
        BotProfile.platform_type == platform
    ).all()

    bot_ids = [b.id for b in bots]
    running_bots = sum(1 for b in bots if b.is_running)

    # Conversation stats
    total_conversations = 0
    total_messages = 0
    messages_today = 0
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    if bot_ids:
        total_conversations = db.query(func.count(Conversation.id)).filter(
            Conversation.bot_profile_id.in_(bot_ids)
        ).scalar() or 0

        total_messages = db.query(func.coalesce(func.sum(Conversation.message_count), 0)).filter(
            Conversation.bot_profile_id.in_(bot_ids)
        ).scalar() or 0

        messages_today = db.query(func.count(Message.id)).join(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Message.timestamp >= today_start
        ).scalar() or 0

    # Contacts count (from conversations with non-group chats)
    total_contacts = 0
    if bot_ids:
        total_contacts = db.query(func.count(Conversation.id)).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Conversation.is_group == False
        ).scalar() or 0

    # Groups count
    total_groups = 0
    if bot_ids:
        total_groups = db.query(func.count(Conversation.id)).filter(
            Conversation.bot_profile_id.in_(bot_ids),
            Conversation.is_group == True
        ).scalar() or 0

    # Bot details
    bots_data = []
    for b in bots:
        conv_count = db.query(func.count(Conversation.id)).filter(Conversation.bot_profile_id == b.id).scalar() or 0
        msg_count = db.query(func.coalesce(func.sum(Conversation.message_count), 0)).filter(Conversation.bot_profile_id == b.id).scalar() or 0
        bots_data.append({
            "id": b.id,
            "name": b.name,
            "is_running": b.is_running,
            "account_name": b.whatsapp_name or "",
            "account_phone": b.whatsapp_phone or "",
            "ai_provider": b.ai_provider,
            "model": b.model,
            "conversation_count": conv_count,
            "message_count": msg_count,
            "last_active": b.last_active.isoformat() + "Z" if b.last_active else None,
        })

    # Recent conversations
    recent_convs = []
    if bot_ids:
        convs = db.query(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_ids)
        ).order_by(desc(Conversation.last_message_at)).limit(10).all()
        recent_convs = [{
            "id": c.id,
            "chat_name": c.chat_name or c.chat_id,
            "message_count": c.message_count or 0,
            "last_message_at": c.last_message_at.isoformat() + "Z" if c.last_message_at else None,
            "is_group": c.is_group,
            "bot_name": next((b.name for b in bots if b.id == c.bot_profile_id), ""),
        } for c in convs]

    return {
        "platform": platform,
        **meta,
        "bot_count": len(bots),
        "running_bots": running_bots,
        "total_conversations": total_conversations,
        "total_messages": total_messages,
        "messages_today": messages_today,
        "total_contacts": total_contacts,
        "total_groups": total_groups,
        "capabilities": PLATFORM_CAPABILITIES.get(platform, {}),
        "bots": bots_data,
        "recent_conversations": recent_convs,
    }


@router.get("/platform-contacts/{platform}")
async def get_platform_contacts(
    platform: str,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get rich contact data for a platform, merging hub Contact records with conversation data."""
    if platform not in PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unknown platform")

    # Get bot IDs for this platform
    bots = db.query(BotProfile).filter(
        BotProfile.user_id == current_user.id,
        BotProfile.platform_type == platform
    ).all()
    bot_ids = [b.id for b in bots]
    bot_names = {b.id: b.name for b in bots}

    if not bot_ids:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    # Get hub IDs that contain these bots
    hub_ids = [m.hub_id for m in db.query(HubBotMembership.hub_id).filter(
        HubBotMembership.bot_profile_id.in_(bot_ids)
    ).distinct().all()]

    # Get all private conversations for these bots (to map phone -> bot)
    convs = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Conversation.is_group == False
    ).all()

    def normalize_phone(p):
        """Strip + prefix and whitespace for matching."""
        if not p:
            return p
        return p.strip().lstrip('+')

    phone_to_conv = {}
    for conv in convs:
        identifier = (conv.phone or "").strip() or conv.chat_id
        if identifier:
            phone_to_conv[identifier] = conv
            # Also store normalized version for matching
            norm = normalize_phone(identifier)
            if norm and norm not in phone_to_conv:
                phone_to_conv[norm] = conv

    # Get rich Contact records from hubs
    hub_contacts = {}
    if hub_ids:
        query = db.query(Contact).filter(Contact.hub_id.in_(hub_ids))
        if search:
            query = query.filter(
                (Contact.phone.ilike(f"%{search}%")) |
                (Contact.display_name.ilike(f"%{search}%"))
            )
        for c in query.all():
            if c.phone in phone_to_conv or c.phone:
                hub_contacts[c.phone] = c

    # Build merged contact list
    all_contacts = []
    seen = set()

    # First: hub contacts with rich data
    for phone, contact in hub_contacts.items():
        if phone in seen:
            continue
        seen.add(phone)
        norm = normalize_phone(phone)
        conv = phone_to_conv.get(phone) or phone_to_conv.get(norm)
        tags = db.query(ContactTag).filter(ContactTag.contact_id == contact.id).limit(5).all()
        contact_bot_ids = set()
        for c in convs:
            cid = (c.phone or "").strip() or c.chat_id
            if cid == phone or normalize_phone(cid) == norm:
                contact_bot_ids.add(c.bot_profile_id)
        bot_name = ", ".join(bot_names.get(bid, "") for bid in contact_bot_ids if bid in bot_names) or None

        all_contacts.append({
            "id": contact.id,
            "phone": contact.phone,
            "display_name": contact.display_name or (conv.chat_name if conv else None),
            "profile_pic": contact.profile_pic or (conv.profile_pic if conv else None),
            "sentiment": contact.sentiment,
            "urgency": contact.urgency,
            "predicted_intent": contact.predicted_intent,
            "engagement_score": contact.engagement_score,
            "follow_up_needed": contact.follow_up_needed,
            "analysis_status": contact.analysis_status,
            "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else (conv.last_message_at.isoformat() if conv and conv.last_message_at else None),
            "first_seen_at": contact.first_seen_at.isoformat() if contact.first_seen_at else None,
            "description": contact.description,
            "tags": [{"id": t.id, "tag": t.tag, "value": t.value, "confidence": t.confidence} for t in tags],
            "tag_count": len(tags),
            "bot_name": bot_name,
            "hub_id": contact.hub_id,
            "has_hub_data": True,
        })

    # Then: conversation-only contacts (not in any hub)
    for identifier, conv in phone_to_conv.items():
        norm_id = normalize_phone(identifier)
        if identifier in seen or norm_id in seen:
            continue
        seen.add(identifier)
        seen.add(norm_id)
        name = conv.chat_name or conv.display_name or identifier
        if search and search.lower() not in name.lower() and search.lower() not in identifier.lower():
            continue
        all_contacts.append({
            "id": None,
            "phone": identifier,
            "display_name": name,
            "profile_pic": conv.profile_pic,
            "sentiment": None,
            "urgency": None,
            "predicted_intent": None,
            "engagement_score": None,
            "follow_up_needed": None,
            "analysis_status": None,
            "last_interaction_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "first_seen_at": None,
            "description": None,
            "tags": [],
            "tag_count": 0,
            "bot_name": bot_names.get(conv.bot_profile_id),
            "hub_id": None,
            "has_hub_data": False,
        })

    # Sort by last_interaction_at desc
    all_contacts.sort(key=lambda x: x["last_interaction_at"] or "", reverse=True)

    total = len(all_contacts)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    offset = (page - 1) * page_size
    page_items = all_contacts[offset:offset + page_size]

    return {"items": page_items, "total": total, "page": page, "page_size": page_size, "total_pages": total_pages, "hub_ids": hub_ids}
