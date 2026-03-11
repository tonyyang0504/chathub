"""
Claude Code Routes - HTTP + WebSocket endpoints for Claude Code CLI integration
"""

import json
import os
import re
import sys
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import (
    get_db, SessionLocal, ClaudeCodeSession, ClaudeCodeMessage, ClaudeCodeSettings
)
from app.auth.utils import get_current_user, get_current_user_optional, get_websocket_user, encrypt_string, decrypt_string
from .manager import claude_code_manager
from app.tools.monitoring import ToolMonitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["Claude Code"])

# Template setup
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys._MEIPASS)
else:
    _BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "app" / "templates"))


# ============================================================================
# Page Route
# ============================================================================

@router.get("/claude-code", response_class=HTMLResponse)
async def claude_code_page(request: Request, db: Session = Depends(get_db)):
    """Claude Code tool page."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse(
        "dashboard/tools/claude_code.html",
        {
            "request": request,
            "user": user,
            "active_page": "tools_claude_code",
            "page_title": "Claude Code"
        }
    )


# ============================================================================
# Settings API
# ============================================================================

@router.get("/api/claude-code/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Get Claude Code settings (API key masked)."""
    user = await get_current_user(request, None, db)
    settings = db.query(ClaudeCodeSettings).filter(
        ClaudeCodeSettings.user_id == user.id
    ).first()

    if not settings:
        return {
            "api_key_set": False,
            "default_model": "sonnet",
            "auto_commit": True,
            "auto_backup_db": True,
            "auth_method": "api_key",
            "default_provider": "claude",
            "openai_api_key_set": False,
            "gemini_api_key_set": False,
            "codex_default_model": "gpt-5.3-codex",
            "gemini_default_model": "auto-gemini-3",
        }

    return {
        "api_key_set": bool(settings.anthropic_api_key_encrypted),
        "api_key_masked": "****" + decrypt_string(settings.anthropic_api_key_encrypted)[-4:] if settings.anthropic_api_key_encrypted else "",
        "default_model": settings.default_model or "sonnet",
        "auto_commit": settings.auto_commit if settings.auto_commit is not None else True,
        "auto_backup_db": settings.auto_backup_db if settings.auto_backup_db is not None else True,
        "auto_approve_scope": settings.auto_approve_scope if settings.auto_approve_scope is not None else False,
        "auth_method": settings.auth_method or "api_key",
        # Multi-provider fields
        "default_provider": settings.default_provider or "claude",
        "openai_api_key_set": bool(settings.openai_api_key_encrypted),
        "openai_api_key_masked": "****" + decrypt_string(settings.openai_api_key_encrypted)[-4:] if settings.openai_api_key_encrypted else "",
        "gemini_api_key_set": bool(settings.gemini_api_key_encrypted),
        "gemini_api_key_masked": "****" + decrypt_string(settings.gemini_api_key_encrypted)[-4:] if settings.gemini_api_key_encrypted else "",
        "codex_default_model": settings.codex_default_model or "gpt-5.3-codex",
        "gemini_default_model": settings.gemini_default_model or "auto-gemini-3",
    }


