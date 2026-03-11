"""
ChatHub Agent Routes - HTTP + WebSocket endpoints for ChatHub Agent.
"""

import json
import re
import sys
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.auth.utils import (
    get_current_user, get_current_user_optional, get_websocket_user,
    encrypt_string, decrypt_string,
)
from .manager import chathub_agent_manager
from .schemas import (
    SessionCreate, MessageCreate, ApprovalRequest, SkillToggle,
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
            "approval_mode": "auto_approve_all",
            "auto_commit": True,
            "auto_backup_db": True,
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
        "approval_mode": settings.approval_mode or "auto_approve_all",
        "auto_commit": settings.auto_commit if settings.auto_commit is not None else True,
        "auto_backup_db": settings.auto_backup_db if settings.auto_backup_db is not None else True,
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

    for field in ["ai_provider", "default_model",
                  "approval_mode", "auto_commit", "auto_backup_db",
                  "workspace_path"]:
        if field in data:
            setattr(settings, field, data[field])

    db.commit()

    # Hot-reload settings into running session (if any)
    approval_mode = data.get("approval_mode") or settings.approval_mode or "auto_approve_all"
    config_updates = {
        "auto_approve_all": approval_mode == "auto_approve_all",
        "auto_approve_read": approval_mode in ("auto_approve_all", "auto_approve_reads"),
    }
    new_model = data.get("default_model") or settings.default_model
    if new_model:
        config_updates["model"] = new_model

    chathub_agent_manager.update_session_config(user.id, config_updates)

    # Also update the provider's model if it changed
    session = chathub_agent_manager.get_active_session(user.id)
    if session and session.agent_loop and new_model:
        session.agent_loop.provider.model = new_model

    return {"status": "ok"}


# ============================================================================
# File Upload & External Connectors
# ============================================================================

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "chathub_agent_uploads"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {
    # Images
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico',
    # Documents
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods',
    '.csv', '.tsv',
    # Code & text
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.scss', '.less',
    '.json', '.yaml', '.yml', '.toml', '.xml', '.md', '.txt', '.rst',
    '.sh', '.bash', '.zsh', '.bat', '.ps1',
    '.java', '.kt', '.go', '.rs', '.c', '.cpp', '.h', '.hpp', '.cs',
    '.rb', '.php', '.swift', '.r', '.sql', '.graphql',
    '.env', '.ini', '.cfg', '.conf', '.dockerfile',
    # Archives (for reference)
    '.zip', '.tar', '.gz',
    # Data
    '.log', '.jsonl', '.ndjson',
}


@router.post("/api/chathub-agent/upload")
async def upload_files(request: Request, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Upload files for ChatHub Agent to work with."""
    user = await get_current_user(request, None, db)

    user_upload_dir = UPLOAD_DIR / str(user.id)
    user_upload_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for file in files:
        # Validate extension
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS and ext != '':
            results.append({"filename": file.filename, "error": f"File type '{ext}' not allowed"})
            continue

        # Read and validate size
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            results.append({"filename": file.filename, "error": "File exceeds 10MB limit"})
            continue

        # Save with UUID prefix to avoid collisions
        safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename or 'file'}"
        file_path = user_upload_dir / safe_name
        file_path.write_bytes(content)

        results.append({
            "filename": file.filename,
            "saved_as": safe_name,
            "path": str(file_path.resolve()),
            "size": len(content),
            "type": ext or "unknown"
        })

    return {"files": results}


@router.post("/api/chathub-agent/fetch-url")
async def fetch_url(request: Request, db: Session = Depends(get_db)):
    """Download a file from a URL (Google Drive, Dropbox, raw URL) for ChatHub Agent."""
    user = await get_current_user(request, None, db)
    data = await request.json()
    url = data.get("url", "").strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Convert Google Drive share links to direct download
    gdrive_match = re.match(r'https://drive\.google\.com/file/d/([^/]+)', url)
    if gdrive_match:
        file_id = gdrive_match.group(1)
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
    elif 'drive.google.com' in url and 'id=' in url:
        file_id_match = re.search(r'id=([^&]+)', url)
        if file_id_match:
            url = f"https://drive.google.com/uc?export=download&id={file_id_match.group(1)}"

    # Convert Dropbox share links to direct download
    if 'dropbox.com' in url:
        url = url.replace('www.dropbox.com', 'dl.dropboxusercontent.com')
        if '?dl=0' in url:
            url = url.replace('?dl=0', '?dl=1')
        elif '?dl=1' not in url:
            url += ('&' if '?' in url else '?') + 'dl=1'

    user_upload_dir = UPLOAD_DIR / str(user.id)
    user_upload_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            if len(resp.content) > MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail="Downloaded file exceeds 10MB limit")

            # Determine filename from Content-Disposition or URL
            filename = "downloaded_file"
            cd = resp.headers.get("content-disposition", "")
            if "filename=" in cd:
                fn_match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
                if fn_match:
                    filename = fn_match.group(1).strip()
            else:
                url_path = url.split('?')[0].split('#')[0]
                url_filename = url_path.rstrip('/').split('/')[-1]
                if '.' in url_filename:
                    filename = url_filename

            safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
            file_path = user_upload_dir / safe_name
            file_path.write_bytes(resp.content)

            ext = Path(filename).suffix.lower()
            return {
                "file": {
                    "filename": filename,
                    "saved_as": safe_name,
                    "path": str(file_path.resolve()),
                    "size": len(resp.content),
                    "type": ext or "unknown"
                }
            }
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download: HTTP {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download: {str(e)}")


@router.delete("/api/chathub-agent/uploads")
async def clear_uploads(request: Request, db: Session = Depends(get_db)):
    """Clear all uploaded files for the current user."""
    import shutil
    user = await get_current_user(request, None, db)
    user_upload_dir = UPLOAD_DIR / str(user.id)
    if user_upload_dir.exists():
        shutil.rmtree(user_upload_dir, ignore_errors=True)
    return {"status": "ok"}


# ============================================================================
# Session API
# ============================================================================

@router.post("/api/chathub-agent/sessions")
async def create_session(request: Request, db: Session = Depends(get_db)):
    """Create a new ChatHub Agent session."""
    from app.database import ChatHubAgentSettings, ChatHubAgentSession, ChatHubAgentMessage
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip()
    file_paths = data.get("file_paths", [])

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Check for existing active session — auto-stop idle sessions
    active = chathub_agent_manager.get_active_session(user.id)
    if active:
        if active.is_waiting or (active.agent_loop and active.agent_loop.is_waiting):
            await chathub_agent_manager.stop_session(user.id)
        else:
            raise HTTPException(status_code=409, detail="You already have an active session")

    # Get settings
    settings = db.query(ChatHubAgentSettings).filter(
        ChatHubAgentSettings.user_id == user.id
    ).first()

    if not settings or not settings.api_key_encrypted:
        raise HTTPException(status_code=400, detail="Please configure your API key in Settings")

    api_key = decrypt_string(settings.api_key_encrypted)

    # Build agent config from settings defaults
    approval_mode = settings.approval_mode or "auto_approve_all"
    agent_config_dict = {
        "ai_provider": settings.ai_provider or "openai",
        "model": data.get("model") or settings.default_model or "gpt-4o",
        "system_prompt": None,
        "temperature": 0.3,
        "max_tokens": 8192,
        "allowed_tools": None,
        "dangerous_tools": ["exec_command", "write_file", "edit_file"],
        "auto_approve_all": approval_mode == "auto_approve_all",
        "auto_approve_read": approval_mode in ("auto_approve_all", "auto_approve_reads"),
        "workspace_path": settings.workspace_path,
        "enabled_skills": None,
    }

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

    # Inject user context so the agent knows who it's working for
    agent_config_dict["user_context"] = {
        "user_id": user.id,
        "email": user.email,
        "name": user.name or user.email,
    }

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
        file_paths=file_paths,
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
    ).order_by(func.coalesce(ChatHubAgentSession.ended_at, ChatHubAgentSession.created_at).desc()).limit(50).all()

    return [{
        "id": s.id,
        "title": s.title,
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

    # Check if the agent is waiting (turn complete) despite DB status being "running"
    active = chathub_agent_manager.get_active_session(user.id)
    if active and active.session_id == session.id:
        is_waiting = active.is_waiting
    elif session.status == "running":
        # No active session in memory but DB says running = stale/server restarted
        is_waiting = True
    else:
        is_waiting = False

    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "is_waiting": is_waiting,
        "prompt": session.prompt,
        "ai_provider": session.ai_provider,
        "model": session.model,
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


@router.delete("/api/chathub-agent/sessions/{session_id}")
async def delete_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a session and all its messages."""
    from app.database import ChatHubAgentSession, ChatHubAgentMessage
    user = await get_current_user(request, None, db)

    session = db.query(ChatHubAgentSession).filter(
        ChatHubAgentSession.id == session_id,
        ChatHubAgentSession.user_id == user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Stop if running
    active = chathub_agent_manager.get_active_session(user.id)
    if active and active.session_id == session_id:
        await chathub_agent_manager.stop_session(user.id)

    # Delete messages first, then session
    db.query(ChatHubAgentMessage).filter(
        ChatHubAgentMessage.session_id == session_id
    ).delete()
    db.delete(session)
    db.commit()

    return {"status": "ok"}


@router.post("/api/chathub-agent/sessions/{session_id}/message")
async def send_message(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Send a follow-up message to an active session."""
    from app.database import ChatHubAgentSession, ChatHubAgentMessage
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip() or data.get("content", "").strip()
    file_paths = data.get("file_paths", [])

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

    # Send via manager (pass session_id to prevent cross-session routing)
    sent = await chathub_agent_manager.send_message(user.id, prompt, session_id=session.id, file_paths=file_paths)
    if not sent:
        # Session not in memory — attempt to resume from DB
        from app.database import ChatHubAgentSettings
        settings = db.query(ChatHubAgentSettings).filter(
            ChatHubAgentSettings.user_id == user.id
        ).first()

        if not settings or not settings.api_key_encrypted:
            raise HTTPException(status_code=400, detail="Session is not ready for input")

        api_key = decrypt_string(settings.api_key_encrypted)

        # Build agent config
        approval_mode = settings.approval_mode or "auto_approve_all"
        agent_config_dict = {
            "ai_provider": session.ai_provider or settings.ai_provider or "openai",
            "model": session.model or settings.default_model or "gpt-4o",
            "system_prompt": None,
            "temperature": 0.3,
            "max_tokens": 8192,
            "allowed_tools": None,
            "dangerous_tools": ["exec_command", "write_file", "edit_file"],
            "auto_approve_all": approval_mode == "auto_approve_all",
            "auto_approve_read": approval_mode in ("auto_approve_all", "auto_approve_reads"),
            "workspace_path": settings.workspace_path,
            "enabled_skills": None,
        }

        agent_config_dict["user_context"] = {
            "user_id": user.id,
            "email": user.email,
            "name": user.name or user.email,
        }

        settings_dict = {
            "api_key": api_key,
            "ai_provider": agent_config_dict["ai_provider"],
            "default_model": agent_config_dict["model"],
            "workspace_path": agent_config_dict.get("workspace_path"),
        }

        # Stop any existing active session before resuming this one
        existing = chathub_agent_manager.get_active_session(user.id)
        if existing:
            await chathub_agent_manager.stop_session(user.id)

        resumed = await chathub_agent_manager.resume_session(
            user_id=user.id,
            session_id=session.id,
            settings_dict=settings_dict,
            agent_config_dict=agent_config_dict,
        )

        if not resumed:
            raise HTTPException(status_code=400, detail="Session is not ready for input")

        # Update DB session status back to running
        session.status = "running"
        db.commit()

        sent = await chathub_agent_manager.send_message(user.id, prompt, session_id=session.id, file_paths=file_paths)
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
async def stream_session(websocket: WebSocket, session_id: int, no_replay: int = 0):
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

        # Get active session (with retry for resume race condition)
        active = chathub_agent_manager.get_active_session(user.id)

        # If no_replay and session not yet active, wait briefly for resume to register
        if no_replay and (not active or active.session_id != session_id):
            for _ in range(10):  # Wait up to 5 seconds
                await asyncio.sleep(0.5)
                active = chathub_agent_manager.get_active_session(user.id)
                if active and active.session_id == session_id:
                    break

        if active and active.session_id == session_id:
            # Send buffered output first (skip if reconnecting after resume)
            if not no_replay:
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
            if no_replay:
                # Resume didn't activate in time, just close gracefully
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": "Session resume timed out. Please try again.",
                }))
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
