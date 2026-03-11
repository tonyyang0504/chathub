"""
Tool Builder API Routes
Endpoints for creating, managing, and publishing custom tools via coding agents in git worktree sandboxes.
"""

import asyncio
import json
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List, Dict

from app.database import get_db, SessionLocal, ChatHubAgentSkill, CustomToolListing
from app.auth.utils import get_current_user_optional, get_websocket_user, decrypt_string
from .builder_manager import tool_builder_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools/api/tool-builder", tags=["Tool Builder"])


# ============================================================================
# Pydantic Models
# ============================================================================

class ToolSaveRequest(BaseModel):
    name: str
    display_name: str
    description: str
    icon: str = "bi-gear"
    gradient_start: str = "#6366f1"
    gradient_end: str = "#8b5cf6"
    skill_md_content: str


class ToolUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    gradient_start: Optional[str] = None
    gradient_end: Optional[str] = None
    skill_md_content: Optional[str] = None


class ToolPublishRequest(BaseModel):
    category: str = "automation"
    long_description: Optional[str] = None
    version: str = "1.0.0"


class SendMessageRequest(BaseModel):
    message: str


class CreateSessionRequest(BaseModel):
    provider: Optional[str] = None  # claude, codex, gemini — defaults to user's default_provider


class RunTestRequest(BaseModel):
    command: Optional[str] = None  # Custom test command, defaults to pytest


# ============================================================================
# Helper: Get API key from ClaudeCodeSettings (multi-provider)
# ============================================================================

def _get_claude_code_settings(user, db: Session, provider: str = None):
    """Get provider, API key, model from ClaudeCodeSettings."""
    from app.database import ClaudeCodeSettings

    settings = db.query(ClaudeCodeSettings).filter(
        ClaudeCodeSettings.user_id == user.id
    ).first()

    if not settings:
        return None, None, None, None, None

    # Determine provider
    selected_provider = provider or settings.default_provider or "claude"

    # Get API key and model based on provider
    api_key = None
    model = None
    auth_method = settings.auth_method or "api_key"
    oauth_token = None

    if selected_provider == "claude":
        if auth_method == "membership":
            oauth_token = decrypt_string(settings.oauth_token_encrypted) if settings.oauth_token_encrypted else None
            api_key = "membership"  # Placeholder, auth handled via oauth
        elif settings.anthropic_api_key_encrypted:
            api_key = decrypt_string(settings.anthropic_api_key_encrypted)
        model = settings.default_model or "sonnet"
    elif selected_provider == "codex":
        if settings.openai_api_key_encrypted:
            api_key = decrypt_string(settings.openai_api_key_encrypted)
        model = settings.codex_default_model or "gpt-5.3-codex"
    elif selected_provider == "gemini":
        if settings.gemini_api_key_encrypted:
            api_key = decrypt_string(settings.gemini_api_key_encrypted)
        model = settings.gemini_default_model or "auto-gemini-3"

    return selected_provider, api_key, model, auth_method, oauth_token


# ============================================================================
# Session Endpoints
# ============================================================================

@router.post("/sessions")
async def create_session(request: Request, data: CreateSessionRequest = None, db: Session = Depends(get_db)):
    """Create a new tool builder session with a worktree sandbox."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Handle both JSON body and empty POST
    provider_requested = data.provider if data else None

    provider, api_key, model, auth_method, oauth_token = _get_claude_code_settings(
        user, db, provider_requested
    )

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No API key configured for the selected provider. Please set up your API keys in Claude Code settings."
        )

    session = await tool_builder_manager.create_session(
        user_id=user.id,
        provider=provider,
        api_key=api_key,
        model=model,
        auth_method=auth_method,
        oauth_token=oauth_token,
        user_email=getattr(user, 'email', None),
        user_name=getattr(user, 'username', None) or getattr(user, 'name', None),
    )

    return {
        "session_id": session.session_id,
        "provider": provider,
        "model": model,
        "worktree_branch": session.worktree.branch if session.worktree else None,
    }


@router.post("/sessions/{session_id}/message")
async def send_message(
    request: Request,
    session_id: str,
    data: SendMessageRequest,
    db: Session = Depends(get_db)
):
    """Send a message to the builder agent."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if not data.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if session.is_running:
        raise HTTPException(status_code=409, detail="Agent is busy processing")

    # Run in background so we return immediately; WebSocket gets the response
    asyncio.create_task(tool_builder_manager.send_message(user.id, data.message))

    return {"status": "processing", "session_id": session_id}


