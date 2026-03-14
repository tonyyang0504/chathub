"""
Tools Routes - Aggregates all tool page routes and APIs
"""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

# Thread pool for running blocking AI operations
ai_executor = ThreadPoolExecutor(max_workers=4)

from app.database import get_db, SessionLocal, ToolExecution, Hub, ScheduledContent, HubBotMembership, Contact, ContactTag, AIAgent, ConversationScript, ScriptExecution, BotProfile, Conversation, Message
from app.auth.utils import get_current_user_optional
from app.auth.ownership import get_user_hub_ids
from sqlalchemy import func, case
from .monitoring import ToolMonitor
from app.ai.cost_tracker import usage_context

router = APIRouter(prefix="/tools", tags=["tools"])


# Detect if running as frozen executable (PyInstaller)
if getattr(sys, 'frozen', False):
    # Running as compiled executable - templates are in _MEIPASS/app/templates
    _BASE_DIR = Path(sys._MEIPASS)
else:
    # Running as script - templates are relative to project root
    _BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "app" / "templates"))


# ============================================================================
# Tool Monitoring API Endpoints
# ============================================================================

@router.get("/api/executions")
async def get_tool_executions(
    request: Request,
    tool_type: Optional[str] = None,
    hub_id: Optional[int] = None,
    # New filters
    q: Optional[str] = None,  # free-text search (message/sender/group + raw JSON)
    status: Optional[str] = None,  # success|error
    operation: Optional[str] = None,
    triggered_by: Optional[str] = None,
    start: Optional[str] = None,  # ISO date/datetime
    end: Optional[str] = None,    # ISO date/datetime
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get tool execution history with optional filtering."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs for filtering
    user_hub_ids = get_user_hub_ids(user, db)

    # If hub_id is specified, verify ownership
    if hub_id:
        if hub_id not in user_hub_ids:
            raise HTTPException(status_code=404, detail="Hub not found")
        executions = ToolMonitor.get_recent_executions(
            db=db,
            tool_type=tool_type,
            hub_id=hub_id,
            limit=limit,
            offset=offset
        )
    else:
        # Filter to only user's hubs
        query = db.query(ToolExecution)

        if tool_type:
            query = query.filter(ToolExecution.tool_type == tool_type)

        # Only show executions from user's hubs (or user's own executions)
        if user_hub_ids:
            query = query.filter(
                (ToolExecution.hub_id.in_(user_hub_ids)) |
                (ToolExecution.user_id == user.id)
            )
        else:
            query = query.filter(ToolExecution.user_id == user.id)

        # Apply filters
        if status:
            query = query.filter(ToolExecution.status == status)
        if operation:
            query = query.filter(ToolExecution.operation == operation)
        if triggered_by:
            query = query.filter(ToolExecution.triggered_by == triggered_by)

        # Date filters (created_at)
        from datetime import datetime
        def _parse_dt(val: str) -> Optional[datetime]:
            if not val:
                return None
            v = val.strip()
            # accept YYYY-MM-DD or full ISO; also accept trailing 'Z'
            if v.endswith('Z'):
                v = v[:-1] + '+00:00'
            try:
                return datetime.fromisoformat(v)
            except Exception:
                # try date-only
                try:
                    return datetime.fromisoformat(v + 'T00:00:00')
                except Exception:
                    return None

        start_dt = _parse_dt(start) if start else None
        end_dt = _parse_dt(end) if end else None
        if start_dt:
            query = query.filter(ToolExecution.created_at >= start_dt)
        if end_dt:
            query = query.filter(ToolExecution.created_at <= end_dt)

        # Free-text search across common JSON fields + raw JSON blobs
        if q:
            from sqlalchemy import or_
            like = f"%{q}%"
            query = query.filter(
                or_(
                    ToolExecution.input_data.like(like),
                    ToolExecution.output_data.like(like),
                    ToolExecution.error_message.like(like),
                )
            )

        executions = query.order_by(ToolExecution.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "executions": [
            {
                "id": e.id,
                "hub_id": e.hub_id,
                "tool_type": e.tool_type,
                "operation": e.operation,
                "status": e.status,
                "error_message": e.error_message,
                "execution_time_ms": e.execution_time_ms,
                "tokens_used": e.tokens_used,
                "triggered_by": e.triggered_by,
                "input_data": e.input_data,
                "output_data": e.output_data,
                "created_at": e.created_at.isoformat() if e.created_at else None
            }
            for e in executions
        ]
    }


@router.get("/api/executions/{execution_id}")
async def get_execution_detail(
    request: Request,
    execution_id: int,
    db: Session = Depends(get_db)
):
    """Get detailed execution info including input/output data."""
    import json

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs for ownership check
    user_hub_ids = get_user_hub_ids(user, db)

    execution = db.query(ToolExecution).filter(ToolExecution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    # Verify user can access this execution (owns the hub or is the user)
    if execution.hub_id and execution.hub_id not in user_hub_ids:
        if execution.user_id != user.id:
            raise HTTPException(status_code=404, detail="Execution not found")

    return {
        "id": execution.id,
        "hub_id": execution.hub_id,
        "tool_type": execution.tool_type,
        "operation": execution.operation,
        "input_data": json.loads(execution.input_data) if execution.input_data else None,
        "output_data": json.loads(execution.output_data) if execution.output_data else None,
        "status": execution.status,
        "error_message": execution.error_message,
        "execution_time_ms": execution.execution_time_ms,
        "tokens_used": execution.tokens_used,
        "triggered_by": execution.triggered_by,
        "related_entity_type": execution.related_entity_type,
        "related_entity_id": execution.related_entity_id,
        "user_id": execution.user_id,
        "created_at": execution.created_at.isoformat() if execution.created_at else None
    }


@router.get("/api/stats")
async def get_tool_stats(
    request: Request,
    tool_type: Optional[str] = None,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Get tool execution statistics."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs for ownership check
    user_hub_ids = get_user_hub_ids(user, db)

    # If hub_id is specified, verify ownership
    if hub_id:
        if hub_id not in user_hub_ids:
            raise HTTPException(status_code=404, detail="Hub not found")
        stats = ToolMonitor.get_execution_stats(
            db=db,
            tool_type=tool_type,
            hub_id=hub_id
        )
    else:
        # Get aggregated stats for all user's hubs
        # We need to call this for each hub and aggregate, or pass user filter
        # For now, aggregate across all user's hubs
        from sqlalchemy import func, case

        query = db.query(
            func.count(ToolExecution.id).label('total'),
            func.sum(case((ToolExecution.status == 'success', 1), else_=0)).label('success_count'),
            func.sum(case((ToolExecution.status == 'error', 1), else_=0)).label('error_count'),
            func.avg(ToolExecution.execution_time_ms).label('avg_time_ms'),
            func.sum(ToolExecution.tokens_used).label('total_tokens')
        )

        if tool_type:
            query = query.filter(ToolExecution.tool_type == tool_type)

        # Filter to user's hubs
        if user_hub_ids:
            query = query.filter(
                (ToolExecution.hub_id.in_(user_hub_ids)) |
                (ToolExecution.user_id == user.id)
            )
        else:
            query = query.filter(ToolExecution.user_id == user.id)

        result = query.first()

        stats = {
            'total': result.total or 0,
            'success_count': result.success_count or 0,
            'error_count': result.error_count or 0,
            'avg_time_ms': round(result.avg_time_ms or 0, 2),
            'total_tokens': result.total_tokens or 0,
            'success_rate': round((result.success_count or 0) / max(result.total or 1, 1) * 100, 2)
        }

    return stats


@router.get("/api/types")
async def get_tool_types(request: Request, db: Session = Depends(get_db)):
    """Get list of available tool types."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return {
        "tool_types": ToolMonitor.TOOL_TYPES
    }


@router.get("/api/scheduled-content/stats")
async def get_scheduled_content_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for scheduled content across all hub instances."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Count hub instances of type 'scheduled_content'
    total_instances = db.query(func.count(Hub.id)).filter(
        Hub.user_id == user.id,
        Hub.task_type == "scheduled_content"
    ).scalar() or 0

    # Get hub IDs for this user
    hub_ids = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count scheduled content by status
        total_scheduled = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids)
        ).scalar() or 0

        total_sent = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids),
            ScheduledContent.status == "sent"
        ).scalar() or 0

        total_pending = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids),
            ScheduledContent.status == "pending"
        ).scalar() or 0

        total_failed = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids),
            ScheduledContent.status == "failed"
        ).scalar() or 0
    else:
        total_scheduled = 0
        total_sent = 0
        total_pending = 0
        total_failed = 0

    return {
        "total_instances": total_instances,
        "total_scheduled": total_scheduled,
        "total_sent": total_sent,
        "total_pending": total_pending,
        "total_failed": total_failed
    }


@router.get("/api/group-management/stats")
async def get_group_management_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for group management across all hub instances."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Count hub instances of type 'group_management'
    total_instances = db.query(func.count(Hub.id)).filter(
        Hub.user_id == user.id,
        Hub.task_type == "group_management"
    ).scalar() or 0

    # Get hub IDs for this user
    hub_ids = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count active bots
        active_bots = db.query(func.count(HubBotMembership.id)).filter(
            HubBotMembership.hub_id.in_(hub_ids),
            HubBotMembership.is_active == True
        ).scalar() or 0

        # Count total bots
        total_bots = db.query(func.count(HubBotMembership.id)).filter(
            HubBotMembership.hub_id.in_(hub_ids)
        ).scalar() or 0

        # Count groups managed (bots with group assignments)
        groups_managed = db.query(func.count(func.distinct(HubBotMembership.hub_id))).filter(
            HubBotMembership.hub_id.in_(hub_ids),
            HubBotMembership.is_active == True
        ).scalar() or 0

        # Get execution stats
        executions = db.query(func.count(ToolExecution.id)).filter(
            ToolExecution.hub_id.in_(hub_ids),
            ToolExecution.tool_type == "group_management"
        ).scalar() or 0
    else:
        active_bots = 0
        total_bots = 0
        groups_managed = 0
        executions = 0

    return {
        "total_instances": total_instances,
        "active_bots": active_bots,
        "total_bots": total_bots,
        "groups_managed": groups_managed,
        "total_executions": executions
    }


@router.get("/api/contact-analyzer/stats")
async def get_contact_analyzer_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for contact analyzer across all hub instances."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Count hub instances of type 'contact_analyzer'
    total_instances = db.query(func.count(Hub.id)).filter(
        Hub.user_id == user.id,
        Hub.task_type == "contact_analyzer"
    ).scalar() or 0

    # Get hub IDs for this user
    hub_ids = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count total contacts
        total_contacts = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids)
        ).scalar() or 0

        # Count total tags across all contacts
        contact_ids = db.query(Contact.id).filter(
            Contact.hub_id.in_(hub_ids)
        ).all()
        contact_ids = [c[0] for c in contact_ids]

        total_tags = 0
        if contact_ids:
            total_tags = db.query(func.count(ContactTag.id)).filter(
                ContactTag.contact_id.in_(contact_ids)
            ).scalar() or 0

        # Count contacts needing follow-up (low engagement or no recent activity)
        from datetime import datetime, timedelta
        stale_date = datetime.utcnow() - timedelta(days=7)
        total_followups = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            (
                (Contact.engagement_score < 0.3) |
                (Contact.last_interaction_at < stale_date) |
                (Contact.last_interaction_at.is_(None))
            )
        ).scalar() or 0

    else:
        total_contacts = 0
        total_tags = 0
        total_followups = 0

    return {
        "total_instances": total_instances,
        "total_contacts": total_contacts,
        "total_tags": total_tags,
        "total_followups": total_followups
    }


@router.get("/api/message-routing/stats")
async def get_message_routing_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for message routing across all hub instances."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Count hub instances of type 'message_routing'
    total_instances = db.query(func.count(Hub.id)).filter(
        Hub.user_id == user.id,
        Hub.task_type == "message_routing"
    ).scalar() or 0

    # Get hub IDs for this user
    hub_ids = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count routing agents
        routing_agents = db.query(func.count(AIAgent.id)).filter(
            AIAgent.hub_id.in_(hub_ids),
            AIAgent.agent_type.in_(["classifier", "router"])
        ).scalar() or 0

        # Get execution stats
        total_routed = db.query(func.count(ToolExecution.id)).filter(
            ToolExecution.hub_id.in_(hub_ids),
            ToolExecution.tool_type == "message_routing"
        ).scalar() or 0

        # Success rate
        successful = db.query(func.count(ToolExecution.id)).filter(
            ToolExecution.hub_id.in_(hub_ids),
            ToolExecution.tool_type == "message_routing",
            ToolExecution.status == "success"
        ).scalar() or 0

        success_rate = round((successful / total_routed * 100), 1) if total_routed > 0 else 0
    else:
        routing_agents = 0
        total_routed = 0
        success_rate = 0

    return {
        "total_instances": total_instances,
        "routing_agents": routing_agents,
        "total_routed": total_routed,
        "success_rate": success_rate
    }


@router.get("/api/scripted-conversations/stats")
async def get_scripted_conversations_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for scripted conversations across all hub instances."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Count hub instances of type 'scripted_conversations'
    total_instances = db.query(func.count(Hub.id)).filter(
        Hub.user_id == user.id,
        Hub.task_type == "scripted_conversations"
    ).scalar() or 0

    # Get hub IDs for this user
    hub_ids = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count total scripts
        total_scripts = db.query(func.count(ConversationScript.id)).filter(
            ConversationScript.hub_id.in_(hub_ids)
        ).scalar() or 0

        # Count completed scripts
        total_completed = db.query(func.count(ConversationScript.id)).filter(
            ConversationScript.hub_id.in_(hub_ids),
            ConversationScript.status == "completed"
        ).scalar() or 0

        # Count scheduled scripts
        total_scheduled = db.query(func.count(ConversationScript.id)).filter(
            ConversationScript.hub_id.in_(hub_ids),
            ConversationScript.status == "scheduled"
        ).scalar() or 0

        # Count failed scripts
        total_failed = db.query(func.count(ConversationScript.id)).filter(
            ConversationScript.hub_id.in_(hub_ids),
            ConversationScript.status == "failed"
        ).scalar() or 0
    else:
        total_scripts = 0
        total_completed = 0
        total_scheduled = 0
        total_failed = 0

    return {
        "total_instances": total_instances,
        "total_scripts": total_scripts,
        "total_completed": total_completed,
        "total_scheduled": total_scheduled,
        "total_failed": total_failed
    }


@router.get("/api/scripted-conversations/executions")
async def get_scripted_conversations_executions(
    request: Request,
    hub_id: Optional[int] = None,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """Get script execution history."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    query = db.query(ScriptExecution).join(ConversationScript)

    if hub_id:
        if hub_id not in user_hub_ids:
            raise HTTPException(status_code=404, detail="Hub not found")
        query = query.filter(ConversationScript.hub_id == hub_id)
    else:
        if user_hub_ids:
            query = query.filter(ConversationScript.hub_id.in_(user_hub_ids))

    executions = query.order_by(ScriptExecution.started_at.desc()).limit(limit).all()

    result = []
    for e in executions:
        # Calculate execution time if both timestamps exist
        execution_time_ms = None
        if e.started_at and e.completed_at:
            delta = e.completed_at - e.started_at
            execution_time_ms = int(delta.total_seconds() * 1000)

        # Get hub name from the script's hub
        hub_name = None
        if e.script and e.script.hub:
            hub_name = e.script.hub.name

        result.append({
            "id": e.id,
            "script_id": e.script_id,
            "script_name": e.script.name if e.script else None,
            "hub_name": hub_name,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            "status": e.status,
            "messages_sent": e.messages_sent,
            "messages_failed": e.messages_failed,
            "error_message": e.error_message,
            "execution_time_ms": execution_time_ms
        })

    return {"executions": result}


@router.get("/api/content-generator/stats")
async def get_content_generator_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for content generator across all hub instances."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Count hub instances of type 'content_generator'
    total_instances = db.query(func.count(Hub.id)).filter(
        Hub.user_id == user.id,
        Hub.task_type == "content_generator"
    ).scalar() or 0

    # Get hub IDs for this user
    hub_ids = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count content generation executions
        total_generated = db.query(func.count(ToolExecution.id)).filter(
            ToolExecution.hub_id.in_(hub_ids),
            ToolExecution.tool_type == "content_generator"
        ).scalar() or 0

        # Count tokens used
        tokens_used = db.query(func.sum(ToolExecution.tokens_used)).filter(
            ToolExecution.hub_id.in_(hub_ids),
            ToolExecution.tool_type == "content_generator"
        ).scalar() or 0

        # Count generator agents
        generator_agents = db.query(func.count(AIAgent.id)).filter(
            AIAgent.hub_id.in_(hub_ids),
            AIAgent.agent_type == "content_generator"
        ).scalar() or 0

        # Get templates count (scheduled content marked as template)
        templates_count = db.query(func.count(ScheduledContent.id)).filter(
            ScheduledContent.hub_id.in_(hub_ids),
            ScheduledContent.content_type == "template"
        ).scalar() or 0
    else:
        total_generated = 0
        tokens_used = 0
        generator_agents = 0
        templates_count = 0

    return {
        "total_instances": total_instances,
        "total_generated": total_generated,
        "tokens_used": tokens_used,
        "generator_agents": generator_agents,
        "templates_count": templates_count
    }


# ============================================================================
# Tool Simulation API Endpoints
# ============================================================================

def get_user_api_key(user, db):
    """Get API key from user's hubs."""
    from app.auth.utils import decrypt_string

    hub = db.query(Hub).filter(
        Hub.user_id == user.id,
        Hub.api_key_encrypted.isnot(None)
    ).first()

    if hub and hub.api_key_encrypted:
        return decrypt_string(hub.api_key_encrypted)
    return None


@router.post("/api/simulate/content-generator")
async def simulate_content_generator(
    request: Request,
    db: Session = Depends(get_db)
):
    """Generate content using AI for simulation."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    content_type = data.get("content_type", "message")
    topic = data.get("topic", "")
    tone = data.get("tone", "professional")

    # Get custom AI configuration from request
    custom_api_key = data.get("api_key", "")
    ai_provider = data.get("ai_provider", "openai")
    ai_model = data.get("ai_model", "gpt-4o-mini")

    if not topic:
        raise HTTPException(status_code=400, detail="Topic is required")

    # Use custom API key if provided (required for simulation)
    api_key = custom_api_key.strip() if custom_api_key else None
    if not api_key:
        raise HTTPException(status_code=400, detail="Please enter an API key to test the content generator.")

    # Basic API key validation
    if len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format. Please check your API key.")

    try:
        from app.ai.providers import get_ai_provider
        provider = get_ai_provider(ai_provider, api_key, ai_model)
        usage_context.set(user_id=user.id, source='tool', operation='content_generation')

        # Reasoning models (GPT-5, o1, o3) need more tokens as they use tokens for internal reasoning
        is_reasoning_model = ai_model.startswith(('gpt-5', 'o1', 'o3'))
        max_tokens = 4000 if is_reasoning_model else 300

        system_prompt = f"""You are a professional content writer for WhatsApp messages.
Generate a {content_type} message with a {tone} tone about the given topic.
Keep it concise (under 200 words), suitable for WhatsApp, and engaging.
Use appropriate emojis if the tone is friendly or casual.
Do not include subject lines or greetings like "Subject:" - just the message body."""

        # Run blocking AI call in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        ai_response = await loop.run_in_executor(
            ai_executor,
            lambda: provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate a {content_type} message about: {topic}"}
                ],
                max_tokens=max_tokens,
                temperature=0.7
            )
        )

        generated_content = ai_response.content.strip()
        tokens_used = ai_response.usage.get("total_tokens", 0) if ai_response.usage else 0

        return {
            "success": True,
            "content": generated_content,
            "tokens_used": tokens_used
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


@router.post("/api/simulate/contact-analyzer")
async def simulate_contact_analyzer(
    request: Request,
    db: Session = Depends(get_db)
):
    """Analyze a message/contact using AI for simulation."""
    import json

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    message = data.get("message", "")

    # Get custom AI configuration from request
    custom_api_key = data.get("api_key", "")
    ai_provider = data.get("ai_provider", "openai")
    ai_model = data.get("ai_model", "gpt-4o-mini")

    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    # Use custom API key if provided (required for simulation)
    api_key = custom_api_key.strip() if custom_api_key else None
    if not api_key:
        raise HTTPException(status_code=400, detail="Please enter an API key to test the contact analyzer.")

    # Basic API key validation
    if len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format. Please check your API key.")

    try:
        from app.ai.providers import get_ai_provider
        provider = get_ai_provider(ai_provider, api_key, ai_model)
        usage_context.set(user_id=user.id, source='tool', operation='contact_analysis')

        # Reasoning models (GPT-5, o1, o3) need more tokens as they use tokens for internal reasoning
        is_reasoning_model = ai_model.startswith(('gpt-5', 'o1', 'o3'))
        max_tokens = 4000 if is_reasoning_model else 200

        system_prompt = """You are a contact analyzer AI. Analyze the given message and return a JSON object with:
{
    "intent": "inquiry|purchase|support|feedback|general",
    "sentiment": "positive|neutral|negative",
    "urgency": "low|medium|high|critical",
    "engagement_score": 1-100,
    "tags": ["tag1", "tag2", "tag3"],
    "summary": "Brief 1-sentence summary of what the person wants"
}
Only return valid JSON, no other text."""

        # Run blocking AI call in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        ai_response = await loop.run_in_executor(
            ai_executor,
            lambda: provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Analyze this message: {message}"}
                ],
                max_tokens=max_tokens,
                temperature=0.3
            )
        )

        result_text = ai_response.content.strip()
        # Try to parse JSON from response
        try:
            analysis = json.loads(result_text)
        except:
            analysis = None
            # Handle markdown code blocks
            if "```" in result_text:
                parts = result_text.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        try:
                            analysis = json.loads(part)
                            break
                        except:
                            continue
            # Handle reasoning models that output text before JSON
            elif "{" in result_text and "}" in result_text:
                start = result_text.find("{")
                end = result_text.rfind("}") + 1
                if start < end:
                    try:
                        analysis = json.loads(result_text[start:end])
                    except:
                        pass

            # Fallback if all parsing failed
            if analysis is None:
                analysis = {
                    "intent": "general",
                    "sentiment": "neutral",
                    "urgency": "low",
                    "engagement_score": 50,
                    "tags": ["unclassified"],
                    "summary": result_text[:100]
                }

        return {
            "success": True,
            "analysis": analysis
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


@router.post("/api/simulate/message-routing")
async def simulate_message_routing(
    request: Request,
    db: Session = Depends(get_db)
):
    """Classify and route a message using AI for simulation with detailed steps."""
    import json

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    message = data.get("message", "")
    available_bots = data.get("bots", ["Sales Bot", "Support Bot", "General Bot"])
    detailed = data.get("detailed", False)  # Whether to return step-by-step info

    # Get custom AI configuration from request
    custom_api_key = data.get("api_key", "")
    ai_provider = data.get("ai_provider", "openai")
    ai_model = data.get("ai_model", "gpt-4o-mini")

    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    # Use custom API key if provided (required for simulation)
    api_key = custom_api_key.strip() if custom_api_key else None
    if not api_key:
        raise HTTPException(status_code=400, detail="Please enter an API key to test the simulation.")

    # Basic API key validation
    if len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format. Please check your API key.")

    # Define bot expertise for simulation
    bot_expertise = {
        "Sales Bot": ["sales", "pricing", "quotes", "products", "purchasing"],
        "Support Bot": ["support", "help", "issues", "orders", "refunds", "complaints"],
        "General Bot": ["general", "info", "greetings", "questions"]
    }

    bots_list = ", ".join(available_bots)
    system_prompt = f"""You are a message routing classifier for a multi-bot WhatsApp system. Analyze the message and determine routing.

Available bots and their expertise:
- Sales Bot: sales, pricing, quotes, products, purchasing
- Support Bot: support, help, issues, orders, refunds, complaints
- General Bot: general info, greetings, casual conversation

Return a JSON object with:
{{
    "category": "sales|support|inquiry|feedback|general",
    "urgency": "low|medium|high|critical",
    "sentiment": "positive|neutral|negative",
    "suggested_expertise": ["list", "of", "relevant", "expertise", "tags"],
    "assigned_bot": "exact bot name from the list",
    "confidence": 0.0-1.0,
    "reason": "Brief explanation of why this bot was chosen",
    "bot_scores": {{
        "Sales Bot": 0.0-1.0,
        "Support Bot": 0.0-1.0,
        "General Bot": 0.0-1.0
    }}
}}
Only return valid JSON, no other text."""

    try:
        # Use the appropriate AI provider
        from app.ai.providers import get_ai_provider
        provider = get_ai_provider(ai_provider, api_key, ai_model)
        usage_context.set(user_id=user.id, source='tool', operation='message_routing')

        # Reasoning models (GPT-5, o1, o3) need more tokens as they use tokens for internal reasoning
        is_reasoning_model = ai_model.startswith(('gpt-5', 'o1', 'o3'))
        max_tokens = 4000 if is_reasoning_model else 300

        # Run blocking AI call in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        ai_response = await loop.run_in_executor(
            ai_executor,
            lambda: provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Route this message: {message}"}
                ],
                max_tokens=max_tokens,
                temperature=0.3
            )
        )
        response_text = ai_response.content

        # Debug logging for troubleshooting model responses
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Message routing - Model: {ai_model}, Response length: {len(response_text) if response_text else 0}")
        logger.info(f"Message routing - Raw response: {response_text[:500] if response_text else 'EMPTY'}")

        result_text = response_text.strip() if response_text else ""
        try:
            # Try direct JSON parse first
            routing = json.loads(result_text)
        except:
            # Handle markdown code blocks
            if "```" in result_text:
                parts = result_text.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        try:
                            routing = json.loads(part)
                            break
                        except:
                            continue
                else:
                    routing = None
            # Handle reasoning models that output text before JSON
            elif "{" in result_text and "}" in result_text:
                # Extract JSON object from mixed content
                start = result_text.find("{")
                end = result_text.rfind("}") + 1
                if start < end:
                    try:
                        routing = json.loads(result_text[start:end])
                    except:
                        routing = None
                else:
                    routing = None
            else:
                routing = None

            # Fallback if all parsing attempts failed
            if routing is None:
                logger.warning(f"Message routing - JSON parsing failed for model {ai_model}. Response was: {result_text[:300]}")
                routing = {
                    "category": "general",
                    "urgency": "low",
                    "sentiment": "neutral",
                    "suggested_expertise": ["general"],
                    "assigned_bot": available_bots[0] if available_bots else "General Bot",
                    "confidence": 0.5,
                    "reason": "Could not classify message",
                    "bot_scores": {"Sales Bot": 0.3, "Support Bot": 0.3, "General Bot": 0.4}
                }

        # Ensure all fields exist
        if "sentiment" not in routing:
            routing["sentiment"] = "neutral"
        if "suggested_expertise" not in routing:
            routing["suggested_expertise"] = [routing.get("category", "general")]
        if "bot_scores" not in routing:
            routing["bot_scores"] = {"Sales Bot": 0.3, "Support Bot": 0.3, "General Bot": 0.4}
            routing["bot_scores"][routing.get("assigned_bot", "General Bot")] = routing.get("confidence", 0.5)

        # Build detailed steps for Group Management simulation
        if detailed:
            steps = [
                {
                    "step": 1,
                    "name": "message_received",
                    "title": "Message Received",
                    "status": "success",
                    "icon": "envelope",
                    "data": {
                        "message": message[:100] + ("..." if len(message) > 100 else ""),
                        "sender": "Test User",
                        "chat_type": "group"
                    }
                },
                {
                    "step": 2,
                    "name": "hub_membership",
                    "title": "Hub Membership",
                    "status": "success",
                    "icon": "people",
                    "data": {
                        "total_bots": len(available_bots),
                        "active_bots": available_bots,
                        "message": f"{len(available_bots)} bots active in hub"
                    }
                },
                {
                    "step": 3,
                    "name": "working_hours",
                    "title": "Working Hours",
                    "status": "success",
                    "icon": "clock",
                    "data": {
                        "available_bots": available_bots,
                        "unavailable_bots": [],
                        "message": "All bots within working hours"
                    }
                },
                {
                    "step": 4,
                    "name": "classification",
                    "title": "AI Classification",
                    "status": "success",
                    "icon": "cpu",
                    "data": {
                        "category": routing.get("category", "general"),
                        "urgency": routing.get("urgency", "low"),
                        "sentiment": routing.get("sentiment", "neutral"),
                        "expertise": routing.get("suggested_expertise", [])
                    }
                },
                {
                    "step": 5,
                    "name": "bot_matching",
                    "title": "Expertise Matching",
                    "status": "success",
                    "icon": "diagram-3",
                    "data": {
                        "scores": routing.get("bot_scores", {}),
                        "matched_expertise": routing.get("suggested_expertise", []),
                        "message": f"Best match: {routing.get('assigned_bot', 'General Bot')}"
                    }
                },
                {
                    "step": 6,
                    "name": "final_selection",
                    "title": "Final Selection",
                    "status": "success",
                    "icon": "check-circle",
                    "data": {
                        "selected_bot": routing.get("assigned_bot", "General Bot"),
                        "confidence": routing.get("confidence", 0.5),
                        "reason": routing.get("reason", "Selected as best match")
                    }
                }
            ]
            return {
                "success": True,
                "routing": routing,
                "steps": steps
            }

        return {
            "success": True,
            "routing": routing
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Routing failed: {str(e)}")


# ============================================================================
# Tool Page Routes (HTML)
# ============================================================================

@router.get("", response_class=HTMLResponse)
async def tools_index_page(request: Request, db: Session = Depends(get_db)):
    """Tools index page - overview of all available tools."""
    from app.database import BuiltTool
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )
    # Query user's active custom tools for the Custom Tools section
    custom_tools = db.query(BuiltTool).filter(
        BuiltTool.user_id == user.id,
        BuiltTool.is_active == True
    ).order_by(BuiltTool.created_at.desc()).all()
    return templates.TemplateResponse(
        "dashboard/tools/index.html",
        {"request": request, "user": user, "page_title": "Tools", "custom_tools": custom_tools}
    )


@router.get("/group-management", response_class=HTMLResponse)
async def group_management_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Group Management tool page - Bot assignments, roles, expertise."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/group_management.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Group Management"
        }
    )


@router.get("/content-generator", response_class=HTMLResponse)
async def content_generator_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Content Generator tool page - AI content creation."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/content_generator.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Content Generator"
        }
    )


@router.get("/contact-analyzer", response_class=HTMLResponse)
async def contact_analyzer_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Contact Analyzer tool page - Profiling, tagging, scores."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/contact_analyzer.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Contact Analyzer"
        }
    )


@router.get("/scheduled-content", response_class=HTMLResponse)
async def scheduled_content_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Scheduled Content tool page - Queue, calendar, sending."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/scheduled_content.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Scheduled Content"
        }
    )


@router.get("/message-routing", response_class=HTMLResponse)
async def message_routing_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Message Routing tool page - Classifier + router config."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/message_routing.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Message Routing"
        }
    )



@router.get("/scripted-conversations", response_class=HTMLResponse)
async def scripted_conversations_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Scripted Conversations tool page - Multi-bot conversation scripts."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/scripted_conversations.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Scripted Conversations"
        }
    )


@router.get("/contact-followup", response_class=HTMLResponse)
async def contact_followup_page(
    request: Request,
    hub_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Contact Follow Up tool page - AI-powered follow-up message generation."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    return templates.TemplateResponse(
        "dashboard/tools/contact_followup.html",
        {
            "request": request,
            "user": user,
            "hub_id": hub_id,
            "page_title": "Contact Follow Up"
        }
    )


# ============================================================================
# Contact Follow Up API Endpoints
# ============================================================================

@router.get("/api/contact-followup/stats")
async def get_contact_followup_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get statistics for contact follow-up across all hub instances."""
    from datetime import datetime, timedelta

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get hub IDs for this user (contact_analyzer and contact_followup hubs have the contacts)
    hub_ids = db.query(Hub.id).filter(
        Hub.user_id == user.id,
        Hub.task_type.in_(["contact_analyzer", "contact_followup"])
    ).all()
    hub_ids = [h[0] for h in hub_ids]

    if hub_ids:
        # Count contacts needing follow-up
        total_pending = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            Contact.follow_up_needed == True,
            (Contact.followup_status.is_(None)) | (Contact.followup_status == 'pending')
        ).scalar() or 0

        # Count follow-ups sent today
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            Contact.followup_status == 'sent',
            Contact.followup_sent_at >= today_start
        ).scalar() or 0

        # Count total sent
        total_sent = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            Contact.followup_status == 'sent'
        ).scalar() or 0

        # Count responded
        total_responded = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            Contact.followup_status == 'responded'
        ).scalar() or 0

        # Count dismissed
        total_dismissed = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            Contact.followup_status == 'dismissed'
        ).scalar() or 0

        # Calculate response rate
        response_rate = round((total_responded / max(total_sent, 1)) * 100, 1)
    else:
        total_pending = 0
        sent_today = 0
        total_sent = 0
        total_responded = 0
        total_dismissed = 0
        response_rate = 0

    return {
        "total_pending": total_pending,
        "sent_today": sent_today,
        "total_sent": total_sent,
        "total_responded": total_responded,
        "total_dismissed": total_dismissed,
        "response_rate": response_rate
    }