@router.put("/api/claude-code/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    """Save Claude Code settings."""
    user = await get_current_user(request, None, db)
    data = await request.json()

    settings = db.query(ClaudeCodeSettings).filter(
        ClaudeCodeSettings.user_id == user.id
    ).first()

    if not settings:
        settings = ClaudeCodeSettings(user_id=user.id)
        db.add(settings)

    # Only update API key if provided (non-empty)
    api_key = data.get("anthropic_api_key")
    if api_key and api_key.strip() and not api_key.startswith("****"):
        settings.anthropic_api_key_encrypted = encrypt_string(api_key.strip())

    if "default_model" in data:
        settings.default_model = data["default_model"]
    if "auto_commit" in data:
        settings.auto_commit = data["auto_commit"]
    if "auto_backup_db" in data:
        settings.auto_backup_db = data["auto_backup_db"]
    if "auto_approve_scope" in data:
        settings.auto_approve_scope = data["auto_approve_scope"]
    if "auth_method" in data and data["auth_method"] in ("api_key", "membership"):
        settings.auth_method = data["auth_method"]

    # Multi-provider fields
    openai_key = data.get("openai_api_key")
    if openai_key and openai_key.strip() and not openai_key.startswith("****"):
        settings.openai_api_key_encrypted = encrypt_string(openai_key.strip())

    gemini_key = data.get("gemini_api_key")
    if gemini_key and gemini_key.strip() and not gemini_key.startswith("****"):
        settings.gemini_api_key_encrypted = encrypt_string(gemini_key.strip())

    if "codex_default_model" in data:
        settings.codex_default_model = data["codex_default_model"]
    if "gemini_default_model" in data:
        settings.gemini_default_model = data["gemini_default_model"]
    if "default_provider" in data and data["default_provider"] in ("claude", "codex", "gemini"):
        settings.default_provider = data["default_provider"]

    db.commit()
    return {"status": "ok"}


# ============================================================================
# Membership Auth API
# ============================================================================

@router.get("/api/claude-code/membership-status")
async def membership_status(request: Request, db: Session = Depends(get_db)):
    """Check Claude membership auth status."""
    await get_current_user(request, None, db)
    result = await claude_code_manager.check_membership_status()
    return result


@router.post("/api/claude-code/membership-login")
async def membership_login(request: Request, db: Session = Depends(get_db)):
    """Trigger Claude membership login (opens browser)."""
    await get_current_user(request, None, db)
    result = await claude_code_manager.trigger_membership_login()
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.post("/api/claude-code/membership-login-code")
async def membership_login_code(request: Request, db: Session = Depends(get_db)):
    """Submit OAuth authorization code to the running login process."""
    await get_current_user(request, None, db)
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code is required")
    result = await claude_code_manager.submit_login_code(code)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/api/claude-code/membership-logout")
async def membership_logout(request: Request, db: Session = Depends(get_db)):
    """Trigger Claude membership logout."""
    await get_current_user(request, None, db)
    result = await claude_code_manager.trigger_membership_logout()
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    return result


# ============================================================================
# File Upload & External Connectors
# ============================================================================

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "claude_code_uploads"
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


@router.post("/api/claude-code/upload")
async def upload_files(request: Request, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Upload files for Claude Code to work with."""
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


@router.post("/api/claude-code/fetch-url")
async def fetch_url(request: Request, db: Session = Depends(get_db)):
    """Download a file from a URL (Google Drive, Dropbox, raw URL) for Claude Code."""
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


@router.delete("/api/claude-code/uploads")
async def clear_uploads(request: Request, db: Session = Depends(get_db)):
    """Clear all uploaded files for the current user."""
    user = await get_current_user(request, None, db)
    user_upload_dir = UPLOAD_DIR / str(user.id)
    if user_upload_dir.exists():
        import shutil
        shutil.rmtree(user_upload_dir, ignore_errors=True)
    return {"status": "ok"}


# ============================================================================
# Session API
# ============================================================================

@router.post("/api/claude-code/sessions")
async def create_session(request: Request, db: Session = Depends(get_db)):
    """Create a new Claude Code session."""
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Auto-stop any existing session (running or waiting) before creating a new one
    active = claude_code_manager.get_active_session(user.id)
    if active:
        await claude_code_manager.stop_session(user.id)

    # Get settings
    settings = db.query(ClaudeCodeSettings).filter(
        ClaudeCodeSettings.user_id == user.id
    ).first()

    # Determine provider
    provider = data.get("provider") or (settings.default_provider if settings else "claude") or "claude"

    auth_method = (settings.auth_method if settings else None) or "api_key"

    # Get API key based on provider
    oauth_token = None
    if provider == "claude":
        if auth_method == "api_key":
            if not settings or not settings.anthropic_api_key_encrypted:
                raise HTTPException(status_code=400, detail="Please configure your Anthropic API key in Settings")
            api_key = decrypt_string(settings.anthropic_api_key_encrypted)
        else:
            api_key = ""  # Membership mode — CLI uses stored credentials
            if settings and settings.oauth_token_encrypted:
                oauth_token = decrypt_string(settings.oauth_token_encrypted)
    elif provider == "codex":
        if not settings or not settings.openai_api_key_encrypted:
            raise HTTPException(status_code=400, detail="Please configure your OpenAI API key in Settings")
        api_key = decrypt_string(settings.openai_api_key_encrypted)
    elif provider == "gemini":
        if not settings or not settings.gemini_api_key_encrypted:
            raise HTTPException(status_code=400, detail="Please configure your Gemini API key in Settings")
        api_key = decrypt_string(settings.gemini_api_key_encrypted)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    # Determine model based on provider
    if provider == "claude":
        model = data.get("model") or (settings.default_model if settings else "sonnet")
    elif provider == "codex":
        model = data.get("model") or (settings.codex_default_model if settings else "gpt-5.3-codex")
    elif provider == "gemini":
        model = data.get("model") or (settings.gemini_default_model if settings else "auto-gemini-3")

    # Safety commit and backup
    git_hash = None
    db_backup_path = None

    if settings.auto_commit:
        git_hash = claude_code_manager.create_safety_commit()

    if settings.auto_backup_db:
        db_backup_path = claude_code_manager.create_db_backup()

    # Create session record
    session = ClaudeCodeSession(
        user_id=user.id,
        prompt=prompt,
        git_commit_hash=git_hash,
        db_backup_path=db_backup_path,
        model=model,
        provider=provider,
        status="pending"
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Also add the user prompt as a message
    user_msg = ClaudeCodeMessage(
        session_id=session.id,
        role="user",
        content=prompt,
        message_type="text"
    )
    db.add(user_msg)
    db.commit()

    # Get attached file paths
    file_paths = data.get("file_paths", [])

    # Spawn CLI subprocess
    active_session = await claude_code_manager.start_session(
        user_id=user.id,
        session_id=session.id,
        prompt=prompt,
        api_key=api_key,
        model=model,
        auth_method=auth_method,
        oauth_token=oauth_token,
        user_email=user.email,
        user_name=user.name,
        file_paths=file_paths,
        provider=provider
    )

    if not active_session:
        session.status = "failed"
        db.commit()
        from .providers import get_provider
        prov = get_provider(provider)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start {prov.display_name} CLI. Ensure '{prov.name}' is installed."
        )

    session.status = "running"
    session.pid = active_session.process.pid if active_session.process else None
    session.started_at = datetime.utcnow()
    db.commit()

    # Log to tool monitor
    try:
        ToolMonitor.log_execution(
            db=db,
            tool_type="claude_code",
            operation="session_start",
            input_data={"prompt": prompt[:200], "model": model},
            output_data={"session_id": session.id, "git_hash": git_hash},
            user_id=user.id
        )
    except Exception:
        pass

    return {
        "session_id": session.id,
        "status": "running",
        "pid": active_session.process.pid,
        "git_commit_hash": git_hash,
        "db_backup_path": db_backup_path
    }


@router.get("/api/claude-code/active-session")
async def get_active_session(request: Request, db: Session = Depends(get_db)):
    """Check if user has an active (running or waiting) session."""
    user = await get_current_user(request, None, db)
    active = claude_code_manager.get_active_session(user.id)
    if active:
        return {
            "active": True,
            "session_id": active.session_id,
            "is_running": active.is_running,
            "is_waiting": active.is_waiting
        }
    return {"active": False}


@router.get("/api/claude-code/sessions")
async def list_sessions(request: Request, db: Session = Depends(get_db)):
    """List user's Claude Code sessions."""
    user = await get_current_user(request, None, db)
    sessions = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.user_id == user.id
    ).order_by(ClaudeCodeSession.created_at.desc()).limit(50).all()

    return [{
        "id": s.id,
        "title": s.title,
        "status": s.status,
        "prompt": s.prompt[:100] if s.prompt else "",
        "model": s.model,
        "provider": s.provider or "claude",
        "git_commit_hash": s.git_commit_hash,
        "rolled_back": s.rolled_back,
        "created_at": (s.created_at.isoformat() + "Z") if s.created_at else None,
        "ended_at": (s.ended_at.isoformat() + "Z") if s.ended_at else None
    } for s in sessions]