@router.websocket("/stream/{session_id}")
async def stream_session(websocket: WebSocket, session_id: str):
    """WebSocket for real-time builder session output streaming."""
    await websocket.accept()

    # Check for no_replay query param
    query_string = str(websocket.scope.get("query_string", b""), "utf-8")
    no_replay = "no_replay=1" in query_string

    db = SessionLocal()
    try:
        user = await get_websocket_user(websocket, db)
        if not user:
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=4001, reason="Authentication required")
            return

        session = tool_builder_manager.get_session(user.id)
        if not session or session.session_id != session_id:
            await websocket.send_json({"error": "Session not found"})
            await websocket.close(code=4004, reason="Session not found")
            return

        # Replay buffer (unless no_replay)
        if not no_replay:
            for data in session.output_buffer:
                await websocket.send_text(json.dumps(data))

        # Register for live broadcasts
        session.websockets.add(websocket)

        try:
            while True:
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
            session.websockets.discard(websocket)
    finally:
        db.close()


# ============================================================================
# Build Status & Actions
# ============================================================================

@router.get("/sessions/{session_id}/status")
async def get_build_status(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Get worktree diff, changed files, branch name."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    return tool_builder_manager.get_build_status(user.id)


@router.post("/sessions/{session_id}/publish")
async def publish_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Merge worktree branch, create skill record, return skill ID."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.is_running:
        raise HTTPException(status_code=409, detail="Agent is still running. Wait for it to finish.")

    result = await tool_builder_manager.publish(user.id)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/sessions/{session_id}/discard")
async def discard_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Clean up worktree without merging."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    result = await tool_builder_manager.discard(user.id)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/sessions/{session_id}/test")
async def run_test(
    request: Request,
    session_id: str,
    data: RunTestRequest = None,
    db: Session = Depends(get_db)
):
    """Run a test command in the worktree."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    from .worktree_manager import worktree_manager

    command_str = (data.command if data and data.command else "pytest").strip()
    # Split into list for subprocess
    command = command_str.split()

    result = worktree_manager.run_command(session.session_id, command, timeout=120)

    return {
        "command": command_str,
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "returncode": result.get("returncode", -1),
        "success": result.get("returncode") == 0,
    }


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Stop the running agent process without discarding the worktree."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    stopped = await tool_builder_manager.stop_process(user.id)
    return {"stopped": stopped}


# ============================================================================
# Tool CRUD Endpoints (unchanged from original)
# ============================================================================

@router.post("/save")
async def save_tool(
    request: Request,
    data: ToolSaveRequest,
    db: Session = Depends(get_db)
):
    """Save a new custom tool from the builder."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not data.skill_md_content or len(data.skill_md_content.strip()) < 10:
        raise HTTPException(status_code=400, detail="Skill content is too short")

    existing = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.user_id == user.id,
        ChatHubAgentSkill.name == data.name
    ).first()

    if existing:
        existing.display_name = data.display_name
        existing.description = data.description
        existing.icon = data.icon
        existing.gradient_start = data.gradient_start
        existing.gradient_end = data.gradient_end
        existing.skill_md_content = data.skill_md_content
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        skill = existing
    else:
        skill = ChatHubAgentSkill(
            user_id=user.id,
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            icon=data.icon,
            gradient_start=data.gradient_start,
            gradient_end=data.gradient_end,
            skill_md_content=data.skill_md_content,
            is_active=True,
            tier="managed"
        )
        db.add(skill)
        db.commit()
        db.refresh(skill)

    return {
        "id": skill.id,
        "name": skill.name,
        "display_name": skill.display_name,
        "message": "Tool saved successfully"
    }


