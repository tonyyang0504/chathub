"""
Hub Routes - API endpoints for Hubs management
"""

import json
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from app.database import (
    get_db, User, BotProfile, Hub, HubBotMembership, AIAgent,
    Contact, ContactTag, ScheduledContent, AgentExecution, Conversation,
    HubMessageTopic
)
from app.auth.utils import get_current_user, encrypt_string, decrypt_string
from app.hubs.models import (
    HubCreate, HubUpdate, HubResponse, HubDetailResponse, HubBotInfo, HubAgentInfo,
    BotMembershipCreate, BotMembershipUpdate, BotMembershipResponse,
    AgentCreate, AgentUpdate, AgentResponse, AgentDetailResponse,
    ContactCreate, ContactUpdate, ContactResponse, ContactListResponse, ContactTagInfo,
    TagCreate, TagUpdate,
    ScheduledContentCreate, ScheduledContentUpdate, ScheduledContentResponse,
    AgentExecutionResponse, MultiResponseRule, SelectedGroup,
    MessageTopicCreate, MessageTopicUpdate, MessageTopicResponse
)

router = APIRouter(tags=["Hubs"])


def build_recipient_summary(recipient_type: str, contact_ids: list, group_ids: list, db: Session, hub_id: int) -> str:
    """Build a human-readable summary of recipients."""
    recipient_type = recipient_type or "broadcast"

    if recipient_type == "broadcast_all":
        # Count both contacts and groups
        contact_count = db.query(func.count(Contact.id)).filter(Contact.hub_id == hub_id).scalar() or 0
        from app.database import HubBotMembership, Conversation
        bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
            HubBotMembership.hub_id == hub_id,
            HubBotMembership.is_active == True
        ).all()
        bot_ids = [b[0] for b in bot_ids]
        group_count = 0
        if bot_ids:
            group_count = db.query(func.count(distinct(Conversation.chat_id))).filter(
                Conversation.bot_profile_id.in_(bot_ids),
                Conversation.is_group == True
            ).scalar() or 0
        return f"All ({contact_count} contacts + {group_count} groups)"
    elif recipient_type == "broadcast":
        count = db.query(func.count(Contact.id)).filter(Contact.hub_id == hub_id).scalar() or 0
        return f"All contacts ({count})"
    elif recipient_type == "all_groups":
        # Count groups from hub bots
        from app.database import HubBotMembership, Conversation
        bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
            HubBotMembership.hub_id == hub_id,
            HubBotMembership.is_active == True
        ).all()
        bot_ids = [b[0] for b in bot_ids]
        if bot_ids:
            count = db.query(func.count(distinct(Conversation.chat_id))).filter(
                Conversation.bot_profile_id.in_(bot_ids),
                Conversation.is_group == True
            ).scalar() or 0
            return f"All groups ({count})"
        return "All groups (0)"
    elif recipient_type == "contacts" and contact_ids:
        if len(contact_ids) == 1:
            contact = db.query(Contact).filter(Contact.id == contact_ids[0]).first()
            return contact.display_name or contact.phone if contact else "1 contact"
        return f"{len(contact_ids)} contacts"
    elif recipient_type == "groups" and group_ids:
        if len(group_ids) == 1:
            name = group_ids[0].get("name", "1 group") if isinstance(group_ids[0], dict) else "1 group"
            return name
        return f"{len(group_ids)} groups"

    # Fallback for legacy single contact/group
    return "Broadcast"


# ============== Hub CRUD ==============

