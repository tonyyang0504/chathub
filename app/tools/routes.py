"""
Tools Routes - Aggregates all tool page routes and APIs
"""

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db, ToolExecution, Hub, ScheduledContent, HubBotMembership, Contact, ContactTag, AIAgent
from app.auth.utils import get_current_user_optional
from app.auth.ownership import get_user_hub_ids
from sqlalchemy import func
from .monitoring import ToolMonitor

router = APIRouter(prefix="/tools", tags=["tools"])

templates = Jinja2Templates(directory="app/templates")


# ============================================================================
# Tool Monitoring API Endpoints
# ============================================================================

@router.get("/api/executions")
async def get_tool_executions(
    request: Request,
    tool_type: Optional[str] = None,
    hub_id: Optional[int] = None,
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

        # Count contacts analyzed (those with description or predicted_intent)
        contacts_analyzed = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            (Contact.description.isnot(None)) | (Contact.predicted_intent.isnot(None))
        ).scalar() or 0

        # Count contacts with tags (using subquery)
        from sqlalchemy import exists
        contacts_tagged = db.query(func.count(Contact.id)).filter(
            Contact.hub_id.in_(hub_ids),
            exists().where(ContactTag.contact_id == Contact.id)
        ).scalar() or 0

        # Get execution stats
        executions = db.query(func.count(ToolExecution.id)).filter(
            ToolExecution.hub_id.in_(hub_ids),
            ToolExecution.tool_type == "contact_analyzer"
        ).scalar() or 0
    else:
        total_contacts = 0
        contacts_analyzed = 0
        contacts_tagged = 0
        executions = 0

    return {
        "total_instances": total_instances,
        "total_contacts": total_contacts,
        "contacts_analyzed": contacts_analyzed,
        "contacts_tagged": contacts_tagged,
        "total_executions": executions
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
    """Get OpenAI API key from user's hubs."""
    from app.auth.utils import decrypt_string

    hub = db.query(Hub).filter(
        Hub.user_id == user.id,
        Hub.openai_api_key_encrypted.isnot(None)
    ).first()

    if hub and hub.openai_api_key_encrypted:
        return decrypt_string(hub.openai_api_key_encrypted)
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

        system_prompt = f"""You are a professional content writer for WhatsApp messages.
Generate a {content_type} message with a {tone} tone about the given topic.
Keep it concise (under 200 words), suitable for WhatsApp, and engaging.
Use appropriate emojis if the tone is friendly or casual.
Do not include subject lines or greetings like "Subject:" - just the message body."""

        ai_response = provider.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate a {content_type} message about: {topic}"}
            ],
            max_tokens=300,
            temperature=0.7
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

        ai_response = provider.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Analyze this message: {message}"}
            ],
            max_tokens=200,
            temperature=0.3
        )

        result_text = ai_response.content.strip()
        # Try to parse JSON from response
        try:
            # Remove markdown code blocks if present
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
            analysis = json.loads(result_text)
        except:
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

        ai_response = provider.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Route this message: {message}"}
            ],
            max_tokens=300,
            temperature=0.3
        )
        response_text = ai_response.content

        result_text = response_text.strip()
        try:
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
            routing = json.loads(result_text)
        except:
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
