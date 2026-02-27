"""
Scripted Conversations API Routes
"""

import json
import logging
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import (
    get_db, User, Hub, BotProfile, ConversationScript, ScriptMessage,
    ScriptExecution, HubBotMembership, Conversation
)
from app.auth.utils import get_current_user
from app.scripts.models import (
    ScriptCreate, ScriptUpdate, ScriptResponse, ScriptDetailResponse,
    ScriptMessageCreate, ScriptMessageUpdate, ScriptMessageResponse,
    ScriptMessageReorder, ScriptExecutionResponse, ExecuteScriptRequest,
    BulkMessageCreate, BulkMessageDelete, SelectedGroup
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["scripts"])


# ============== Helper Functions ==============

def verify_hub_access(hub_id: int, user: User, db: Session) -> Hub:
    """Verify user has access to the hub."""
    hub = db.query(Hub).filter(Hub.id == hub_id, Hub.user_id == user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")
    return hub


def verify_script_access(script_id: int, user: User, db: Session) -> ConversationScript:
    """Verify user has access to the script."""
    script = db.query(ConversationScript).filter(ConversationScript.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    # Verify hub access
    hub = db.query(Hub).filter(Hub.id == script.hub_id, Hub.user_id == user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Script not found")

    return script


def sync_bots_for_groups(hub_id: int, group_chat_ids: List[str], user: User, db: Session):
    """
    Auto-sync bots that are members of the specified groups to the hub.
    This ensures scripts have bots available for execution.
    """
    if not group_chat_ids:
        return

    # Find bots that have conversations with these groups
    bot_ids = db.query(Conversation.bot_profile_id).filter(
        Conversation.chat_id.in_(group_chat_ids),
        Conversation.is_group == True
    ).distinct().all()
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return

    # Verify these bots belong to the user
    valid_bot_ids = db.query(BotProfile.id).filter(
        BotProfile.id.in_(bot_ids),
        BotProfile.user_id == user.id
    ).all()
    valid_bot_ids = [b[0] for b in valid_bot_ids]

    # Add bots to hub membership if not already present
    for bot_id in valid_bot_ids:
        existing = db.query(HubBotMembership).filter(
            HubBotMembership.hub_id == hub_id,
            HubBotMembership.bot_profile_id == bot_id
        ).first()

        if not existing:
            membership = HubBotMembership(
                hub_id=hub_id,
                bot_profile_id=bot_id,
                is_active=True
            )
            db.add(membership)

    db.commit()
    logger.info(f"Auto-synced {len(valid_bot_ids)} bots for hub {hub_id}")


def build_script_response(script: ConversationScript, db: Session) -> ScriptResponse:
    """Build a script response object."""
    # Parse group_ids
    group_ids = None
    group_summary = None
    if script.group_ids:
        try:
            group_ids = json.loads(script.group_ids)
            if group_ids:
                if len(group_ids) == 1:
                    group_summary = group_ids[0].get("name", "1 group")
                else:
                    group_summary = f"{len(group_ids)} groups"
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse recurring_days
    recurring_days = None
    if script.recurring_days:
        try:
            recurring_days = json.loads(script.recurring_days)
        except (json.JSONDecodeError, TypeError):
            pass

    # Count messages
    message_count = db.query(func.count(ScriptMessage.id)).filter(
        ScriptMessage.script_id == script.id
    ).scalar() or 0

    return ScriptResponse(
        id=script.id,
        hub_id=script.hub_id,
        name=script.name,
        description=script.description,
        group_ids=[SelectedGroup(**g) for g in group_ids] if group_ids else None,
        group_summary=group_summary,
        schedule_type=script.schedule_type or "immediate",
        scheduled_for=script.scheduled_for,
        recurring_frequency=script.recurring_frequency,
        recurring_time=script.recurring_time,
        recurring_days=recurring_days,
        recurring_start_date=script.recurring_start_date,
        recurring_end_date=script.recurring_end_date,
        sending_speed_mode=script.sending_speed_mode or "auto",
        stagger_delay_min=script.stagger_delay_min,
        stagger_delay_max=script.stagger_delay_max,
        status=script.status or "draft",
        message_count=message_count,
        last_run_at=script.last_run_at,
        created_at=script.created_at,
        updated_at=script.updated_at
    )


def build_message_response(message: ScriptMessage, db: Session) -> ScriptMessageResponse:
    """Build a message response object."""
    # Get bot name
    bot_name = None
    bot = db.query(BotProfile).filter(BotProfile.id == message.bot_profile_id).first()
    if bot:
        bot_name = bot.name

    return ScriptMessageResponse(
        id=message.id,
        script_id=message.script_id,
        bot_profile_id=message.bot_profile_id,
        bot_name=bot_name,
        time_type=message.time_type or "relative",
        absolute_time=message.absolute_time,
        delay_seconds=message.delay_seconds or 0,
        content=message.content,
        sequence_order=message.sequence_order or 0,
        status=message.status or "pending",
        sent_at=message.sent_at,
        error_message=message.error_message,
        created_at=message.created_at
    )


# ============== Bot Sync ==============

@router.post("/hubs/{hub_id}/bots-for-groups")
async def get_bots_for_groups(
    hub_id: int,
    group_ids: List[str],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get bots that are members of the specified groups.
    Also syncs these bots to the hub for script execution.
    """
    verify_hub_access(hub_id, current_user, db)

    if not group_ids:
        return {"bots": []}

    # Find bots that have conversations with these groups
    bot_ids = db.query(Conversation.bot_profile_id).filter(
        Conversation.chat_id.in_(group_ids),
        Conversation.is_group == True
    ).distinct().all()
    bot_ids = [b[0] for b in bot_ids]

    if not bot_ids:
        return {"bots": []}

    # Get bot details (only user's bots)
    bots = db.query(BotProfile).filter(
        BotProfile.id.in_(bot_ids),
        BotProfile.user_id == current_user.id
    ).all()

    # Sync bots to hub for script execution
    sync_bots_for_groups(hub_id, group_ids, current_user, db)

    return {
        "bots": [{"id": b.id, "name": b.name} for b in bots]
    }


# ============== Script CRUD ==============

@router.get("/hubs/{hub_id}/scripts", response_model=List[ScriptResponse])
async def list_scripts(
    hub_id: int,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all scripts for a hub."""
    verify_hub_access(hub_id, current_user, db)

    query = db.query(ConversationScript).filter(ConversationScript.hub_id == hub_id)

    if status:
        query = query.filter(ConversationScript.status == status)

    scripts = query.order_by(ConversationScript.created_at.desc()).all()

    return [build_script_response(s, db) for s in scripts]


@router.post("/hubs/{hub_id}/scripts", response_model=ScriptDetailResponse)
async def create_script(
    hub_id: int,
    data: ScriptCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new conversation script."""
    verify_hub_access(hub_id, current_user, db)

    # Serialize group_ids
    group_ids_json = None
    if data.group_ids:
        group_ids_json = json.dumps([g.dict() for g in data.group_ids])

    # Serialize recurring_days
    recurring_days_json = None
    if data.recurring_days:
        recurring_days_json = json.dumps(data.recurring_days)

    # Determine status and scheduled_for based on schedule_type
    status = "draft"
    scheduled_for = data.scheduled_for

    if data.schedule_type == "immediate":
        # Immediate scripts should be scheduled to run now
        status = "scheduled"
        scheduled_for = datetime.utcnow()
    elif data.schedule_type == "exact" and data.scheduled_for:
        # Exact time scripts are scheduled
        status = "scheduled"
    elif data.schedule_type == "recurring":
        # Recurring scripts - calculate first run time
        status = "scheduled"
        if data.recurring_time:
            try:
                hour, minute = map(int, data.recurring_time.split(":"))
                now = datetime.utcnow()
                # Set to today's recurring time
                scheduled_for = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                # If time already passed today, schedule for tomorrow
                if scheduled_for <= now:
                    from datetime import timedelta
                    scheduled_for = scheduled_for + timedelta(days=1)
                # If start date is in the future, use that instead
                if data.recurring_start_date and data.recurring_start_date > scheduled_for:
                    scheduled_for = data.recurring_start_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except (ValueError, AttributeError):
                scheduled_for = datetime.utcnow()

    script = ConversationScript(
        hub_id=hub_id,
        name=data.name,
        description=data.description,
        group_ids=group_ids_json,
        schedule_type=data.schedule_type,
        scheduled_for=scheduled_for,
        recurring_frequency=data.recurring_frequency,
        recurring_time=data.recurring_time,
        recurring_days=recurring_days_json,
        recurring_start_date=data.recurring_start_date,
        recurring_end_date=data.recurring_end_date,
        sending_speed_mode=data.sending_speed_mode,
        stagger_delay_min=data.stagger_delay_min,
        stagger_delay_max=data.stagger_delay_max,
        status=status
    )

    db.add(script)
    db.commit()
    db.refresh(script)

    # Auto-sync bots for the target groups
    if data.group_ids:
        sync_bots_for_groups(hub_id, [g.id for g in data.group_ids], current_user, db)

    # Add messages if provided
    messages = []
    if data.messages:
        for i, msg_data in enumerate(data.messages):
            message = ScriptMessage(
                script_id=script.id,
                bot_profile_id=msg_data.bot_profile_id,
                time_type=msg_data.time_type,
                absolute_time=msg_data.absolute_time,
                delay_seconds=msg_data.delay_seconds or 0,
                content=msg_data.content,
                sequence_order=msg_data.sequence_order if msg_data.sequence_order is not None else i
            )
            db.add(message)
            messages.append(message)

        db.commit()
        for msg in messages:
            db.refresh(msg)

    # Build response
    response = build_script_response(script, db)
    return ScriptDetailResponse(
        **response.dict(),
        messages=[build_message_response(m, db) for m in messages]
    )


@router.get("/hubs/{hub_id}/scripts/{script_id}", response_model=ScriptDetailResponse)
async def get_script(
    hub_id: int,
    script_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a script with its messages."""
    verify_hub_access(hub_id, current_user, db)
    script = verify_script_access(script_id, current_user, db)

    if script.hub_id != hub_id:
        raise HTTPException(status_code=404, detail="Script not found")

    # Get messages
    messages = db.query(ScriptMessage).filter(
        ScriptMessage.script_id == script_id
    ).order_by(ScriptMessage.sequence_order).all()

    response = build_script_response(script, db)
    return ScriptDetailResponse(
        **response.dict(),
        messages=[build_message_response(m, db) for m in messages]
    )


@router.put("/hubs/{hub_id}/scripts/{script_id}", response_model=ScriptResponse)
async def update_script(
    hub_id: int,
    script_id: int,
    data: ScriptUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a script."""
    verify_hub_access(hub_id, current_user, db)
    script = verify_script_access(script_id, current_user, db)

    if script.hub_id != hub_id:
        raise HTTPException(status_code=404, detail="Script not found")

    # Update fields
    if data.name is not None:
        script.name = data.name
    if data.description is not None:
        script.description = data.description
    if data.group_ids is not None:
        script.group_ids = json.dumps([g.dict() for g in data.group_ids])
        # Auto-sync bots for the new target groups
        sync_bots_for_groups(hub_id, [g.id for g in data.group_ids], current_user, db)
    if data.schedule_type is not None:
        script.schedule_type = data.schedule_type
    if data.scheduled_for is not None:
        script.scheduled_for = data.scheduled_for
    if data.recurring_frequency is not None:
        script.recurring_frequency = data.recurring_frequency
    if data.recurring_time is not None:
        script.recurring_time = data.recurring_time
    if data.recurring_days is not None:
        script.recurring_days = json.dumps(data.recurring_days)
    if data.recurring_start_date is not None:
        script.recurring_start_date = data.recurring_start_date
    if data.recurring_end_date is not None:
        script.recurring_end_date = data.recurring_end_date
    if data.sending_speed_mode is not None:
        script.sending_speed_mode = data.sending_speed_mode
    if data.stagger_delay_min is not None:
        script.stagger_delay_min = data.stagger_delay_min
    if data.stagger_delay_max is not None:
        script.stagger_delay_max = data.stagger_delay_max
    if data.status is not None:
        script.status = data.status

    script.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(script)

    return build_script_response(script, db)


@router.delete("/hubs/{hub_id}/scripts/{script_id}")
async def delete_script(
    hub_id: int,
    script_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a script and all its messages."""
    verify_hub_access(hub_id, current_user, db)
    script = verify_script_access(script_id, current_user, db)

    if script.hub_id != hub_id:
        raise HTTPException(status_code=404, detail="Script not found")

    db.delete(script)
    db.commit()

    return {"message": "Script deleted"}


# ============== Script Message CRUD ==============

@router.post("/scripts/{script_id}/messages", response_model=ScriptMessageResponse)
async def create_message(
    script_id: int,
    data: ScriptMessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a message to a script."""
    script = verify_script_access(script_id, current_user, db)

    # Verify bot belongs to the hub
    membership = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == script.hub_id,
        HubBotMembership.bot_profile_id == data.bot_profile_id,
        HubBotMembership.is_active == True
    ).first()

    if not membership:
        raise HTTPException(status_code=400, detail="Bot is not a member of this hub")

    # Get next sequence order if not provided
    sequence_order = data.sequence_order
    if sequence_order is None:
        max_order = db.query(func.max(ScriptMessage.sequence_order)).filter(
            ScriptMessage.script_id == script_id
        ).scalar() or -1
        sequence_order = max_order + 1

    message = ScriptMessage(
        script_id=script_id,
        bot_profile_id=data.bot_profile_id,
        time_type=data.time_type,
        absolute_time=data.absolute_time,
        delay_seconds=data.delay_seconds or 0,
        content=data.content,
        sequence_order=sequence_order
    )

    db.add(message)
    db.commit()
    db.refresh(message)

    return build_message_response(message, db)


@router.put("/scripts/messages/{message_id}", response_model=ScriptMessageResponse)
async def update_message(
    message_id: int,
    data: ScriptMessageUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a script message."""
    message = db.query(ScriptMessage).filter(ScriptMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    # Verify access
    script = verify_script_access(message.script_id, current_user, db)

    # Update fields
    if data.bot_profile_id is not None:
        # Verify bot belongs to the hub
        membership = db.query(HubBotMembership).filter(
            HubBotMembership.hub_id == script.hub_id,
            HubBotMembership.bot_profile_id == data.bot_profile_id,
            HubBotMembership.is_active == True
        ).first()
        if not membership:
            raise HTTPException(status_code=400, detail="Bot is not a member of this hub")
        message.bot_profile_id = data.bot_profile_id

    if data.time_type is not None:
        message.time_type = data.time_type
    if data.absolute_time is not None:
        message.absolute_time = data.absolute_time
    if data.delay_seconds is not None:
        message.delay_seconds = data.delay_seconds
    if data.content is not None:
        message.content = data.content
    if data.sequence_order is not None:
        message.sequence_order = data.sequence_order

    db.commit()
    db.refresh(message)

    return build_message_response(message, db)


@router.delete("/scripts/messages/{message_id}")
async def delete_message(
    message_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a script message."""
    message = db.query(ScriptMessage).filter(ScriptMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    # Verify access
    verify_script_access(message.script_id, current_user, db)

    db.delete(message)
    db.commit()

    return {"message": "Message deleted"}


@router.post("/scripts/{script_id}/messages/reorder")
async def reorder_messages(
    script_id: int,
    data: ScriptMessageReorder,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Reorder messages in a script."""
    verify_script_access(script_id, current_user, db)

    for i, message_id in enumerate(data.message_ids):
        message = db.query(ScriptMessage).filter(
            ScriptMessage.id == message_id,
            ScriptMessage.script_id == script_id
        ).first()
        if message:
            message.sequence_order = i

    db.commit()

    return {"message": "Messages reordered"}


@router.post("/scripts/{script_id}/messages/bulk", response_model=List[ScriptMessageResponse])
async def create_bulk_messages(
    script_id: int,
    data: BulkMessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add multiple messages to a script at once."""
    script = verify_script_access(script_id, current_user, db)

    # Get hub bot IDs for validation
    hub_bot_ids = set()
    memberships = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == script.hub_id,
        HubBotMembership.is_active == True
    ).all()
    hub_bot_ids = {m.bot_profile_id for m in memberships}

    # Get current max sequence order
    max_order = db.query(func.max(ScriptMessage.sequence_order)).filter(
        ScriptMessage.script_id == script_id
    ).scalar() or -1

    messages = []
    for i, msg_data in enumerate(data.messages):
        if msg_data.bot_profile_id not in hub_bot_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Bot {msg_data.bot_profile_id} is not a member of this hub"
            )

        sequence_order = msg_data.sequence_order if msg_data.sequence_order is not None else max_order + i + 1

        message = ScriptMessage(
            script_id=script_id,
            bot_profile_id=msg_data.bot_profile_id,
            time_type=msg_data.time_type,
            absolute_time=msg_data.absolute_time,
            delay_seconds=msg_data.delay_seconds or 0,
            content=msg_data.content,
            sequence_order=sequence_order
        )
        db.add(message)
        messages.append(message)

    db.commit()
    for msg in messages:
        db.refresh(msg)

    return [build_message_response(m, db) for m in messages]


# ============== Script Execution ==============

@router.post("/scripts/{script_id}/execute", response_model=ScriptExecutionResponse)
async def execute_script(
    script_id: int,
    data: Optional[ExecuteScriptRequest] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start or schedule a script execution."""
    script = verify_script_access(script_id, current_user, db)

    # Check if script has messages
    message_count = db.query(func.count(ScriptMessage.id)).filter(
        ScriptMessage.script_id == script_id
    ).scalar() or 0

    if message_count == 0:
        raise HTTPException(status_code=400, detail="Script has no messages")

    # Check if script has target groups
    if not script.group_ids:
        raise HTTPException(status_code=400, detail="Script has no target groups")

    # Update script status
    script.status = "scheduled"
    if data and data.scheduled_for:
        script.scheduled_for = data.scheduled_for
    elif data and data.schedule_type:
        script.schedule_type = data.schedule_type

    # Create execution record
    execution = ScriptExecution(
        script_id=script_id,
        status="pending" if script.scheduled_for else "running"
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    # TODO: Trigger scheduler to execute script
    # For now, just return the execution record

    return ScriptExecutionResponse(
        id=execution.id,
        script_id=execution.script_id,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        status=execution.status,
        messages_sent=execution.messages_sent,
        messages_failed=execution.messages_failed,
        error_message=execution.error_message
    )


@router.post("/scripts/{script_id}/cancel")
async def cancel_script(
    script_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a running or scheduled script."""
    script = verify_script_access(script_id, current_user, db)

    if script.status not in ("scheduled", "running"):
        raise HTTPException(status_code=400, detail="Script is not running or scheduled")

    previous_status = script.status
    script.status = "cancelled"

    # Cancel any running executions
    db.query(ScriptExecution).filter(
        ScriptExecution.script_id == script_id,
        ScriptExecution.status.in_(["pending", "running"])
    ).update({"status": "cancelled"})

    db.commit()

    # If it was running, try to stop the active task
    if previous_status == "running":
        from app.scripts.scheduler import script_scheduler
        script_scheduler.cancel_script(script_id)

    return {"message": "Script cancelled", "previous_status": previous_status}


@router.get("/scripts/{script_id}/executions", response_model=List[ScriptExecutionResponse])
async def list_executions(
    script_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get execution history for a script."""
    verify_script_access(script_id, current_user, db)

    executions = db.query(ScriptExecution).filter(
        ScriptExecution.script_id == script_id
    ).order_by(ScriptExecution.started_at.desc()).limit(50).all()

    return [
        ScriptExecutionResponse(
            id=e.id,
            script_id=e.script_id,
            started_at=e.started_at,
            completed_at=e.completed_at,
            status=e.status,
            messages_sent=e.messages_sent,
            messages_failed=e.messages_failed,
            error_message=e.error_message
        )
        for e in executions
    ]


# ============== Hub Bots for Script ==============

@router.get("/hubs/{hub_id}/scripts/bots")
async def get_hub_bots_for_script(
    hub_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get available bots for a hub's scripts."""
    verify_hub_access(hub_id, current_user, db)

    memberships = db.query(HubBotMembership).filter(
        HubBotMembership.hub_id == hub_id,
        HubBotMembership.is_active == True
    ).all()

    bots = []
    for m in memberships:
        bot = db.query(BotProfile).filter(BotProfile.id == m.bot_profile_id).first()
        if bot:
            bots.append({
                "id": bot.id,
                "name": bot.name,
                "is_running": bot.is_running,
                "whatsapp_connected": bot.whatsapp_connected
            })

    return bots