@router.get("/my-tools")
async def list_my_tools(request: Request, db: Session = Depends(get_db)):
    """List user's custom tools."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    skills = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.user_id == user.id
    ).order_by(ChatHubAgentSkill.created_at.desc()).all()

    return {
        "tools": [
            {
                "id": s.id,
                "name": s.name,
                "display_name": getattr(s, 'display_name', None) or s.name,
                "description": s.description,
                "icon": getattr(s, 'icon', None) or "bi-gear",
                "gradient_start": getattr(s, 'gradient_start', None) or "#6366f1",
                "gradient_end": getattr(s, 'gradient_end', None) or "#8b5cf6",
                "is_active": s.is_active,
                "tier": s.tier,
                "has_listing": s.listing is not None if hasattr(s, 'listing') else False,
                "listing_status": s.listing.status if (hasattr(s, 'listing') and s.listing) else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in skills
        ]
    }


@router.get("/tools/{tool_id}")
async def get_tool_detail(
    request: Request,
    tool_id: int,
    db: Session = Depends(get_db)
):
    """Get a specific custom tool's details."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    skill = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.id == tool_id,
        ChatHubAgentSkill.user_id == user.id
    ).first()

    if not skill:
        raise HTTPException(status_code=404, detail="Tool not found")

    return {
        "id": skill.id,
        "name": skill.name,
        "display_name": getattr(skill, 'display_name', None) or skill.name,
        "description": skill.description,
        "icon": getattr(skill, 'icon', None) or "bi-gear",
        "gradient_start": getattr(skill, 'gradient_start', None) or "#6366f1",
        "gradient_end": getattr(skill, 'gradient_end', None) or "#8b5cf6",
        "skill_md_content": getattr(skill, 'skill_md_content', None),
        "is_active": skill.is_active,
        "tier": skill.tier,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


@router.put("/tools/{tool_id}")
async def update_tool(
    request: Request,
    tool_id: int,
    data: ToolUpdateRequest,
    db: Session = Depends(get_db)
):
    """Update a custom tool."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    skill = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.id == tool_id,
        ChatHubAgentSkill.user_id == user.id
    ).first()

    if not skill:
        raise HTTPException(status_code=404, detail="Tool not found")

    if data.display_name is not None:
        skill.display_name = data.display_name
    if data.description is not None:
        skill.description = data.description
    if data.icon is not None:
        skill.icon = data.icon
    if data.gradient_start is not None:
        skill.gradient_start = data.gradient_start
    if data.gradient_end is not None:
        skill.gradient_end = data.gradient_end
    if data.skill_md_content is not None:
        skill.skill_md_content = data.skill_md_content

    skill.updated_at = datetime.utcnow()
    db.commit()

    return {"message": "Tool updated successfully"}


@router.delete("/tools/{tool_id}")
async def delete_tool(
    request: Request,
    tool_id: int,
    db: Session = Depends(get_db)
):
    """Delete a custom tool."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    skill = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.id == tool_id,
        ChatHubAgentSkill.user_id == user.id
    ).first()

    if not skill:
        raise HTTPException(status_code=404, detail="Tool not found")

    if skill.listing:
        db.delete(skill.listing)

    db.delete(skill)
    db.commit()

    return {"message": "Tool deleted successfully"}


@router.put("/tools/{tool_id}/toggle")
async def toggle_tool(
    request: Request,
    tool_id: int,
    db: Session = Depends(get_db)
):
    """Toggle a custom tool's active state."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    skill = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.id == tool_id,
        ChatHubAgentSkill.user_id == user.id
    ).first()

    if not skill:
        raise HTTPException(status_code=404, detail="Tool not found")

    skill.is_active = not skill.is_active
    skill.updated_at = datetime.utcnow()
    db.commit()

    return {
        "is_active": skill.is_active,
        "message": f"Tool {'activated' if skill.is_active else 'deactivated'}"
    }


@router.post("/tools/{tool_id}/publish")
async def publish_tool(
    request: Request,
    tool_id: int,
    data: ToolPublishRequest,
    db: Session = Depends(get_db)
):
    """Publish a custom tool to the marketplace."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    skill = db.query(ChatHubAgentSkill).filter(
        ChatHubAgentSkill.id == tool_id,
        ChatHubAgentSkill.user_id == user.id
    ).first()

    if not skill:
        raise HTTPException(status_code=404, detail="Tool not found")

    if not skill.skill_md_content:
        raise HTTPException(status_code=400, detail="Tool has no skill content")

    existing_listing = db.query(CustomToolListing).filter(
        CustomToolListing.skill_id == skill.id
    ).first()

    if existing_listing:
        existing_listing.display_name = skill.display_name
        existing_listing.description = skill.description
        existing_listing.long_description = data.long_description or skill.description
        existing_listing.category = data.category
        existing_listing.version = data.version
        existing_listing.icon = skill.icon
        existing_listing.gradient_start = skill.gradient_start
        existing_listing.gradient_end = skill.gradient_end
        existing_listing.skill_md_content = skill.skill_md_content
        existing_listing.status = "published"
        existing_listing.updated_at = datetime.utcnow()
        db.commit()
        listing = existing_listing
    else:
        name_taken = db.query(CustomToolListing).filter(
            CustomToolListing.name == skill.name
        ).first()
        if name_taken:
            raise HTTPException(status_code=400, detail="A tool with this name already exists in the marketplace")

        listing = CustomToolListing(
            author_id=user.id,
            skill_id=skill.id,
            name=skill.name,
            display_name=skill.display_name,
            description=skill.description,
            long_description=data.long_description or skill.description,
            category=data.category,
            version=data.version,
            icon=skill.icon,
            gradient_start=skill.gradient_start,
            gradient_end=skill.gradient_end,
            skill_md_content=skill.skill_md_content,
            status="published"
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)

    return {
        "listing_id": listing.id,
        "message": "Tool published to marketplace"
    }
