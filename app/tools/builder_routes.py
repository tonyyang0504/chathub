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

from app.database import get_db, SessionLocal, BuiltTool, CustomToolListing, AiWorkspaceSession, AiWorkspaceMessage
from app.auth.utils import get_current_user_optional, get_websocket_user, decrypt_string
from .builder_manager import tool_builder_manager
from .sandbox_manager import sandbox_manager
from .readiness import build_publish_readiness

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
    tool_md_content: str


class ToolUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    gradient_start: Optional[str] = None
    gradient_end: Optional[str] = None
    tool_md_content: Optional[str] = None


class ToolPublishRequest(BaseModel):
    category: str = "automation"
    long_description: Optional[str] = None
    version: str = "1.0.0"


class SendMessageRequest(BaseModel):
    message: str


class CreateSessionRequest(BaseModel):
    provider: Optional[str] = None  # claude, codex, gemini, chathub — defaults to user's default_provider


class PublishReadinessRequest(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None


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

    # Determine provider
    selected_provider = provider or settings.default_provider or "claude"

    # Get API key and model based on provider
    api_key = None
    model = None
    auth_method = settings.auth_method or "api_key"
    oauth_token = None
    ai_provider = None  # AI backend for ChatHub provider (openai/anthropic/google)

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
    elif selected_provider == "chathub":
        # ChatHub Agent reuses whichever AI provider the user has configured
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
            # Fall back to the user's default AI workspace key
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
    """Create a new tool builder session with a worktree sandbox."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Handle both JSON body and empty POST
    provider_requested = data.provider if data else None

    provider, api_key, model, auth_method, oauth_token, ai_provider = _get_ai_workspace_settings(
        user, db, provider_requested
    )

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No API key configured for the selected provider. Please set up your API keys in AI Workspace settings."
        )

    try:
        session = await tool_builder_manager.create_session(
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
        AiWorkspaceSession.session_type == "tool_builder"
    ).order_by(AiWorkspaceSession.created_at.desc()).all()
    active = tool_builder_manager.get_session(user.id)
    return [{"db_id": s.id, "title": s.title or "[Tool Builder Session]",
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
        AiWorkspaceSession.session_type == "tool_builder"
    ).first()
    if not session:
        raise HTTPException(status_code=404)
    from app.database import AiWorkspaceMessage
    messages = db.query(AiWorkspaceMessage).filter(
        AiWorkspaceMessage.session_id == db_id
    ).order_by(AiWorkspaceMessage.created_at.asc()).all()
    active = tool_builder_manager.get_session(user.id)
    # Parse suggested_metadata from JSON if present
    suggested_metadata = None
    if session.suggested_metadata:
        try:
            suggested_metadata = json.loads(session.suggested_metadata)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "db_id": session.id, "title": session.title or "[Tool Builder Session]",
        "status": session.status, "provider": session.provider,
        "published": session.status == "published",
        "active_session_id": active.session_id if active and active.db_session_id == db_id else None,
        "suggested_metadata": suggested_metadata,
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
        AiWorkspaceSession.session_type == "tool_builder"
    ).first()
    if not session:
        raise HTTPException(status_code=404)
    active = tool_builder_manager.get_session(user.id)
    if active and active.db_session_id == db_id:
        await tool_builder_manager.discard(user.id)
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
        AiWorkspaceSession.session_type == "tool_builder"
    ).first()
    if not db_session:
        raise HTTPException(status_code=404)
    provider, api_key, model, auth_method, oauth_token, ai_provider = \
        _get_ai_workspace_settings(user, db, db_session.provider)
    if not api_key:
        raise HTTPException(status_code=400, detail="No API key configured")
    try:
        session = await tool_builder_manager.resume_session(
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
    """Get sandbox status, changed files, preview URL."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    status = tool_builder_manager.get_build_status(user.id)
    # Ensure preview_url is always in the response
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
    """Trigger Docker install + image build + container creation on demand. Returns preview_url."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        # start_preview blocks for up to 3 min (Docker install) — run off event loop
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: sandbox_manager.start_preview(session_id)
        )
        return {"preview_url": result.preview_url, "preview_port": result.preview_port}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/sessions/{session_id}/preview/restart")
async def restart_preview(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Restart the preview container to pick up code changes."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: sandbox_manager.restart_preview(session_id)
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
    """Merge worktree branch, create tool record, return tool ID."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.is_running:
        raise HTTPException(status_code=409, detail="Agent is still running. Wait for it to finish.")

    # Read optional JSON body with tool metadata (name, display_name, description, icon)
    metadata = None
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("name"):
            metadata = body
    except Exception:
        pass

    result = await tool_builder_manager.publish(user.id, metadata=metadata)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/sessions/{session_id}/readiness")
async def get_publish_readiness(
    request: Request,
    session_id: str,
    data: PublishReadinessRequest,
    db: Session = Depends(get_db)
):
    """Compute publish-readiness status for the current session draft."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    changed_files = sandbox_manager.get_changed_files(session_id)

    tool_md_content = None
    for file_path in changed_files:
        if file_path.upper().endswith("TOOL.MD"):
            from pathlib import Path
            _wt_path = session.sandbox_info.worktree_path if session.sandbox_info else None
            _base = _wt_path or Path(__file__).resolve().parent.parent.parent
            full_path = _base / file_path
            if full_path.exists():
                tool_md_content = full_path.read_text(encoding="utf-8")
            break

    metadata = {
        "name": data.name or "",
        "display_name": data.display_name or "",
        "description": data.description or "",
        "icon": data.icon or "",
    }

    readiness = build_publish_readiness(
        changed_files=changed_files,
        metadata=metadata,
        skill_md_content=tool_md_content,
    )
    readiness["changed_files"] = changed_files
    return readiness


async def generate_suggested_metadata(session, user_id: int, db: Session) -> dict:
    """Core logic: gather sandbox context, call AI, return metadata dict.

    Used by both the suggest-metadata endpoint and the auto-suggest on turn completion.
    """
    import re as _re
    from pathlib import Path

    # Gather context from sandbox
    changed_files = sandbox_manager.get_changed_files(session.session_id)
    session_title = ""
    if session.db_session_id:
        db_session = db.query(AiWorkspaceSession).filter(
            AiWorkspaceSession.id == session.db_session_id
        ).first()
        if db_session:
            session_title = db_session.title or ""

    # Try TOOL.md first — if it has quality metadata, use it directly
    for fpath in changed_files:
        if fpath.upper().endswith("TOOL.MD"):
            wt_path = session.sandbox_info.worktree_path if session.sandbox_info else None
            base = wt_path or Path(__file__).resolve().parent.parent.parent
            full_path = base / fpath
            if full_path.exists():
                from app.tools.readiness import parse_tool_frontmatter
                fm = parse_tool_frontmatter(full_path.read_text(encoding="utf-8", errors="ignore"))
                if fm.get("name") and fm.get("display_name") and fm.get("description"):
                    return {
                        "name": fm["name"],
                        "display_name": fm["display_name"],
                        "description": fm["description"],
                        "icon": fm.get("icon", "bi-gear"),
                    }
            break

    # Try to read routes.py from sandbox for extra context
    routes_snippet = ""
    for fpath in changed_files:
        if fpath.endswith("routes.py"):
            wt_path = session.sandbox_info.worktree_path if session.sandbox_info else None
            base = wt_path or Path(__file__).resolve().parent.parent.parent
            full_path = base / fpath
            if full_path.exists():
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                routes_snippet = content[:3000]
            break

    # Determine AI provider and use a fast model
    provider_name = session.provider or "claude"
    provider_map = {"claude": "anthropic", "codex": "openai", "gemini": "google", "chathub": None}
    ai_backend = provider_map.get(provider_name)

    if provider_name == "chathub":
        _, _, _, _, _, ai_provider = _get_ai_workspace_settings_from_db(user_id, db, "chathub")
        ai_backend = ai_provider or "openai"

    fast_models = {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-4o-mini", "google": "gemini-2.0-flash"}
    model = fast_models.get(ai_backend, "claude-haiku-4-5-20251001")

    # Get API key
    _, api_key, _, _, _, _ = _get_ai_workspace_settings_from_db(user_id, db, provider_name)
    if not api_key or api_key == "membership":
        return _fallback_metadata(changed_files, session_title, session.db_session_id, db)

    try:
        from app.ai.factory import get_ai_provider
        provider = get_ai_provider(ai_backend, api_key, model)

        context_parts = []
        if session_title:
            context_parts.append(f"Session title: {session_title}")
        if changed_files:
            context_parts.append(f"Changed files: {', '.join(changed_files[:20])}")
        if routes_snippet:
            context_parts.append(f"Routes code snippet:\n```python\n{routes_snippet}\n```")

        # Add conversation history for richer context
        if session.db_session_id:
            recent_msgs = db.query(AiWorkspaceMessage).filter(
                AiWorkspaceMessage.session_id == session.db_session_id,
                AiWorkspaceMessage.role == "assistant",
                AiWorkspaceMessage.message_type == "result",
            ).order_by(AiWorkspaceMessage.id.desc()).limit(5).all()

            if recent_msgs:
                summaries = "\n\n".join(m.content[:800] for m in reversed(recent_msgs) if m.content)
                if summaries:
                    context_parts.append(f"Agent conversation summaries:\n{summaries[:3000]}")

        context_text = "\n\n".join(context_parts) or "No context available"

        messages = [
            {"role": "system", "content": (
                "Generate JSON tool metadata from the provided code context. "
                "Return ONLY valid JSON with these fields:\n"
                '- "name": kebab-case slug (e.g. "contact-analyzer")\n'
                '- "display_name": human-readable title\n'
                '- "description": one-sentence description (10-80 chars) derived from the code, NOT echoing the user\'s prompt\n'
                '- "icon": Bootstrap icon class (e.g. "bi-robot", "bi-graph-up", "bi-chat-dots")\n'
                "Do not include any text outside the JSON object. "
                "Do NOT use the user's raw prompt or session title as the description — derive it from the actual code changes."
            )},
            {"role": "user", "content": context_text},
        ]

        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provider.chat_completion(messages, max_tokens=200)
        )

        # Parse JSON from response
        response_text = response.content if hasattr(response, 'content') else str(response)
        json_match = _re.search(r'\{[^{}]*\}', response_text, _re.DOTALL)
        if json_match:
            suggested = json.loads(json_match.group())
            result = {}
            if suggested.get("name") and _re.match(r'^[a-z0-9]+(?:-[a-z0-9]+)*$', suggested["name"]):
                result["name"] = suggested["name"]
            if suggested.get("display_name"):
                result["display_name"] = str(suggested["display_name"])[:100]
            if suggested.get("description"):
                result["description"] = str(suggested["description"])[:200]
            if suggested.get("icon") and str(suggested["icon"]).startswith("bi-"):
                result["icon"] = suggested["icon"]
            if result:
                return result

    except Exception as e:
        logger.warning(f"AI suggest-metadata failed: {e}")

    return _fallback_metadata(changed_files, session_title, session.db_session_id, db)


def _get_ai_workspace_settings_from_db(user_id: int, db: Session, provider: str = None):
    """Like _get_ai_workspace_settings but takes user_id instead of user object."""
    from app.database import AiWorkspaceSettings, User
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None, None, None, None, None, None
    return _get_ai_workspace_settings(user, db, provider)


@router.post("/sessions/{session_id}/suggest-metadata")
async def suggest_metadata(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Use AI to suggest tool metadata (name, display_name, description, icon) from sandbox context."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    return await generate_suggested_metadata(session, user.id, db)


def _extract_metadata_from_conversation(db_session_id: int, db) -> Dict:
    """Parse conversation history for metadata patterns like 'Feature slug:', 'Display name:', etc."""
    import re

    recent_msgs = db.query(AiWorkspaceMessage).filter(
        AiWorkspaceMessage.session_id == db_session_id,
        AiWorkspaceMessage.role == "assistant",
        AiWorkspaceMessage.message_type == "result",
    ).order_by(AiWorkspaceMessage.id.desc()).limit(5).all()

    if not recent_msgs:
        return {}

    # Combine messages (oldest first) for scanning
    combined = "\n\n".join(m.content[:1500] for m in reversed(recent_msgs) if m.content)
    if not combined:
        return {}

    result = {}

    # Try to extract structured metadata patterns from agent output
    slug_match = re.search(r'(?:feature\s+slug|tool\s+slug|slug)\s*[:=]\s*["`\']?([a-z0-9-]+)["`\']?', combined, re.IGNORECASE)
    if slug_match:
        result["name"] = slug_match.group(1)

    display_match = re.search(r'(?:display\s+name|tool\s+name|feature\s+name)\s*[:=]\s*["`\']?([^"`\'\n]+)["`\']?', combined, re.IGNORECASE)
    if display_match:
        result["display_name"] = display_match.group(1).strip()[:100]

    desc_match = re.search(r'(?:description|what\s+it\s+does)\s*[:=]\s*["`\']?([^"`\'\n]+)["`\']?', combined, re.IGNORECASE)
    if desc_match:
        result["description"] = desc_match.group(1).strip()[:200]

    # If we got a name but no display_name, derive it
    if result.get("name") and not result.get("display_name"):
        result["display_name"] = result["name"].replace('-', ' ').title()

    # If no structured patterns found, try to extract from summary-style text
    if not result.get("name"):
        # Look for "I've built/created/implemented the X tool/feature"
        built_match = re.search(
            r'(?:built|created|implemented|added)\s+(?:a\s+|the\s+)?["`\']?([A-Z][A-Za-z\s]+?)["`\']?\s+(?:tool|feature|page|dashboard)',
            combined
        )
        if built_match:
            feature_name = built_match.group(1).strip()
            result["name"] = re.sub(r'[^a-z0-9]+', '-', feature_name.lower()).strip('-')[:40]
            result["display_name"] = feature_name[:100]

    # Try to get description from the last message's first sentence if we still need one
    if result.get("name") and not result.get("description"):
        last_content = recent_msgs[0].content or ""
        # First meaningful sentence
        sent_match = re.search(r'([A-Z][^.!?\n]{15,120}[.!?])', last_content)
        if sent_match:
            result["description"] = sent_match.group(1).strip()

    if result.get("name"):
        result.setdefault("icon", "bi-gear")

    return result


def _fallback_metadata(changed_files: List[str], session_title: str, db_session_id: int = None, db=None) -> Dict:
    """Derive metadata from conversation history, directory structure, or session title."""
    import re

    # First try: extract from conversation history
    if db_session_id and db:
        metadata = _extract_metadata_from_conversation(db_session_id, db)
        if metadata.get("name"):
            return metadata

    # Second try: extract from directory structure
    name = ""
    for fpath in changed_files:
        # Look for app/tools/custom/{name}/ pattern
        match = re.match(r'app/tools/custom/([a-z0-9-]+)/', fpath)
        if match:
            name = match.group(1)
            break

    # Last resort: slugify session title
    if not name and session_title:
        slug = re.sub(r'[^a-z0-9]+', '-', session_title.lower()).strip('-')
        if slug:
            name = slug[:40]

    display_name = name.replace('-', ' ').title() if name else ""

    return {
        "name": name,
        "display_name": display_name,
        "description": f"{display_name} tool for ChatHub" if display_name else "",
        "icon": "bi-gear",
    }


@router.post("/sessions/{session_id}/generate-tool")
async def generate_tool_file(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db)
):
    """Auto-generate TOOL.md from form metadata, write+commit in sandbox, return updated readiness."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = tool_builder_manager.get_session(user.id)
    if not session or session.session_id != session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    name = (body.get("name") or "").strip()
    display_name = (body.get("display_name") or name or "").strip()
    description = (body.get("description") or "").strip()
    icon = (body.get("icon") or "bi-gear").strip()
    trigger = (body.get("trigger") or name or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Tool name is required")

    tool_md = (
        f"---\n"
        f"name: {name}\n"
        f"display_name: {display_name}\n"
        f"description: {description}\n"
        f"icon: {icon}\n"
        f"trigger: {trigger}\n"
        f"---\n\n"
        f"# {display_name}\n\n"
        f"{description}\n"
    )

    written = sandbox_manager.write_file(session.session_id, "TOOL.md", tool_md)
    if not written:
        raise HTTPException(status_code=500, detail="Failed to write TOOL.md in sandbox")

    sandbox_manager.commit_file(session.session_id, "TOOL.md", f"Add TOOL.md for {name}")

    # Return updated readiness
    changed_files = sandbox_manager.get_changed_files(session.session_id)
    from .readiness import build_publish_readiness
    readiness = build_publish_readiness(
        changed_files=changed_files,
        metadata={"name": name, "display_name": display_name, "description": description, "icon": icon},
        skill_md_content=tool_md,
    )
    readiness["changed_files"] = changed_files
    return readiness


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
# Tool CRUD Endpoints
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

    if not data.tool_md_content or len(data.tool_md_content.strip()) < 10:
        raise HTTPException(status_code=400, detail="Tool content is too short")

    existing = db.query(BuiltTool).filter(
        BuiltTool.user_id == user.id,
        BuiltTool.name == data.name
    ).first()

    if existing:
        existing.display_name = data.display_name
        existing.description = data.description
        existing.icon = data.icon
        existing.gradient_start = data.gradient_start
        existing.gradient_end = data.gradient_end
        existing.tool_md_content = data.tool_md_content
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        tool = existing
    else:
        tool = BuiltTool(
            user_id=user.id,
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            icon=data.icon,
            gradient_start=data.gradient_start,
            gradient_end=data.gradient_end,
            tool_md_content=data.tool_md_content,
            is_active=True,
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)

    return {
        "id": tool.id,
        "name": tool.name,
        "display_name": tool.display_name,
        "message": "Tool saved successfully"
    }


@router.get("/my-tools")
async def list_my_tools(request: Request, db: Session = Depends(get_db)):
    """List user's custom tools."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    tools = db.query(BuiltTool).filter(
        BuiltTool.user_id == user.id
    ).order_by(BuiltTool.created_at.desc()).all()

    return {
        "tools": [
            {
                "id": t.id,
                "name": t.name,
                "display_name": t.display_name or t.name,
                "description": t.description,
                "icon": t.icon or "bi-gear",
                "gradient_start": t.gradient_start or "#6366f1",
                "gradient_end": t.gradient_end or "#8b5cf6",
                "is_active": t.is_active,
                "has_listing": t.listing is not None,
                "listing_status": t.listing.status if t.listing else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in tools
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

    tool = db.query(BuiltTool).filter(
        BuiltTool.id == tool_id,
        BuiltTool.user_id == user.id
    ).first()

    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    return {
        "id": tool.id,
        "name": tool.name,
        "display_name": tool.display_name or tool.name,
        "description": tool.description,
        "icon": tool.icon or "bi-gear",
        "gradient_start": tool.gradient_start or "#6366f1",
        "gradient_end": tool.gradient_end or "#8b5cf6",
        "tool_md_content": tool.tool_md_content,
        "is_active": tool.is_active,
        "created_at": tool.created_at.isoformat() if tool.created_at else None,
        "updated_at": tool.updated_at.isoformat() if tool.updated_at else None,
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

    tool = db.query(BuiltTool).filter(
        BuiltTool.id == tool_id,
        BuiltTool.user_id == user.id
    ).first()

    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    if data.display_name is not None:
        tool.display_name = data.display_name
    if data.description is not None:
        tool.description = data.description
    if data.icon is not None:
        tool.icon = data.icon
    if data.gradient_start is not None:
        tool.gradient_start = data.gradient_start
    if data.gradient_end is not None:
        tool.gradient_end = data.gradient_end
    if data.tool_md_content is not None:
        tool.tool_md_content = data.tool_md_content

    tool.updated_at = datetime.utcnow()
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

    tool = db.query(BuiltTool).filter(
        BuiltTool.id == tool_id,
        BuiltTool.user_id == user.id
    ).first()

    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    if tool.listing:
        db.delete(tool.listing)

    db.delete(tool)
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

    tool = db.query(BuiltTool).filter(
        BuiltTool.id == tool_id,
        BuiltTool.user_id == user.id
    ).first()

    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    tool.is_active = not tool.is_active
    tool.updated_at = datetime.utcnow()
    db.commit()

    return {
        "is_active": tool.is_active,
        "message": f"Tool {'activated' if tool.is_active else 'deactivated'}"
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

    tool = db.query(BuiltTool).filter(
        BuiltTool.id == tool_id,
        BuiltTool.user_id == user.id
    ).first()

    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    if not tool.tool_md_content:
        raise HTTPException(status_code=400, detail="Tool has no TOOL.md content")

    existing_listing = db.query(CustomToolListing).filter(
        CustomToolListing.tool_id == tool.id
    ).first()

    if existing_listing:
        existing_listing.display_name = tool.display_name
        existing_listing.description = tool.description
        existing_listing.long_description = data.long_description or tool.description
        existing_listing.category = data.category
        existing_listing.version = data.version
        existing_listing.icon = tool.icon
        existing_listing.gradient_start = tool.gradient_start
        existing_listing.gradient_end = tool.gradient_end
        existing_listing.tool_md_content = tool.tool_md_content
        existing_listing.status = "published"
        existing_listing.updated_at = datetime.utcnow()
        db.commit()
        listing = existing_listing
    else:
        name_taken = db.query(CustomToolListing).filter(
            CustomToolListing.name == tool.name
        ).first()
        if name_taken:
            raise HTTPException(status_code=400, detail="A tool with this name already exists in the marketplace")

        listing = CustomToolListing(
            author_id=user.id,
            tool_id=tool.id,
            name=tool.name,
            display_name=tool.display_name,
            description=tool.description,
            long_description=data.long_description or tool.description,
            category=data.category,
            version=data.version,
            icon=tool.icon,
            gradient_start=tool.gradient_start,
            gradient_end=tool.gradient_end,
            tool_md_content=tool.tool_md_content,
            status="published"
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)

    return {
        "listing_id": listing.id,
        "message": "Tool published to marketplace"
    }


# ============================================================================
# Uninstall & Reset Endpoints
# ============================================================================

@router.delete("/tools/{tool_id}/uninstall")
async def uninstall_tool(
    request: Request,
    tool_id: int,
    db: Session = Depends(get_db)
):
    """Uninstall a custom tool: git revert + DB restore, or legacy shutil.rmtree fallback."""
    import shutil
    import os
    from pathlib import Path

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    tool = db.query(BuiltTool).filter(
        BuiltTool.id == tool_id,
        BuiltTool.user_id == user.id
    ).first()

    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    dir_deleted = False

    if tool.publish_commit_hash:
        # Git revert approach
        from app.tools.worktree_manager import worktree_manager
        result = worktree_manager.revert_commit(tool.publish_commit_hash)
        if not result.success:
            raise HTTPException(status_code=409, detail=result.message)
        dir_deleted = True

        # Restore DB from pre-publish backup
        if tool.pre_publish_db_backup and os.path.exists(tool.pre_publish_db_backup):
            try:
                from app.config import settings
                db_path = settings.DATABASE_URL.replace("sqlite:///", "").replace("sqlite:", "")
                if os.path.exists(db_path):
                    shutil.copy2(tool.pre_publish_db_backup, db_path)
                    logger.info(f"Restored DB from backup: {tool.pre_publish_db_backup}")
            except Exception as e:
                logger.error(f"Failed to restore DB backup: {e}")
    else:
        # Legacy fallback: just delete directory
        custom_tools_dir = Path(__file__).resolve().parent / "custom" / tool.name
        if custom_tools_dir.exists():
            shutil.rmtree(str(custom_tools_dir))
            dir_deleted = True

    # Delete marketplace listing if exists
    if tool.listing:
        db.delete(tool.listing)

    # Delete the tool record
    db.delete(tool)
    db.commit()

    return {
        "success": True,
        "message": "Tool uninstalled successfully",
        "directory_deleted": dir_deleted,
        "restart_required": dir_deleted
    }


@router.post("/reset")
async def reset_custom_tools(
    request: Request,
    db: Session = Depends(get_db)
):
    """Reset all custom tools: git revert in reverse order + DB restore, with legacy fallback."""
    import shutil
    import os
    from pathlib import Path
    from app.database import CustomToolInstall

    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    custom_tools_dir = Path(__file__).resolve().parent / "custom"
    dirs_deleted = 0
    reverted = 0
    errors = []

    # Get all user's tools ordered by created_at DESC (newest first for safe revert order)
    user_tools = db.query(BuiltTool).filter(
        BuiltTool.user_id == user.id
    ).order_by(BuiltTool.created_at.desc()).all()

    # Find the oldest DB backup for full restore
    oldest_backup = None
    for tool in reversed(user_tools):  # oldest first
        if tool.pre_publish_db_backup and os.path.exists(tool.pre_publish_db_backup):
            oldest_backup = tool.pre_publish_db_backup
            break

    # Revert commits newest-first
    from app.tools.worktree_manager import worktree_manager
    for tool in user_tools:
        if tool.publish_commit_hash:
            result = worktree_manager.revert_commit(tool.publish_commit_hash)
            if result.success:
                reverted += 1
            else:
                errors.append(f"{tool.display_name or tool.name}: {result.message}")
                # Also try legacy cleanup for this tool
                tool_dir = custom_tools_dir / tool.name
                if tool_dir.exists():
                    shutil.rmtree(str(tool_dir))
                    dirs_deleted += 1
        else:
            # Legacy fallback
            tool_dir = custom_tools_dir / tool.name
            if tool_dir.exists():
                shutil.rmtree(str(tool_dir))
                dirs_deleted += 1

    # Restore DB from oldest backup (covers all schema changes)
    if oldest_backup and os.path.exists(oldest_backup):
        try:
            from app.config import settings
            db_path = settings.DATABASE_URL.replace("sqlite:///", "").replace("sqlite:", "")
            if os.path.exists(db_path):
                shutil.copy2(oldest_backup, db_path)
                logger.info(f"Restored DB from oldest backup: {oldest_backup}")
        except Exception as e:
            logger.error(f"Failed to restore DB backup during reset: {e}")

    # Delete all DB records for this user
    tool_ids = [t.id for t in user_tools]
    if tool_ids:
        db.query(CustomToolListing).filter(
            CustomToolListing.tool_id.in_(tool_ids)
        ).delete(synchronize_session=False)

    db.query(CustomToolInstall).filter(CustomToolInstall.user_id == user.id).delete()
    db.query(BuiltTool).filter(BuiltTool.user_id == user.id).delete()
    db.commit()

    message = f"All custom tools removed ({len(user_tools)} tools, {reverted} reverted, {dirs_deleted} directories deleted)"
    if errors:
        message += f". Warnings: {'; '.join(errors)}"

    return {
        "success": True,
        "message": message,
        "tools_deleted": len(user_tools),
        "directories_deleted": dirs_deleted,
        "reverted": reverted,
        "restart_required": (dirs_deleted + reverted) > 0
    }
