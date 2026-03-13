"""
Agents Routes - Global AI Agent management APIs and pages
"""

import sys
from pathlib import Path
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional, List
from datetime import datetime
import json

from app.database import get_db, AIAgent, AgentTemplate, Hub, ToolExecution
from app.auth.utils import get_current_user_optional, decrypt_string
from app.auth.ownership import get_user_hub_ids, verify_hub_ownership
from app.tools.monitoring import ToolMonitor
from pydantic import BaseModel

router = APIRouter(prefix="/agents", tags=["agents"])

# Detect if running as frozen executable (PyInstaller)
if getattr(sys, 'frozen', False):
    # Running as compiled executable - templates are in _MEIPASS/app/templates
    _BASE_DIR = Path(sys._MEIPASS)
else:
    # Running as script - templates are relative to project root
    _BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "app" / "templates"))


# ============================================================================
# Pydantic Models
# ============================================================================

class AgentCreate(BaseModel):
    name: str
    agent_type: str
    hub_id: Optional[int] = None
    system_prompt: Optional[str] = None
    additional_instructions: Optional[str] = None
    model: str = "gpt-4"
    temperature: float = 0.7
    max_tokens: int = 1000
    tools_config: Optional[dict] = None
    is_global: bool = False
    template_id: Optional[int] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    agent_type: Optional[str] = None
    system_prompt: Optional[str] = None
    additional_instructions: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools_config: Optional[dict] = None
    is_active: Optional[bool] = None
    is_global: Optional[bool] = None


class TemplateCreate(BaseModel):
    name: str
    agent_type: str
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    default_config: Optional[dict] = None


class AgentTestRequest(BaseModel):
    agent_id: int
    message: str


# ============================================================================
# Agent API Endpoints
# ============================================================================

@router.get("/api/list")
async def list_agents(
    request: Request,
    hub_id: Optional[int] = None,
    include_global: bool = True,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all agents, optionally filtered by hub or status."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs for ownership filtering
    user_hub_ids = get_user_hub_ids(user, db)

    query = db.query(AIAgent)

    # Filter by hub or global - but only show user's hubs
    if hub_id:
        # Verify user owns this hub
        if hub_id not in user_hub_ids:
            raise HTTPException(status_code=404, detail="Hub not found")
        if include_global:
            query = query.filter(
                or_(AIAgent.hub_id == hub_id, AIAgent.is_global == True)
            )
        else:
            query = query.filter(AIAgent.hub_id == hub_id)
    else:
        # Only show agents from user's hubs
        if user_hub_ids:
            if include_global:
                query = query.filter(
                    or_(AIAgent.hub_id.in_(user_hub_ids), AIAgent.is_global == True)
                )
            else:
                query = query.filter(AIAgent.hub_id.in_(user_hub_ids))
        else:
            # User has no hubs, only show global agents
            if include_global:
                query = query.filter(AIAgent.is_global == True)
            else:
                # Return empty list
                return {"agents": []}

    # Filter by status
    if status:
        query = query.filter(AIAgent.status == status)

    agents = query.order_by(AIAgent.created_at.desc()).all()

    return {
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "agent_type": a.agent_type,
                "hub_id": a.hub_id,
                "hub_name": a.hub.name if a.hub else None,
                "model": a.model,
                "is_active": a.is_active,
                "is_global": a.is_global,
                "status": a.status or "idle",
                "total_executions": a.total_executions or 0,
                "successful_executions": a.successful_executions or 0,
                "total_tokens_used": a.total_tokens_used or 0,
                "last_run_at": (a.last_run_at.isoformat() + "Z") if a.last_run_at else None,
                "last_error": a.last_error,
                "created_at": (a.created_at.isoformat() + "Z") if a.created_at else None
            }
            for a in agents
        ]
    }


