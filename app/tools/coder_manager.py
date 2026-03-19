"""
AI Coder Agent Manager
Delegates to CLI coding agents running in isolated git worktree sandboxes.
Unlike Tool Builder, AI Coder modifies the main project codebase.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket

from .sandbox_manager import sandbox_manager, SandboxInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

AI_CODER_SYSTEM_CONTEXT = """You are an AI Coder agent for the ChatHub platform. You are working inside an isolated git worktree sandbox.

Your job is to MODIFY the main project codebase — adding features, fixing bugs, refactoring, or making any changes the user requests. Unlike the Tool Builder (which creates isolated plugins), you have full access to ALL project files.

## MANDATORY WORKFLOW: Plan → Propose → Confirm → Execute

You MUST follow these phases in order. Do NOT skip to coding.

### Phase 1 — Understand

**BEFORE proposing anything**, you MUST explore the codebase to understand the project:

1. **Read the project broadly** — browse the directory structure, read source files, templates, models, routes, and stylesheets. Understand what already exists.

2. **Dive deeper into areas related to the user's request** — read any files, templates, routes, or models that are relevant.

3. Analyze the user's request with this context.

### Phase 2 — Propose

Present a concrete plan:
- What files will be changed and why
- What the expected behavior change is
- Any risks or side effects

### Phase 3 — Confirm

Wait for the user to approve the plan before making changes.

### Phase 4 — Execute

Implement the approved changes. Make clean, focused commits.

## RESTRICTIONS

- **NEVER** modify `.env`, credentials files, or database files directly
- **NEVER** delete or corrupt the database
- **NEVER** modify `app/config.py` SECRET_KEY or ENCRYPTION_KEY values
- **NEVER** introduce security vulnerabilities
- Keep changes focused and minimal — don't refactor unrelated code
- Write clean, production-quality code that follows existing patterns

## PROJECT CONTEXT

This is a FastAPI + SQLAlchemy project with Jinja2 templates and Bootstrap 5 frontend.
Read CLAUDE.md at the project root for full architecture documentation.

## KEY ROUTING PREFIXES

- Dashboard pages: /dashboard/ (index, bots, conversations, analytics, settings, hubs, agents)
- Tool pages: /tools/ (scheduled-content, contact-analyzer, group-management, ai-workspace, tool-builder, ai-coder, etc.)
- Tool API endpoints: /tools/api/ (tool-specific REST APIs)
- Bot API: /api/bots/ (CRUD, start/stop, contacts, groups)
- Hub API: /api/hubs/ (CRUD, agents, contacts, content, groups)
- Conversation API: /api/conversations/ (list, messages, send)
- Analytics API: /api/analytics/ (overview, daily, activity)
- Auth: /auth/ (login, register, logout)
- Scripts: /scripts/ (script CRUD, execution)

IMPORTANT: Do NOT mix these prefixes. /dashboard/tools/ does NOT exist.
Template path ≠ URL path (e.g., app/templates/dashboard/tools/X.html → /tools/X, NOT /dashboard/tools/X).

## KEY FILE LOCATIONS

- Routes: app/tools/routes.py, app/bots/routes.py, app/hubs/routes.py, etc.
- Templates: app/templates/dashboard/ (pages), app/templates/dashboard/tools/ (tool pages)
- Models: app/database.py (all 23+ SQLAlchemy models)
- Static: static/css/style.css, static/css/tools.css, static/js/app.js
- Custom tools: app/tools/custom/{name}/ (isolated plugins — do NOT modify these)

## EXISTING PATTERNS TO FOLLOW