@router.get("/api/claude-code/sessions/{session_id}")
async def get_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Get a session with its messages."""
    user = await get_current_user(request, None, db)
    session = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.id == session_id,
        ClaudeCodeSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = db.query(ClaudeCodeMessage).filter(
        ClaudeCodeMessage.session_id == session_id
    ).order_by(ClaudeCodeMessage.created_at.asc(), ClaudeCodeMessage.id.asc()).all()

    # Check if this session is actively waiting for follow-up
    active = claude_code_manager.get_active_session(user.id)
    session_is_waiting = bool(
        active and active.session_id == session.id and active.is_waiting and not active.is_running
    )

    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "prompt": session.prompt,
        "model": session.model,
        "provider": session.provider or "claude",
        "git_commit_hash": session.git_commit_hash,
        "db_backup_path": session.db_backup_path,
        "rolled_back": session.rolled_back,
        "resumable": bool(session.claude_session_uuid) and session.status in ("stopped", "completed", "failed"),
        "is_waiting": session_is_waiting,
        "created_at": (session.created_at.isoformat() + "Z") if session.created_at else None,
        "started_at": (session.started_at.isoformat() + "Z") if session.started_at else None,
        "ended_at": (session.ended_at.isoformat() + "Z") if session.ended_at else None,
        "messages": [{
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "message_type": m.message_type,
            "event_data": m.event_data,
            "created_at": (m.created_at.isoformat() + "Z") if m.created_at else None
        } for m in messages if not (m.message_type == 'system' and m.event_data and '"subtype": "init"' in m.event_data)]
    }


@router.delete("/api/claude-code/sessions/{session_id}")
async def delete_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a session and its messages."""
    user = await get_current_user(request, None, db)
    session = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.id == session_id,
        ClaudeCodeSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # If session is running, stop it first
    if session.status in ("running", "pending"):
        await claude_code_manager.stop_session(user.id)
        session.status = "stopped"
        session.ended_at = datetime.utcnow()

    # Delete messages first, then session
    db.query(ClaudeCodeMessage).filter(ClaudeCodeMessage.session_id == session_id).delete()
    db.delete(session)
    db.commit()

    return {"status": "ok"}


