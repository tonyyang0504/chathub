"""
ChatHub Agent Routes - HTTP + WebSocket endpoints for ChatHub Agent.
"""

import json
import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.auth.utils import (
    get_current_user, get_current_user_optional, get_websocket_user,
    encrypt_string, decrypt_string,
)
from .manager import chathub_agent_manager
from .schemas import (
    SessionCreate, MessageCreate, ApprovalRequest,
    AgentConfigCreate, AgentConfigUpdate, SkillToggle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["ChatHub Agent"])

# Template setup
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys._MEIPASS)
else:
    _BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "app" / "templates"))


# ============================================================================
# Page Route
# ============================================================================

@router.get("/chathub-agent", response_class=HTMLResponse)
async def chathub_agent_page(request: Request, db: Session = Depends(get_db)):
    """ChatHub Agent tool page."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse(
        "dashboard/tools/chathub_agent.html",
        {
            "request": request,
            "user": user,
            "active_page": "tools_chathub_agent",
            "page_title": "ChatHub Agent",
        },
    )


# ============================================================================
# Settings API
# ============================================================================

@router.get("/api/chathub-agent/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Get ChatHub Agent settings (API key masked)."""
    from app.database import ChatHubAgentSettings
    user = await get_current_user(request, None, db)

    settings = db.query(ChatHubAgentSettings).filter(
        ChatHubAgentSettings.user_id == user.id
    ).first()

    if not settings:
        return {
            "api_key_set": False,
            "ai_provider": "openai",
            "default_model": "gpt-4o",
            "auto_commit": True,
            "auto_backup_db": True,
            "max_session_minutes": 30,
            "queue_mode": "fifo",
        }

    api_key_masked = ""
    if settings.api_key_encrypted:
        try:
            decrypted = decrypt_string(settings.api_key_encrypted)
            api_key_masked = "****" + decrypted[-4:]
        except Exception:
            api_key_masked = "****"

    return {
        "api_key_set": bool(settings.api_key_encrypted),
        "api_key_masked": api_key_masked,
        "ai_provider": settings.ai_provider or "openai",
        "default_model": settings.default_model or "gpt-4o",
        "default_agent_id": settings.default_agent_id,
        "auto_commit": settings.auto_commit if settings.auto_commit is not None else True,
        "auto_backup_db": settings.auto_backup_db if settings.auto_backup_db is not None else True,
        "max_session_minutes": settings.max_session_minutes or 30,
        "queue_mode": settings.queue_mode or "fifo",
        "workspace_path": settings.workspace_path,
    }