@router.get("", response_model=List[HubResponse])
async def list_hubs(
    task_type: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all hubs for the current user, optionally filtered by task_type."""
    query = db.query(Hub).filter(Hub.user_id == current_user.id)

    if task_type:
        query = query.filter(Hub.task_type == task_type)

    hubs = query.all()

    result = []
    for hub in hubs:
        bot_count = db.query(func.count(HubBotMembership.id)).filter(
            HubBotMembership.hub_id == hub.id
        ).scalar() or 0

        agent_count = db.query(func.count(AIAgent.id)).filter(
            AIAgent.hub_id == hub.id
        ).scalar() or 0

        contact_count = db.query(func.count(Contact.id)).filter(
            Contact.hub_id == hub.id
        ).scalar() or 0

        content_count = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id == hub.id
        ).scalar() or 0

        # Parse multi_response_rules from JSON
        multi_rules = None
        if hub.multi_response_rules:
            try:
                rules_data = json.loads(hub.multi_response_rules)
                multi_rules = [MultiResponseRule(**r) for r in rules_data]
            except:
                pass

        # Parse selected_groups from JSON
        selected_groups = None
        if hub.selected_groups:
            try:
                groups_data = json.loads(hub.selected_groups)
                selected_groups = [SelectedGroup(**g) for g in groups_data]
            except:
                pass

        result.append(HubResponse(
            id=hub.id,
            name=hub.name,
            description=hub.description,
            task_type=hub.task_type,
            openai_model=hub.openai_model,
            is_active=hub.is_active,
            created_at=hub.created_at,
            updated_at=hub.updated_at,
            bot_count=bot_count,
            agent_count=agent_count,
            contact_count=contact_count,
            content_count=content_count,
            selected_groups=selected_groups,
            max_responding_bots=hub.max_responding_bots or 1,
            response_delay_min=hub.response_delay_min or 1,
            response_delay_max=hub.response_delay_max or 3,
            multi_response_rules=multi_rules,
            bot_conversation_limit=hub.bot_conversation_limit if hub.bot_conversation_limit is not None else -1,
            bot_conversation_interval=hub.bot_conversation_interval or "hour"
        ))

    return result


@router.post("", response_model=HubResponse)
async def create_hub(
    hub_data: HubCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new hub."""
    # Encrypt API key if provided
    encrypted_key = None
    if hub_data.openai_api_key:
        encrypted_key = encrypt_string(hub_data.openai_api_key)

    # Serialize multi_response_rules to JSON
    multi_rules_json = None
    if hub_data.multi_response_rules:
        multi_rules_json = json.dumps([r.dict() for r in hub_data.multi_response_rules])

    selected_groups_json = None
    if hub_data.selected_groups:
        selected_groups_json = json.dumps([g.dict() for g in hub_data.selected_groups])

    hub = Hub(
        user_id=current_user.id,
        name=hub_data.name,
        description=hub_data.description,
        task_type=hub_data.task_type,
        openai_api_key_encrypted=encrypted_key,
        openai_model=hub_data.openai_model,
        selected_groups=selected_groups_json,
        max_responding_bots=hub_data.max_responding_bots,
        response_delay_min=hub_data.response_delay_min,
        response_delay_max=hub_data.response_delay_max,
        multi_response_rules=multi_rules_json,
        bot_conversation_limit=hub_data.bot_conversation_limit,
        bot_conversation_interval=hub_data.bot_conversation_interval
    )

    db.add(hub)
    db.commit()
    db.refresh(hub)

    return HubResponse(
        id=hub.id,
        name=hub.name,
        description=hub.description,
        task_type=hub.task_type,
        openai_model=hub.openai_model,
        is_active=hub.is_active,
        created_at=hub.created_at,
        updated_at=hub.updated_at,
        bot_count=0,
        agent_count=0,
        contact_count=0,
        content_count=0,
        selected_groups=hub_data.selected_groups,
        max_responding_bots=hub.max_responding_bots or 1,
        response_delay_min=hub.response_delay_min or 1,
        response_delay_max=hub.response_delay_max or 3,
        multi_response_rules=hub_data.multi_response_rules,
        bot_conversation_limit=hub.bot_conversation_limit if hub.bot_conversation_limit is not None else -1,
        bot_conversation_interval=hub.bot_conversation_interval or "hour"
    )


@router.get("/{hub_id}", response_model=HubDetailResponse)
async def get_hub(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get hub details with bots and agents."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get bot memberships with bot info
    memberships = db.query(HubBotMembership, BotProfile).join(
        BotProfile, HubBotMembership.bot_profile_id == BotProfile.id
    ).filter(HubBotMembership.hub_id == hub_id).all()

    bots = []
    for membership, bot in memberships:
        expertise = None
        if membership.expertise:
            try:
                expertise = json.loads(membership.expertise)
            except:
                expertise = None

        working_days = None
        if membership.working_days:
            try:
                working_days = json.loads(membership.working_days)
            except:
                working_days = None

        working_periods = None
        if membership.working_periods:
            try:
                working_periods = json.loads(membership.working_periods)
            except:
                working_periods = None

        bots.append(HubBotInfo(
            id=membership.id,
            bot_profile_id=bot.id,
            bot_name=bot.name,
            role=membership.role,
            expertise=expertise,
            priority=membership.priority,
            can_initiate=membership.can_initiate,
            is_active=membership.is_active,
            is_running=bot.is_running,
            working_hours_start=membership.working_hours_start,
            working_hours_end=membership.working_hours_end,
            working_periods=working_periods,
            working_days=working_days
        ))

    # Get agents
    agents = db.query(AIAgent).filter(AIAgent.hub_id == hub_id).all()
    agent_list = [
        HubAgentInfo(
            id=a.id,
            name=a.name,
            agent_type=a.agent_type,
            description=a.description,
            is_active=a.is_active,
            last_run_at=a.last_run_at
        )
        for a in agents
    ]

    # Get counts
    contact_count = db.query(func.count(Contact.id)).filter(
        Contact.hub_id == hub_id
    ).scalar() or 0

    content_count = db.query(func.count(ScheduledContent.id)).filter(
        ScheduledContent.hub_id == hub_id
    ).scalar() or 0

    # Parse multi_response_rules from JSON
    multi_rules = None
    if hub.multi_response_rules:
        try:
            rules_data = json.loads(hub.multi_response_rules)
            multi_rules = [MultiResponseRule(**r) for r in rules_data]
        except:
            pass

    # Parse selected_groups from JSON
    selected_groups = None
    if hub.selected_groups:
        try:
            groups_data = json.loads(hub.selected_groups)
            selected_groups = [SelectedGroup(**g) for g in groups_data]
        except:
            pass

    return HubDetailResponse(
        id=hub.id,
        name=hub.name,
        description=hub.description,
        task_type=hub.task_type,
        openai_model=hub.openai_model,
        is_active=hub.is_active,
        created_at=hub.created_at,
        updated_at=hub.updated_at,
        bot_count=len(bots),
        agent_count=len(agents),
        contact_count=contact_count,
        content_count=content_count,
        selected_groups=selected_groups,
        max_responding_bots=hub.max_responding_bots or 1,
        response_delay_min=hub.response_delay_min or 1,
        response_delay_max=hub.response_delay_max or 3,
        multi_response_rules=multi_rules,
        bot_conversation_limit=hub.bot_conversation_limit if hub.bot_conversation_limit is not None else -1,
        bot_conversation_interval=hub.bot_conversation_interval or "hour",
        bots=bots,
        agents=agent_list
    )


@router.put("/{hub_id}", response_model=HubResponse)
async def update_hub(
    hub_id: int,
    hub_data: HubUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    if hub_data.name is not None:
        hub.name = hub_data.name
    if hub_data.description is not None:
        hub.description = hub_data.description
    if hub_data.task_type is not None:
        hub.task_type = hub_data.task_type
    if hub_data.openai_api_key is not None:
        hub.openai_api_key_encrypted = encrypt_string(hub_data.openai_api_key)
    if hub_data.openai_model is not None:
        hub.openai_model = hub_data.openai_model
    if hub_data.is_active is not None:
        hub.is_active = hub_data.is_active
    # Multi-bot response settings
    if hub_data.max_responding_bots is not None:
        hub.max_responding_bots = hub_data.max_responding_bots
    if hub_data.response_delay_min is not None:
        hub.response_delay_min = hub_data.response_delay_min
    if hub_data.response_delay_max is not None:
        hub.response_delay_max = hub_data.response_delay_max
    if hub_data.multi_response_rules is not None:
        hub.multi_response_rules = json.dumps([r.dict() for r in hub_data.multi_response_rules])
    # Bot-to-bot conversation settings
    if hub_data.bot_conversation_limit is not None:
        hub.bot_conversation_limit = hub_data.bot_conversation_limit
    if hub_data.bot_conversation_interval is not None:
        hub.bot_conversation_interval = hub_data.bot_conversation_interval
    # Selected groups
    if hub_data.selected_groups is not None:
        hub.selected_groups = json.dumps([g.dict() for g in hub_data.selected_groups])

    hub.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(hub)

    # Get counts
    bot_count = db.query(func.count(HubBotMembership.id)).filter(
        HubBotMembership.hub_id == hub.id
    ).scalar() or 0

    agent_count = db.query(func.count(AIAgent.id)).filter(
        AIAgent.hub_id == hub.id
    ).scalar() or 0

    contact_count = db.query(func.count(Contact.id)).filter(
        Contact.hub_id == hub.id
    ).scalar() or 0

    content_count = db.query(func.count(ScheduledContent.id)).filter(
        ScheduledContent.hub_id == hub.id
    ).scalar() or 0

    # Parse multi_response_rules from JSON
    multi_rules = None
    if hub.multi_response_rules:
        try:
            rules_data = json.loads(hub.multi_response_rules)
            multi_rules = [MultiResponseRule(**r) for r in rules_data]
        except:
            pass

    # Parse selected_groups from JSON
    selected_groups = None
    if hub.selected_groups:
        try:
            groups_data = json.loads(hub.selected_groups)
            selected_groups = [SelectedGroup(**g) for g in groups_data]
        except:
            pass

    return HubResponse(
        id=hub.id,
        name=hub.name,
        description=hub.description,
        task_type=hub.task_type,
        openai_model=hub.openai_model,
        is_active=hub.is_active,
        created_at=hub.created_at,
        updated_at=hub.updated_at,
        bot_count=bot_count,
        agent_count=agent_count,
        contact_count=contact_count,
        content_count=content_count,
        selected_groups=selected_groups,
        max_responding_bots=hub.max_responding_bots or 1,
        response_delay_min=hub.response_delay_min or 1,
        response_delay_max=hub.response_delay_max or 3,
        multi_response_rules=multi_rules,
        bot_conversation_limit=hub.bot_conversation_limit if hub.bot_conversation_limit is not None else -1,
        bot_conversation_interval=hub.bot_conversation_interval or "hour"
    )


@router.delete("/{hub_id}")
async def delete_hub(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    db.delete(hub)
    db.commit()

    return {"message": "Hub deleted successfully"}


@router.post("/{hub_id}/toggle")
async def toggle_hub_active(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Toggle hub active status."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    hub.is_active = not hub.is_active
    db.commit()
    db.refresh(hub)

    return {
        "id": hub.id,
        "is_active": hub.is_active,
        "message": f"Hub {'activated' if hub.is_active else 'deactivated'} successfully"
    }


# ============== Bot Membership ==============

@router.get("/{hub_id}/bots", response_model=List[BotMembershipResponse])
async def list_hub_bots(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List bots in a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    memberships = db.query(HubBotMembership, BotProfile).join(
        BotProfile, HubBotMembership.bot_profile_id == BotProfile.id
    ).filter(HubBotMembership.hub_id == hub_id).all()

    result = []
    for membership, bot in memberships:
        expertise = None
        if membership.expertise:
            try:
                expertise = json.loads(membership.expertise)
            except:
                expertise = None

        result.append(BotMembershipResponse(
            id=membership.id,
            hub_id=hub_id,
            bot_profile_id=bot.id,
            bot_name=bot.name,
            role=membership.role,
            expertise=expertise,
            priority=membership.priority,
            can_initiate=membership.can_initiate,
            is_active=membership.is_active,
            created_at=membership.created_at
        ))

    return result


@router.post("/{hub_id}/bots", response_model=BotMembershipResponse)
async def add_bot_to_hub(
    hub_id: int,
    data: BotMembershipCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a bot to a hub."""
    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Verify bot ownership
    bot = db.query(BotProfile).filter(
        BotProfile.id == data.bot_profile_id,
        BotProfile.user_id == current_user.id
    ).first()

    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Check if already a member
    existing = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.bot_profile_id == data.bot_profile_id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Bot is already a member of this hub")

    # Convert working_periods to JSON if provided
    working_periods_json = None
    if data.working_periods:
        working_periods_json = json.dumps([p.model_dump() for p in data.working_periods])

    # Create membership
    membership = HubBotMembership(
        hub_id=hub_id,
        bot_profile_id=data.bot_profile_id,
        role=data.role,
        expertise=json.dumps(data.expertise) if data.expertise else None,
        priority=data.priority,
        can_initiate=data.can_initiate,
        working_hours_start=data.working_hours_start,
        working_hours_end=data.working_hours_end,
        working_periods=working_periods_json,
        working_days=json.dumps(data.working_days) if data.working_days else None
    )

    db.add(membership)
    db.commit()
    db.refresh(membership)

    return BotMembershipResponse(
        id=membership.id,
        hub_id=hub_id,
        bot_profile_id=bot.id,
        bot_name=bot.name,
        role=membership.role,
        expertise=data.expertise,
        priority=membership.priority,
        can_initiate=membership.can_initiate,
        is_active=membership.is_active,
        created_at=membership.created_at,
        working_hours_start=membership.working_hours_start,
        working_hours_end=membership.working_hours_end,
        working_periods=data.working_periods,
        working_days=data.working_days
    )


@router.put("/{hub_id}/bots/{bot_id}", response_model=BotMembershipResponse)
async def update_bot_membership(
    hub_id: int,
    bot_id: int,
    data: BotMembershipUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update bot membership in a hub."""
    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    membership = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.bot_profile_id == bot_id
    ).first()

    if not membership:
        raise HTTPException(status_code=404, detail="Bot membership not found")

    if data.role is not None:
        membership.role = data.role
    if data.expertise is not None:
        membership.expertise = json.dumps(data.expertise)
    if data.priority is not None:
        membership.priority = data.priority
    if data.can_initiate is not None:
        membership.can_initiate = data.can_initiate
    if data.is_active is not None:
        membership.is_active = data.is_active
    # Working hours
    if data.working_hours_start is not None:
        membership.working_hours_start = data.working_hours_start if data.working_hours_start else None
    if data.working_hours_end is not None:
        membership.working_hours_end = data.working_hours_end if data.working_hours_end else None
    if data.working_periods is not None:
        membership.working_periods = json.dumps([p.model_dump() for p in data.working_periods]) if data.working_periods else None
    if data.working_days is not None:
        membership.working_days = json.dumps(data.working_days) if data.working_days else None

    db.commit()
    db.refresh(membership)

    # Get bot name
    bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()

    expertise = None
    if membership.expertise:
        try:
            expertise = json.loads(membership.expertise)
        except:
            expertise = None

    working_days = None
    if membership.working_days:
        try:
            working_days = json.loads(membership.working_days)
        except:
            working_days = None

    working_periods = None
    if membership.working_periods:
        try:
            working_periods = json.loads(membership.working_periods)
        except:
            working_periods = None

    return BotMembershipResponse(
        id=membership.id,
        hub_id=hub_id,
        bot_profile_id=bot_id,
        bot_name=bot.name if bot else "Unknown",
        role=membership.role,
        expertise=expertise,
        priority=membership.priority,
        can_initiate=membership.can_initiate,
        is_active=membership.is_active,
        created_at=membership.created_at,
        working_hours_start=membership.working_hours_start,
        working_hours_end=membership.working_hours_end,
        working_periods=working_periods,
        working_days=working_days
    )


@router.delete("/{hub_id}/bots/{bot_id}")
async def remove_bot_from_hub(
    hub_id: int,
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove a bot from a hub."""
    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    membership = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.bot_profile_id == bot_id
    ).first()

    if not membership:
        raise HTTPException(status_code=404, detail="Bot membership not found")

    db.delete(membership)
    db.commit()

    return {"message": "Bot removed from hub successfully"}


# ============== AI Agents ==============

@router.get("/{hub_id}/agents", response_model=List[AgentResponse])
async def list_agents(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List agents in a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    agents = db.query(AIAgent).filter(AIAgent.hub_id == hub_id).all()

    return [
        AgentResponse(
            id=a.id,
            hub_id=hub_id,
            name=a.name,
            agent_type=a.agent_type,
            description=a.description,
            openai_model=a.openai_model,
            is_active=a.is_active,
            last_run_at=a.last_run_at,
            created_at=a.created_at
        )
        for a in agents
    ]


@router.post("/{hub_id}/agents", response_model=AgentResponse)
async def create_agent(
    hub_id: int,
    data: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create an AI agent for a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Validate agent type
    valid_types = ['classifier', 'router', 'generator', 'scheduler', 'analyzer', 'followup']
    if data.agent_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid agent type. Must be one of: {', '.join(valid_types)}"
        )

    # Encrypt API key if provided
    encrypted_key = None
    if data.openai_api_key:
        encrypted_key = encrypt_string(data.openai_api_key)

    agent = AIAgent(
        hub_id=hub_id,
        name=data.name,
        agent_type=data.agent_type,
        description=data.description,
        openai_api_key_encrypted=encrypted_key,
        openai_model=data.openai_model,
        system_prompt=data.system_prompt,
        additional_instructions=data.additional_instructions,
        config=json.dumps(data.config) if data.config else None
    )

    db.add(agent)
    db.commit()
    db.refresh(agent)

    return AgentResponse(
        id=agent.id,
        hub_id=hub_id,
        name=agent.name,
        agent_type=agent.agent_type,
        description=agent.description,
        openai_model=agent.openai_model,
        is_active=agent.is_active,
        last_run_at=agent.last_run_at,
        created_at=agent.created_at
    )


@router.get("/agents/{agent_id}", response_model=AgentDetailResponse)
async def get_agent(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get agent details."""
    agent = db.query(AIAgent).join(Hub).filter(
        AIAgent.id == agent_id,
        Hub.user_id == current_user.id
    ).first()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    config = None
    if agent.config:
        try:
            config = json.loads(agent.config)
        except:
            config = None

    return AgentDetailResponse(
        id=agent.id,
        hub_id=agent.hub_id,
        name=agent.name,
        agent_type=agent.agent_type,
        description=agent.description,
        openai_model=agent.openai_model,
        is_active=agent.is_active,
        last_run_at=agent.last_run_at,
        created_at=agent.created_at,
        system_prompt=agent.system_prompt,
        additional_instructions=agent.additional_instructions,
        config=config
    )


@router.put("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: int,
    data: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update an AI agent."""
    agent = db.query(AIAgent).join(Hub).filter(
        AIAgent.id == agent_id,
        Hub.user_id == current_user.id
    ).first()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if data.name is not None:
        agent.name = data.name
    if data.description is not None:
        agent.description = data.description
    if data.openai_api_key is not None:
        agent.openai_api_key_encrypted = encrypt_string(data.openai_api_key)
    if data.openai_model is not None:
        agent.openai_model = data.openai_model
    if data.system_prompt is not None:
        agent.system_prompt = data.system_prompt
    if data.additional_instructions is not None:
        agent.additional_instructions = data.additional_instructions
    if data.config is not None:
        agent.config = json.dumps(data.config)
    if data.is_active is not None:
        agent.is_active = data.is_active

    db.commit()
    db.refresh(agent)

    return AgentResponse(
        id=agent.id,
        hub_id=agent.hub_id,
        name=agent.name,
        agent_type=agent.agent_type,
        description=agent.description,
        openai_model=agent.openai_model,
        is_active=agent.is_active,
        last_run_at=agent.last_run_at,
        created_at=agent.created_at
    )


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete an AI agent."""
    agent = db.query(AIAgent).join(Hub).filter(
        AIAgent.id == agent_id,
        Hub.user_id == current_user.id
    ).first()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    db.delete(agent)
    db.commit()

    return {"message": "Agent deleted successfully"}


# ============== Contacts ==============

@router.get("/{hub_id}/contacts", response_model=List[ContactListResponse])
async def list_contacts(
    hub_id: int,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List contacts in a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    query = db.query(Contact).filter(Contact.hub_id == hub_id)

    if search:
        query = query.filter(
            (Contact.phone.ilike(f"%{search}%")) |
            (Contact.display_name.ilike(f"%{search}%"))
        )

    if tag:
        query = query.join(ContactTag).filter(ContactTag.tag == tag)

    contacts = query.order_by(Contact.last_interaction_at.desc().nullslast()).offset(offset).limit(limit).all()

    result = []
    for c in contacts:
        tag_count = db.query(func.count(ContactTag.id)).filter(
            ContactTag.contact_id == c.id
        ).scalar() or 0

        result.append(ContactListResponse(
            id=c.id,
            phone=c.phone,
            display_name=c.display_name,
            profile_pic=c.profile_pic,
            engagement_score=c.engagement_score,
            last_interaction_at=c.last_interaction_at,
            tag_count=tag_count
        ))

    return result


@router.get("/contacts/{contact_id}", response_model=ContactResponse)
async def get_contact(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get contact details with tags."""
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    tags = db.query(ContactTag).filter(ContactTag.contact_id == contact_id).all()

    return ContactResponse(
        id=contact.id,
        hub_id=contact.hub_id,
        phone=contact.phone,
        display_name=contact.display_name,
        profile_pic=contact.profile_pic,
        description=contact.description,
        predicted_intent=contact.predicted_intent,
        engagement_score=contact.engagement_score,
        last_interaction_at=contact.last_interaction_at,
        first_seen_at=contact.first_seen_at,
        created_at=contact.created_at,
        tags=[
            ContactTagInfo(
                id=t.id,
                tag=t.tag,
                value=t.value,
                confidence=t.confidence,
                source=t.source,
                created_at=t.created_at
            )
            for t in tags
        ]
    )


@router.post("/{hub_id}/contacts", response_model=ContactResponse)
async def create_contact(
    hub_id: int,
    data: ContactCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new contact."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Check if contact already exists
    existing = db.query(Contact).filter(
        Contact.hub_id == hub_id,
        Contact.phone == data.phone
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Contact with this phone already exists")

    contact = Contact(
        hub_id=hub_id,
        phone=data.phone,
        display_name=data.display_name,
        profile_pic=data.profile_pic,
        extra_data=json.dumps(data.extra_data) if data.extra_data else None
    )

    db.add(contact)
    db.commit()
    db.refresh(contact)

    return ContactResponse(
        id=contact.id,
        hub_id=contact.hub_id,
        phone=contact.phone,
        display_name=contact.display_name,
        profile_pic=contact.profile_pic,
        description=contact.description,
        predicted_intent=contact.predicted_intent,
        engagement_score=contact.engagement_score,
        last_interaction_at=contact.last_interaction_at,
        first_seen_at=contact.first_seen_at,
        created_at=contact.created_at,
        tags=[]
    )


@router.put("/contacts/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: int,
    data: ContactUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a contact."""
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    if data.display_name is not None:
        contact.display_name = data.display_name
    if data.profile_pic is not None:
        contact.profile_pic = data.profile_pic
    if data.description is not None:
        contact.description = data.description
    if data.predicted_intent is not None:
        contact.predicted_intent = data.predicted_intent
    if data.engagement_score is not None:
        contact.engagement_score = data.engagement_score
    if data.extra_data is not None:
        contact.extra_data = json.dumps(data.extra_data)

    contact.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(contact)

    tags = db.query(ContactTag).filter(ContactTag.contact_id == contact_id).all()

    return ContactResponse(
        id=contact.id,
        hub_id=contact.hub_id,
        phone=contact.phone,
        display_name=contact.display_name,
        profile_pic=contact.profile_pic,
        description=contact.description,
        predicted_intent=contact.predicted_intent,
        engagement_score=contact.engagement_score,
        last_interaction_at=contact.last_interaction_at,
        first_seen_at=contact.first_seen_at,
        created_at=contact.created_at,
        tags=[
            ContactTagInfo(
                id=t.id,
                tag=t.tag,
                value=t.value,
                confidence=t.confidence,
                source=t.source,
                created_at=t.created_at
            )
            for t in tags
        ]
    )


@router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a contact."""
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    db.delete(contact)
    db.commit()

    return {"message": "Contact deleted successfully"}


# ============== Contact Tags ==============

@router.post("/contacts/{contact_id}/tags")
async def add_tag(
    contact_id: int,
    data: TagCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a tag to a contact."""
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    tag = ContactTag(
        contact_id=contact_id,
        tag=data.tag,
        value=data.value,
        confidence=data.confidence,
        source=data.source
    )

    db.add(tag)
    db.commit()
    db.refresh(tag)

    return ContactTagInfo(
        id=tag.id,
        tag=tag.tag,
        value=tag.value,
        confidence=tag.confidence,
        source=tag.source,
        created_at=tag.created_at
    )


@router.delete("/contacts/{contact_id}/tags/{tag_id}")
async def remove_tag(
    contact_id: int,
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove a tag from a contact."""
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    tag = db.query(ContactTag).filter(
        ContactTag.id == tag_id,
        ContactTag.contact_id == contact_id
    ).first()

    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    db.delete(tag)
    db.commit()

    return {"message": "Tag removed successfully"}


# ============== Scheduled Content ==============

@router.get("/{hub_id}/content", response_model=List[ScheduledContentResponse])
async def list_scheduled_content(
    hub_id: int,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List scheduled content for a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    query = db.query(ScheduledContent).filter(ScheduledContent.hub_id == hub_id)

    if status:
        query = query.filter(ScheduledContent.status == status)

    contents = query.order_by(ScheduledContent.created_at.desc()).limit(limit).all()

    result = []
    for c in contents:
        bot_name = None
        if c.bot_profile_id:
            bot = db.query(BotProfile).filter(BotProfile.id == c.bot_profile_id).first()
            bot_name = bot.name if bot else None

        contact_name = None
        if c.contact_id:
            contact = db.query(Contact).filter(Contact.id == c.contact_id).first()
            contact_name = contact.display_name or contact.phone if contact else None

        # Parse JSON fields
        contact_ids_list = None
        group_ids_list = None
        try:
            if c.contact_ids:
                contact_ids_list = json.loads(c.contact_ids)
            if c.group_ids:
                group_ids_list = json.loads(c.group_ids)
        except:
            pass

        # Build recipient summary
        recipient_summary = build_recipient_summary(
            c.recipient_type or "broadcast",
            contact_ids_list,
            group_ids_list,
            db,
            hub_id
        )

        result.append(ScheduledContentResponse(
            id=c.id,
            hub_id=hub_id,
            bot_profile_id=c.bot_profile_id,
            bot_name=bot_name,
            contact_id=c.contact_id,
            contact_name=contact_name,
            content=c.content,
            content_type=c.content_type,
            topic=c.topic,
            scheduled_for=c.scheduled_for,
            sent_at=c.sent_at,
            status=c.status,
            created_at=c.created_at,
            recipient_type=c.recipient_type or "broadcast",
            group_id=c.group_id,
            group_name=c.group_name,
            contact_ids=contact_ids_list,
            group_ids=group_ids_list,
            recipient_summary=recipient_summary
        ))

    return result


@router.post("/{hub_id}/content", response_model=ScheduledContentResponse)
async def create_scheduled_content(
    hub_id: int,
    data: ScheduledContentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create scheduled content."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Handle empty strings as None
    bot_profile_id = data.bot_profile_id if data.bot_profile_id else None
    recipient_type = data.recipient_type or "broadcast"

    # Debug logging
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Creating scheduled content: recipient_type={recipient_type}, contact_ids={data.contact_ids}, group_ids={data.group_ids}, bot_profile_id={bot_profile_id}")

    content = ScheduledContent(
        hub_id=hub_id,
        bot_profile_id=bot_profile_id,
        contact_id=data.contact_id,
        content=data.content,
        content_type=data.content_type,
        topic=data.topic,
        scheduled_for=data.scheduled_for,
        recipient_type=recipient_type,
        group_id=data.group_id,
        group_name=data.group_name,
        contact_ids=json.dumps(data.contact_ids) if data.contact_ids else None,
        group_ids=json.dumps(data.group_ids) if data.group_ids else None
    )

    db.add(content)
    db.commit()
    db.refresh(content)

    # Build recipient summary
    recipient_summary = build_recipient_summary(data.recipient_type, data.contact_ids, data.group_ids, db, hub_id)

    return ScheduledContentResponse(
        id=content.id,
        hub_id=hub_id,
        bot_profile_id=content.bot_profile_id,
        bot_name=None,
        contact_id=content.contact_id,
        contact_name=None,
        content=content.content,
        content_type=content.content_type,
        topic=content.topic,
        scheduled_for=content.scheduled_for,
        sent_at=content.sent_at,
        status=content.status,
        created_at=content.created_at,
        recipient_type=content.recipient_type or "broadcast",
        group_id=content.group_id,
        group_name=content.group_name,
        contact_ids=data.contact_ids,
        group_ids=data.group_ids,
        recipient_summary=recipient_summary
    )


@router.put("/content/{content_id}", response_model=ScheduledContentResponse)
async def update_scheduled_content(
    content_id: int,
    data: ScheduledContentUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update scheduled content."""
    content = db.query(ScheduledContent).join(Hub).filter(
        ScheduledContent.id == content_id,
        Hub.user_id == current_user.id
    ).first()

    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    if data.content is not None:
        content.content = data.content
    if data.content_type is not None:
        content.content_type = data.content_type
    if data.topic is not None:
        content.topic = data.topic
    if data.bot_profile_id is not None:
        content.bot_profile_id = data.bot_profile_id
    if data.contact_id is not None:
        content.contact_id = data.contact_id
    if data.scheduled_for is not None:
        content.scheduled_for = data.scheduled_for
    if data.status is not None:
        content.status = data.status
    if data.recipient_type is not None:
        content.recipient_type = data.recipient_type
    if data.group_id is not None:
        content.group_id = data.group_id
    if data.group_name is not None:
        content.group_name = data.group_name
    if data.contact_ids is not None:
        content.contact_ids = json.dumps(data.contact_ids) if data.contact_ids else None
    if data.group_ids is not None:
        content.group_ids = json.dumps(data.group_ids) if data.group_ids else None

    db.commit()
    db.refresh(content)

    bot_name = None
    if content.bot_profile_id:
        bot = db.query(BotProfile).filter(BotProfile.id == content.bot_profile_id).first()
        bot_name = bot.name if bot else None

    contact_name = None
    if content.contact_id:
        contact = db.query(Contact).filter(Contact.id == content.contact_id).first()
        contact_name = contact.display_name or contact.phone if contact else None

    # Parse JSON fields for response
    contact_ids_list = None
    group_ids_list = None
    try:
        if content.contact_ids:
            contact_ids_list = json.loads(content.contact_ids)
        if content.group_ids:
            group_ids_list = json.loads(content.group_ids)
    except:
        pass

    recipient_summary = build_recipient_summary(
        content.recipient_type or "broadcast",
        contact_ids_list,
        group_ids_list,
        db,
        content.hub_id
    )

    return ScheduledContentResponse(
        id=content.id,
        hub_id=content.hub_id,
        bot_profile_id=content.bot_profile_id,
        bot_name=bot_name,
        contact_id=content.contact_id,
        contact_name=contact_name,
        content=content.content,
        content_type=content.content_type,
        topic=content.topic,
        scheduled_for=content.scheduled_for,
        sent_at=content.sent_at,
        status=content.status,
        created_at=content.created_at,
        recipient_type=content.recipient_type or "broadcast",
        group_id=content.group_id,
        group_name=content.group_name,
        contact_ids=contact_ids_list,
        group_ids=group_ids_list,
        recipient_summary=recipient_summary
    )


@router.delete("/{hub_id}/content/all")
async def delete_all_scheduled_content(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete all scheduled content for a hub."""
    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Delete all content for this hub
    deleted_count = db.query(ScheduledContent).filter(
        ScheduledContent.hub_id == hub_id
    ).delete(synchronize_session='fetch')

    db.commit()

    return {"message": "All content deleted successfully", "deleted_count": deleted_count}


@router.delete("/content/{content_id}")
async def delete_scheduled_content(
    content_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete scheduled content."""
    content = db.query(ScheduledContent).join(Hub).filter(
        ScheduledContent.id == content_id,
        Hub.user_id == current_user.id
    ).first()

    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    db.delete(content)
    db.commit()

    return {"message": "Content deleted successfully"}


@router.post("/content/{content_id}/send")
async def send_content_now(
    content_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Send scheduled content immediately."""
    content = db.query(ScheduledContent).join(Hub).filter(
        ScheduledContent.id == content_id,
        Hub.user_id == current_user.id
    ).first()

    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    if content.status == "sent":
        raise HTTPException(status_code=400, detail="Content already sent")

    if content.status == "processing":
        raise HTTPException(status_code=400, detail="Content is currently being sent")

    # Import scheduler and send
    from app.hubs.scheduler import content_scheduler
    success, error_message = await content_scheduler.send_immediate(content_id)

    if success:
        return {"message": "Content sent successfully"}
    else:
        raise HTTPException(status_code=400, detail=error_message or "Failed to send content")


# ============== Agent Executions (Read-only) ==============

@router.get("/agents/{agent_id}/executions", response_model=List[AgentExecutionResponse])
async def list_agent_executions(
    agent_id: int,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List execution history for an agent."""
    agent = db.query(AIAgent).join(Hub).filter(
        AIAgent.id == agent_id,
        Hub.user_id == current_user.id
    ).first()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    executions = db.query(AgentExecution).filter(
        AgentExecution.agent_id == agent_id
    ).order_by(AgentExecution.created_at.desc()).limit(limit).all()

    result = []
    for e in executions:
        input_data = None
        if e.input_data:
            try:
                input_data = json.loads(e.input_data)
            except:
                input_data = None

        output_data = None
        if e.output_data:
            try:
                output_data = json.loads(e.output_data)
            except:
                output_data = None

        result.append(AgentExecutionResponse(
            id=e.id,
            agent_id=agent_id,
            agent_name=agent.name,
            trigger_type=e.trigger_type,
            input_data=input_data,
            output_data=output_data,
            tokens_used=e.tokens_used,
            execution_time_ms=e.execution_time_ms,
            status=e.status,
            error_message=e.error_message,
            created_at=e.created_at
        ))

    return result


# ============== Available Bots (for adding to hub) ==============

@router.get("/{hub_id}/available-bots")
async def get_available_bots(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get user's bots that are not yet in this hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get bots already in this hub
    existing_bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
        HubBotMembership.hub_id == hub_id
    ).all()
    existing_bot_ids = [b[0] for b in existing_bot_ids]

    # Get user's bots not in this hub
    bots = db.query(BotProfile).filter(
        BotProfile.user_id == current_user.id,
        ~BotProfile.id.in_(existing_bot_ids) if existing_bot_ids else True
    ).all()

    return [
        {
            "id": b.id,
            "name": b.name,
            "is_running": b.is_running,
            "whatsapp_connected": b.whatsapp_connected
        }
        for b in bots
    ]


# ============== Bots by Groups ==============

@router.get("/{hub_id}/bots-by-groups")
async def get_bots_by_groups(
    hub_id: int,
    group_ids: Optional[str] = None,  # Comma-separated chat_ids
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get bots that are members of specified groups.

    If group_ids is provided, filter by those groups.
    If not provided, use hub's selected_groups.
    Returns all user's bots if no groups are selected.
    """
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Parse group_ids from query param or use hub's selected_groups
    selected_chat_ids = []
    if group_ids:
        selected_chat_ids = [g.strip() for g in group_ids.split(",") if g.strip()]
    elif hub.selected_groups:
        try:
            groups_data = json.loads(hub.selected_groups)
            selected_chat_ids = [g.get("chat_id") for g in groups_data if g.get("chat_id")]
        except:
            pass

    # If no groups selected, return all user's bots
    if not selected_chat_ids:
        bots = db.query(BotProfile).filter(
            BotProfile.user_id == current_user.id
        ).all()
        return [
            {
                "id": b.id,
                "name": b.name,
                "is_running": b.is_running,
                "whatsapp_connected": b.whatsapp_connected,
                "groups": []
            }
            for b in bots
        ]

    # Find bots that have conversations with selected groups
    bot_ids_in_groups = db.query(Conversation.bot_profile_id).filter(
        Conversation.is_group == True,
        Conversation.chat_id.in_(selected_chat_ids)
    ).distinct().all()
    bot_ids_in_groups = [b[0] for b in bot_ids_in_groups]

    # Get those bots (only if they belong to current user)
    bots = db.query(BotProfile).filter(
        BotProfile.user_id == current_user.id,
        BotProfile.id.in_(bot_ids_in_groups) if bot_ids_in_groups else False
    ).all()

    # For each bot, get which of the selected groups it belongs to
    result = []
    for bot in bots:
        bot_groups = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot.id,
            Conversation.is_group == True,
            Conversation.chat_id.in_(selected_chat_ids)
        ).all()

        result.append({
            "id": bot.id,
            "name": bot.name,
            "is_running": bot.is_running,
            "whatsapp_connected": bot.whatsapp_connected,
            "groups": [{"chat_id": g.chat_id, "name": g.chat_name} for g in bot_groups]
        })

    return result


# ============== Groups (for scheduled content) ==============

@router.get("/{hub_id}/groups")
async def list_hub_groups(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List available WhatsApp groups from bots in this hub.

    Returns groups from all bot conversations that are members of this hub.
    """
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get bot IDs that are members of this hub
    bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.is_active == True
    ).all()
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return []

    # Get group conversations from these bots
    groups = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Conversation.is_group == True
    ).order_by(Conversation.last_message_at.desc().nullslast()).all()

    # Deduplicate by chat_id (same group might appear in multiple bots)
    seen_chat_ids = set()
    result = []

    for g in groups:
        if g.chat_id not in seen_chat_ids:
            seen_chat_ids.add(g.chat_id)
            result.append({
                "chat_id": g.chat_id,
                "name": g.chat_name or g.chat_id,
                "profile_pic": g.profile_pic,
                "message_count": g.message_count,
                "last_message_at": g.last_message_at.isoformat() if g.last_message_at else None,
                "bot_profile_id": g.bot_profile_id
            })

    return result


# ============== Message Topics ==============

# Default system topics that are created for new hubs
DEFAULT_TOPICS = [
    {"name": "sales", "description": "Sales inquiries and product questions"},
    {"name": "support", "description": "Customer support and help requests"},
    {"name": "billing", "description": "Billing, payments, and invoices"},
    {"name": "general", "description": "General inquiries and conversations"},
    {"name": "greeting", "description": "Greetings and welcome messages"},
    {"name": "complaint", "description": "Customer complaints and issues"},
    {"name": "feedback", "description": "Feedback and suggestions"},
]


@router.get("/{hub_id}/topics", response_model=List[MessageTopicResponse])
async def get_hub_topics(
    hub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all message topics for a hub."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == current_user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    topics = db.query(HubMessageTopic).filter(
        HubMessageTopic.hub_id == hub_id
    ).order_by(HubMessageTopic.is_system.desc(), HubMessageTopic.name).all()

    return [
        MessageTopicResponse(
            id=t.id,
            hub_id=t.hub_id,
            name=t.name,
            description=t.description,
            is_system=t.is_system,
            created_at=t.created_at
        )
        for t in topics
    ]


@router.post("/{hub_id}/topics", response_model=MessageTopicResponse)
async def create_hub_topic(
    hub_id: int,
    data: MessageTopicCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new message topic for a hub."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == current_user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Check if topic already exists
    existing = db.query(HubMessageTopic).filter(
        HubMessageTopic.hub_id == hub_id,
        HubMessageTopic.name == data.name.lower().strip()
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Topic already exists")

    topic = HubMessageTopic(
        hub_id=hub_id,
        name=data.name.lower().strip(),
        description=data.description,
        is_system=data.is_system
    )

    db.add(topic)
    db.commit()
    db.refresh(topic)

    return MessageTopicResponse(
        id=topic.id,
        hub_id=topic.hub_id,
        name=topic.name,
        description=topic.description,
        is_system=topic.is_system,
        created_at=topic.created_at
    )


@router.put("/{hub_id}/topics/{topic_id}", response_model=MessageTopicResponse)
async def update_hub_topic(
    hub_id: int,
    topic_id: int,
    data: MessageTopicUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a message topic."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == current_user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    topic = db.query(HubMessageTopic).filter(
        HubMessageTopic.id == topic_id,
        HubMessageTopic.hub_id == hub_id
    ).first()

    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    if data.name is not None:
        # Check if new name already exists
        existing = db.query(HubMessageTopic).filter(
            HubMessageTopic.hub_id == hub_id,
            HubMessageTopic.name == data.name.lower().strip(),
            HubMessageTopic.id != topic_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Topic with this name already exists")
        topic.name = data.name.lower().strip()

    if data.description is not None:
        topic.description = data.description

    db.commit()
    db.refresh(topic)

    return MessageTopicResponse(
        id=topic.id,
        hub_id=topic.hub_id,
        name=topic.name,
        description=topic.description,
        is_system=topic.is_system,
        created_at=topic.created_at
    )


@router.delete("/{hub_id}/topics/{topic_id}")
async def delete_hub_topic(
    hub_id: int,
    topic_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a message topic."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == current_user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    topic = db.query(HubMessageTopic).filter(
        HubMessageTopic.id == topic_id,
        HubMessageTopic.hub_id == hub_id
    ).first()

    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    db.delete(topic)
    db.commit()

    return {"status": "success", "message": "Topic deleted"}


@router.post("/{hub_id}/topics/init-defaults")
async def init_default_topics(
    hub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Initialize default system topics for a hub (if not already present)."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == current_user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    created_count = 0
    for topic_data in DEFAULT_TOPICS:
        existing = db.query(HubMessageTopic).filter(
            HubMessageTopic.hub_id == hub_id,
            HubMessageTopic.name == topic_data["name"]
        ).first()

        if not existing:
            topic = HubMessageTopic(
                hub_id=hub_id,
                name=topic_data["name"],
                description=topic_data["description"],
                is_system=True
            )
            db.add(topic)
            created_count += 1

    db.commit()

    # Return all topics for this hub
    all_topics = db.query(HubMessageTopic).filter(
        HubMessageTopic.hub_id == hub_id
    ).order_by(HubMessageTopic.is_system.desc(), HubMessageTopic.name).all()

    return {
        "status": "success",
        "added": created_count,
        "topics": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "is_system": t.is_system,
                "created_at": t.created_at.isoformat() if t.created_at else None
            }
            for t in all_topics
        ]
    }