@router.get("/api/{agent_id}")
async def get_agent(
    request: Request,
    agent_id: int,
    db: Session = Depends(get_db)
):
    """Get detailed agent information."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the hub this agent belongs to (or it's global)
    if not agent.is_global and agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Parse config JSON if exists
    config = {}
    if agent.config:
        try:
            config = json.loads(agent.config)
        except:
            pass

    return {
        "id": agent.id,
        "name": agent.name,
        "agent_type": agent.agent_type,
        "hub_id": agent.hub_id,
        "hub_name": agent.hub.name if agent.hub else None,
        "system_prompt": agent.system_prompt,
        "additional_instructions": agent.additional_instructions,
        "model": agent.model,
        "config": config,
        "is_active": agent.is_active,
        "is_global": agent.is_global,
        "template_id": agent.template_id,
        "status": agent.status or "idle",
        "total_executions": agent.total_executions or 0,
        "successful_executions": agent.successful_executions or 0,
        "total_tokens_used": agent.total_tokens_used or 0,
        "last_run_at": (agent.last_run_at.isoformat() + "Z") if agent.last_run_at else None,
        "last_error": agent.last_error,
        "created_at": (agent.created_at.isoformat() + "Z") if agent.created_at else None
    }


@router.post("/api/create")
async def create_agent(
    request: Request,
    agent_data: AgentCreate,
    db: Session = Depends(get_db)
):
    """Create a new agent."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Verify user owns the hub if hub_id is provided
    if agent_data.hub_id:
        verify_hub_ownership(agent_data.hub_id, user, db)

    # If template_id provided, load defaults from template
    template_config = {}
    if agent_data.template_id:
        template = db.query(AgentTemplate).filter(
            AgentTemplate.id == agent_data.template_id
        ).first()
        if template:
            template_config = json.loads(template.default_config) if template.default_config else {}
            if not agent_data.system_prompt and template.system_prompt:
                agent_data.system_prompt = template.system_prompt

    # Build config with temperature, max_tokens, and tools_config
    config_data = {
        "temperature": agent_data.temperature,
        "max_tokens": agent_data.max_tokens
    }
    if agent_data.tools_config:
        config_data["tools"] = agent_data.tools_config
    elif template_config:
        config_data.update(template_config)

    agent = AIAgent(
        hub_id=agent_data.hub_id,
        name=agent_data.name,
        agent_type=agent_data.agent_type,
        system_prompt=agent_data.system_prompt,
        additional_instructions=agent_data.additional_instructions,
        model=agent_data.model,
        config=json.dumps(config_data),
        is_active=True,
        is_global=agent_data.is_global,
        template_id=agent_data.template_id,
        status="idle"
    )

    db.add(agent)
    db.commit()
    db.refresh(agent)

    # Log the creation
    ToolMonitor.log_execution(
        db=db,
        tool_type="agent_execution",
        operation="create_agent",
        hub_id=agent_data.hub_id,
        input_data={"agent_name": agent_data.name, "agent_type": agent_data.agent_type},
        output_data={"agent_id": agent.id},
        triggered_by="user",
        related_entity_type="agent",
        related_entity_id=agent.id,
        user_id=user.id
    )

    return {"success": True, "agent_id": agent.id}


@router.put("/api/{agent_id}")
async def update_agent(
    request: Request,
    agent_id: int,
    agent_data: AgentUpdate,
    db: Session = Depends(get_db)
):
    """Update an existing agent."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the hub this agent belongs to
    if agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update fields if provided
    update_dict = agent_data.dict(exclude_unset=True)

    # Handle config-based fields separately
    config_updates = {}

    for key, value in update_dict.items():
        if key == "model":
            agent.model = value
        elif key == "temperature":
            config_updates["temperature"] = value
        elif key == "max_tokens":
            config_updates["max_tokens"] = value
        elif key == "tools_config" and value is not None:
            config_updates["tools"] = value
        elif hasattr(agent, key):
            setattr(agent, key, value)

    # Update config if there are config changes
    if config_updates:
        existing_config = {}
        if agent.config:
            try:
                existing_config = json.loads(agent.config)
            except:
                pass
        existing_config.update(config_updates)
        agent.config = json.dumps(existing_config)

    db.commit()

    # Log the update
    ToolMonitor.log_execution(
        db=db,
        tool_type="agent_execution",
        operation="update_agent",
        hub_id=agent.hub_id,
        input_data={"agent_id": agent_id, "updates": update_dict},
        triggered_by="user",
        related_entity_type="agent",
        related_entity_id=agent.id,
        user_id=user.id
    )

    return {"success": True}


@router.delete("/api/{agent_id}")
async def delete_agent(
    request: Request,
    agent_id: int,
    db: Session = Depends(get_db)
):
    """Delete an agent."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the hub this agent belongs to
    if agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    hub_id = agent.hub_id
    agent_name = agent.name

    db.delete(agent)
    db.commit()

    # Log the deletion
    ToolMonitor.log_execution(
        db=db,
        tool_type="agent_execution",
        operation="delete_agent",
        hub_id=hub_id,
        input_data={"agent_id": agent_id, "agent_name": agent_name},
        triggered_by="user",
        related_entity_type="agent",
        related_entity_id=agent_id,
        user_id=user.id
    )

    return {"success": True}