- Check existing links in the file you're modifying for URL pattern reference
- Use CSS variables (var(--card-bg), var(--text-primary), etc.) — never hardcode colors
- Use Bootstrap 5.3 + Bootstrap Icons
- Use rounded-pill on buttons, 12px border-radius on cards
- Modal close: class="modal-close-btn" (not btn-close)
"""


class AiCoderSession:
    """An active AI Coder session backed by a CLI agent in a worktree."""

    def __init__(self, session_id: str, db_session_id: int, user_id: int,
                 api_key: str, model: Optional[str] = None,
                 provider: str = "claude", auth_method: str = "api_key",
                 oauth_token: Optional[str] = None,
                 user_email: Optional[str] = None, user_name: Optional[str] = None,
                 ai_provider: Optional[str] = None):
        self.session_id = session_id
        self.db_session_id = db_session_id
        self.user_id = user_id
        self.claude_session_id = str(uuid.uuid4())
        self._api_key = api_key
        self._model = model
        self._auth_method = auth_method
        self._oauth_token = oauth_token
        self._user_email = user_email
        self._user_name = user_name
        self._ai_provider = ai_provider
        self.provider = provider

        from app.ai_workspace.providers import get_provider
        self.cli_provider = get_provider(provider)

        self.worktree: Optional[SandboxInfo] = None
        self.sandbox_info: Optional[SandboxInfo] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.websockets: Set[WebSocket] = set()
        self.output_buffer: list = []
        self.is_running = False
        self.is_waiting = False
        self.turn_number = 0
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    async def broadcast(self, data: dict):
        """Broadcast data to all connected WebSocket clients."""
        self.output_buffer.append(data)
        message = json.dumps(data)
        dead_ws = set()
        for ws in self.websockets:
            try:
                await ws.send_text(message)
            except Exception:
                dead_ws.add(ws)
        self.websockets -= dead_ws


class AiCoderManager:
    """Manages AI Coder sessions with CLI agents in worktree sandboxes."""

    def __init__(self):
        self._sessions: Dict[int, AiCoderSession] = {}

    def get_session(self, user_id: int) -> Optional[AiCoderSession]:
        session = self._sessions.get(user_id)
        if session and (session.is_running or session.is_waiting or session.sandbox_info):
            return session
        return None

    async def create_session(
        self,
        user_id: int,
        provider: str,
        api_key: str,
        model: Optional[str] = None,
        auth_method: str = "api_key",
        oauth_token: Optional[str] = None,
        user_email: Optional[str] = None,
        user_name: Optional[str] = None,
        ai_provider: Optional[str] = None,
    ) -> AiCoderSession:
        """Create a new AI Coder session with a worktree sandbox."""
        if user_id in self._sessions:
            await self.discard(user_id)

        session_id = str(uuid.uuid4())

        from app.database import SessionLocal, AiWorkspaceSession
        db = SessionLocal()
        try:
            db_session = AiWorkspaceSession(
                user_id=user_id,
                status="pending",
                prompt="[AI Coder Session]",
                provider=provider,
                model=model,
                session_type="ai_coder",
            )
            db.add(db_session)
            db.commit()
            db.refresh(db_session)
            db_session_id = db_session.id
        finally:
            db.close()

        sandbox_info = sandbox_manager.create(session_id, user_id)

        session = AiCoderSession(
            session_id=session_id,
            db_session_id=db_session_id,
            user_id=user_id,
            api_key=api_key,
            model=model,
            provider=provider,
            auth_method=auth_method,
            oauth_token=oauth_token,
            user_email=user_email,
            user_name=user_name,
            ai_provider=ai_provider,
        )
        session.sandbox_info = sandbox_info
        session.worktree = sandbox_info

        db = SessionLocal()
        try:
            db_sess = db.query(AiWorkspaceSession).filter(AiWorkspaceSession.id == db_session_id).first()
            if db_sess:
                db_sess.worktree_path = sandbox_info.container_name or "pending"
                db_sess.worktree_branch = sandbox_info.branch
                db_sess.claude_session_uuid = session.claude_session_id
                db_sess.status = "running"
                db_sess.started_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        self._sessions[user_id] = session
        logger.info(f"Created AI Coder session {session_id} for user {user_id}")
        return session

    async def send_message(self, user_id: int, message: str) -> bool:
        """Send a message to the agent."""
        session = self._sessions.get(user_id)
        if not session or not session.sandbox_info:
            return False

        if session.is_running:
            return False

        try:
            is_first = session.turn_number == 0
            if is_first:
                title = message[:120].strip()
                try:
                    from app.database import SessionLocal, AiWorkspaceSession
                    db = SessionLocal()
                    db_sess = db.query(AiWorkspaceSession).filter(
                        AiWorkspaceSession.id == session.db_session_id
                    ).first()
                    if db_sess:
                        db_sess.title = title
                        db.commit()
                    db.close()
                except Exception as e:
                    logger.warning(f"Failed to save session title: {e}")

            await session.broadcast({"type": "user_message", "content": message})
            await self._run_turn(session, message, is_first=is_first)
            return True
        except Exception as e:
            logger.error(f"Failed to send message in AI Coder session: {e}")
            await session.broadcast({"type": "error", "error": {"message": str(e)}})
            return False

    async def _run_turn(self, session: AiCoderSession, prompt: str, is_first: bool = False):
        """Spawn one CLI process for a single turn in the worktree directory."""
        session.turn_number += 1

        effective_prompt = prompt
        if not is_first and not session.cli_provider.supports_resume:
            history = self._build_conversation_history(session)
            if history:
                effective_prompt = history + effective_prompt

        system_context = AI_CODER_SYSTEM_CONTEXT
        if session._user_name:
            system_context += f"\n\nYou are coding on behalf of: {session._user_name}"
        if session._user_email:
            system_context += f" ({session._user_email})"

        worktree_path = str(
            session.sandbox_info.worktree_path
            if session.sandbox_info and session.sandbox_info.worktree_path
            else PROJECT_ROOT
        )

        cmd = session.cli_provider.build_command(
            prompt=effective_prompt,
            session_uuid=session.claude_session_id,
            is_first=is_first,
            model=session._model,
            system_context=system_context
        )

        base_env = os.environ.copy()
        env = session.cli_provider.build_env(
            base_env,
            api_key=session._api_key,
            auth_method=session._auth_method,
            oauth_token=session._oauth_token,
            ai_provider=session._ai_provider,
        )

        try:
            from app.auth.utils import create_access_token
            from app.config import settings
            api_token = create_access_token(
                data={"sub": str(session.user_id)},
                expires_delta=timedelta(hours=24)
            )
            env["CHATHUB_API_TOKEN"] = api_token
            env["CHATHUB_API_URL"] = f"http://localhost:{settings.PORT}"
        except Exception as e:
            logger.warning(f"Failed to set API token: {e}")

        session.cli_provider.prepare_session(worktree_path, system_context)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
            env=env,
            limit=10 * 1024 * 1024,
        )

        session.process = process
        session.is_running = True
        session.is_waiting = False

        session._read_task = asyncio.create_task(self._read_output(session))
        session._stderr_task = asyncio.create_task(self._read_stderr(session))

        logger.info(f"AI Coder turn started, session={session.session_id}, PID={process.pid}")

    async def _read_stderr(self, session: AiCoderSession):
        process = session.process
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.debug(f"AiCoder {session.session_id} stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stderr: {e}")

    async def _read_output(self, session: AiCoderSession):
        """Read stdout, parse JSON, persist to DB, broadcast to WebSockets."""
        from app.database import SessionLocal, AiWorkspaceSession, AiWorkspaceMessage

        process = session.process
        stderr_task = session._stderr_task

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    data = {"type": "raw", "content": line_str}

                if session.provider != "claude":
                    data = session.cli_provider.normalize_event(data)
                    if data is None:
                        continue

                msg_type = data.get("type", "unknown")
                role = "system"
                content = ""
                skip_db = False

                if msg_type == "assistant" and data.get("message"):
                    role = "assistant"
                    blocks = data["message"].get("content", [])
                    text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                    content = "\n".join(text_parts)
                elif msg_type == "content_block_start":
                    role = "assistant"
                    block = data.get("content_block", {})
                    if block.get("type") == "tool_use":
                        role = "tool_use"
                        content = json.dumps({"tool": block.get("name", ""), "id": block.get("id", "")})
                    skip_db = True
                elif msg_type == "content_block_delta":
                    skip_db = (session.provider == "claude")
                    role = "assistant"
                    delta = data.get("delta", {})
                    content = delta.get("text", "")
                elif msg_type == "content_block_stop":
                    skip_db = True
                elif msg_type == "tool_use":
                    role = "tool_use"
                    content = json.dumps({
                        "tool": data.get("tool", {}).get("name", data.get("name", "")),
                        "input": data.get("tool", {}).get("input", data.get("input", {}))
                    })
                elif msg_type == "tool_result":
                    role = "tool_result"
                    content = json.dumps(data.get("content", data.get("output", "")))
                elif msg_type == "result":
                    role = "assistant"
                    result_content = data.get("result", "")
                    if isinstance(result_content, list):
                        text_parts = [b.get("text", "") for b in result_content if b.get("type") == "text"]
                        content = "\n".join(text_parts)
                    elif isinstance(result_content, str):
                        content = result_content
                elif msg_type == "error":
                    role = "system"
                    content = data.get("error", {}).get("message", str(data))
                elif msg_type == "system":
                    role = "system"
                    content = data.get("message", data.get("content", str(data)))
                elif msg_type == "user":
                    message_data = data.get("message", data)
                    content_blocks = message_data.get("content", [])
                    if isinstance(content_blocks, list):
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                result_content = block.get("content", "")
                                if isinstance(result_content, list):
                                    result_content = "\n".join(
                                        p.get("text", "") for p in result_content
                                        if isinstance(p, dict) and p.get("type") == "text"
                                    )
                                elif not isinstance(result_content, str):
                                    result_content = str(result_content)
                                tool_result_event = {"type": "tool_result", "content": result_content}
                                try:
                                    tr_db = SessionLocal()
                                    tr_msg = AiWorkspaceMessage(
                                        session_id=session.db_session_id,
                                        role="tool_result",
                                        content=(result_content[:10000] if result_content else ""),
                                        message_type="tool_result",
                                        event_data=json.dumps(tool_result_event)[:5000]
                                    )
                                    tr_db.add(tr_msg)
                                    tr_db.commit()
                                    tr_db.close()
                                except Exception as e:
                                    logger.error(f"Failed to persist tool_result: {e}")
                                await session.broadcast(tool_result_event)
                    continue
                else:
                    content = line_str

                if msg_type == "system" and data.get("subtype") == "init":
                    continue

                if not skip_db:
                    try:
                        db = SessionLocal()
                        event_json = json.dumps(data)
                        max_event = 50000 if msg_type in ("result", "assistant") else 5000
                        msg = AiWorkspaceMessage(
                            session_id=session.db_session_id,
                            role=role,
                            content=content[:10000] if content else "",
                            message_type=msg_type,
                            event_data=event_json[:max_event]
                        )
                        db.add(msg)
                        db.commit()
                        db.close()
                    except Exception as e:
                        logger.error(f"Failed to persist message: {e}")

                await session.broadcast(data)

                if msg_type == "result" and session.provider == "claude":
                    await session.broadcast({"type": "result_done"})

            if stderr_task and not stderr_task.done():
                try:
                    await asyncio.wait_for(stderr_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            exit_code = await process.wait()

            if session.process is not process:
                return

            if exit_code == 0:
                session.is_running = False
                session.is_waiting = True
                _cleanup_path = (
                    str(session.sandbox_info.worktree_path)
                    if session.sandbox_info and session.sandbox_info.worktree_path
                    else str(PROJECT_ROOT)
                )
                session.cli_provider.cleanup_session(_cleanup_path)
                await session.broadcast({"type": "turn_end", "exit_code": 0})
                session.output_buffer.clear()
            else:
                session.is_running = False
                session.is_waiting = True
                await session.broadcast({
                    "type": "error",
                    "error": {"message": f"Agent process exited with code {exit_code}"}
                })
                await session.broadcast({"type": "turn_end", "exit_code": exit_code})
                session.output_buffer.clear()

            try:
                db = SessionLocal()
                db_sess = db.query(AiWorkspaceSession).filter(
                    AiWorkspaceSession.id == session.db_session_id
                ).first()
                if db_sess:
                    db_sess.status = "completed" if exit_code == 0 else "failed"
                    db.commit()
                db.close()
            except Exception as e:
                logger.error(f"Failed to update session status: {e}")

        except Exception as e:
            logger.error(f"Error in _read_output: {e}")
            session.is_running = False
            await session.broadcast({"type": "error", "error": {"message": str(e)}})

    def _build_conversation_history(self, session: AiCoderSession) -> str:
        from app.database import SessionLocal, AiWorkspaceMessage
        try:
            db = SessionLocal()
            messages = db.query(AiWorkspaceMessage).filter(
                AiWorkspaceMessage.session_id == session.db_session_id
            ).order_by(AiWorkspaceMessage.id.asc()).all()
            db.close()

            if not messages:
                return ""

            history_parts = []
            current_turn = 0
            for msg in messages:
                if msg.message_type == "user_message" or (msg.role == "user" and msg.message_type == "text"):
                    current_turn += 1
                    content = msg.content[:500] if msg.content else ""
                    history_parts.append(f"[Turn {current_turn}] User: {content}")
                elif msg.message_type == "result" and msg.role == "assistant":
                    content = msg.content[:1000] if msg.content else ""
                    history_parts.append(f"[Turn {current_turn}] Assistant: {content}")
                elif msg.role == "tool_use" and msg.content:
                    try:
                        tool_data = json.loads(msg.content)
                        tool_name = tool_data.get("tool", "unknown")
                        history_parts.append(f"[Turn {current_turn}] Used tool: {tool_name}")
                    except (json.JSONDecodeError, AttributeError):
                        pass

            if not history_parts:
                return ""

            history = "\n".join(history_parts)
            if len(history) > 3000:
                history = history[:3000] + "\n... (earlier history truncated)"

            return (
                f"CONVERSATION HISTORY (previous turns in this session):\n"
                f"{history}\n\n"
                f"Continue the conversation based on the above context. "
                f"The user's new message follows:\n\n"
            )
        except Exception as e:
            logger.error(f"Failed to build conversation history: {e}")
            return ""

    def get_build_status(self, user_id: int) -> dict:
        session = self._sessions.get(user_id)
        if not session or not session.sandbox_info:
            return {"error": "No active session"}

        status = sandbox_manager.get_status(session.session_id)

        return {
            "session_id": session.session_id,
            "provider": session.provider,
            "is_running": session.is_running,
            "is_waiting": session.is_waiting,
            "turn_number": session.turn_number,
            "branch": session.sandbox_info.branch,
            "container_name": session.sandbox_info.container_name,
            "container_status": status.get("container_status", ""),
            "preview_url": session.sandbox_info.preview_url,
            "preview_port": session.sandbox_info.preview_port,
            "changed_files": status.get("changed_files", []),
            "diff_stat": status.get("diff_stat", ""),
        }

    async def publish(self, user_id: int, metadata: Optional[Dict] = None) -> dict:
        """Publish sandbox changes: merge worktree to main, create CodeModification record."""
        session = self._sessions.get(user_id)
        if not session or not session.sandbox_info:
            return {"error": "No active session"}

        title = (metadata or {}).get("title", "AI Coder modification")
        description = (metadata or {}).get("description", "")

        # Get changed files before publish
        changed_files = sandbox_manager.get_changed_files(session.session_id)
        if not changed_files:
            return {"error": "No changes to publish"}

        # Publish using legacy mode (copy to PROJECT_ROOT preserving structure)
        commit_prefix = f"AI Coder: {title[:60]}"
        result = sandbox_manager.publish(session.session_id, tool_name=None, commit_prefix=commit_prefix)
        if not result.get("success"):
            return {"error": result.get("message", "Publish failed")}

        commit_hash = result.get("commit_hash", "")

        # Create CodeModification record
        from app.database import SessionLocal, CodeModification
        db = SessionLocal()
        try:
            mod = CodeModification(
                user_id=user_id,
                session_id=session.db_session_id,
                title=title[:200],
                description=description,
                commit_hash=commit_hash,
                files_changed=json.dumps(changed_files),
                status="active",
                published_at=datetime.utcnow(),
            )
            db.add(mod)
            db.commit()
            db.refresh(mod)
            mod_id = mod.id
        except Exception as e:
            logger.error(f"Failed to create CodeModification: {e}")
            mod_id = None
        finally:
            db.close()

        # Update DB session status
        try:
            db = SessionLocal()
            from app.database import AiWorkspaceSession
            db_sess = db.query(AiWorkspaceSession).filter(
                AiWorkspaceSession.id == session.db_session_id
            ).first()
            if db_sess:
                db_sess.status = "published"
                db.commit()
            db.close()
        except Exception as e:
            logger.error(f"Failed to update session status: {e}")

        if user_id in self._sessions:
            del self._sessions[user_id]

        return {
            "success": True,
            "message": "Changes applied successfully",
            "commit_hash": commit_hash,
            "mod_id": mod_id,
            "files_changed": changed_files,
        }

    async def revert(self, mod_id: int, user_id: int) -> dict:
        """Revert a specific modification by running git revert."""
        from app.database import SessionLocal, CodeModification
        db = SessionLocal()
        try:
            mod = db.query(CodeModification).filter(
                CodeModification.id == mod_id,
                CodeModification.user_id == user_id,
            ).first()
            if not mod:
                return {"error": "Modification not found"}
            if mod.status == "reverted":
                return {"error": "Already reverted"}
            if not mod.commit_hash:
                return {"error": "No commit hash to revert"}

            # Run git revert
            result = subprocess.run(
                ["git", "revert", "--no-edit", mod.commit_hash],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            if result.returncode != 0:
                return {"error": f"Git revert failed: {result.stderr.strip()}"}

            # Get the revert commit hash
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            revert_hash = head.stdout.strip() if head.returncode == 0 else ""

            mod.status = "reverted"
            mod.revert_commit_hash = revert_hash
            mod.reverted_at = datetime.utcnow()
            db.commit()

            return {"success": True, "revert_commit_hash": revert_hash}
        except Exception as e:
            logger.error(f"Failed to revert modification: {e}")
            return {"error": str(e)}
        finally:
            db.close()

    def get_modifications(self, user_id: int) -> list:
        """Get all modifications for a user."""
        from app.database import SessionLocal, CodeModification
        db = SessionLocal()
        try:
            mods = db.query(CodeModification).filter(
                CodeModification.user_id == user_id
            ).order_by(CodeModification.published_at.desc()).all()
            return [{
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "commit_hash": m.commit_hash,
                "revert_commit_hash": m.revert_commit_hash,
                "files_changed": json.loads(m.files_changed) if m.files_changed else [],
                "status": m.status,
                "published_at": m.published_at.isoformat() if m.published_at else None,
                "reverted_at": m.reverted_at.isoformat() if m.reverted_at else None,
            } for m in mods]
        finally:
            db.close()

    async def resume_session(
        self,
        db_id: int,
        user_id: int,
        provider: str,
        api_key: str,
        model: Optional[str] = None,
        auth_method: str = "api_key",
        oauth_token: Optional[str] = None,
        user_email: Optional[str] = None,
        user_name: Optional[str] = None,
        ai_provider: Optional[str] = None,
        claude_session_uuid: Optional[str] = None,
    ) -> AiCoderSession:
        """Resume a historical session from DB."""
        if user_id in self._sessions:
            await self.discard(user_id)

        from app.database import SessionLocal, AiWorkspaceSession, AiWorkspaceMessage
        db = SessionLocal()
        try:
            db_sess = db.query(AiWorkspaceSession).filter(
                AiWorkspaceSession.id == db_id
            ).first()
            if not db_sess:
                raise RuntimeError("Session not found in database")

            msgs = db.query(AiWorkspaceMessage).filter(
                AiWorkspaceMessage.session_id == db_id
            ).all()
            turn_count = sum(
                1 for m in msgs
                if m.message_type == "user_message" or (m.role == "user" and m.message_type == "text")
            )
        finally:
            db.close()

        session_id = str(uuid.uuid4())
        sandbox_info = sandbox_manager.create(session_id, user_id)

        session = AiCoderSession(
            session_id=session_id,
            db_session_id=db_id,
            user_id=user_id,
            api_key=api_key,
            model=model,
            provider=provider,
            auth_method=auth_method,
            oauth_token=oauth_token,
            user_email=user_email,
            user_name=user_name,
            ai_provider=ai_provider,
        )
        session.sandbox_info = sandbox_info
        session.worktree = sandbox_info
        session.turn_number = turn_count
        if claude_session_uuid:
            session.claude_session_id = claude_session_uuid

        db = SessionLocal()
        try:
            db_sess = db.query(AiWorkspaceSession).filter(AiWorkspaceSession.id == db_id).first()
            if db_sess:
                db_sess.worktree_path = sandbox_info.container_name or "pending"
                db_sess.worktree_branch = sandbox_info.branch
                db_sess.status = "running"
                db_sess.started_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        self._sessions[user_id] = session
        logger.info(f"Resumed AI Coder session db_id={db_id} as {session_id}")
        return session

    async def discard(self, user_id: int) -> dict:
        session = self._sessions.get(user_id)
        if not session:
            return {"error": "No active session"}

        if session.process and session.is_running:
            try:
                session.process.kill()
            except Exception:
                pass

        if session.sandbox_info:
            sandbox_manager.discard(session.session_id)

        try:
            from app.database import SessionLocal, AiWorkspaceSession
            db = SessionLocal()
            db_sess = db.query(AiWorkspaceSession).filter(
                AiWorkspaceSession.id == session.db_session_id
            ).first()
            if db_sess:
                db_sess.status = "stopped"
                db_sess.ended_at = datetime.utcnow()
                db.commit()
            db.close()
        except Exception as e:
            logger.error(f"Failed to update session status: {e}")

        if user_id in self._sessions:
            del self._sessions[user_id]

        return {"success": True, "message": "Session discarded"}

    async def stop_process(self, user_id: int) -> bool:
        session = self._sessions.get(user_id)
        if not session or not session.is_running:
            return False

        if session.process:
            try:
                session.process.kill()
            except Exception:
                pass

        session.is_running = False
        session.is_waiting = True
        return True

    async def stop_all(self):
        """Stop all active sessions."""
        for user_id in list(self._sessions.keys()):
            await self.discard(user_id)


# Singleton
ai_coder_manager = AiCoderManager()