@router.post("/api/claude-code/sessions/{session_id}/resume")
async def resume_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Resume a stopped/completed session with a new prompt."""
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Check for existing active session
    active = claude_code_manager.get_active_session(user.id)
    if active:
        if active.is_waiting and not active.is_running:
            # Clean up idle waiting session
            try:
                db_old = db.query(ClaudeCodeSession).filter(
                    ClaudeCodeSession.id == active.session_id
                ).first()
                if db_old and db_old.status == "running":
                    db_old.status = "completed"
                    db_old.ended_at = datetime.utcnow()
                    db.commit()
            except Exception:
                pass
            if user.id in claude_code_manager._sessions:
                del claude_code_manager._sessions[user.id]
        else:
            raise HTTPException(status_code=409, detail="You already have an active session")

    # Verify the old session belongs to user and is resumable
    old_session = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.id == session_id,
        ClaudeCodeSession.user_id == user.id
    ).first()

    if not old_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if old_session.status not in ("stopped", "completed", "failed"):
        raise HTTPException(status_code=400, detail=f"Cannot resume session with status '{old_session.status}'")

    if not old_session.claude_session_uuid:
        raise HTTPException(status_code=400, detail="Session does not have a CLI session UUID (created before resume support)")

    # Get settings
    settings = db.query(ClaudeCodeSettings).filter(
        ClaudeCodeSettings.user_id == user.id
    ).first()

    # Determine provider from the old session
    provider = old_session.provider or "claude"
    auth_method = (settings.auth_method if settings else None) or "api_key"

    # Get API key based on provider
    oauth_token = None
    if provider == "claude":
        if auth_method == "api_key":
            if not settings or not settings.anthropic_api_key_encrypted:
                raise HTTPException(status_code=400, detail="Please configure your Anthropic API key in Settings")
            api_key = decrypt_string(settings.anthropic_api_key_encrypted)
        else:
            api_key = ""
            if settings and settings.oauth_token_encrypted:
                oauth_token = decrypt_string(settings.oauth_token_encrypted)
    elif provider == "codex":
        if not settings or not settings.openai_api_key_encrypted:
            raise HTTPException(status_code=400, detail="Please configure your OpenAI API key in Settings")
        api_key = decrypt_string(settings.openai_api_key_encrypted)
    elif provider == "gemini":
        if not settings or not settings.gemini_api_key_encrypted:
            raise HTTPException(status_code=400, detail="Please configure your Gemini API key in Settings")
        api_key = decrypt_string(settings.gemini_api_key_encrypted)
    else:
        api_key = ""

    if provider == "codex":
        model = data.get("model") or (settings.codex_default_model if settings else "gpt-5.3-codex")
    elif provider == "gemini":
        model = data.get("model") or (settings.gemini_default_model if settings else "auto-gemini-3")
    else:
        model = data.get("model") or (settings.default_model if settings else "sonnet")

    # Safety commit and backup
    git_hash = None
    db_backup_path = None

    if settings and settings.auto_commit:
        git_hash = claude_code_manager.create_safety_commit()

    if settings and settings.auto_backup_db:
        db_backup_path = claude_code_manager.create_db_backup()

    # Reuse the existing session — update it back to running
    old_session.status = "pending"
    old_session.git_commit_hash = git_hash
    old_session.db_backup_path = db_backup_path
    old_session.ended_at = None
    db.commit()

    # Add user prompt as a message to the SAME session
    user_msg = ClaudeCodeMessage(
        session_id=old_session.id,
        role="user",
        content=prompt,
        message_type="text"
    )
    db.add(user_msg)
    db.commit()

    # Get attached file paths
    file_paths = data.get("file_paths", [])

    # Spawn CLI subprocess with --resume
    active_session = await claude_code_manager.resume_session(
        user_id=user.id,
        session_id=session_id,
        prompt=prompt,
        api_key=api_key,
        model=model,
        auth_method=auth_method,
        oauth_token=oauth_token,
        user_email=user.email,
        user_name=user.name,
        claude_session_uuid=old_session.claude_session_uuid,
        file_paths=file_paths,
        provider=provider
    )

    if not active_session:
        old_session.status = "failed"
        db.commit()
        raise HTTPException(status_code=500, detail="Failed to resume Claude Code session")

    old_session.status = "running"
    old_session.pid = active_session.process.pid if active_session.process else None
    old_session.started_at = datetime.utcnow()
    db.commit()

    # Log to tool monitor
    try:
        ToolMonitor.log_execution(
            db=db,
            tool_type="claude_code",
            operation="session_resume",
            input_data={"prompt": prompt[:200], "model": model},
            output_data={"session_id": old_session.id, "git_hash": git_hash},
            user_id=user.id
        )
    except Exception:
        pass

    return {
        "session_id": old_session.id,
        "status": "running",
        "pid": active_session.process.pid,
        "git_commit_hash": git_hash,
        "db_backup_path": db_backup_path
    }


@router.post("/api/claude-code/sessions/{session_id}/message")
async def send_message(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Send a follow-up message to an active session."""
    user = await get_current_user(request, None, db)
    data = await request.json()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # Verify ownership
    session = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.id == session_id,
        ClaudeCodeSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Persist the user message
    user_msg = ClaudeCodeMessage(
        session_id=session.id,
        role="user",
        content=prompt,
        message_type="text"
    )
    db.add(user_msg)
    db.commit()

    # Get attached file paths
    file_paths = data.get("file_paths", [])

    # Send via stdin
    sent = await claude_code_manager.send_message(user.id, prompt, file_paths=file_paths)
    if not sent:
        # Check if session is actively running in memory (not just stale)
        active = claude_code_manager.get_active_session(user.id)
        if active and active.is_running:
            raise HTTPException(status_code=409, detail="Session is currently processing. Please wait.")

        # Session not in memory — attempt to auto-resume (e.g., after server restart)
        if not session.claude_session_uuid:
            raise HTTPException(status_code=400, detail="Session is not ready for input")

        settings = db.query(ClaudeCodeSettings).filter(
            ClaudeCodeSettings.user_id == user.id
        ).first()

        # Determine provider from session
        msg_provider = session.provider or "claude"
        auth_method = (settings.auth_method if settings else None) or "api_key"
        oauth_token = None

        if msg_provider == "claude":
            if auth_method == "api_key":
                if not settings or not settings.anthropic_api_key_encrypted:
                    raise HTTPException(status_code=400, detail="Please configure your Anthropic API key in Settings")
                api_key = decrypt_string(settings.anthropic_api_key_encrypted)
            else:
                api_key = ""
                if settings and settings.oauth_token_encrypted:
                    oauth_token = decrypt_string(settings.oauth_token_encrypted)
        elif msg_provider == "codex":
            if not settings or not settings.openai_api_key_encrypted:
                raise HTTPException(status_code=400, detail="Please configure your OpenAI API key in Settings")
            api_key = decrypt_string(settings.openai_api_key_encrypted)
        elif msg_provider == "gemini":
            if not settings or not settings.gemini_api_key_encrypted:
                raise HTTPException(status_code=400, detail="Please configure your Gemini API key in Settings")
            api_key = decrypt_string(settings.gemini_api_key_encrypted)
        else:
            api_key = ""

        model = session.model or (settings.default_model if settings else "sonnet")

        # Mark session as running again
        session.status = "running"
        session.ended_at = None
        db.commit()

        # Resume CLI subprocess with --resume
        active_session = await claude_code_manager.resume_session(
            user_id=user.id,
            session_id=session.id,
            prompt=prompt,
            api_key=api_key,
            model=model,
            auth_method=auth_method,
            oauth_token=oauth_token,
            user_email=user.email,
            user_name=user.name,
            claude_session_uuid=session.claude_session_uuid,
            file_paths=file_paths,
            provider=msg_provider
        )

        if not active_session:
            session.status = "failed"
            db.commit()
            raise HTTPException(status_code=400, detail="Failed to resume session")

    return {"status": "ok"}