@router.post("/api/{agent_id}/duplicate")
async def duplicate_agent(
    request: Request,
    agent_id: int,
    db: Session = Depends(get_db)
):
    """Duplicate an existing agent with reset runtime stats."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_hub_ids = get_user_hub_ids(user, db)

    source_agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not source_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not source_agent.is_global and source_agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    base_name = f"Copy of {source_agent.name}"
    duplicate_name = base_name
    duplicate_count = 2

    while db.query(AIAgent).filter(
        AIAgent.hub_id == source_agent.hub_id,
        AIAgent.is_global == source_agent.is_global,
        AIAgent.name == duplicate_name
    ).first():
        duplicate_name = f"{base_name} ({duplicate_count})"
        duplicate_count += 1

    duplicated_agent = AIAgent(
        hub_id=source_agent.hub_id,
        name=duplicate_name,
        agent_type=source_agent.agent_type,
        description=source_agent.description,
        ai_provider=source_agent.ai_provider,
        api_key_encrypted=source_agent.api_key_encrypted,
        model=source_agent.model,
        system_prompt=source_agent.system_prompt,
        additional_instructions=source_agent.additional_instructions,
        config=source_agent.config,
        is_active=source_agent.is_active,
        is_global=source_agent.is_global,
        template_id=source_agent.template_id,
        status="idle",
        last_error=None,
        last_run_at=None,
        total_executions=0,
        successful_executions=0,
        total_tokens_used=0
    )

    db.add(duplicated_agent)
    db.commit()
    db.refresh(duplicated_agent)

    ToolMonitor.log_execution(
        db=db,
        tool_type="agent_execution",
        operation="duplicate_agent",
        hub_id=duplicated_agent.hub_id,
        input_data={"source_agent_id": source_agent.id, "source_agent_name": source_agent.name},
        output_data={"duplicated_agent_id": duplicated_agent.id, "duplicated_agent_name": duplicated_agent.name},
        triggered_by="user",
        related_entity_type="agent",
        related_entity_id=duplicated_agent.id,
        user_id=user.id
    )

    return {"success": True, "agent_id": duplicated_agent.id, "name": duplicated_agent.name}


@router.post("/api/{agent_id}/toggle")
async def toggle_agent(
    request: Request,
    agent_id: int,
    db: Session = Depends(get_db)
):
    """Toggle agent active status."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the hub this agent belongs to
    if agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.is_active = not agent.is_active
    db.commit()

    return {"success": True, "is_active": agent.is_active}


@router.post("/api/test")
async def test_agent(
    request: Request,
    test_data: AgentTestRequest,
    db: Session = Depends(get_db)
):
    """Test an agent with a sample message."""
    import time
    from openai import OpenAI

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    agent = db.query(AIAgent).filter(AIAgent.id == test_data.agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the hub this agent belongs to (or it's global)
    if not agent.is_global and agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    start_time = time.time()

    # Get API key from agent, hub, or user settings
    api_key = None

    # Try agent's own API key first
    if agent.api_key_encrypted:
        api_key = decrypt_string(agent.api_key_encrypted)

    # Try hub's API key if agent belongs to a hub
    if not api_key and agent.hub_id:
        hub = db.query(Hub).filter(Hub.id == agent.hub_id).first()
        if hub and hub.api_key_encrypted:
            api_key = decrypt_string(hub.api_key_encrypted)

    # Try user's default API key
    if not api_key and hasattr(user, 'api_key_encrypted') and user.api_key_encrypted:
        api_key = decrypt_string(user.api_key_encrypted)

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No API key configured. Please add an OpenAI API key in your settings, hub, or agent configuration."
        )

    # Get system prompt
    system_prompt = agent.system_prompt or f"You are {agent.name}, a helpful AI assistant."

    # For classifier/router agents, use additional_instructions
    if agent.agent_type in ['classifier', 'router'] and agent.additional_instructions:
        system_prompt = f"You are {agent.name}. {agent.additional_instructions}"

    # Get model and config
    model = agent.model or "gpt-4o-mini"
    config = json.loads(agent.config) if agent.config else {}
    temperature = config.get('temperature', 0.7)
    max_tokens = config.get('max_tokens', 1000)

    try:
        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": test_data.message}
            ],
            temperature=temperature,
            max_tokens=max_tokens
        )

        response_text = response.choices[0].message.content
        tokens_used = response.usage.total_tokens if response.usage else 0

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI request failed: {str(e)}")

    execution_time_ms = int((time.time() - start_time) * 1000)

    # Update agent stats
    agent.total_executions = (agent.total_executions or 0) + 1
    agent.successful_executions = (agent.successful_executions or 0) + 1
    agent.total_tokens_used = (agent.total_tokens_used or 0) + tokens_used
    agent.last_run_at = datetime.utcnow()
    db.commit()

    # Log the test execution
    ToolMonitor.log_execution(
        db=db,
        tool_type="agent_execution",
        operation=f"test_agent: {agent.name}",
        hub_id=agent.hub_id,
        input_data={"message": test_data.message[:200], "agent_id": agent.id},
        output_data={"response_length": len(response_text)},
        status="success",
        execution_time_ms=execution_time_ms,
        tokens_used=tokens_used,
        triggered_by="user",
        related_entity_type="agent",
        related_entity_id=agent.id,
        user_id=user.id
    )

    return {
        "success": True,
        "response": response_text,
        "execution_time_ms": execution_time_ms,
        "tokens_used": tokens_used,
        "agent_name": agent.name,
        "agent_type": agent.agent_type
    }