@router.put("/api/chathub-agent/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    """Save ChatHub Agent settings."""
    from app.database import ChatHubAgentSettings
    user = await get_current_user(request, None, db)
    data = await request.json()

    settings = db.query(ChatHubAgentSettings).filter(
        ChatHubAgentSettings.user_id == user.id
    ).first()

    if not settings:
        settings = ChatHubAgentSettings(user_id=user.id)
        db.add(settings)

    # Only update API key if provided (non-empty, not masked)
    api_key = data.get("api_key")
    if api_key and api_key.strip() and not api_key.startswith("****"):
        settings.api_key_encrypted = encrypt_string(api_key.strip())

    for field in ["ai_provider", "default_model", "default_agent_id",
                  "auto_commit", "auto_backup_db", "max_session_minutes",
                  "queue_mode", "workspace_path"]:
        if field in data:
            setattr(settings, field, data[field])

    db.commit()
    return {"status": "ok"}


# ============================================================================
# Session API
# ============================================================================

@router.post("/api/chathub-agent/sessions")
async def create_session(request: Request, db: Session = Depends(get_db)):
    """Create a new ChatHub Agent session."""
    from app.database import ChatHubAgentSettings, ChatHubAgentSession, ChatHubAgentMessage, ChatHubAgentConfig
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Check for existing active session
    active = chathub_agent_manager.get_active_session(user.id)
    if active:
        raise HTTPException(status_code=409, detail="You already have an active session")

    # Get settings
    settings = db.query(ChatHubAgentSettings).filter(
        ChatHubAgentSettings.user_id == user.id
    ).first()

    if not settings or not settings.api_key_encrypted:
        raise HTTPException(status_code=400, detail="Please configure your API key in Settings")

    api_key = decrypt_string(settings.api_key_encrypted)

    # Build agent config from selected agent or defaults
    agent_config_id = data.get("agent_config_id") or settings.default_agent_id
    agent_config_dict = {
        "ai_provider": settings.ai_provider or "openai",
        "model": data.get("model") or settings.default_model or "gpt-4o",
        "system_prompt": None,
        "temperature": 0.3,
        "max_tokens": 8192,
        "allowed_tools": None,
        "dangerous_tools": ["exec_command"],
        "auto_approve_read": True,
        "workspace_path": settings.workspace_path,
        "enabled_skills": None,
    }

    if agent_config_id:
        agent_config = db.query(ChatHubAgentConfig).filter(
            ChatHubAgentConfig.id == agent_config_id,
            ChatHubAgentConfig.user_id == user.id,
            ChatHubAgentConfig.is_active == True,
        ).first()
        if agent_config:
            agent_config_dict.update({
                "ai_provider": agent_config.ai_provider or agent_config_dict["ai_provider"],
                "model": agent_config.model or agent_config_dict["model"],
                "system_prompt": agent_config.system_prompt,
                "temperature": agent_config.temperature,
                "max_tokens": agent_config.max_tokens,
                "allowed_tools": json.loads(agent_config.allowed_tools) if agent_config.allowed_tools else None,
                "dangerous_tools": json.loads(agent_config.dangerous_tools) if agent_config.dangerous_tools else ["exec_command"],
                "auto_approve_read": agent_config.auto_approve_read,
                "workspace_path": agent_config.workspace_path or settings.workspace_path,
                "enabled_skills": json.loads(agent_config.enabled_skills) if agent_config.enabled_skills else None,
            })
            # Use agent-specific API key if set
            if agent_config.api_key_encrypted:
                try:
                    api_key = decrypt_string(agent_config.api_key_encrypted)
                except Exception:
                    pass

    # Safety commit and backup
    git_hash = None
    db_backup_path = None

    if settings.auto_commit:
        git_hash = chathub_agent_manager.create_safety_commit()
    if settings.auto_backup_db:
        db_backup_path = chathub_agent_manager.create_db_backup()

    # Create session record
    session = ChatHubAgentSession(
        user_id=user.id,
        agent_config_id=agent_config_id,
        prompt=prompt,
        ai_provider=agent_config_dict["ai_provider"],
        model=agent_config_dict["model"],
        git_commit_hash=git_hash,
        db_backup_path=db_backup_path,
        status="pending",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Persist the user prompt as a message
    user_msg = ChatHubAgentMessage(
        session_id=session.id,
        role="user",
        content=prompt,
        message_type="text",
    )
    db.add(user_msg)
    db.commit()

    # Build settings dict for manager
    settings_dict = {
        "api_key": api_key,
        "ai_provider": agent_config_dict["ai_provider"],
        "default_model": agent_config_dict["model"],
        "workspace_path": agent_config_dict.get("workspace_path"),
    }

    # Start agent session
    active_session = await chathub_agent_manager.start_session(
        user_id=user.id,
        session_id=session.id,
        prompt=prompt,
        settings_dict=settings_dict,
        agent_config_dict=agent_config_dict,
    )

    if not active_session:
        session.status = "failed"
        db.commit()
        raise HTTPException(status_code=500, detail="Failed to start ChatHub Agent session. Check server logs for details.")

    session.status = "running"
    session.started_at = datetime.utcnow()
    db.commit()

    # Log to tool monitor
    try:
        from app.tools.monitoring import ToolMonitor
        ToolMonitor.log_execution(
            db=db,
            tool_type="chathub_agent",
            operation="session_start",
            input_data={"prompt": prompt[:200], "model": agent_config_dict["model"]},
            output_data={"session_id": session.id, "git_hash": git_hash},
            user_id=user.id,
        )
    except Exception:
        pass

    return {
        "session_id": session.id,
        "status": "running",
        "git_commit_hash": git_hash,
        "db_backup_path": db_backup_path,
    }


@router.get("/api/chathub-agent/sessions")
async def list_sessions(request: Request, db: Session = Depends(get_db)):
    """List user's ChatHub Agent sessions."""
    from app.database import ChatHubAgentSession
    user = await get_current_user(request, None, db)

    sessions = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.user_id == user.id
    ).order_by(ChatHubAgentSession.created_at.desc()).limit(50).all()

    return [{
        "id": s.id,
        "status": s.status,
        "prompt": s.prompt[:100] if s.prompt else "",
        "ai_provider": s.ai_provider,
        "model": s.model,
        "total_turns": s.total_turns,
        "total_tool_calls": s.total_tool_calls,
        "total_tokens": s.total_tokens,
        "git_commit_hash": s.git_commit_hash,
        "rolled_back": s.rolled_back,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
    } for s in sessions]


