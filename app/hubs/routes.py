"""
Hub Routes - API endpoints for Hubs management
"""

import json
import logging
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Thread pool for running blocking AI operations
ai_executor = ThreadPoolExecutor(max_workers=4)
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct, or_

from app.database import (
    get_db, User, BotProfile, Hub, HubBotMembership, AIAgent,
    Contact, ContactTag, ScheduledContent, AgentExecution, Conversation, Message,
    HubMessageTopic, ConversationScript, MessageRouting, ScriptMessage, ScriptExecution
)
from app.auth.utils import get_current_user, encrypt_string, decrypt_string
from app.tools.monitoring import ToolMonitor
from app.tools.event_bus import tool_event_bus
from app.platforms.message_handler import emit_event_sync
from app.hubs.models import (
    HubCreate, HubUpdate, HubResponse, HubDetailResponse, HubBotInfo, HubAgentInfo,
    BotMembershipCreate, BotMembershipUpdate, BotMembershipResponse,
    AgentCreate, AgentUpdate, AgentResponse, AgentDetailResponse,
    ContactCreate, ContactUpdate, ContactResponse, ContactListResponse, ContactTagInfo,
    TagCreate, TagUpdate,
    ScheduledContentCreate, ScheduledContentUpdate, ScheduledContentResponse,
    AgentExecutionResponse, MultiResponseRule, SelectedGroup,
    MessageTopicCreate, MessageTopicUpdate, MessageTopicResponse,
    GenerateContentRequest
)

router = APIRouter(tags=["Hubs"])


async def _fetch_platform_contacts(bot_profile_id: int, platform_type: str) -> list:
    """Fetch contacts from a live platform adapter (async-safe)."""
    if platform_type == "telegram":
        from app.platforms.telegram.adapter import _active_clients, TelegramAdapter
        client = _active_clients.get(bot_profile_id)
        if not client:
            logger.warning(f"Bot {bot_profile_id}: No active Telegram client for contact sync")
            return []
        adapter = TelegramAdapter()
        result = await adapter._async_get_contacts(client, bot_profile_id)
        logger.info(f"Bot {bot_profile_id}: Telegram returned {len(result)} contacts")
        return result
    if platform_type in ("messenger", "instagram"):
        return await _fetch_messenger_contacts(bot_profile_id)
    # For other platforms, try the sync get_contacts
    from app.platforms.registry import platform_registry
    from app.platforms.base import PlatformType
    adapter = platform_registry.get_adapter(PlatformType(platform_type))
    return adapter.get_contacts(bot_profile_id)


async def _fetch_messenger_contacts(bot_profile_id: int) -> list:
    """Fetch contacts from Messenger/Instagram via Graph API."""
    import httpx
    from app.database import BotProfile as BPModel, SessionLocal
    from app.auth.utils import decrypt_string

    db = SessionLocal()
    try:
        bot = db.query(BPModel).filter(BPModel.id == bot_profile_id).first()
        if not bot:
            return []
        platform_config = json.loads(bot.platform_config or "{}")
        token_encrypted = platform_config.get("platform_token_encrypted")
        if not token_encrypted:
            return []
        page_token = decrypt_string(token_encrypted)
        page_id = platform_config.get("page_id", "")
    finally:
        db.close()

    contacts = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://graph.facebook.com/v21.0/me/conversations",
                params={"access_token": page_token, "fields": "id,participants", "limit": "100"},
            )
            if resp.status_code != 200:
                logger.warning(f"Bot {bot_profile_id}: Failed to fetch Messenger conversations: {resp.text[:200]}")
                return []

            # Detect page participant IDs
            conversations = resp.json().get("data", [])
            page_ids = {page_id}
            me_resp = await client.get(
                "https://graph.facebook.com/v21.0/me",
                params={"access_token": page_token, "fields": "id"},
            )
            if me_resp.status_code == 200:
                page_ids.add(me_resp.json().get("id", ""))
            if len(conversations) >= 2:
                from collections import Counter
                all_pids = []
                for c in conversations:
                    for p in c.get("participants", {}).get("data", []):
                        all_pids.append(p.get("id", ""))
                for pid, count in Counter(all_pids).items():
                    if count == len(conversations):
                        page_ids.add(pid)

            for conv in conversations:
                for p in conv.get("participants", {}).get("data", []):
                    if p.get("id") not in page_ids:
                        contacts.append({
                            "chat_id": p.get("id", ""),
                            "name": p.get("name", "Unknown"),
                            "phone": "",
                        })
        logger.info(f"Bot {bot_profile_id}: Messenger returned {len(contacts)} contacts")
    except Exception as e:
        logger.error(f"Bot {bot_profile_id}: Error fetching Messenger contacts: {e}")
    return contacts


async def _fetch_platform_groups(bot_profile_id: int, platform_type: str) -> list:
    """Fetch groups from a live platform adapter (async-safe)."""
    if platform_type == "telegram":
        from app.platforms.telegram.adapter import _active_clients, TelegramAdapter
        client = _active_clients.get(bot_profile_id)
        if not client:
            return []
        adapter = TelegramAdapter()
        return await adapter._async_get_groups(client, bot_profile_id)
    from app.platforms.registry import platform_registry
    from app.platforms.base import PlatformType
    adapter = platform_registry.get_adapter(PlatformType(platform_type))
    return adapter.get_groups(bot_profile_id)


def mask_api_key(encrypted_key: str) -> Optional[str]:
    """
    Decrypt and mask API key for display.
    Shows first 8 and last 4 characters: sk-proj-...gasA
    """
    if not encrypted_key:
        return None
    try:
        decrypted = decrypt_string(encrypted_key)
        if not decrypted or len(decrypted) < 12:
            return None
        # Show first 8 chars and last 4 chars
        return f"{decrypted[:8]}...{decrypted[-4:]}"
    except Exception:
        return None