@router.get("/api/{agent_id}/history")
async def get_agent_history(
    request: Request,
    agent_id: int,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get execution history for a specific agent."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get user's hub IDs
    user_hub_ids = get_user_hub_ids(user, db)

    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the hub this agent belongs to (or it's global)
    if not agent.is_global and agent.hub_id not in user_hub_ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    executions = db.query(ToolExecution).filter(
        ToolExecution.related_entity_type == "agent",
        ToolExecution.related_entity_id == agent_id
    ).order_by(ToolExecution.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "executions": [
            {
                "id": e.id,
                "operation": e.operation,
                "status": e.status,
                "error_message": e.error_message,
                "execution_time_ms": e.execution_time_ms,
                "tokens_used": e.tokens_used,
                "created_at": e.created_at.isoformat() if e.created_at else None
            }
            for e in executions
        ]
    }


# ============================================================================
# Template API Endpoints
# ============================================================================

@router.get("/api/templates/list")
async def list_templates(
    request: Request,
    agent_type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List available agent templates."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    query = db.query(AgentTemplate).filter(
        or_(AgentTemplate.is_system == True, AgentTemplate.user_id == user.id)
    )

    if agent_type:
        query = query.filter(AgentTemplate.agent_type == agent_type)

    templates = query.order_by(AgentTemplate.name).all()

    return {
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "agent_type": t.agent_type,
                "description": t.description,
                "is_system": t.is_system,
                "created_at": t.created_at.isoformat() if t.created_at else None
            }
            for t in templates
        ]
    }


@router.get("/api/templates/{template_id}")
async def get_template(
    request: Request,
    template_id: int,
    db: Session = Depends(get_db)
):
    """Get detailed template information."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    template = db.query(AgentTemplate).filter(AgentTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    return {
        "id": template.id,
        "name": template.name,
        "agent_type": template.agent_type,
        "description": template.description,
        "system_prompt": template.system_prompt,
        "default_config": json.loads(template.default_config) if template.default_config else None,
        "is_system": template.is_system,
        "created_at": template.created_at.isoformat() if template.created_at else None
    }


@router.post("/api/templates/create")
async def create_template(
    request: Request,
    template_data: TemplateCreate,
    db: Session = Depends(get_db)
):
    """Create a new user template."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    template = AgentTemplate(
        name=template_data.name,
        agent_type=template_data.agent_type,
        description=template_data.description,
        system_prompt=template_data.system_prompt,
        default_config=json.dumps(template_data.default_config) if template_data.default_config else None,
        is_system=False,
        user_id=user.id
    )

    db.add(template)
    db.commit()
    db.refresh(template)

    return {"success": True, "template_id": template.id}


# ============================================================================
# Agent Page Routes (HTML)
# ============================================================================

@router.get("", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    db: Session = Depends(get_db)
):
    """Global Agents management page."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Please log in to access this page"}
        )

    # Get all hubs for the dropdown
    hubs = db.query(Hub).filter(Hub.user_id == user.id).all()

    # Get agent stats
    total_agents = db.query(AIAgent).count()
    active_agents = db.query(AIAgent).filter(AIAgent.is_active == True).count()
    running_agents = db.query(AIAgent).filter(AIAgent.status == "running").count()

    return templates.TemplateResponse(
        "dashboard/agents.html",
        {
            "request": request,
            "user": user,
            "hubs": hubs,
            "stats": {
                "total": total_agents,
                "active": active_agents,
                "running": running_agents
            },
            "page_title": "AI Agents"
        }
    )