@router.post("/api/claude-code/sessions/{session_id}/stop")
async def stop_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Stop a running session."""
    user = await get_current_user(request, None, db)

    # Verify ownership
    session = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.id == session_id,
        ClaudeCodeSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    stopped = await claude_code_manager.stop_session(user.id)
    if not stopped:
        raise HTTPException(status_code=400, detail="No active session to stop")

    return {"status": "stopped"}


@router.post("/api/claude-code/sessions/{session_id}/rollback")
async def rollback_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Rollback git + DB to pre-session state."""
    user = await get_current_user(request, None, db)

    session = db.query(ClaudeCodeSession).filter(
        ClaudeCodeSession.id == session_id,
        ClaudeCodeSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == "running":
        raise HTTPException(status_code=400, detail="Cannot rollback a running session")

    if session.rolled_back:
        raise HTTPException(status_code=400, detail="Session already rolled back")

    result = claude_code_manager.rollback_session(
        session.git_commit_hash,
        session.db_backup_path
    )

    session.rolled_back = True
    session.rolled_back_at = datetime.utcnow()
    db.commit()

    # Log rollback
    try:
        ToolMonitor.log_execution(
            db=db,
            tool_type="claude_code",
            operation="rollback",
            input_data={"session_id": session_id, "git_hash": session.git_commit_hash},
            output_data=result,
            user_id=user.id
        )
    except Exception:
        pass

    return {"status": "ok", "result": result}


# ============================================================================
# WebSocket Streaming
# ============================================================================

@router.websocket("/api/claude-code/stream/{session_id}")
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
        session = db.query(ClaudeCodeSession).filter(
            ClaudeCodeSession.id == session_id,
            ClaudeCodeSession.user_id == user.id
        ).first()

        if not session:
            await websocket.send_json({"error": "Session not found"})
            await websocket.close(code=4004, reason="Session not found")
            return

        # Get active session
        active = claude_code_manager.get_active_session(user.id)
        if active and active.session_id == session_id:
            # Send buffered output first
            if active.output_buffer:
                for data in active.output_buffer:
                    await websocket.send_text(json.dumps(data))

            # Register for live broadcasts
            active.websockets.add(websocket)

            try:
                # Keep alive with ping/pong
                while active.is_running or active.is_waiting:
                    try:
                        msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                        # Handle ping
                        if msg == "ping":
                            await websocket.send_text(json.dumps({"type": "pong"}))
                    except asyncio.TimeoutError:
                        # Send keepalive ping
                        try:
                            await websocket.send_text(json.dumps({"type": "ping"}))
                        except Exception:
                            break
                    except WebSocketDisconnect:
                        break

                # Session ended — _read_output already broadcasts session_end

            except WebSocketDisconnect:
                pass
            finally:
                active.websockets.discard(websocket)
        else:
            # Session not active in memory — if DB says 'running', it's stale (server restarted)
            if session.status == 'running':
                session.status = 'completed'
                session.ended_at = datetime.utcnow()
                db.commit()

            # Session not active, send historical messages
            messages = db.query(ClaudeCodeMessage).filter(
                ClaudeCodeMessage.session_id == session_id
            ).order_by(ClaudeCodeMessage.created_at.asc(), ClaudeCodeMessage.id.asc()).all()

            for m in messages:
                # Skip system/init events (large session metadata)
                if m.message_type == 'system' and m.event_data and '"subtype": "init"' in m.event_data:
                    continue
                try:
                    if m.event_data:
                        data = json.loads(m.event_data)
                    else:
                        data = {"type": m.message_type, "role": m.role, "content": m.content}
                    await websocket.send_text(json.dumps(data))
                except Exception:
                    pass

            await websocket.send_text(json.dumps({
                "type": "session_end",
                "status": session.status or "completed"
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