def build_recipient_summary(recipient_type: str, contact_ids: list, group_ids: list, db: Session, hub_id: int) -> str:
    """Build a human-readable summary of recipients."""
    recipient_type = recipient_type or "broadcast"

    if recipient_type == "broadcast_all":
        return "All Contacts + Groups"
    elif recipient_type == "broadcast":
        return "All Contacts"
    elif recipient_type == "all_groups":
        return "All Groups"
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

        script_count = db.query(func.count(ConversationScript.id)).filter(
            ConversationScript.hub_id == hub.id
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
            ai_provider=hub.ai_provider or "openai",
            api_key_masked=mask_api_key(hub.api_key_encrypted),
            model=hub.model,
            is_active=hub.is_active,
            created_at=hub.created_at,
            updated_at=hub.updated_at,
            bot_count=bot_count,
            agent_count=agent_count,
            contact_count=contact_count,
            content_count=content_count,
            script_count=script_count,
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
    if hub_data.api_key:
        encrypted_key = encrypt_string(hub_data.api_key)

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
        ai_provider=hub_data.ai_provider,
        api_key_encrypted=encrypted_key,
        model=hub_data.model,
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
    emit_event_sync("hub.created", db=db, hub_id=hub.id, user_id=current_user.id)

    return HubResponse(
        id=hub.id,
        name=hub.name,
        description=hub.description,
        task_type=hub.task_type,
        ai_provider=hub.ai_provider or "openai",
        api_key_masked=mask_api_key(hub.api_key_encrypted),
        model=hub.model,
        is_active=hub.is_active,
        created_at=hub.created_at,
        updated_at=hub.updated_at,
        bot_count=0,
        agent_count=0,
        contact_count=0,
        content_count=0,
        script_count=0,
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

    script_count = db.query(func.count(ConversationScript.id)).filter(
        ConversationScript.hub_id == hub_id
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
        ai_provider=hub.ai_provider or "openai",
        api_key_masked=mask_api_key(hub.api_key_encrypted),
        model=hub.model,
        is_active=hub.is_active,
        created_at=hub.created_at,
        updated_at=hub.updated_at,
        bot_count=len(bots),
        agent_count=len(agents),
        contact_count=contact_count,
        content_count=content_count,
        script_count=script_count,
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
    if hub_data.ai_provider is not None:
        hub.ai_provider = hub_data.ai_provider
    if hub_data.api_key is not None:
        hub.api_key_encrypted = encrypt_string(hub_data.api_key)
    if hub_data.model is not None:
        hub.model = hub_data.model
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
    emit_event_sync("hub.updated", db=db, hub_id=hub_id)

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

    script_count = db.query(func.count(ConversationScript.id)).filter(
        ConversationScript.hub_id == hub.id
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
        ai_provider=hub.ai_provider or "openai",
        api_key_masked=mask_api_key(hub.api_key_encrypted),
        model=hub.model,
        is_active=hub.is_active,
        created_at=hub.created_at,
        updated_at=hub.updated_at,
        bot_count=bot_count,
        agent_count=agent_count,
        contact_count=contact_count,
        content_count=content_count,
        script_count=script_count,
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

    # Manually delete all related records (SQLite doesn't enforce CASCADE by default)
    # Delete script-related records first (child tables)
    script_ids = [s.id for s in db.query(ConversationScript.id).filter(ConversationScript.hub_id == hub_id).all()]
    if script_ids:
        db.query(ScriptMessage).filter(ScriptMessage.script_id.in_(script_ids)).delete(synchronize_session=False)
        db.query(ScriptExecution).filter(ScriptExecution.script_id.in_(script_ids)).delete(synchronize_session=False)
        db.query(ConversationScript).filter(ConversationScript.hub_id == hub_id).delete(synchronize_session=False)

    # Delete agent executions before agents
    agent_ids = [a.id for a in db.query(AIAgent.id).filter(AIAgent.hub_id == hub_id).all()]
    if agent_ids:
        db.query(AgentExecution).filter(AgentExecution.agent_id.in_(agent_ids)).delete(synchronize_session=False)

    # Delete other hub-related records
    db.query(MessageRouting).filter(MessageRouting.hub_id == hub_id).delete(synchronize_session=False)
    db.query(HubMessageTopic).filter(HubMessageTopic.hub_id == hub_id).delete(synchronize_session=False)
    db.query(ScheduledContent).filter(ScheduledContent.hub_id == hub_id).delete(synchronize_session=False)
    db.query(Contact).filter(Contact.hub_id == hub_id).delete(synchronize_session=False)
    db.query(AIAgent).filter(AIAgent.hub_id == hub_id).delete(synchronize_session=False)
    db.query(HubBotMembership).filter(HubBotMembership.hub_id == hub_id).delete(synchronize_session=False)

    db.delete(hub)
    db.commit()
    emit_event_sync("hub.deleted", db=db, hub_id=hub_id)

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
    emit_event_sync("hub.toggled", db=db, hub_id=hub_id)

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
    emit_event_sync("hub.bot_added", db=db, hub_id=hub_id, bot_profile_id=data.bot_profile_id)

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
    emit_event_sync("hub.bot_removed", db=db, hub_id=hub_id, bot_profile_id=bot_id)

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
            ai_provider=a.ai_provider or "openai",
            api_key_masked=mask_api_key(a.api_key_encrypted),
            model=a.model,
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
    if data.api_key:
        encrypted_key = encrypt_string(data.api_key)

    agent = AIAgent(
        hub_id=hub_id,
        name=data.name,
        agent_type=data.agent_type,
        description=data.description,
        ai_provider=data.ai_provider,
        api_key_encrypted=encrypted_key,
        model=data.model,
        system_prompt=data.system_prompt,
        additional_instructions=data.additional_instructions,
        config=json.dumps(data.config) if data.config else None
    )

    db.add(agent)
    db.commit()
    db.refresh(agent)
    emit_event_sync("agent.created", db=db, agent_id=agent.id)

    return AgentResponse(
        id=agent.id,
        hub_id=hub_id,
        name=agent.name,
        agent_type=agent.agent_type,
        description=agent.description,
        ai_provider=agent.ai_provider,
        api_key_masked=mask_api_key(agent.api_key_encrypted),
        model=agent.model,
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
        ai_provider=agent.ai_provider or "openai",
        api_key_masked=mask_api_key(agent.api_key_encrypted),
        model=agent.model,
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
    if data.ai_provider is not None:
        agent.ai_provider = data.ai_provider
    if data.api_key is not None:
        agent.api_key_encrypted = encrypt_string(data.api_key)
    if data.model is not None:
        agent.model = data.model
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
    emit_event_sync("agent.updated", db=db, agent_id=agent_id)

    return AgentResponse(
        id=agent.id,
        hub_id=agent.hub_id,
        name=agent.name,
        agent_type=agent.agent_type,
        description=agent.description,
        ai_provider=agent.ai_provider or "openai",
        api_key_masked=mask_api_key(agent.api_key_encrypted),
        model=agent.model,
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
    emit_event_sync("agent.deleted", db=db, agent_id=agent_id)

    return {"message": "Agent deleted successfully"}


# ============== Contacts ==============

@router.get("/{hub_id}/contacts")
async def list_contacts(
    hub_id: int,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List contacts in a hub with pagination, filtered by bots assigned to the hub."""
    from app.database import Conversation, HubBotMembership

    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get bot profile IDs that are members of this hub
    bot_memberships = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.is_active == True
    ).all()
    bot_profile_ids = [m.bot_profile_id for m in bot_memberships]

    # If no bots in hub, return empty result
    if not bot_profile_ids:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    # Get bot names for lookup
    bot_profiles = db.query(BotProfile).filter(BotProfile.id.in_(bot_profile_ids)).all()
    bot_names = {b.id: b.name for b in bot_profiles}

    # Get all bot phone numbers to exclude (bots shouldn't appear as contacts)
    all_bots_with_phones = db.query(BotProfile).filter(
        BotProfile.whatsapp_phone.isnot(None),
        BotProfile.whatsapp_phone != ''
    ).all()
    bot_phone_set = set()
    for bot in all_bots_with_phones:
        # Normalize phone number (remove +, spaces, dashes)
        normalized = ''.join(c for c in bot.whatsapp_phone if c.isdigit())
        if normalized:
            bot_phone_set.add(normalized)
            # Also add with + prefix variations
            bot_phone_set.add(f"+{normalized}")

    # Get phone numbers and chat_ids from all private conversations for these bots
    phone_to_bots = {}
    convs = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_profile_ids),
        Conversation.is_group == False,
    ).all()
    for conv in convs:
        # Use phone if available, otherwise use chat_id as identifier
        identifier = (conv.phone or "").strip()
        if not identifier:
            identifier = conv.chat_id  # Messenger/Instagram use chat_id

        if not identifier:
            continue

        # Skip if this phone belongs to a bot
        normalized_phone = ''.join(c for c in identifier if c.isdigit())
        if normalized_phone in bot_phone_set:
            continue
        # Skip invalid identifiers (less than 5 digits)
        if len(normalized_phone) < 5:
            continue

        if identifier not in phone_to_bots:
            phone_to_bots[identifier] = set()
        phone_to_bots[identifier].add(conv.bot_profile_id)
        # Also add chat_id as a valid identifier (contacts may be stored with chat_id as phone)
        if conv.chat_id and conv.chat_id != identifier:
            if conv.chat_id not in phone_to_bots:
                phone_to_bots[conv.chat_id] = set()
            phone_to_bots[conv.chat_id].add(conv.bot_profile_id)

    valid_phone_set = set(phone_to_bots.keys())

    # Filter contacts by hub and valid phones/chat_ids
    query = db.query(Contact).filter(
        Contact.hub_id == hub_id,
        Contact.phone.in_(valid_phone_set)
    )

    if search:
        query = query.filter(
            (Contact.phone.ilike(f"%{search}%")) |
            (Contact.display_name.ilike(f"%{search}%"))
        )

    if tag:
        query = query.join(ContactTag).filter(ContactTag.tag == tag)

    # Get total count
    total = query.count()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    # Apply pagination
    offset = (page - 1) * page_size
    contacts = query.order_by(Contact.last_interaction_at.desc().nullslast()).offset(offset).limit(page_size).all()

    result = []
    for c in contacts:
        # Get tags for this contact
        tags = db.query(ContactTag).filter(
            ContactTag.contact_id == c.id
        ).all()

        # Get all bot names for this contact
        bot_ids = phone_to_bots.get(c.phone, set())
        contact_bot_names = [bot_names[bid] for bid in bot_ids if bid in bot_names]
        bot_name = ", ".join(contact_bot_names) if contact_bot_names else None

        result.append({
            "id": c.id,
            "phone": c.phone,
            "display_name": c.display_name,
            "profile_pic": c.profile_pic,
            "description": c.description,
            "predicted_intent": c.predicted_intent,
            "engagement_score": c.engagement_score,
            "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
            "tag_count": len(tags),
            "tags": [
                {
                    "id": t.id,
                    "tag": t.tag,
                    "value": t.value,
                    "confidence": t.confidence,
                    "source": t.source
                }
                for t in tags[:5]  # Limit to first 5 tags for list view
            ],
            "bot_name": bot_name,
            "analysis_status": c.analysis_status
        })

    return {"items": result, "total": total, "page": page, "page_size": page_size, "total_pages": total_pages}


@router.get("/{hub_id}/contacts/export")
async def export_contacts(
    hub_id: int,
    format: str = Query("csv", pattern="^(csv|json)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export contacts with analysis results as CSV or JSON."""
    from fastapi.responses import StreamingResponse
    import csv
    import io

    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get all contacts for the hub
    contacts = db.query(Contact).filter(Contact.hub_id == hub_id).all()

    # Build export data
    export_data = []
    for c in contacts:
        # Get tags for this contact
        tags = db.query(ContactTag).filter(ContactTag.contact_id == c.id).all()
        tag_list = [t.tag for t in tags]

        # Parse key_topics if stored as JSON
        key_topics = []
        if c.key_topics:
            try:
                key_topics = json.loads(c.key_topics) if isinstance(c.key_topics, str) else c.key_topics
            except (json.JSONDecodeError, TypeError):
                key_topics = []

        export_data.append({
            "phone": c.phone,
            "display_name": c.display_name or "",
            "description": c.description or "",
            "predicted_intent": c.predicted_intent or "",
            "engagement_score": c.engagement_score or 0,
            "sentiment": c.sentiment or "",
            "urgency": c.urgency or "",
            "follow_up_needed": c.follow_up_needed or False,
            "follow_up_reason": c.follow_up_reason or "",
            "key_topics": ", ".join(key_topics) if key_topics else "",
            "tags": ", ".join(tag_list),
            "analysis_status": c.analysis_status or "",
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else "",
            "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else ""
        })

    if format == "json":
        return {
            "hub_id": hub_id,
            "hub_name": hub.name,
            "exported_at": datetime.utcnow().isoformat(),
            "total_contacts": len(export_data),
            "contacts": export_data
        }
    else:
        # Generate CSV
        output = io.StringIO()
        if export_data:
            fieldnames = list(export_data[0].keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(export_data)

        output.seek(0)
        filename = f"contacts_{hub.name.replace(' ', '_')}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get contact details with tags and bot info."""
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    tags = db.query(ContactTag).filter(ContactTag.contact_id == contact_id).all()

    # Get bot info for this contact
    hub = db.query(Hub).filter(Hub.id == contact.hub_id).first()
    hub_bot_ids = [hb.bot_profile_id for hb in hub.bot_memberships if hb.is_active]

    bot_names = []
    if hub_bot_ids:
        bot_ids = set()

        # Find private conversations with this contact's phone
        private_convs = db.query(Conversation).filter(
            Conversation.bot_profile_id.in_(hub_bot_ids),
            Conversation.phone == contact.phone,
            Conversation.is_group == False
        ).all()
        bot_ids.update(c.bot_profile_id for c in private_convs)

        # Also find bots from group messages where this contact is the sender
        group_convs = db.query(Conversation).filter(
            Conversation.bot_profile_id.in_(hub_bot_ids),
            Conversation.is_group == True
        ).all()
        if group_convs:
            group_conv_ids = [c.id for c in group_convs]
            group_msgs = db.query(Message).filter(
                Message.conversation_id.in_(group_conv_ids),
                Message.sender_phone == contact.phone
            ).all()
            for msg in group_msgs:
                conv = next((c for c in group_convs if c.id == msg.conversation_id), None)
                if conv:
                    bot_ids.add(conv.bot_profile_id)

        # Get bot names
        if bot_ids:
            bots = db.query(BotProfile).filter(BotProfile.id.in_(bot_ids)).all()
            bot_names = [b.name for b in bots]

    return {
        "id": contact.id,
        "hub_id": contact.hub_id,
        "phone": contact.phone,
        "display_name": contact.display_name,
        "profile_pic": contact.profile_pic,
        "description": contact.description,
        "predicted_intent": contact.predicted_intent,
        "engagement_score": contact.engagement_score,
        "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
        "first_seen_at": contact.first_seen_at.isoformat() if contact.first_seen_at else None,
        "created_at": contact.created_at.isoformat() if contact.created_at else None,
        "bot_names": bot_names,
        "tags": [
            {
                "id": t.id,
                "tag": t.tag,
                "value": t.value,
                "confidence": t.confidence,
                "source": t.source,
                "created_at": t.created_at.isoformat() if t.created_at else None
            }
            for t in tags
        ],
        # AI Analysis fields
        "sentiment": contact.sentiment,
        "urgency": contact.urgency,
        "follow_up_needed": contact.follow_up_needed or False,
        "follow_up_reason": contact.follow_up_reason,
        "key_topics": json.loads(contact.key_topics) if contact.key_topics else None,
        # Analysis queue fields
        "analysis_status": contact.analysis_status
    }


@router.get("/contacts/{contact_id}/messages")
async def get_contact_messages(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get contact message statistics and recent messages."""
    from sqlalchemy import func, case

    # Verify contact belongs to user
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Get hub's bot profile IDs
    hub = db.query(Hub).filter(Hub.id == contact.hub_id).first()
    hub_bot_ids = [hb.bot_profile_id for hb in hub.bot_memberships if hb.is_active]

    if not hub_bot_ids:
        return {
            "statistics": {
                "total_messages": 0,
                "messages_received": 0,
                "messages_sent": 0,
                "first_message_at": None,
                "last_message_at": None,
                "conversations_count": 0
            },
            "recent_messages": []
        }

    # Find conversations with this contact (match by phone OR chat_id)
    from sqlalchemy import or_
    private_conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(hub_bot_ids),
        or_(
            Conversation.phone == contact.phone,
            Conversation.chat_id == contact.phone,
        ),
        Conversation.is_group == False
    ).all()

    private_conv_ids = [c.id for c in private_conversations]

    # Find messages from this contact in group chats
    group_conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(hub_bot_ids),
        Conversation.is_group == True
    ).all()

    group_conv_ids = [c.id for c in group_conversations]

    # Get all messages from private conversations
    private_messages = []
    if private_conv_ids:
        private_messages = db.query(Message).filter(
            Message.conversation_id.in_(private_conv_ids)
        ).all()

    # Get messages from group chats where this contact is sender
    group_messages = []
    if group_conv_ids:
        group_messages = db.query(Message).filter(
            Message.conversation_id.in_(group_conv_ids),
            Message.sender_phone == contact.phone
        ).all()

    # Combine and calculate statistics
    all_messages = private_messages + group_messages

    if not all_messages:
        return {
            "statistics": {
                "total_messages": 0,
                "messages_received": 0,
                "messages_sent": 0,
                "first_message_at": None,
                "last_message_at": None,
                "conversations_count": len(private_conv_ids)
            },
            "recent_messages": []
        }

    # Calculate statistics
    messages_received = sum(1 for m in all_messages if m.role == "user")
    messages_sent = sum(1 for m in all_messages if m.role == "assistant")

    timestamps = [m.timestamp for m in all_messages if m.timestamp]
    first_message_at = min(timestamps) if timestamps else None
    last_message_at = max(timestamps) if timestamps else None

    # Get recent messages (last 10, sorted by timestamp)
    sorted_messages = sorted(all_messages, key=lambda m: m.timestamp or datetime.min, reverse=True)[:10]

    # Build bot name lookup
    bot_name_map = {}
    if hub_bot_ids:
        bots = db.query(BotProfile).filter(BotProfile.id.in_(hub_bot_ids)).all()
        bot_name_map = {b.id: b.name for b in bots}

    recent_messages = []
    for msg in sorted_messages:
        # Get conversation info for context
        conv = db.query(Conversation).filter(Conversation.id == msg.conversation_id).first()
        bot_name = bot_name_map.get(conv.bot_profile_id) if conv else None
        recent_messages.append({
            "id": msg.id,
            "role": msg.role,
            "content": msg.content[:500] if msg.content else "",  # Truncate long messages
            "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
            "sender_name": msg.sender_name,
            "bot_name": bot_name,
            "conversation_name": conv.chat_name if conv else None,
            "is_group": conv.is_group if conv else False,
            "has_attachment": bool(msg.file_url)
        })

    return {
        "statistics": {
            "total_messages": len(all_messages),
            "messages_received": messages_received,
            "messages_sent": messages_sent,
            "first_message_at": first_message_at.isoformat() if first_message_at else None,
            "last_message_at": last_message_at.isoformat() if last_message_at else None,
            "conversations_count": len(private_conv_ids)
        },
        "recent_messages": recent_messages
    }


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
    emit_event_sync("contact.created", db=db, contact_id=contact.id, hub_id=hub_id)

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
        tags=[],
        sentiment=contact.sentiment,
        urgency=contact.urgency,
        follow_up_needed=contact.follow_up_needed or False,
        follow_up_reason=contact.follow_up_reason,
        key_topics=json.loads(contact.key_topics) if contact.key_topics else None,
        analysis_status=contact.analysis_status
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
    emit_event_sync("contact.updated", db=db, contact_id=contact_id)

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
        ],
        sentiment=contact.sentiment,
        urgency=contact.urgency,
        follow_up_needed=contact.follow_up_needed or False,
        follow_up_reason=contact.follow_up_reason,
        key_topics=json.loads(contact.key_topics) if contact.key_topics else None,
        analysis_status=contact.analysis_status
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
    emit_event_sync("contact.deleted", db=db, contact_id=contact_id)

    return {"message": "Contact deleted successfully"}


@router.post("/contacts/{contact_id}/analyze")
async def analyze_contact(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Analyze a contact using AI to generate tags, engagement score, and predictions.

    This endpoint:
    1. Fetches the contact's conversation history
    2. Runs the analyzer agent to generate insights
    3. Updates the contact with new tags and scores
    """
    from app.hubs.agents.analyzer import AnalyzerAgent
    from app.auth.utils import decrypt_string

    # Get the contact with hub verification
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    hub = contact.hub

    # Check if hub has an analyzer agent
    analyzer_agent = db.query(AIAgent).filter(
        AIAgent.hub_id == hub.id,
        AIAgent.agent_type == "analyzer",
        AIAgent.is_active == True
    ).first()

    # Get API key (from agent or hub)
    api_key = None
    if analyzer_agent and analyzer_agent.api_key_encrypted:
        api_key = decrypt_string(analyzer_agent.api_key_encrypted)
    elif hub.api_key_encrypted:
        api_key = decrypt_string(hub.api_key_encrypted)

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No API key configured. Please configure an API key in the hub settings or create an analyzer agent."
        )

    # Get conversation history for this contact
    # Find conversations where phone matches (try exact, then normalized without spaces/+)
    conversations = db.query(Conversation).filter(
        or_(
            Conversation.phone == contact.phone,
            Conversation.chat_id == contact.phone
        )
    ).all()

    # If no match, try normalized phone (strip spaces, +, dashes)
    if not conversations:
        import re
        clean_phone = re.sub(r'[\s\+\-]', '', contact.phone or '')
        if clean_phone:
            all_convs = db.query(Conversation).filter(
                Conversation.phone.isnot(None)
            ).all()
            conversations = [c for c in all_convs if re.sub(r'[\s\+\-]', '', c.phone or '') == clean_phone]

    # Collect messages from all conversations
    messages = []
    bot_names = set()
    found_profile_pic = None
    for conv in conversations:
        # Get bot name
        if conv.bot_profile:
            bot_names.add(conv.bot_profile.name)

        # Try to get profile_pic from conversation if contact doesn't have one
        if not contact.profile_pic and not found_profile_pic and conv.profile_pic:
            found_profile_pic = conv.profile_pic

        # Get messages for this conversation
        conv_messages = db.query(Message).filter(
            Message.conversation_id == conv.id
        ).order_by(Message.timestamp.asc()).limit(100).all()

        for msg in conv_messages:
            messages.append({
                "role": msg.role or "user",
                "content": msg.content or "",
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None
            })
            # Also try to get profile_pic from message sender_profile_pic (for user messages)
            if not contact.profile_pic and not found_profile_pic and msg.role == "user" and msg.sender_profile_pic:
                found_profile_pic = msg.sender_profile_pic

    # Update contact's profile_pic if we found one and contact doesn't have one
    if found_profile_pic and not contact.profile_pic:
        contact.profile_pic = found_profile_pic
        db.commit()

    # Get existing tags
    existing_tags = db.query(ContactTag).filter(
        ContactTag.contact_id == contact_id
    ).all()
    existing_tag_names = [t.tag for t in existing_tags]

    # Prepare input data for analyzer
    input_data = {
        "contact": {
            "phone": contact.phone,
            "display_name": contact.display_name,
            "existing_tags": existing_tag_names,
            "engagement_score": contact.engagement_score,
            "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None
        },
        "messages": messages,
        "context": {
            "hub_name": hub.name,
            "bot_names": list(bot_names)
        }
    }

    # Track execution time
    start_time = time.time()

    # Create analyzer agent instance
    try:
        if analyzer_agent:
            agent = AnalyzerAgent(
                agent=analyzer_agent,
                hub_api_key=hub.api_key_encrypted,
                hub_ai_provider=hub.ai_provider
            )
        else:
            # Create a minimal agent-like object for direct analysis
            from app.ai import get_ai_provider
            provider = get_ai_provider(
                provider_name=hub.ai_provider or "openai",
                api_key=api_key,
                model=hub.model or "gpt-4o-mini"
            )

            # Run analysis directly with improved prompt
            system_prompt = """You are a contact analyzer AI. Analyze conversation history to build a comprehensive contact profile.

IMPORTANT: You MUST include ALL fields in your response.

Your analysis should include:
1. **Tags**: Generate 2-5 relevant tags based on interests, behaviors, topics discussed. Examples: "interested_in_product", "price_conscious", "tech_savvy", "quick_responder", "new_customer", "returning_customer", "support_seeker", "business_inquiry"
2. **Engagement Score**: 0-100 based on message frequency, response patterns, and interaction quality
3. **Predicted Intent**: Choose the most appropriate from: buyer, browser, support_seeker, information_seeker, price_checker, partner, complaint, feedback, general_inquiry, returning_customer
4. **Sentiment**: Overall sentiment (positive, neutral, negative)
5. **Urgency**: How urgent is follow-up needed (low, medium, high)
6. **Follow-up Needed**: Whether this contact needs follow-up and why
7. **Description**: A brief profile summary
8. **Key Topics**: Main topics discussed

Return a JSON object with this EXACT structure:
{
    "tags": [
        {"tag": "interested_in_product", "confidence": 0.85, "value": null},
        {"tag": "new_inquiry", "confidence": 0.9, "value": null}
    ],
    "engagement_score": 75,
    "predicted_intent": "information_seeker",
    "sentiment": "positive",
    "urgency": "medium",
    "follow_up_needed": true,
    "follow_up_reason": "Reason for follow-up",
    "description": "Brief profile summary",
    "key_topics": ["topic1", "topic2"]
}

CRITICAL: Choose predicted_intent from the list provided. The "tags" array MUST contain at least 2 relevant tags.
Return ONLY valid JSON."""

            # Format messages for prompt
            message_history = "\n".join([
                f"[{m.get('timestamp', '')}] {m['role'].upper()}: {m['content']}"
                for m in messages[-50:]  # Last 50 messages
            ])

            user_message = f"""Analyze this contact's conversation history and return your analysis as JSON:

Phone: {contact.phone}
Name: {contact.display_name or 'Unknown'}
Existing Tags: {', '.join(existing_tag_names) if existing_tag_names else 'None'}

Conversation History:
{message_history if message_history else 'No messages available'}

Provide a comprehensive analysis in JSON format."""

            # Run blocking AI call in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            ai_response = await loop.run_in_executor(
                ai_executor,
                lambda: provider.chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    max_tokens=1000,
                    temperature=0.3,
                    json_mode=True
                )
            )

            result = json.loads(ai_response.content)
            result["tokens_used"] = ai_response.usage.get("total_tokens", 0)

    except Exception as e:
        logger.error(f"Failed to create analyzer: {e}")
        # Log error to ToolMonitor
        execution_time_ms = int((time.time() - start_time) * 1000)
        ToolMonitor.log_execution(
            db=db,
            tool_type="contact_analyzer",
            operation="analyze_contact",
            hub_id=hub.id,
            input_data={
                "contact_id": contact_id,
                "contact_phone": contact.phone,
                "contact_name": contact.display_name
            },
            status="error",
            error_message=str(e),
            execution_time_ms=execution_time_ms,
            triggered_by="user",
            related_entity_type="contact",
            related_entity_id=contact_id,
            user_id=current_user.id
        )
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    # Run the analysis if using agent
    if analyzer_agent:
        try:
            # Run blocking AI call in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                ai_executor,
                lambda: agent.run(input_data, trigger_type="manual")
            )
        except Exception as e:
            logger.error(f"Analysis failed: {e}")
            # Log error to ToolMonitor
            execution_time_ms = int((time.time() - start_time) * 1000)
            ToolMonitor.log_execution(
                db=db,
                tool_type="contact_analyzer",
                operation="analyze_contact",
                hub_id=hub.id,
                input_data={
                    "contact_id": contact_id,
                    "contact_phone": contact.phone,
                    "contact_name": contact.display_name,
                    "message_count": len(messages)
                },
                status="error",
                error_message=str(e),
                execution_time_ms=execution_time_ms,
                triggered_by="user",
                related_entity_type="contact",
                related_entity_id=contact_id,
                user_id=current_user.id
            )
            raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    # Check for errors
    if result.get("error"):
        # Log error to ToolMonitor
        execution_time_ms = int((time.time() - start_time) * 1000)
        ToolMonitor.log_execution(
            db=db,
            tool_type="contact_analyzer",
            operation="analyze_contact",
            hub_id=hub.id,
            input_data={
                "contact_id": contact_id,
                "contact_phone": contact.phone,
                "contact_name": contact.display_name,
                "message_count": len(messages)
            },
            status="error",
            error_message=result["error"],
            execution_time_ms=execution_time_ms,
            tokens_used=result.get("tokens_used", 0),
            triggered_by="user",
            related_entity_type="contact",
            related_entity_id=contact_id,
            user_id=current_user.id
        )
        raise HTTPException(status_code=500, detail=result["error"])

    # Log what the AI returned for debugging
    logger.info(f"Contact analyzer result for {contact.phone}: "
                f"description={result.get('description')!r}, "
                f"key_topics={result.get('key_topics')!r}, "
                f"follow_up_needed={result.get('follow_up_needed')!r}, "
                f"follow_up_reason={result.get('follow_up_reason')!r}")

    # Update contact with analysis results
    if result.get("description"):
        desc = result["description"]
        # Handle if description is a dict
        if isinstance(desc, dict):
            desc = desc.get("text") or desc.get("summary") or str(desc)
        contact.description = str(desc) if desc else None

    if result.get("predicted_intent"):
        intent = result["predicted_intent"]
        # Handle if predicted_intent is a dict (e.g., {'intent': '...', 'likelihood_score': 0.75})
        if isinstance(intent, dict):
            intent = intent.get("intent") or intent.get("type") or intent.get("value") or list(intent.values())[0]
        contact.predicted_intent = str(intent) if intent else None

    if result.get("engagement_score") is not None:
        # Normalize to 0-1 range for storage
        score = result["engagement_score"]
        # Handle if score is a dict
        if isinstance(score, dict):
            score = score.get("score") or score.get("value") or 50
        if score > 1:
            score = score / 100.0
        contact.engagement_score = min(1.0, max(0.0, float(score)))

    # Save new AI analysis fields
    if result.get("sentiment"):
        sentiment = result["sentiment"]
        if isinstance(sentiment, dict):
            sentiment = sentiment.get("value") or sentiment.get("sentiment") or str(sentiment)
        contact.sentiment = str(sentiment) if sentiment else None

    if result.get("urgency"):
        urgency = result["urgency"]
        if isinstance(urgency, dict):
            urgency = urgency.get("value") or urgency.get("level") or str(urgency)
        contact.urgency = str(urgency) if urgency else None

    # Save follow-up info - handle bool, string, or other types
    follow_up = result.get("follow_up_needed", False)
    if isinstance(follow_up, bool):
        contact.follow_up_needed = follow_up
    elif isinstance(follow_up, str):
        contact.follow_up_needed = follow_up.lower() in ("true", "yes", "1")
    else:
        contact.follow_up_needed = bool(follow_up)

    if result.get("follow_up_reason"):
        reason = result["follow_up_reason"]
        if isinstance(reason, dict):
            reason = reason.get("reason") or reason.get("text") or str(reason)
        contact.follow_up_reason = str(reason) if reason else None

    # Save key topics as JSON
    if result.get("key_topics"):
        topics = result["key_topics"]
        if isinstance(topics, list):
            # Ensure all items are strings
            topics = [str(t) if not isinstance(t, str) else t for t in topics]
            contact.key_topics = json.dumps(topics)
        elif isinstance(topics, str):
            contact.key_topics = topics

    contact.updated_at = datetime.utcnow()
    # Also update last_interaction_at since analysis counts as an interaction
    contact.last_interaction_at = datetime.utcnow()

    # Replace AI-generated tags (keep manually added tags)
    # First, delete existing AI-generated tags for this contact
    deleted_count = db.query(ContactTag).filter(
        ContactTag.contact_id == contact_id,
        ContactTag.source == "ai_analyzer"
    ).delete()
    logger.info(f"Contact analyzer: deleted {deleted_count} previous AI-generated tags for contact {contact_id}")

    # Add new tags from analysis
    new_tags_added = 0
    if result.get("tags"):
        for tag_data in result["tags"]:
            tag_name = tag_data.get("tag") if isinstance(tag_data, dict) else str(tag_data)
            if not tag_name:
                continue

            # Check if this exact tag already exists (could be manually added)
            existing = db.query(ContactTag).filter(
                ContactTag.contact_id == contact_id,
                ContactTag.tag == tag_name
            ).first()

            if not existing:
                new_tag = ContactTag(
                    contact_id=contact_id,
                    tag=tag_name,
                    value=tag_data.get("value") if isinstance(tag_data, dict) else None,
                    confidence=tag_data.get("confidence", 1.0) if isinstance(tag_data, dict) else 1.0,
                    source="ai_analyzer"
                )
                db.add(new_tag)
                new_tags_added += 1

    db.commit()
    db.refresh(contact)
    emit_event_sync("contact.analyzed", db=db, contact_id=contact_id)

    # Log the execution to ToolMonitor
    execution_time_ms = int((time.time() - start_time) * 1000)

    # Extract tag names for logging
    result_tags = result.get("tags", [])
    logger.info(f"Contact analyzer: raw result keys={list(result.keys())}, tags type={type(result_tags)}, tags count={len(result_tags) if result_tags else 0}")
    if result_tags:
        logger.info(f"Contact analyzer: first tag sample={result_tags[0] if result_tags else 'none'}")

    tag_names = []
    for tag_data in result_tags[:10]:  # Limit to 10 tags
        if isinstance(tag_data, dict):
            tag_name = tag_data.get("tag") or tag_data.get("name") or ""
            if tag_name:
                tag_names.append(str(tag_name))
        elif isinstance(tag_data, str) and tag_data:
            tag_names.append(tag_data)

    logger.info(f"Contact analyzer for {contact.phone}: result_tags count={len(result_tags)}, extracted tag_names={tag_names}")

    ToolMonitor.log_execution(
        db=db,
        tool_type="contact_analyzer",
        operation="analyze_contact",
        hub_id=hub.id,
        input_data={
            "contact_id": contact_id,
            "contact_phone": contact.phone,
            "contact_name": contact.display_name,
            "contact_profile_pic": contact.profile_pic,
            "message_count": len(messages),
            "bot_names": list(bot_names)
        },
        output_data={
            "predicted_intent": result.get("predicted_intent"),
            "sentiment": result.get("sentiment"),
            "urgency": result.get("urgency"),
            "engagement_score": result.get("engagement_score"),
            "tags": tag_names,
            "tags_count": len(result_tags),
            "new_tags_added": new_tags_added,
            "follow_up_needed": result.get("follow_up_needed"),
            "follow_up_reason": result.get("follow_up_reason"),
            "key_topics": result.get("key_topics", []),
            "description": result.get("description")
        },
        status="success",
        execution_time_ms=execution_time_ms,
        tokens_used=result.get("tokens_used", 0),
        triggered_by="user",
        related_entity_type="contact",
        related_entity_id=contact_id,
        user_id=current_user.id
    )

    # Get updated tags
    tags = db.query(ContactTag).filter(ContactTag.contact_id == contact_id).all()

    return {
        "success": True,
        "contact": ContactResponse(
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
            ],
            sentiment=contact.sentiment,
            urgency=contact.urgency,
            follow_up_needed=contact.follow_up_needed or False,
            follow_up_reason=contact.follow_up_reason,
            key_topics=json.loads(contact.key_topics) if contact.key_topics else None,
            analysis_status=contact.analysis_status
        ),
        "analysis": {
            "predicted_intent": result.get("predicted_intent"),
            "sentiment": result.get("sentiment"),
            "urgency": result.get("urgency"),
            "follow_up_needed": result.get("follow_up_needed"),
            "follow_up_reason": result.get("follow_up_reason"),
            "key_topics": result.get("key_topics", []),
            "new_tags_added": new_tags_added,
            "tokens_used": result.get("tokens_used", 0)
        }
    }


@router.post("/{hub_id}/contacts/analyze-all")
async def analyze_all_contacts(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Queue all contacts in a hub for AI analysis.
    Returns immediately with the count of queued contacts.
    """
    from app.hubs.analysis_scheduler import contact_analysis_scheduler

    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Queue all contacts
    queued_count = contact_analysis_scheduler.queue_contacts(db, hub_id)

    # Get current queue status
    status = contact_analysis_scheduler.get_queue_status(db, hub_id)

    return {
        "message": f"Queued {queued_count} contacts for analysis",
        "queued_count": queued_count,
        "queue_status": status
    }


@router.post("/contacts/{contact_id}/queue-analysis")
async def queue_contact_analysis(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Queue a single contact for AI analysis.
    Returns immediately - analysis will be processed in background.
    """
    from app.hubs.analysis_scheduler import contact_analysis_scheduler

    # Verify contact and hub ownership
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Queue the contact
    success = contact_analysis_scheduler.queue_contact(db, contact_id)

    if not success:
        return {
            "message": "Contact already queued or analyzing",
            "queued": False,
            "analysis_status": contact.analysis_status
        }

    return {
        "message": "Contact queued for analysis",
        "queued": True,
        "analysis_status": "pending"
    }


@router.post("/contacts/{contact_id}/cancel-analysis")
async def cancel_contact_analysis(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a pending or running contact analysis."""
    from app.hubs.analysis_scheduler import contact_analysis_scheduler

    # Verify contact and hub ownership
    contact = db.query(Contact).join(Hub).filter(
        Contact.id == contact_id,
        Hub.user_id == current_user.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    previous_status = contact.analysis_status

    if previous_status not in ("pending", "analyzing"):
        return {
            "message": f"Contact is not pending or analyzing (status: {previous_status})",
            "cancelled": False
        }

    # Cancel the analysis
    contact_analysis_scheduler.cancel_contact(contact_id)

    # Update status immediately for pending contacts
    if previous_status == "pending":
        contact.analysis_status = "cancelled"
        db.commit()

    return {
        "message": "Analysis cancelled",
        "cancelled": True,
        "previous_status": previous_status
    }


@router.post("/{hub_id}/contacts/cancel-all-analysis")
async def cancel_all_contact_analysis(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel all pending contact analyses for a hub."""
    from app.hubs.analysis_scheduler import contact_analysis_scheduler

    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Cancel all pending
    cancelled_count = contact_analysis_scheduler.cancel_all_pending(db, hub_id)

    return {
        "message": f"Cancelled {cancelled_count} pending analyses",
        "cancelled_count": cancelled_count
    }


@router.get("/{hub_id}/contacts/analysis-status")
async def get_contact_analysis_status(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get the current analysis queue status for a hub."""
    from app.hubs.analysis_scheduler import contact_analysis_scheduler

    # Verify hub ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    status = contact_analysis_scheduler.get_queue_status(db, hub_id)

    # Also get completed and failed counts
    completed = db.query(Contact).filter(
        Contact.hub_id == hub_id,
        Contact.analysis_status == "completed"
    ).count()

    failed = db.query(Contact).filter(
        Contact.hub_id == hub_id,
        Contact.analysis_status == "failed"
    ).count()

    cancelled = db.query(Contact).filter(
        Contact.hub_id == hub_id,
        Contact.analysis_status == "cancelled"
    ).count()

    total = db.query(Contact).filter(Contact.hub_id == hub_id).count()

    return {
        **status,
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "total_contacts": total
    }


@router.get("/{hub_id}/auto-analysis")
async def get_auto_analysis_config(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get auto-analysis configuration for a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    return {
        "enabled": hub.auto_analysis_enabled or False,
        "interval_hours": hub.auto_analysis_interval_hours or 24,
        "min_new_messages": hub.auto_analysis_min_new_messages or 5,
        "last_run": hub.auto_analysis_last_run.isoformat() if hub.auto_analysis_last_run else None
    }


@router.put("/{hub_id}/auto-analysis")
async def update_auto_analysis_config(
    hub_id: int,
    enabled: bool = None,
    interval_hours: int = Query(None, ge=1, le=168),  # 1 hour to 1 week
    min_new_messages: int = Query(None, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update auto-analysis configuration for a hub."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    if enabled is not None:
        hub.auto_analysis_enabled = enabled
    if interval_hours is not None:
        hub.auto_analysis_interval_hours = interval_hours
    if min_new_messages is not None:
        hub.auto_analysis_min_new_messages = min_new_messages

    db.commit()

    return {
        "message": "Auto-analysis configuration updated",
        "enabled": hub.auto_analysis_enabled,
        "interval_hours": hub.auto_analysis_interval_hours,
        "min_new_messages": hub.auto_analysis_min_new_messages,
        "last_run": hub.auto_analysis_last_run.isoformat() if hub.auto_analysis_last_run else None
    }


@router.post("/{hub_id}/auto-analysis/run")
async def trigger_auto_analysis(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Manually trigger auto-analysis check for contacts with new messages."""
    from app.hubs.analysis_scheduler import contact_analysis_scheduler

    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Run auto-analysis check
    queued_count = await contact_analysis_scheduler.check_and_queue_auto_analysis(db, hub)

    return {
        "message": f"Auto-analysis check completed",
        "contacts_queued": queued_count
    }


@router.post("/{hub_id}/sync-contacts")
async def sync_contacts_from_conversations(
    hub_id: int,
    include_group_participants: bool = True,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Sync contacts from bot conversations to the hub.

    For Contact Analyzer hubs, this also extracts participants from group messages.
    """
    from app.database import Conversation, HubBotMembership

    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get all bot profile IDs that are members of this hub
    bot_memberships = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.is_active == True
    ).all()

    bot_profile_ids = [m.bot_profile_id for m in bot_memberships]

    if not bot_profile_ids:
        return {
            "message": "No bots in hub. Please add bots to the hub first.",
            "synced": 0,
            "skipped": 0,
            "total_found": 0
        }

    # Pre-sync: fetch contacts from live platform adapters (Telegram, etc.)
    # This creates Conversation records for contacts not yet in the DB
    try:
        from app.database import BotProfile as BotProfileModel
        for bot_id in bot_profile_ids:
            bot_profile = db.query(BotProfileModel).filter(BotProfileModel.id == bot_id).first()
            if not bot_profile or not bot_profile.is_running:
                logger.info(f"Hub {hub_id}: Skipping bot {bot_id} (not found or not running)")
                continue
            platform_type = bot_profile.platform_type or "whatsapp"
            if platform_type == "whatsapp":
                continue
            logger.info(f"Hub {hub_id}: Pre-syncing contacts from bot {bot_id} ({platform_type})")
            try:
                contacts_from_platform = await _fetch_platform_contacts(bot_id, platform_type)
                logger.info(f"Hub {hub_id}: Got {len(contacts_from_platform)} contacts from bot {bot_id}")
                for contact in contacts_from_platform:
                    existing = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_id,
                        Conversation.chat_id == contact["chat_id"]
                    ).first()
                    if not existing:
                        new_conv = Conversation(
                            bot_profile_id=bot_id,
                            chat_id=contact["chat_id"],
                            chat_name=contact["name"],
                            display_name=contact["name"],
                            phone=contact.get("phone", ""),
                            is_group=False,
                            profile_pic=contact.get("profile_pic", ""),
                        )
                        db.add(new_conv)
                    elif not existing.phone and contact.get("phone"):
                        existing.phone = contact["phone"]
                db.commit()
            except Exception as e:
                logger.warning(f"Hub {hub_id}: Failed to pre-sync contacts from bot {bot_id}: {e}")
    except Exception as e:
        logger.warning(f"Hub {hub_id}: Platform contact pre-sync error: {e}")

    unique_contacts = {}

    # 1. Get contacts from private (non-group) conversations
    private_conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_profile_ids),
        Conversation.is_group == False,
    ).all()

    for conv in private_conversations:
        # Use phone as key if available, otherwise use chat_id
        phone = (conv.phone or "").strip()
        identifier = phone if phone else conv.chat_id
        if identifier and identifier not in unique_contacts:
            unique_contacts[identifier] = {
                "phone": identifier,
                "display_name": conv.chat_name or conv.display_name or identifier,
                "profile_pic": conv.profile_pic,
                "source": "private_chat"
            }

    # 2. For Contact Analyzer hubs, also extract contacts from group messages
    if include_group_participants and hub.task_type == "contact_analyzer":
        # Get all group conversations for these bots
        group_conversations = db.query(Conversation).filter(
            Conversation.bot_profile_id.in_(bot_profile_ids),
            Conversation.is_group == True
        ).all()

        group_conv_ids = [conv.id for conv in group_conversations]

        if group_conv_ids:
            # Get unique senders from group messages (role='user' means incoming message)
            group_messages = db.query(Message).filter(
                Message.conversation_id.in_(group_conv_ids),
                Message.role == "user",
                Message.sender_phone.isnot(None),
                Message.sender_phone != ""
            ).all()

            for msg in group_messages:
                phone = msg.sender_phone.strip()
                if phone and phone not in unique_contacts:
                    unique_contacts[phone] = {
                        "phone": phone,
                        "display_name": msg.sender_name,
                        "profile_pic": msg.sender_profile_pic,
                        "source": "group_chat"
                    }
                # Update profile_pic if we have one and the existing entry doesn't
                elif phone and msg.sender_profile_pic and not unique_contacts[phone].get("profile_pic"):
                    unique_contacts[phone]["profile_pic"] = msg.sender_profile_pic

    # Get existing contacts with their data
    existing_contacts = {
        c.phone: c for c in db.query(Contact).filter(
            Contact.hub_id == hub_id
        ).all()
    }

    # Create new contacts and update existing ones with missing profile_pics
    synced = 0
    skipped = 0
    updated = 0
    for phone, data in unique_contacts.items():
        if phone in existing_contacts:
            # Update profile_pic for existing contact if missing
            existing_contact = existing_contacts[phone]
            if not existing_contact.profile_pic and data.get("profile_pic"):
                existing_contact.profile_pic = data["profile_pic"]
                updated += 1
            skipped += 1
            continue

        contact = Contact(
            hub_id=hub_id,
            phone=data["phone"],
            display_name=data["display_name"],
            profile_pic=data.get("profile_pic"),
            first_seen_at=datetime.utcnow()
        )
        db.add(contact)
        synced += 1

    db.commit()

    # Build message
    msg_parts = []
    if synced:
        msg_parts.append(f"synced {synced} new contacts")
    if updated:
        msg_parts.append(f"updated {updated} contact profile pictures")
    message = ", ".join(msg_parts).capitalize() if msg_parts else "No changes made"

    return {
        "message": message,
        "synced": synced,
        "updated": updated,
        "skipped": skipped,
        "total_found": len(unique_contacts)
    }


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
    emit_event_sync("contact.tag_added", db=db, contact_id=contact_id, tag=data.tag)

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
    emit_event_sync("contact.tag_removed", db=db, contact_id=contact_id, tag_id=tag_id)

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
        # Parse bot_profile_ids JSON
        bot_profile_ids_list = None
        try:
            if c.bot_profile_ids:
                bot_profile_ids_list = json.loads(c.bot_profile_ids)
        except:
            pass

        bot_send_mode = c.bot_send_mode or "any"

        # Determine bot_name based on send mode
        bot_name = None
        if bot_send_mode == "all":
            bot_name = "All Bots"
        elif bot_send_mode == "any":
            bot_name = "Any Available Bot"
        elif bot_profile_ids_list and len(bot_profile_ids_list) > 0:
            # Multiple selected bots
            bot_names = []
            for bid in bot_profile_ids_list:
                bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
                if bot:
                    bot_names.append(bot.name)
            bot_name = ", ".join(bot_names) if bot_names else None
        elif c.bot_profile_id:
            # Legacy single bot
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
            bot_profile_ids=bot_profile_ids_list,
            bot_send_mode=bot_send_mode,
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
    bot_profile_ids = data.bot_profile_ids if data.bot_profile_ids else None
    bot_send_mode = data.bot_send_mode or "any"
    recipient_type = data.recipient_type or "broadcast"

    # Debug logging
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Creating scheduled content: recipient_type={recipient_type}, contact_ids={data.contact_ids}, group_ids={data.group_ids}, bot_profile_ids={bot_profile_ids}, bot_send_mode={bot_send_mode}")

    content = ScheduledContent(
        hub_id=hub_id,
        bot_profile_id=bot_profile_id,
        bot_profile_ids=json.dumps(bot_profile_ids) if bot_profile_ids else None,
        bot_send_mode=bot_send_mode,
        contact_id=data.contact_id,
        content=data.content,
        content_type=data.content_type,
        topic=data.topic,
        scheduled_for=data.scheduled_for,
        recipient_type=recipient_type,
        group_id=data.group_id,
        group_name=data.group_name,
        contact_ids=json.dumps(data.contact_ids) if data.contact_ids else None,
        group_ids=json.dumps(data.group_ids) if data.group_ids else None,
        # Rate limiting settings
        sending_speed_mode=data.sending_speed_mode or "auto",
        delay_min=data.delay_min,
        delay_max=data.delay_max,
        batch_size=data.batch_size,
        batch_pause=data.batch_pause
    )

    db.add(content)
    db.commit()
    db.refresh(content)
    emit_event_sync("content.created", db=db, content_id=content.id, hub_id=hub_id)

    # Build recipient summary
    recipient_summary = build_recipient_summary(
        data.recipient_type, data.contact_ids, data.group_ids, db, hub_id
    )

    # Build bot name(s) for display
    bot_name = None
    if bot_send_mode == "all":
        bot_name = "All Bots"
    elif bot_send_mode == "any":
        bot_name = "Any Available Bot"
    elif bot_profile_ids:
        bot_names_list = []
        for bid in bot_profile_ids:
            bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
            if bot:
                bot_names_list.append(bot.name)
        bot_name = ", ".join(bot_names_list) if bot_names_list else None

    return ScheduledContentResponse(
        id=content.id,
        hub_id=hub_id,
        bot_profile_id=content.bot_profile_id,
        bot_profile_ids=bot_profile_ids,
        bot_send_mode=bot_send_mode,
        bot_name=bot_name,
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
        recipient_summary=recipient_summary,
        # Rate limiting settings
        sending_speed_mode=content.sending_speed_mode or "auto",
        delay_min=content.delay_min,
        delay_max=content.delay_max,
        batch_size=content.batch_size,
        batch_pause=content.batch_pause
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
    if data.bot_profile_ids is not None:
        content.bot_profile_ids = json.dumps(data.bot_profile_ids) if data.bot_profile_ids else None
    if data.bot_send_mode is not None:
        content.bot_send_mode = data.bot_send_mode
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
    emit_event_sync("content.updated", db=db, content_id=content_id)

    # Parse bot_profile_ids from JSON
    bot_profile_ids_list = None
    try:
        if content.bot_profile_ids:
            bot_profile_ids_list = json.loads(content.bot_profile_ids)
    except:
        pass

    bot_send_mode = content.bot_send_mode or "any"

    # Build bot name(s) for display
    bot_name = None
    if bot_send_mode == "all":
        bot_name = "All Bots"
    elif bot_send_mode == "any":
        bot_name = "Any Available Bot"
    elif bot_profile_ids_list:
        bot_names_list = []
        for bid in bot_profile_ids_list:
            bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
            if bot:
                bot_names_list.append(bot.name)
        bot_name = ", ".join(bot_names_list) if bot_names_list else None
    elif content.bot_profile_id:
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
        bot_profile_ids=bot_profile_ids_list,
        bot_send_mode=bot_send_mode,
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


@router.post("/scheduled-content/{content_id}/cancel")
async def cancel_scheduled_content(
    content_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a pending, scheduled, or sending content."""
    content = db.query(ScheduledContent).join(Hub).filter(
        ScheduledContent.id == content_id,
        Hub.user_id == current_user.id
    ).first()

    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    # Only allow cancelling if not already sent or cancelled
    if content.status in ["sent", "cancelled"]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel content with status '{content.status}'")

    previous_status = content.status
    content.status = "cancelled"
    db.commit()
    emit_event_sync("content.cancelled", db=db, content_id=content_id)

    # If it was sending, try to stop the active task
    if previous_status == "sending":
        from app.hubs.scheduler import content_scheduler
        content_scheduler.cancel_content(content_id)

    return {"message": "Content cancelled", "previous_status": previous_status}


@router.post("/{hub_id}/content/bulk", response_model=List[ScheduledContentResponse])
async def bulk_create_scheduled_content(
    hub_id: int,
    items: List[ScheduledContentCreate],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Bulk create scheduled content items."""
    logger.debug(f"Bulk create content request for hub {hub_id}: {len(items)} items")

    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    created_items = []
    try:
        for idx, data in enumerate(items):
            bot_profile_id = data.bot_profile_id if data.bot_profile_id else None
            bot_profile_ids = data.bot_profile_ids if data.bot_profile_ids else None
            bot_send_mode = data.bot_send_mode or "any"
            recipient_type = data.recipient_type or "broadcast"

            # Handle recurring schedule
            schedule_type = data.schedule_type if hasattr(data, 'schedule_type') else "immediate"
            recurring_frequency = None
            recurring_time = None
            recurring_start_date = None
            recurring_end_date = None

            if data.recurring:
                schedule_type = "recurring"
                recurring_frequency = data.recurring.frequency
                recurring_time = data.recurring.time
                if data.recurring.start_date:
                    try:
                        recurring_start_date = datetime.fromisoformat(data.recurring.start_date)
                    except:
                        pass
                if data.recurring.end_date:
                    try:
                        recurring_end_date = datetime.fromisoformat(data.recurring.end_date)
                    except:
                        pass

            content = ScheduledContent(
                hub_id=hub_id,
                bot_profile_id=bot_profile_id,
                bot_profile_ids=json.dumps(bot_profile_ids) if bot_profile_ids else None,
                bot_send_mode=bot_send_mode,
                contact_id=data.contact_id,
                content=data.content,
                content_type=data.content_type,
                topic=data.topic,
                scheduled_for=data.scheduled_for,
                schedule_type=schedule_type,
                recurring_frequency=recurring_frequency,
                recurring_time=recurring_time,
                recurring_start_date=recurring_start_date,
                recurring_end_date=recurring_end_date,
                recipient_type=recipient_type,
                group_id=data.group_id,
                group_name=data.group_name,
                contact_ids=json.dumps(data.contact_ids) if data.contact_ids else None,
                group_ids=json.dumps(data.group_ids) if data.group_ids else None
            )
            db.add(content)
            db.flush()

            # Build recipient summary
            recipient_summary = build_recipient_summary(
                data.recipient_type, data.contact_ids, data.group_ids, db, hub_id
            )

            # Build bot name(s) for display
            bot_name = None
            if bot_send_mode == "all":
                bot_name = "All Bots"
            elif bot_send_mode == "any":
                bot_name = "Any Available Bot"
            elif bot_profile_ids:
                bot_names_list = []
                for bid in bot_profile_ids:
                    bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
                    if bot:
                        bot_names_list.append(bot.name)
                bot_name = ", ".join(bot_names_list) if bot_names_list else None

            created_items.append(ScheduledContentResponse(
                id=content.id,
                hub_id=hub_id,
                bot_profile_id=content.bot_profile_id,
                bot_profile_ids=bot_profile_ids,
                bot_send_mode=bot_send_mode,
                bot_name=bot_name,
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
            ))

        db.commit()
        logger.info(f"Bulk created {len(created_items)} content items for hub {hub_id}")

        return created_items
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error bulk creating content for hub {hub_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating content: {str(e)}")


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


@router.get("/{hub_id}/groups")
async def list_hub_groups(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List available WhatsApp groups from ALL user's bots.

    Returns groups from all bot conversations owned by the user.
    Each group includes list of bot_ids that are members of that group.
    """
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get ALL bot IDs owned by this user
    all_bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    all_bot_ids = [b[0] for b in all_bot_ids]

    if not all_bot_ids:
        return []

    # Get group conversations from ALL user's bots
    groups = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(all_bot_ids),
        Conversation.is_group == True
    ).order_by(Conversation.last_message_at.desc().nullslast()).all()

    # Get bot names for display
    bot_names_map = {}
    if all_bot_ids:
        bots = db.query(BotProfile.id, BotProfile.name).filter(BotProfile.id.in_(all_bot_ids)).all()
        bot_names_map = {b.id: b.name for b in bots}

    # Group by chat_id and collect all bot_ids for each group
    groups_map = {}
    for g in groups:
        if g.chat_id not in groups_map:
            groups_map[g.chat_id] = {
                "chat_id": g.chat_id,
                "name": g.chat_name or g.chat_id,
                "profile_pic": g.profile_pic,
                "message_count": g.message_count,
                "last_message_at": g.last_message_at.isoformat() if g.last_message_at else None,
                "bot_ids": [],
                "bot_names": []
            }
        groups_map[g.chat_id]["bot_ids"].append(g.bot_profile_id)

    # Add bot names to each group
    for group in groups_map.values():
        group["bot_names"] = [bot_names_map.get(bid, f"Bot {bid}") for bid in group["bot_ids"]]
        # Format bot_name string (show first 2 names, then "+N" if more)
        if len(group["bot_names"]) <= 2:
            group["bot_name"] = ", ".join(group["bot_names"])
        else:
            group["bot_name"] = ", ".join(group["bot_names"][:2]) + f" +{len(group['bot_names']) - 2}"

    return list(groups_map.values())


@router.get("/{hub_id}/all-contacts")
async def list_all_user_contacts(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all private conversation contacts from ALL user's bots.

    Returns contacts from all private (non-group) bot conversations owned by the user.
    Each contact includes list of bot_ids that have conversations with that contact.
    """
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get ALL bot IDs owned by this user
    all_bot_ids = db.query(BotProfile.id).filter(
        BotProfile.user_id == current_user.id
    ).all()
    all_bot_ids = [b[0] for b in all_bot_ids]

    if not all_bot_ids:
        return []

    # Get bot phone numbers to exclude them from contacts
    bot_phones = db.query(BotProfile.whatsapp_phone).filter(
        BotProfile.id.in_(all_bot_ids),
        BotProfile.whatsapp_phone.isnot(None)
    ).all()
    bot_phone_set = set()
    for (phone,) in bot_phones:
        if phone:
            normalized = ''.join(c for c in phone if c.isdigit())
            if normalized:
                bot_phone_set.add(normalized)

    # Get private conversations from ALL user's bots
    conversations = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(all_bot_ids),
        Conversation.is_group == False,
        Conversation.phone.isnot(None),
        Conversation.phone != ""
    ).order_by(Conversation.last_message_at.desc().nullslast()).all()

    # Group by phone and collect all bot_ids for each contact
    contacts_map = {}
    for conv in conversations:
        # Skip if this phone belongs to a bot
        normalized_phone = ''.join(c for c in conv.phone if c.isdigit())
        if normalized_phone in bot_phone_set or len(normalized_phone) < 5:
            continue

        if conv.phone not in contacts_map:
            contacts_map[conv.phone] = {
                "id": conv.phone,  # Use phone as ID for selection
                "phone": conv.phone,
                "display_name": conv.chat_name or conv.phone,
                "profile_pic": conv.profile_pic,
                "message_count": conv.message_count,
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                "bot_ids": [],
                "bot_names": []
            }
        contacts_map[conv.phone]["bot_ids"].append(conv.bot_profile_id)

    # Get bot names for display
    bot_names_map = {}
    if all_bot_ids:
        bots = db.query(BotProfile.id, BotProfile.name).filter(BotProfile.id.in_(all_bot_ids)).all()
        bot_names_map = {b.id: b.name for b in bots}

    for contact in contacts_map.values():
        contact["bot_names"] = [bot_names_map.get(bid, f"Bot {bid}") for bid in contact["bot_ids"]]
        contact["bot_name"] = ", ".join(contact["bot_names"][:2])  # Show first 2 bot names
        if len(contact["bot_names"]) > 2:
            contact["bot_name"] += f" +{len(contact['bot_names']) - 2}"

    return list(contacts_map.values())


@router.get("/{hub_id}/groups-paginated")
async def list_hub_groups_paginated(
    hub_id: int,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    all_user_bots: bool = Query(False, description="If true, fetch groups from all user's bots instead of just hub bots"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List WhatsApp groups for the hub with pagination (for scheduled content)."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Get bot IDs - either from hub membership or all user's bots
    if all_user_bots:
        # Get ALL bot IDs owned by this user (for scripted_conversations)
        bot_ids = db.query(BotProfile.id).filter(
            BotProfile.user_id == current_user.id
        ).all()
        bot_ids = [b[0] for b in bot_ids]
    else:
        # Get bot IDs that are members of this hub
        bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
            HubBotMembership.hub_id == hub_id,
            HubBotMembership.is_active == True
        ).all()
        bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    # Get bot names for all bots
    bot_profiles = db.query(BotProfile).filter(BotProfile.id.in_(bot_ids)).all()
    bot_names = {b.id: b.name for b in bot_profiles}

    # Parse current selected_groups (skip for all_user_bots mode)
    selected_groups = []
    selected_chat_ids = set()
    if not all_user_bots and hub.selected_groups:
        try:
            selected_groups = json.loads(hub.selected_groups) if isinstance(hub.selected_groups, str) else hub.selected_groups
            selected_chat_ids = {g.get('chat_id') for g in selected_groups if g.get('chat_id')}
        except (json.JSONDecodeError, TypeError):
            pass

    # Build query for groups from bot conversations
    query = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Conversation.is_group == True
    )

    # Only filter by selected groups if not in all_user_bots mode
    if not all_user_bots and selected_chat_ids:
        query = query.filter(Conversation.chat_id.in_(selected_chat_ids))

    groups = query.order_by(Conversation.last_message_at.desc().nullslast()).all()

    # Get conversation IDs for member count query
    conversation_ids = [g.id for g in groups]

    # Query distinct sender count per conversation (member count)
    member_counts = {}
    if conversation_ids:
        member_query = db.query(
            Message.conversation_id,
            func.count(distinct(Message.sender_id))
        ).filter(
            Message.conversation_id.in_(conversation_ids),
            Message.sender_id.isnot(None)
        ).group_by(Message.conversation_id).all()

        member_counts = {conv_id: count for conv_id, count in member_query}

    # Group by chat_id
    groups_map = {}
    for g in groups:
        if g.chat_id not in groups_map:
            groups_map[g.chat_id] = {
                "chat_id": g.chat_id,
                "name": g.chat_name or g.chat_id,
                "profile_pic": g.profile_pic,
                "message_count": g.message_count,
                "member_count": member_counts.get(g.id, 0),
                "last_message_at": g.last_message_at.isoformat() if g.last_message_at else None,
                "bot_ids": [],
                "bot_names": []
            }
        groups_map[g.chat_id]["bot_ids"].append(g.bot_profile_id)
        if g.bot_profile_id in bot_names:
            bot_name = bot_names[g.bot_profile_id]
            if bot_name not in groups_map[g.chat_id]["bot_names"]:
                groups_map[g.chat_id]["bot_names"].append(bot_name)

    # Build result list
    all_groups = []
    for group in groups_map.values():
        group["bot_name"] = ", ".join(group["bot_names"]) if group["bot_names"] else None
        all_groups.append(group)

    # Apply search filter
    if search:
        search_lower = search.lower()
        all_groups = [g for g in all_groups if search_lower in g["name"].lower() or search_lower in g["chat_id"].lower()]

    # Calculate pagination
    total = len(all_groups)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    # Apply pagination
    offset = (page - 1) * page_size
    paginated_groups = all_groups[offset:offset + page_size]

    return {"items": paginated_groups, "total": total, "page": page, "page_size": page_size, "total_pages": total_pages}


@router.delete("/{hub_id}/groups")
async def delete_hub_group(
    hub_id: int,
    chat_id: str = Query(..., description="The chat ID of the group to remove"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove a group from the hub's selected groups list."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Parse current selected_groups
    selected_groups = []
    if hub.selected_groups:
        try:
            selected_groups = json.loads(hub.selected_groups) if isinstance(hub.selected_groups, str) else hub.selected_groups
        except (json.JSONDecodeError, TypeError):
            selected_groups = []

    # Remove the group
    original_count = len(selected_groups)
    selected_groups = [g for g in selected_groups if g.get('chat_id') != chat_id]

    if len(selected_groups) == original_count:
        raise HTTPException(status_code=404, detail="Group not found in hub")

    # Save updated list
    hub.selected_groups = json.dumps(selected_groups)
    db.commit()

    return {"message": "Group removed successfully", "remaining_count": len(selected_groups)}


@router.post("/{hub_id}/sync-groups")
async def sync_hub_groups(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Sync groups from bot conversations to the hub's selected groups list."""
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # For group_management hubs, use all user's bots; otherwise use hub members
    if hub.task_type == 'group_management':
        # Get ALL bot IDs owned by this user
        bot_ids = db.query(BotProfile.id).filter(
            BotProfile.user_id == current_user.id
        ).all()
        bot_ids = [b[0] for b in bot_ids]
    else:
        # Get bot IDs that are members of this hub
        bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
            HubBotMembership.hub_id == hub_id,
            HubBotMembership.is_active == True
        ).all()
        bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return {"synced": 0, "total": 0, "message": "No bots available"}

    # Pre-sync: fetch groups from live platform adapters
    try:
        for bot_id in bot_ids:
            bot_prof = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
            if not bot_prof or not bot_prof.is_running:
                continue
            platform_type = bot_prof.platform_type or "whatsapp"
            if platform_type == "whatsapp":
                continue
            try:
                groups_from_platform = await _fetch_platform_groups(bot_id, platform_type)
                for grp in groups_from_platform:
                    existing = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_id,
                        Conversation.chat_id == grp["chat_id"]
                    ).first()
                    if not existing:
                        new_conv = Conversation(
                            bot_profile_id=bot_id,
                            chat_id=grp["chat_id"],
                            chat_name=grp["name"],
                            display_name=grp["name"],
                            is_group=True,
                        )
                        db.add(new_conv)
                db.commit()
            except Exception as e:
                logger.warning(f"Hub {hub_id}: Failed to pre-sync groups from bot {bot_id}: {e}")
    except Exception as e:
        logger.warning(f"Hub {hub_id}: Platform group pre-sync error: {e}")

    # Get all groups from bot conversations
    groups = db.query(Conversation).filter(
        Conversation.bot_profile_id.in_(bot_ids),
        Conversation.is_group == True
    ).order_by(Conversation.last_message_at.desc().nullslast()).all()

    # Build unique groups list by chat_id
    groups_map = {}
    for g in groups:
        if g.chat_id not in groups_map:
            groups_map[g.chat_id] = {
                "chat_id": g.chat_id,
                "name": g.chat_name or g.chat_id
            }

    # Update hub's selected_groups
    new_groups = list(groups_map.values())
    hub.selected_groups = json.dumps(new_groups)
    db.commit()

    return {"synced": len(new_groups), "total": len(new_groups), "message": f"Synced {len(new_groups)} groups"}


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
    emit_event_sync("topic.created", db=db, hub_id=hub_id, topic_id=topic.id)

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
    emit_event_sync("topic.updated", db=db, topic_id=topic_id)

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
    emit_event_sync("topic.deleted", db=db, topic_id=topic_id)

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


@router.post("/{hub_id}/topics/upload")
async def upload_topics(
    hub_id: int,
    data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload topics from file (CSV or JSON format)."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == current_user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    topics = data.get("topics", [])
    import_mode = data.get("import_mode", "add")  # add, replace, update

    if not topics:
        raise HTTPException(status_code=400, detail="No topics provided")

    added_count = 0
    updated_count = 0

    # If replace mode, delete all existing topics first
    if import_mode == "replace":
        db.query(HubMessageTopic).filter(HubMessageTopic.hub_id == hub_id).delete()
        db.flush()

    for topic_data in topics:
        name = topic_data.get("name", "").strip().lower()
        description = topic_data.get("description", "").strip()

        if not name or len(name) > 100:
            continue  # Skip invalid names

        existing = db.query(HubMessageTopic).filter(
            HubMessageTopic.hub_id == hub_id,
            HubMessageTopic.name == name
        ).first()

        if existing:
            if import_mode == "update":
                # Update description
                existing.description = description
                updated_count += 1
            # In "add" mode, skip duplicates
        else:
            # Add new topic
            topic = HubMessageTopic(
                hub_id=hub_id,
                name=name,
                description=description,
                is_system=False
            )
            db.add(topic)
            added_count += 1

    db.commit()

    # Return all topics for this hub
    all_topics = db.query(HubMessageTopic).filter(
        HubMessageTopic.hub_id == hub_id
    ).order_by(HubMessageTopic.is_system.desc(), HubMessageTopic.name).all()

    return {
        "status": "success",
        "added": added_count,
        "updated": updated_count,
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


# ============== AI Content Generation ==============

@router.post("/{hub_id}/generate-content")
async def generate_ai_content(
    hub_id: int,
    request: GenerateContentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate content using the hub's Content Generator agent."""
    # Get hub and verify ownership
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == current_user.id
    ).first()

    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Find Content Generator agent in the hub
    generator_agent = db.query(AIAgent).filter(
        AIAgent.hub_id == hub_id,
        AIAgent.agent_type == "generator",
        AIAgent.is_active == True
    ).first()

    if not generator_agent:
        raise HTTPException(
            status_code=400,
            detail="No Content Generator agent found. Please create a Content Generator agent in the Agents tab first."
        )

    # Check if agent has API key configured
    if not generator_agent.api_key_encrypted:
        raise HTTPException(
            status_code=400,
            detail="Content Generator agent has no API key configured. Please edit the agent and add your API key."
        )

    try:
        # Get AI provider from agent settings
        from app.ai.providers import get_ai_provider

        api_key = decrypt_string(generator_agent.api_key_encrypted)
        provider = get_ai_provider(
            provider_name=generator_agent.ai_provider or "openai",
            api_key=api_key,
            model=generator_agent.model or "gpt-4o-mini"
        )

        # Build the prompt - use agent's system prompt if available
        content_type_descriptions = {
            "message": "a general message",
            "followup": "a follow-up message to continue a conversation",
            "promo": "a promotional message for marketing purposes"
        }
        content_desc = content_type_descriptions.get(request.content_type, "a message")

        # Build system prompt - always use the structured format for options
        base_prompt = f"""You are a professional content writer creating WhatsApp messages.
Generate exactly 3 different variations of {content_desc} based on the user's instructions.

Guidelines:
- Each message should be concise and engaging (under 500 characters each)
- Use a friendly, conversational tone
- Use WhatsApp formatting: *bold* for emphasis, _italic_ for subtle emphasis
- Emojis are welcome but use sparingly
- Each message must be ready to send WITHOUT any modifications
- Do NOT include placeholder text like [link], [name], [Your Venue], etc. - write complete, ready-to-send messages
- Do NOT include audience segment labels or headers
- Provide variety in tone: one professional, one casual, one creative

CRITICAL: Return ONLY a valid JSON array with exactly 3 message strings. No other text, no explanations, no markdown.
Example format: ["First message here", "Second message here", "Third message here"]"""

        # Add agent's custom instructions if available
        if generator_agent.system_prompt:
            base_prompt += f"\n\nAdditional context: {generator_agent.system_prompt}"

        if request.topic:
            base_prompt += f"\n\nTopic/context: {request.topic}"

        system_prompt = base_prompt

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request.prompt}
        ]

        # Generate content - run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            ai_executor,
            lambda: provider.chat_completion(
                messages=messages,
                temperature=0.8,
                max_tokens=1500
            )
        )

        # Parse the response - try to extract JSON array
        content = response.content.strip()
        options = []

        try:
            # Try to parse as JSON array
            import json
            # Find JSON array in response (in case there's extra text)
            start_idx = content.find('[')
            end_idx = content.rfind(']') + 1
            if start_idx != -1 and end_idx > start_idx:
                json_str = content[start_idx:end_idx]
                options = json.loads(json_str)
                if not isinstance(options, list):
                    options = [content]
            else:
                options = [content]
        except (json.JSONDecodeError, ValueError):
            # If parsing fails, return as single option
            options = [content]

        # Ensure we have at least one option
        if not options:
            options = [content]

        emit_event_sync("content.generated", db=db, hub_id=hub_id)

        return {
            "options": options,
            "tokens_used": response.usage.get("total_tokens", 0),
            "agent_name": generator_agent.name
        }

    except Exception as e:
        logger.error(f"Error generating AI content: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate content: {str(e)}"
        )