@router.get("/api/contact-followup/queue")
async def get_contact_followup_queue(
    request: Request,
    hub_id: Optional[int] = None,
    bot_ids: Optional[str] = None,  # Comma-separated list of bot IDs
    urgency: Optional[str] = None,
    sentiment: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,  # urgency, last_message_newest, last_message_oldest, engagement_highest, engagement_lowest
    last_message_before: Optional[str] = None,  # ISO date string
    last_message_after: Optional[str] = None,  # ISO date string
    engagement_min: Optional[int] = None,
    engagement_max: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get contacts that need follow-up, sorted by urgency."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Parse bot_ids if provided
    selected_bot_ids = []
    if bot_ids:
        try:
            selected_bot_ids = [int(bid.strip()) for bid in bot_ids.split(",") if bid.strip()]
        except ValueError:
            pass

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    # Build query for contacts needing follow-up
    query = db.query(Contact).filter(
        Contact.follow_up_needed == True,
        (Contact.followup_status.is_(None)) | (Contact.followup_status == 'pending')
    )

    # Check if provided hub_id is a contact_followup hub
    # If so, we need to look across all contact_analyzer hubs (where contacts are stored)
    if hub_id:
        if hub_id not in user_hub_ids:
            raise HTTPException(status_code=404, detail="Hub not found")

        # Check the hub type
        hub = db.query(Hub).filter(Hub.id == hub_id).first()
        if hub and hub.task_type == "contact_followup":
            # For Contact Follow Up hubs, get contacts from ALL Contact Analyzer hubs
            ca_hub_ids = db.query(Hub.id).filter(
                Hub.id.in_(user_hub_ids),
                Hub.task_type == "contact_analyzer"
            ).all()
            ca_hub_ids = [h[0] for h in ca_hub_ids]
            if ca_hub_ids:
                query = query.filter(Contact.hub_id.in_(ca_hub_ids))
            else:
                return {"contacts": [], "total": 0}
        else:
            # For other hub types, filter by specific hub_id
            query = query.filter(Contact.hub_id == hub_id)
    else:
        # No hub_id provided - get contacts from all contact_analyzer hubs
        ca_hub_ids = db.query(Hub.id).filter(
            Hub.id.in_(user_hub_ids),
            Hub.task_type == "contact_analyzer"
        ).all()
        ca_hub_ids = [h[0] for h in ca_hub_ids]
        if ca_hub_ids:
            query = query.filter(Contact.hub_id.in_(ca_hub_ids))
        else:
            return {"contacts": [], "total": 0}

    # Filter by urgency
    if urgency:
        query = query.filter(Contact.urgency == urgency)

    # Filter by sentiment
    if sentiment:
        query = query.filter(Contact.sentiment == sentiment)

    # Filter by search term (name or phone)
    if search and search.strip():
        search_term = f"%{search.strip()}%"
        query = query.filter(
            (Contact.display_name.ilike(search_term)) | (Contact.phone.ilike(search_term))
        )

    # Filter by engagement score range
    if engagement_min is not None:
        query = query.filter(Contact.engagement_score >= engagement_min)
    if engagement_max is not None:
        query = query.filter(Contact.engagement_score <= engagement_max)

    # Filter by selected bots - only show contacts that have conversations with the selected bots
    if selected_bot_ids:
        # Subquery to find phones that have conversations with selected bots
        phones_with_selected_bots = db.query(Conversation.phone).filter(
            Conversation.bot_profile_id.in_(selected_bot_ids),
            Conversation.is_group == False
        ).distinct().subquery()

        query = query.filter(Contact.phone.in_(phones_with_selected_bots))

    # Build last_message subquery for date filtering and sorting
    last_msg_subq = db.query(
        Conversation.phone,
        func.max(Message.timestamp).label('last_msg_at')
    ).join(Message, Message.conversation_id == Conversation.id).filter(
        Conversation.is_group == False
    ).group_by(Conversation.phone).subquery()

    # Filter by last message date range
    if last_message_before or last_message_after or (sort_by and sort_by.startswith('last_message')):
        query = query.outerjoin(last_msg_subq, Contact.phone == last_msg_subq.c.phone)

        if last_message_before:
            try:
                from datetime import datetime as dt
                before_date = dt.fromisoformat(last_message_before)
                query = query.filter(last_msg_subq.c.last_msg_at <= before_date)
            except ValueError:
                pass

        if last_message_after:
            try:
                from datetime import datetime as dt
                after_date = dt.fromisoformat(last_message_after)
                query = query.filter(last_msg_subq.c.last_msg_at >= after_date)
            except ValueError:
                pass

    # Get total count
    total = query.count()

    # Sort
    if sort_by == 'last_message_newest':
        query = query.order_by(last_msg_subq.c.last_msg_at.desc().nullslast())
    elif sort_by == 'last_message_oldest':
        query = query.order_by(last_msg_subq.c.last_msg_at.asc().nullslast())
    elif sort_by == 'engagement_highest':
        query = query.order_by(Contact.engagement_score.desc().nullslast())
    elif sort_by == 'engagement_lowest':
        query = query.order_by(Contact.engagement_score.asc().nullslast())
    elif sort_by == 'urgency_lowest':
        urgency_order = case(
            (Contact.urgency == 'low', 1),
            (Contact.urgency == 'medium', 2),
            (Contact.urgency == 'high', 3),
            else_=4
        )
        query = query.order_by(urgency_order, Contact.engagement_score.desc())
    else:
        # Default: urgency (high first), then engagement score (high first)
        urgency_order = case(
            (Contact.urgency == 'high', 1),
            (Contact.urgency == 'medium', 2),
            (Contact.urgency == 'low', 3),
            else_=4
        )
        query = query.order_by(urgency_order, Contact.engagement_score.desc())

    # Apply pagination
    contacts = query.offset(offset).limit(limit).all()

    # Build response
    # Batch query last message times for all contacts' phones
    # Use Message.timestamp (always populated) via Conversation join
    contact_phones = [c.phone for c in contacts if c.phone]
    last_message_map = {}
    if contact_phones:
        last_msg_query = db.query(
            Conversation.phone,
            func.max(Message.timestamp).label('last_msg')
        ).join(Message, Message.conversation_id == Conversation.id).filter(
            Conversation.phone.in_(contact_phones),
            Conversation.is_group == False
        ).group_by(Conversation.phone).all()
        last_message_map = {row.phone: row.last_msg for row in last_msg_query}

    result = []
    for contact in contacts:
        # Get hub name
        hub = db.query(Hub).filter(Hub.id == contact.hub_id).first()

        # Parse key_topics if JSON string
        key_topics = []
        if contact.key_topics:
            try:
                import json
                key_topics = json.loads(contact.key_topics) if isinstance(contact.key_topics, str) else contact.key_topics
            except:
                key_topics = []

        # Get tags
        tags = [t.tag for t in contact.tags] if contact.tags else []

        last_message_at = last_message_map.get(contact.phone)

        result.append({
            "id": contact.id,
            "hub_id": contact.hub_id,
            "hub_name": hub.name if hub else None,
            "phone": contact.phone,
            "display_name": contact.display_name,
            "profile_pic": contact.profile_pic,
            "description": contact.description,
            "predicted_intent": contact.predicted_intent,
            "follow_up_reason": contact.follow_up_reason,
            "sentiment": contact.sentiment,
            "urgency": contact.urgency,
            "engagement_score": contact.engagement_score,
            "key_topics": key_topics,
            "tags": tags,
            "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
            "last_message_at": last_message_at.isoformat() if last_message_at else None,
            "followup_attempts": contact.followup_attempts or 0,
            "followup_last_attempt_at": contact.followup_last_attempt_at.isoformat() if contact.followup_last_attempt_at else None
        })

    return {"contacts": result, "total": total}


@router.post("/api/contact-followup/generate")
async def generate_followup_message(
    request: Request,
    db: Session = Depends(get_db)
):
    """Generate a personalized follow-up message for a contact using AI."""
    import json

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    contact_id = data.get("contact_id")
    tone = data.get("tone", "friendly")  # friendly, professional, casual
    followup_hub_id = data.get("hub_id")  # The Contact Follow Up hub ID

    # Get custom AI configuration from request
    custom_api_key = data.get("api_key", "")
    ai_provider = data.get("ai_provider", "")
    ai_model = data.get("ai_model", "")

    if not contact_id:
        raise HTTPException(status_code=400, detail="contact_id is required")

    # Get contact
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Verify user owns the contact's hub (Contact Analyzer hub)
    contact_hub = db.query(Hub).filter(Hub.id == contact.hub_id, Hub.user_id == user.id).first()
    if not contact_hub:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Get Contact Follow Up hub if specified
    followup_hub = None
    followup_agent = None
    if followup_hub_id:
        followup_hub = db.query(Hub).filter(
            Hub.id == followup_hub_id,
            Hub.user_id == user.id,
            Hub.task_type == "contact_followup"
        ).first()

        # Look for a followup or generator agent in the hub
        if followup_hub:
            followup_agent = db.query(AIAgent).filter(
                AIAgent.hub_id == followup_hub.id,
                AIAgent.agent_type.in_(["followup", "generator"]),
                AIAgent.is_active == True
            ).first()

    # Get API key - try from: request > followup agent > followup hub > contact hub
    from app.auth.utils import decrypt_string
    api_key = custom_api_key.strip() if custom_api_key else None

    if not api_key and followup_agent and followup_agent.api_key_encrypted:
        api_key = decrypt_string(followup_agent.api_key_encrypted)

    if not api_key and followup_hub and followup_hub.api_key_encrypted:
        api_key = decrypt_string(followup_hub.api_key_encrypted)

    if not api_key and contact_hub.api_key_encrypted:
        api_key = decrypt_string(contact_hub.api_key_encrypted)

    if not api_key:
        raise HTTPException(status_code=400, detail="API key required. Configure a followup agent with API key in the Agents tab.")

    # Get AI provider/model - prefer followup agent > followup hub > defaults
    if not ai_provider:
        if followup_agent and followup_agent.ai_provider:
            ai_provider = followup_agent.ai_provider
        elif followup_hub and followup_hub.ai_provider:
            ai_provider = followup_hub.ai_provider
        else:
            ai_provider = "openai"

    if not ai_model:
        if followup_agent and followup_agent.model:
            ai_model = followup_agent.model
        elif followup_hub and followup_hub.model:
            ai_model = followup_hub.model
        else:
            ai_model = "gpt-4o-mini"

    # Parse key_topics
    key_topics = []
    if contact.key_topics:
        try:
            key_topics = json.loads(contact.key_topics) if isinstance(contact.key_topics, str) else contact.key_topics
        except:
            key_topics = []

    # Get tags
    tags = [t.tag for t in contact.tags] if contact.tags else []

    # Build AI prompt using all Contact Analyzer data
    system_prompt = f"""You are generating a follow-up message for a WhatsApp contact.

CONTACT PROFILE:
- Name: {contact.display_name or 'Unknown'}
- Description: {contact.description or 'No description available'}
- Predicted Intent: {contact.predicted_intent or 'Unknown'}
- Follow-up Reason: {contact.follow_up_reason or 'General follow-up'}
- Key Topics: {', '.join(key_topics) if key_topics else 'None identified'}
- Sentiment: {contact.sentiment or 'neutral'}
- Urgency: {contact.urgency or 'low'}
- Engagement Score: {contact.engagement_score or 0}%
- Tags: {', '.join(tags) if tags else 'None'}
- Last Interaction: {contact.last_interaction_at.strftime('%Y-%m-%d') if contact.last_interaction_at else 'Unknown'}

Generate a personalized, natural follow-up message that:
1. References their specific situation from the description
2. Addresses their predicted intent
3. Matches their sentiment (warm for positive, professional for neutral, empathetic for negative)
4. Is appropriately urgent based on urgency level
5. Uses a {tone} tone
6. Is concise and actionable (2-4 sentences)
7. Does not use markdown formatting (no asterisks, underscores, etc.)

Return only the message text, no explanations or quotes."""

    # Append additional instructions from the followup agent if provided
    if followup_agent and followup_agent.additional_instructions:
        system_prompt += f"\n\nAdditional Instructions:\n{followup_agent.additional_instructions}"

    try:
        from app.ai.providers import get_ai_provider
        provider = get_ai_provider(ai_provider, api_key, ai_model)
        usage_context.set(user_id=user.id, hub_id=hub_id, source='tool', operation='followup_generation')

        # Reasoning models need more tokens
        is_reasoning_model = ai_model.startswith(('gpt-5', 'o1', 'o3'))
        max_tokens = 4000 if is_reasoning_model else 300

        # Run AI call in thread pool
        loop = asyncio.get_event_loop()
        ai_response = await loop.run_in_executor(
            ai_executor,
            lambda: provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Generate a follow-up message for this contact."}
                ],
                max_tokens=max_tokens,
                temperature=0.7
            )
        )

        generated_message = ai_response.content.strip()
        # Remove quotes if present
        if generated_message.startswith('"') and generated_message.endswith('"'):
            generated_message = generated_message[1:-1]

        tokens_used = ai_response.usage.get("total_tokens", 0) if ai_response.usage else 0

        return {
            "success": True,
            "message": generated_message,
            "tokens_used": tokens_used,
            "contact": {
                "id": contact.id,
                "display_name": contact.display_name,
                "phone": contact.phone
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


@router.get("/api/contact-followup/contact/{contact_id}/bots")
async def get_contact_bots(
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """Get bots that have conversation history with a specific contact."""
    from app.bots.manager import BotManager

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get contact and verify ownership
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    hub = db.query(Hub).filter(Hub.id == contact.hub_id, Hub.user_id == user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Find bots that have conversations with this contact's phone number
    bot_ids = db.query(Conversation.bot_profile_id).filter(
        Conversation.phone == contact.phone,
        Conversation.is_group == False
    ).distinct().all()
    bot_ids = [bid[0] for bid in bot_ids]

    if not bot_ids:
        return {"bots": [], "message": "No bots have conversation history with this contact."}

    # Get bot profiles that belong to the user
    bots = db.query(BotProfile).filter(
        BotProfile.id.in_(bot_ids),
        BotProfile.user_id == user.id
    ).all()

    bot_manager = BotManager()
    result = []
    for bot in bots:
        instance = bot_manager.get_instance(bot.id)
        is_running = instance.is_running if instance else False
        result.append({
            "id": bot.id,
            "name": bot.name,
            "whatsapp_connected": bot.whatsapp_connected or False,
            "whatsapp_phone": bot.whatsapp_phone,
            "is_running": is_running
        })

    return {"bots": result}


@router.post("/api/contact-followup/send")
async def send_followup_message(
    request: Request,
    db: Session = Depends(get_db)
):
    """Send a follow-up message to a contact via a bot."""
    from datetime import datetime
    from app.bots.manager import BotManager

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    contact_id = data.get("contact_id")
    message = data.get("message")
    bot_id = data.get("bot_id")

    if not contact_id or not message or not bot_id:
        raise HTTPException(status_code=400, detail="contact_id, message, and bot_id are required")

    # Get contact
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Verify user owns the hub
    hub = db.query(Hub).filter(Hub.id == contact.hub_id, Hub.user_id == user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Verify user owns the bot
    bot = db.query(BotProfile).filter(BotProfile.id == bot_id, BotProfile.user_id == user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Check if bot is running
    bot_manager = BotManager()
    bot_instance = bot_manager.get_instance(bot_id)
    if not bot_instance or not bot_instance.is_running:
        raise HTTPException(status_code=400, detail="Bot is not running. Please start the bot first.")

    try:
        # Send message via bot using the send_whatsapp_message function
        from app.bots.whatsapp_bot import send_whatsapp_message

        # The chat_id for a contact is typically their phone number with @c.us suffix
        chat_id = f"{contact.phone}@c.us" if not contact.phone.endswith("@c.us") else contact.phone
        chat_name = contact.display_name or contact.phone
        success = await send_whatsapp_message(bot_id, chat_id, chat_name, message)

        if success:
            # Update contact follow-up status
            contact.followup_status = 'sent'
            contact.followup_sent_at = datetime.utcnow()
            contact.followup_message = message
            contact.followup_attempts = (contact.followup_attempts or 0) + 1
            contact.followup_last_attempt_at = datetime.utcnow()
            db.commit()

            # Log the execution
            from .monitoring import ToolMonitor
            ToolMonitor.log_execution(
                db=db,
                tool_type="contact_followup",
                operation="send_followup",
                hub_id=hub.id,
                user_id=user.id,
                input_data={
                    "contact_id": contact_id,
                    "bot_id": bot_id,
                    "bot_name": bot.name if bot else None,
                    "contact_name": contact.display_name,
                    "tone": data.get("tone", "friendly"),
                    "message_length": len(message),
                    "message_preview": message[:80] + ("..." if len(message) > 80 else ""),
                },
                output_data={
                    "status": "sent",
                    "contact_phone": contact.phone,
                    "predicted_intent": contact.predicted_intent,
                    "follow_up_reason": contact.follow_up_reason,
                },
                status="success",
                triggered_by="user"
            )

            return {
                "success": True,
                "message": "Follow-up message sent successfully",
                "contact_id": contact_id,
                "bot_id": bot_id
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to send message via bot")

    except Exception as e:
        # Log the failure
        from .monitoring import ToolMonitor
        ToolMonitor.log_execution(
            db=db,
            tool_type="contact_followup",
            operation="send_followup",
            hub_id=hub.id,
            user_id=user.id,
            input_data={"contact_id": contact_id, "bot_id": bot_id},
            output_data={"error": str(e)},
            status="error",
            error_message=str(e),
            triggered_by="user"
        )
        raise HTTPException(status_code=500, detail=f"Failed to send message: {str(e)}")


@router.post("/api/contact-followup/{contact_id}/dismiss")
async def dismiss_followup(
    request: Request,
    contact_id: int,
    db: Session = Depends(get_db)
):
    """Mark a contact as dismissed (no follow-up needed)."""
    from datetime import datetime

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get contact
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Verify user owns the hub
    hub = db.query(Hub).filter(Hub.id == contact.hub_id, Hub.user_id == user.id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Update status
    contact.followup_status = 'dismissed'
    contact.follow_up_needed = False
    db.commit()

    return {
        "success": True,
        "message": "Follow-up dismissed",
        "contact_id": contact_id
    }


@router.get("/api/contact-followup/auto-send/settings")
async def get_auto_send_settings(
    request: Request,
    hub_id: int = None,
    db: Session = Depends(get_db)
):
    """Get auto-send settings for a contact_followup hub."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not hub_id:
        raise HTTPException(status_code=400, detail="hub_id is required")

    user_hub_ids = get_user_hub_ids(user, db)
    if hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Hub not found")

    hub = db.query(Hub).filter(Hub.id == hub_id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    import json as _json
    filters = {}
    if hub.auto_send_filters:
        try:
            filters = _json.loads(hub.auto_send_filters)
        except (ValueError, TypeError):
            pass

    bot_ids = []
    if hub.auto_send_bot_ids:
        try:
            bot_ids = _json.loads(hub.auto_send_bot_ids)
        except (ValueError, TypeError):
            pass

    return {
        "enabled": bool(hub.auto_send_enabled),
        "interval_minutes": hub.auto_send_interval_minutes or 15,
        "tone": hub.auto_send_tone or "friendly",
        "speed_mode": hub.auto_send_speed_mode or "auto",
        "delay_min": hub.auto_send_delay_min or 5,
        "delay_max": hub.auto_send_delay_max or 15,
        "batch_size": hub.auto_send_batch_size or 20,
        "batch_pause": hub.auto_send_batch_pause or 180,
        "bot_ids": bot_ids,
        "filters": filters,
        "last_run": hub.auto_send_last_run.isoformat() if hub.auto_send_last_run else None
    }


@router.put("/api/contact-followup/auto-send/settings")
async def save_auto_send_settings(
    request: Request,
    db: Session = Depends(get_db)
):
    """Save auto-send settings for a contact_followup hub."""
    import json as _json
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    hub_id = data.get("hub_id")
    if not hub_id:
        raise HTTPException(status_code=400, detail="hub_id is required")

    user_hub_ids = get_user_hub_ids(user, db)
    if hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Hub not found")

    hub = db.query(Hub).filter(Hub.id == hub_id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    hub.auto_send_enabled = bool(data.get("enabled", False))
    hub.auto_send_interval_minutes = int(data.get("interval_minutes", 15))
    hub.auto_send_tone = data.get("tone", "friendly")
    hub.auto_send_speed_mode = data.get("speed_mode", "auto")
    hub.auto_send_delay_min = int(data.get("delay_min", 5))
    hub.auto_send_delay_max = int(data.get("delay_max", 15))
    hub.auto_send_batch_size = int(data.get("batch_size", 20))
    hub.auto_send_batch_pause = int(data.get("batch_pause", 180))
    bot_ids = data.get("bot_ids", [])
    hub.auto_send_bot_ids = _json.dumps(bot_ids) if bot_ids else None
    hub.auto_send_filters = _json.dumps(data.get("filters", {}))
    db.commit()

    return {"success": True, "message": "Auto-send settings saved"}


@router.post("/api/simulate/contact-followup")
async def simulate_contact_followup(
    request: Request,
    db: Session = Depends(get_db)
):
    """Test follow-up message generation with sample contact data."""
    import json

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()

    # Sample contact data from request
    contact_name = data.get("contact_name", "Sample Contact")
    description = data.get("description", "Customer interested in our services")
    predicted_intent = data.get("predicted_intent", "purchase")
    follow_up_reason = data.get("follow_up_reason", "Follow up on product inquiry")
    key_topics = data.get("key_topics", ["pricing", "features"])
    sentiment = data.get("sentiment", "positive")
    urgency = data.get("urgency", "medium")
    engagement_score = data.get("engagement_score", 75)
    tone = data.get("tone", "friendly")

    # Get AI configuration
    custom_api_key = data.get("api_key", "")
    ai_provider = data.get("ai_provider", "openai")
    ai_model = data.get("ai_model", "gpt-4o-mini")

    # Validate API key
    api_key = custom_api_key.strip() if custom_api_key else None
    if not api_key:
        raise HTTPException(status_code=400, detail="Please enter an API key to test the follow-up generator.")

    if len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format. Please check your API key.")

    # Build AI prompt
    system_prompt = f"""You are generating a follow-up message for a WhatsApp contact.

CONTACT PROFILE:
- Name: {contact_name}
- Description: {description}
- Predicted Intent: {predicted_intent}
- Follow-up Reason: {follow_up_reason}
- Key Topics: {', '.join(key_topics) if key_topics else 'None identified'}
- Sentiment: {sentiment}
- Urgency: {urgency}
- Engagement Score: {engagement_score}%

Generate a personalized, natural follow-up message that:
1. References their specific situation from the description
2. Addresses their predicted intent
3. Matches their sentiment (warm for positive, professional for neutral, empathetic for negative)
4. Is appropriately urgent based on urgency level
5. Uses a {tone} tone
6. Is concise and actionable (2-4 sentences)
7. Does not use markdown formatting (no asterisks, underscores, etc.)

Return only the message text, no explanations or quotes."""

    try:
        from app.ai.providers import get_ai_provider
        provider = get_ai_provider(ai_provider, api_key, ai_model)
        usage_context.set(user_id=user.id, hub_id=hub_id, source='tool', operation='followup_simulation')

        # Reasoning models need more tokens
        is_reasoning_model = ai_model.startswith(('gpt-5', 'o1', 'o3'))
        max_tokens = 4000 if is_reasoning_model else 300

        # Run AI call in thread pool
        loop = asyncio.get_event_loop()
        ai_response = await loop.run_in_executor(
            ai_executor,
            lambda: provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Generate a follow-up message for this contact."}
                ],
                max_tokens=max_tokens,
                temperature=0.7
            )
        )

        generated_message = ai_response.content.strip()
        # Remove quotes if present
        if generated_message.startswith('"') and generated_message.endswith('"'):
            generated_message = generated_message[1:-1]

        tokens_used = ai_response.usage.get("total_tokens", 0) if ai_response.usage else 0

        return {
            "success": True,
            "message": generated_message,
            "tokens_used": tokens_used
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


# ============================================================================
# Tool Builder & Marketplace Page Routes
# ============================================================================

@router.get("/tool-builder", response_class=HTMLResponse)
async def tool_builder_page(request: Request, db: Session = Depends(get_db)):
    """Tool Builder page."""
    from fastapi.responses import RedirectResponse
    user = await get_current_user_optional(request, None, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    return templates.TemplateResponse(
        "dashboard/tools/tool_builder.html",
        {"request": request, "user": user, "active_page": "tools_builder", "page_title": "Tool Builder"},
    )


@router.get("/marketplace", response_class=HTMLResponse)
async def marketplace_page(request: Request, db: Session = Depends(get_db)):
    """Marketplace page."""
    from fastapi.responses import RedirectResponse
    user = await get_current_user_optional(request, None, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    return templates.TemplateResponse(
        "dashboard/tools/marketplace.html",
        {"request": request, "user": user, "active_page": "tools_marketplace", "page_title": "Marketplace"},
    )