@router.get("/api/chathub-agent/sessions/{session_id}")
async def get_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Get a session with its messages."""
    from app.database import ChatHubAgentSession, ChatHubAgentMessage
    user = await get_current_user(request, None, db)

    session = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.id == session_id,
        ChatHubAgentSession.user_id == user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = db.query(ChatHubAgentMessage).filter(
        ChatHubAgentMessage.session_id == session_id
    ).order_by(ChatHubAgentMessage.created_at.asc()).all()

    return {
        "id": session.id,
        "status": session.status,
        "prompt": session.prompt,
        "ai_provider": session.ai_provider,
        "model": session.model,
        "agent_config_id": session.agent_config_id,
        "total_turns": session.total_turns,
        "total_tool_calls": session.total_tool_calls,
        "total_tokens": session.total_tokens,
        "git_commit_hash": session.git_commit_hash,
        "db_backup_path": session.db_backup_path,
        "rolled_back": session.rolled_back,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "messages": [{
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "message_type": m.message_type,
            "tool_name": m.tool_name,
            "tool_call_id": m.tool_call_id,
            "tokens_used": m.tokens_used,
            "execution_time_ms": m.execution_time_ms,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in messages],
    }


@router.post("/api/chathub-agent/sessions/{session_id}/message")
async def send_message(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Send a follow-up message to an active session."""
    from app.database import ChatHubAgentSession, ChatHubAgentMessage
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip() or data.get("content", "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Verify ownership
    session = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.id == session_id,
        ChatHubAgentSession.user_id == user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Persist the user message
    user_msg = ChatHubAgentMessage(
        session_id=session.id,
        role="user",
        content=prompt,
        message_type="text",
    )
    db.add(user_msg)
    db.commit()

    # Send via manager
    sent = await chathub_agent_manager.send_message(user.id, prompt)
    if not sent:
        raise HTTPException(status_code=400, detail="Session is not ready for input")

    return {"status": "ok"}


@router.post("/api/chathub-agent/sessions/{session_id}/stop")
async def stop_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Stop a running session."""
    from app.database import ChatHubAgentSession
    user = await get_current_user(request, None, db)

    session = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.id == session_id,
        ChatHubAgentSession.user_id == user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    stopped = await chathub_agent_manager.stop_session(user.id)
    if not stopped:
        raise HTTPException(status_code=400, detail="No active session to stop")

    return {"status": "stopped"}


@router.post("/api/chathub-agent/sessions/{session_id}/rollback")
async def rollback_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Rollback git + DB to pre-session state."""
    from app.database import ChatHubAgentSession
    user = await get_current_user(request, None, db)

    session = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.id == session_id,
        ChatHubAgentSession.user_id == user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == "running":
        raise HTTPException(status_code=400, detail="Cannot rollback a running session")

    if session.rolled_back:
        raise HTTPException(status_code=400, detail="Session already rolled back")

    result = chathub_agent_manager.rollback_session(
        session.git_commit_hash,
        session.db_backup_path,
    )

    session.rolled_back = True
    session.rolled_back_at = datetime.utcnow()
    db.commit()

    try:
        from app.tools.monitoring import ToolMonitor
        ToolMonitor.log_execution(
            db=db,
            tool_type="chathub_agent",
            operation="rollback",
            input_data={"session_id": session_id, "git_hash": session.git_commit_hash},
            output_data=result,
            user_id=user.id,
        )
    except Exception:
        pass

    return {"status": "ok", "result": result}


@router.post("/api/chathub-agent/sessions/{session_id}/approve")
async def approve_tool(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Approve or deny a pending tool execution."""
    from app.database import ChatHubAgentSession
    user = await get_current_user(request, None, db)
    data = await request.json()

    # Verify ownership
    session = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.id == session_id,
        ChatHubAgentSession.user_id == user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    approved = data.get("approved", False)
    result = chathub_agent_manager.approve_tool(user.id, approved)

    if not result:
        raise HTTPException(status_code=400, detail="No pending approval request")

    return {"status": "ok", "approved": approved}


# ============================================================================
# Agent Config CRUD
# ============================================================================

@router.get("/api/chathub-agent/agents")
async def list_agent_configs(request: Request, db: Session = Depends(get_db)):
    """List agent configurations."""
    from app.database import ChatHubAgentConfig
    user = await get_current_user(request, None, db)

    configs = db.query(ChatHubAgentConfig).filter(
        ChatHubAgentConfig.user_id == user.id
    ).order_by(ChatHubAgentConfig.created_at.desc()).all()

    results = []
    for c in configs:
        results.append({
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "ai_provider": c.ai_provider,
            "model": c.model,
            "system_prompt": c.system_prompt,
            "temperature": c.temperature,
            "max_tokens": c.max_tokens,
            "allowed_tools": json.loads(c.allowed_tools) if c.allowed_tools else None,
            "dangerous_tools": json.loads(c.dangerous_tools) if c.dangerous_tools else None,
            "auto_approve_read": c.auto_approve_read,
            "workspace_path": c.workspace_path,
            "enabled_skills": json.loads(c.enabled_skills) if c.enabled_skills else None,
            "routing_rules": json.loads(c.routing_rules) if c.routing_rules else None,
            "is_default": c.is_default,
            "is_active": c.is_active,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })
    return results


@router.post("/api/chathub-agent/agents")
async def create_agent_config(request: Request, db: Session = Depends(get_db)):
    """Create a new agent configuration."""
    from app.database import ChatHubAgentConfig
    user = await get_current_user(request, None, db)
    data = await request.json()

    config = ChatHubAgentConfig(
        user_id=user.id,
        name=data.get("name", "Default Agent"),
        description=data.get("description"),
        ai_provider=data.get("ai_provider", "openai"),
        model=data.get("model", "gpt-4o"),
        system_prompt=data.get("system_prompt"),
        temperature=data.get("temperature", 0.3),
        max_tokens=data.get("max_tokens", 8192),
        allowed_tools=json.dumps(data["allowed_tools"]) if data.get("allowed_tools") else None,
        dangerous_tools=json.dumps(data.get("dangerous_tools", ["exec_command"])),
        auto_approve_read=data.get("auto_approve_read", True),
        workspace_path=data.get("workspace_path"),
        enabled_skills=json.dumps(data["enabled_skills"]) if data.get("enabled_skills") else None,
        routing_rules=json.dumps(data["routing_rules"]) if data.get("routing_rules") else None,
        is_default=data.get("is_default", False),
    )

    # Handle API key encryption
    if data.get("api_key"):
        config.api_key_encrypted = encrypt_string(data["api_key"])

    # If marking as default, unmark other defaults
    if config.is_default:
        db.query(ChatHubAgentConfig).filter(
            ChatHubAgentConfig.user_id == user.id,
            ChatHubAgentConfig.is_default == True,
        ).update({"is_default": False})

    db.add(config)
    db.commit()
    db.refresh(config)

    return {"status": "ok", "id": config.id}


@router.put("/api/chathub-agent/agents/{config_id}")
async def update_agent_config(config_id: int, request: Request, db: Session = Depends(get_db)):
    """Update an agent configuration."""
    from app.database import ChatHubAgentConfig
    user = await get_current_user(request, None, db)
    data = await request.json()

    config = db.query(ChatHubAgentConfig).filter(
        ChatHubAgentConfig.id == config_id,
        ChatHubAgentConfig.user_id == user.id,
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Agent config not found")

    # Simple fields
    for field in ["name", "description", "ai_provider", "model", "system_prompt",
                  "temperature", "max_tokens", "auto_approve_read", "workspace_path",
                  "is_active"]:
        if field in data:
            setattr(config, field, data[field])

    # JSON fields
    for field in ["allowed_tools", "dangerous_tools", "enabled_skills", "routing_rules"]:
        if field in data:
            val = data[field]
            setattr(config, field, json.dumps(val) if val is not None else None)

    # API key
    if data.get("api_key") and not data["api_key"].startswith("****"):
        config.api_key_encrypted = encrypt_string(data["api_key"])

    # Default handling
    if data.get("is_default"):
        db.query(ChatHubAgentConfig).filter(
            ChatHubAgentConfig.user_id == user.id,
            ChatHubAgentConfig.id != config_id,
            ChatHubAgentConfig.is_default == True,
        ).update({"is_default": False})
        config.is_default = True

    config.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.delete("/api/chathub-agent/agents/{config_id}")
async def delete_agent_config(config_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete an agent configuration."""
    from app.database import ChatHubAgentConfig
    user = await get_current_user(request, None, db)

    config = db.query(ChatHubAgentConfig).filter(
        ChatHubAgentConfig.id == config_id,
        ChatHubAgentConfig.user_id == user.id,
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Agent config not found")

    db.delete(config)
    db.commit()
    return {"status": "ok"}


# ============================================================================
# Skills API
# ============================================================================

@router.get("/api/chathub-agent/skills")
async def list_skills(request: Request, db: Session = Depends(get_db)):
    """List available skills with their activation status."""
    from app.database import ChatHubAgentSettings, ChatHubAgentSkill
    from app.chathub_agent.skills import SkillRegistry
    user = await get_current_user(request, None, db)

    # Get workspace path from settings
    settings = db.query(ChatHubAgentSettings).filter(
        ChatHubAgentSettings.user_id == user.id
    ).first()
    workspace = settings.workspace_path if settings else None

    # Load skill registry
    registry = SkillRegistry()
    registry.load_all(workspace)

    # Get user's skill activation status from DB
    db_skills = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.user_id == user.id
    ).all()
    db_skill_map = {s.name: s for s in db_skills}

    results = []
    for skill in registry.get_all():
        db_entry = db_skill_map.get(skill.name)
        results.append({
            "name": skill.name,
            "display_name": skill.display_name,
            "description": skill.description,
            "tier": skill.tier,
            "path": skill.path,
            "user_invocable": skill.user_invocable,
            "auto_activate": skill.auto_activate,
            "is_active": db_entry.is_active if db_entry else skill.auto_activate,
            "tools": skill.tools,
        })

    return results


@router.post("/api/chathub-agent/skills/{skill_name}/toggle")
async def toggle_skill(skill_name: str, request: Request, db: Session = Depends(get_db)):
    """Toggle a skill's activation status."""
    from app.database import ChatHubAgentSkill
    user = await get_current_user(request, None, db)
    data = await request.json()
    is_active = data.get("is_active", True)

    # Upsert skill status
    db_skill = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.user_id == user.id,
        ChatHubAgentSkill.name == skill_name,
    ).first()

    if db_skill:
        db_skill.is_active = is_active
        db_skill.updated_at = datetime.utcnow()
    else:
        db_skill = ChatHubAgentSkill(
            user_id=user.id,
            name=skill_name,
            tier="bundled",
            is_active=is_active,
        )
        db.add(db_skill)

    db.commit()
    return {"status": "ok", "is_active": is_active}


# ============================================================================
# WebSocket Streaming
# ============================================================================

@router.websocket("/api/chathub-agent/stream/{session_id}")
async def stream_session(websocket: WebSocket, session_id: int):
    """WebSocket for real-time session output streaming."""
    await websocket.accept()

    db = SessionLocal()
    try:
        user = await get_websocket_user(websocket, db)
        if not user:
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=4001, reason="Authentication required")
            return

        # Verify ownership
        from app.database import ChatHubAgentSession, ChatHubAgentMessage
        session = db.query(ChatHubAgentSession).filter(
            ChatHubAgentSession.id == session_id,
            ChatHubAgentSession.user_id == user.id,
        ).first()

        if not session:
            await websocket.send_json({"error": "Session not found"})
            await websocket.close(code=4004, reason="Session not found")
            return

        # Get active session
        active = chathub_agent_manager.get_active_session(user.id)
        if active and active.session_id == session_id:
            # Send buffered output first
            for data in active.output_buffer:
                await websocket.send_text(json.dumps(data))

            # Register for live broadcasts
            active.websockets.add(websocket)

            try:
                while active.is_running or (active.agent_loop and not active.agent_loop.is_waiting):
                    try:
                        msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                        if msg == "ping":
                            await websocket.send_text(json.dumps({"type": "pong"}))
                    except asyncio.TimeoutError:
                        try:
                            await websocket.send_text(json.dumps({"type": "ping"}))
                        except Exception:
                            break
                    except WebSocketDisconnect:
                        break

            except WebSocketDisconnect:
                pass
            finally:
                active.websockets.discard(websocket)
        else:
            # Session not active, send historical messages
            messages = db.query(ChatHubAgentMessage).filter(
                ChatHubAgentMessage.session_id == session_id
            ).order_by(ChatHubAgentMessage.created_at.asc()).all()

            for m in messages:
                try:
                    data = {
                        "type": m.message_type or m.role,
                        "role": m.role,
                        "content": m.content,
                        "tool_name": m.tool_name,
                        "tool_call_id": m.tool_call_id,
                    }
                    await websocket.send_text(json.dumps(data))
                except Exception:
                    pass

            await websocket.send_text(json.dumps({
                "type": "session_end",
                "status": session.status or "completed",
            }))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        db.close()
        try:
            await websocket.close()
        except Exception:
            pass
