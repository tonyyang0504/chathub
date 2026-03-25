"""
AI Coder API Routes
Endpoints for creating, managing, and publishing codebase modifications via coding agents.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db, SessionLocal, CustomToolListing, AiWorkspaceSession, CodeModification
from app.auth.utils import get_current_user_optional, get_websocket_user, decrypt_string
from .coder_manager import ai_coder_manager
from .sandbox_manager import sandbox_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools/api/ai-coder", tags=["AI Coder"])


# ============================================================================
# Pydantic Models
# ============================================================================

class SendMessageRequest(BaseModel):
    message: str


class CreateSessionRequest(BaseModel):
    provider: Optional[str] = None


class PublishRequest(BaseModel):
    title: str = "AI Coder modification"
    description: Optional[str] = ""


class ShareRequest(BaseModel):
    category: str = "modification"
    version: str = "1.0.0"
    long_description: Optional[str] = None


# ============================================================================
# Helper: Get API key from AiWorkspaceSettings (multi-provider)
# ============================================================================

def _get_ai_workspace_settings(user, db: Session, provider: str = None):
    """Get provider, API key, model from AiWorkspaceSettings."""
    from app.database import AiWorkspaceSettings

    settings = db.query(AiWorkspaceSettings).filter(
        AiWorkspaceSettings.user_id == user.id
    ).first()

    if not settings:
        return None, None, None, None, None, None

    selected_provider = provider or settings.default_provider or "claude"

    api_key = None
    model = None
    auth_method = settings.auth_method or "api_key"
    oauth_token = None
    ai_provider = None

    if selected_provider == "claude":
        if auth_method == "membership":
            oauth_token = decrypt_string(settings.oauth_token_encrypted) if settings.oauth_token_encrypted else None
            api_key = "membership"
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
    elif selected_provider == "chathub":
        from app.database import ChatHubAgentSettings
        agent_settings = db.query(ChatHubAgentSettings).filter(
            ChatHubAgentSettings.user_id == user.id
        ).first()
        if agent_settings:
            ai_provider = agent_settings.ai_provider or "openai"
            if agent_settings.api_key_encrypted:
                api_key = decrypt_string(agent_settings.api_key_encrypted)
            model = settings.chathub_default_model if hasattr(settings, 'chathub_default_model') and settings.chathub_default_model else (agent_settings.default_model or "gpt-4o")
        else:
            ai_provider = "openai"
            if settings.openai_api_key_encrypted:
                api_key = decrypt_string(settings.openai_api_key_encrypted)
            model = settings.chathub_default_model if hasattr(settings, 'chathub_default_model') and settings.chathub_default_model else "gpt-4o"

    return selected_provider, api_key, model, auth_method, oauth_token, ai_provider


# ============================================================================
# Session Endpoints
# ============================================================================

@router.post("/sessions")
async def create_session(request: Request, data: CreateSessionRequest = None, db: Session = Depends(get_db)):
    """Create a new AI Coder session with a worktree sandbox."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    provider_requested = data.provider if data else None

    provider, api_key, model, auth_method, oauth_token, ai_provider = _get_ai_workspace_settings(
        user, db, provider_requested
    )

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No API key configured. Please set up your API keys in AI Workspace settings."
        )

    try:
        session = await ai_coder_manager.create_session(
            user_id=user.id,
            provider=provider,
            api_key=api_key,
            model=model,
            auth_method=auth_method,
            oauth_token=oauth_token,
            user_email=getattr(user, 'email', None),
            user_name=getattr(user, 'username', None) or getattr(user, 'name', None),
            ai_provider=ai_provider,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {
        "session_id": session.session_id,
        "provider": provider,
        "model": model,
        "worktree_branch": session.sandbox_info.branch if session.sandbox_info else None,
        "preview_url": session.sandbox_info.preview_url if session.sandbox_info else None,
        "preview_port": session.sandbox_info.preview_port if session.sandbox_info else None,
    }


@router.get("/sessions")
async def list_sessions(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    sessions = db.query(AiWorkspaceSession).filter(
        AiWorkspaceSession.user_id == user.id,
        AiWorkspaceSession.session_type == "ai_coder"
    ).order_by(AiWorkspaceSession.created_at.desc()).all()
    active = ai_coder_manager.get_session(user.id)
    return [{"db_id": s.id, "title": s.title or "[AI Coder Session]",
             "status": s.status, "provider": s.provider,
             "created_at": s.created_at.isoformat() if s.created_at else None,
             "published": s.status == "published",
             "active_session_id": active.session_id if active and active.db_session_id == s.id else None}
            for s in sessions]


@router.get("/sessions/{db_id}")
async def get_session_detail(db_id: int, request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    session = db.query(AiWorkspaceSession).filter(
        AiWorkspaceSession.id == db_id,
        AiWorkspaceSession.user_id == user.id,
        AiWorkspaceSession.session_type == "ai_coder"
    ).first()
    if not session:
        raise HTTPException(status_code=404)
    from app.database import AiWorkspaceMessage
    messages = db.query(AiWorkspaceMessage).filter(
        AiWorkspaceMessage.session_id == db_id
    ).order_by(AiWorkspaceMessage.created_at.asc()).all()
    active = ai_coder_manager.get_session(user.id)
    return {
        "db_id": session.id, "title": session.title or "[AI Coder Session]",
        "status": session.status, "provider": session.provider,
        "published": session.status == "published",
        "active_session_id": active.session_id if active and active.db_session_id == db_id else None,
        "messages": [{"id": m.id, "role": m.role, "content": m.content,
                      "message_type": m.message_type, "event_data": m.event_data,
                      "created_at": m.created_at.isoformat()} for m in messages]
    }


@router.delete("/sessions/{db_id}")
async def delete_session(db_id: int, request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    session = db.query(AiWorkspaceSession).filter(
        AiWorkspaceSession.id == db_id,
        AiWorkspaceSession.user_id == user.id,
        AiWorkspaceSession.session_type == "ai_coder"
    ).first()
    if not session:
        raise HTTPException(status_code=404)
    active = ai_coder_manager.get_session(user.id)
    if active and active.db_session_id == db_id:
        await ai_coder_manager.discard(user.id)
    from app.database import AiWorkspaceMessage
    db.query(AiWorkspaceMessage).filter(AiWorkspaceMessage.session_id == db_id).delete()
    db.delete(session)
    db.commit()
    return {"success": True}


@router.post("/sessions/{db_id}/resume")
async def resume_session(db_id: int, request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    db_session = db.query(AiWorkspaceSession).filter(
        AiWorkspaceSession.id == db_id,
        AiWorkspaceSession.user_id == user.id,
        AiWorkspaceSession.session_type == "ai_coder"
    ).first()
    if not db_session:
        raise HTTPException(status_code=404)
    provider, api_key, model, auth_method, oauth_token, ai_provider = \
        _get_ai_workspace_settings(user, db, db_session.provider)
    if not api_key:
        raise HTTPException(status_code=400, detail="No API key configured")
    try:
        session = await ai_coder_manager.resume_session(
            db_id=db_id, user_id=user.id, provider=provider, api_key=api_key,
            model=model, auth_method=auth_method, oauth_token=oauth_token,
            user_email=getattr(user, 'email', None),
            user_name=getattr(user, 'username', None),
            ai_provider=ai_provider,
            claude_session_uuid=db_session.claude_session_uuid
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"session_id": session.session_id, "db_id": db_id}


@router.post("/sessions/{session_id}/message")
async def send_message(
    request: Request,
    session_id: str,
    data: SendMessageRequest,
    db: Session = Depends(get_db)
):
    """Send a message to the AI Coder agent."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if not data.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if session.is_running:
        raise HTTPException(status_code=409, detail="Agent is busy processing")

    asyncio.create_task(ai_coder_manager.send_message(user.id, data.message))

    return {"status": "processing", "session_id": session_id}


@router.websocket("/stream/{session_id}")
async def stream_session(websocket: WebSocket, session_id: str):
    """WebSocket for real-time AI Coder session output streaming."""
    await websocket.accept()

    query_string = str(websocket.scope.get("query_string", b""), "utf-8")
    no_replay = "no_replay=1" in query_string

    db = SessionLocal()
    try:
        user = await get_websocket_user(websocket, db)
        if not user:
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=4001, reason="Authentication required")
            return

        session = ai_coder_manager.get_session(user.id)
        if not session or session.session_id != session_id:
            await websocket.send_json({"type": "session_expired", "error": "Session not found"})
            await websocket.close(code=4004, reason="Session not found")
            return

        if not no_replay:
            for data in session.output_buffer:
                await websocket.send_text(json.dumps(data))

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
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404)

    status = ai_coder_manager.get_build_status(user.id)
    if session.sandbox_info:
        status.setdefault("preview_url", session.sandbox_info.preview_url)
        status.setdefault("preview_port", session.sandbox_info.preview_port)
        status.setdefault("container_status", "")
    return status


@router.post("/sessions/{session_id}/preview")
async def start_preview(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: sandbox_manager.start_preview(session_id)
        )
        return {"preview_url": result.preview_url, "preview_port": result.preview_port}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/sessions/{session_id}/publish")
async def publish_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Merge worktree branch, create CodeModification record."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404)

    if session.is_running:
        raise HTTPException(status_code=409, detail="Agent is still running")

    metadata = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            metadata = body
    except Exception:
        pass

    result = await ai_coder_manager.publish(user.id, metadata=metadata)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/sessions/{session_id}/discard")
async def discard_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404)

    result = await ai_coder_manager.discard(user.id)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404)

    stopped = await ai_coder_manager.stop_process(user.id)
    return {"stopped": stopped}


@router.get("/sessions/{session_id}/mod-summary")
async def get_mod_summary(request: Request, session_id: str, db: Session = Depends(get_db)):
    """Read MOD.md from worktree and return parsed title + description."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    session = ai_coder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404)
    return ai_coder_manager.get_mod_summary(user.id)


# ============================================================================
# Modifications Endpoints
# ============================================================================

@router.get("/modifications")
async def list_modifications(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    return {"modifications": ai_coder_manager.get_modifications(user.id)}


@router.post("/modifications/{mod_id}/revert")
async def revert_modification(mod_id: int, request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    result = await ai_coder_manager.revert(mod_id, user.id)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/modifications/{mod_id}/share")
async def share_modification(mod_id: int, request: Request, data: ShareRequest = None, db: Session = Depends(get_db)):
    """Share a modification to the marketplace."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    mod = db.query(CodeModification).filter(
        CodeModification.id == mod_id,
        CodeModification.user_id == user.id,
    ).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Modification not found")

    # Check if already shared
    existing = db.query(CustomToolListing).filter(
        CustomToolListing.name == f"mod-{mod.id}"
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already shared")

    listing = CustomToolListing(
        author_id=user.id,
        tool_id=None,
        name=f"mod-{mod.id}",
        display_name=mod.title,
        description=mod.description or mod.title,
        long_description=(data.long_description if data else None) or mod.description,
        category=(data.category if data else None) or "modification",
        version=(data.version if data else None) or "1.0.0",
        icon="bi-cpu",
        listing_type="mod",
        status="published",
        tool_md_content=json.dumps({
            "commit_hash": mod.commit_hash,
            "files_changed": json.loads(mod.files_changed) if mod.files_changed else [],
        }),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)

    return {"listing_id": listing.id, "message": "Modification shared to marketplace"}


# ============================================================================
# Modifications Page & Detail
# ============================================================================

@router.get("/modifications/page", response_class=HTMLResponse)
async def modifications_page(request: Request, db: Session = Depends(get_db)):
    """Full modifications history page."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)

    from fastapi.templating import Jinja2Templates
    from pathlib import Path
    _base = Path(__file__).resolve().parent.parent
    templates = Jinja2Templates(directory=str(_base / "templates"))
    return templates.TemplateResponse(
        "dashboard/tools/ai_coder_modifications.html",
        {"request": request, "user": user, "active_page": "tools_ai_coder", "page_title": "AI Coder - Modifications"},
    )


@router.get("/modifications/{mod_id}")
async def get_modification_detail(mod_id: int, request: Request, db: Session = Depends(get_db)):
    """Get full details of a single modification."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    mod = db.query(CodeModification).filter(
        CodeModification.id == mod_id,
        CodeModification.user_id == user.id,
    ).first()
    if not mod:
        raise HTTPException(status_code=404)

    files = []
    if mod.files_changed:
        try:
            files = json.loads(mod.files_changed)
        except Exception:
            pass

    return {
        "id": mod.id,
        "title": mod.title,
        "description": mod.description,
        "commit_hash": mod.commit_hash,
        "revert_commit_hash": mod.revert_commit_hash,
        "files_changed": files,
        "files_count": len(files),
        "status": mod.status,
        "published_at": mod.published_at.isoformat() if mod.published_at else None,
        "reverted_at": mod.reverted_at.isoformat() if mod.reverted_at else None,
        "session_id": mod.session_id,
    }


# ============================================================================
# Restart Endpoint
# ============================================================================

@router.post("/restart")
async def restart_server(request: Request, db: Session = Depends(get_db)):
    """Restart the server process so code modifications take effect."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    def _do_restart():
        import time
        time.sleep(1.5)
        if getattr(sys, 'frozen', False):
            os.execv(sys.executable, [sys.executable])
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"success": True, "message": "Server restarting..."}
