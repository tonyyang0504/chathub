"""
Tool Builder Agent Manager
Delegates to CLI coding agents (Claude Code, Codex, Gemini CLI) running in
isolated git worktree sandboxes. Replaces the old chat-completion wrapper.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket

from .sandbox_manager import sandbox_manager, SandboxInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

TOOL_BUILDER_SYSTEM_CONTEXT = """You are a Tool Builder agent for the ChatHub platform. You are working inside an isolated git worktree sandbox.

Your job is to BUILD custom tools by writing actual code files. You can:
- Create TOOL.md files that define tool behavior
- Write Python route handlers, templates, and utility modules
- Run tests to verify your code works
- Iterate on failures until everything passes

## MANDATORY WORKFLOW: Plan First, Build Second

You MUST follow these phases in order. Do NOT skip to building.

### Phase 1 — Understand

**BEFORE proposing anything**, you MUST explore the codebase to understand the project you're working in:

1. **Read the project broadly** — browse the directory structure, read source files, templates, models, routes, and stylesheets. Understand what already exists, how pages are structured, what database models are available, and what UI patterns are used. The more you read, the better your proposal will be.

2. **Dive deeper into areas related to the user's request** — read any files, templates, routes, or models that are relevant to what the user is asking for. Explore thoroughly so your proposal is grounded in the actual codebase.

3. **Then** analyze the user's request with this context. If anything is unclear, ask clarifying questions based on what you learned from the codebase.

If the request is already clear and detailed, move directly to Phase 2.

**IMPORTANT**: Never propose a tool based on assumptions. Your proposal must reference actual models, routes, templates, and patterns from this project — not generic guesses. Read the code first.

### Phase 2 — Propose
Present a concrete plan to the user:
- **Tool name** (kebab-case slug) and **display name**
- **What it does** — 2-3 sentence summary
- **Key features** — bullet list of capabilities
- **UI layout** — what the page will look like (cards, tables, forms, etc.)
- **Routes needed** — API endpoints the tool will expose
- **Database needs** — any new tables or use of existing ones

### Phase 3 — Confirm
Ask the user to approve the plan. Wait for explicit confirmation ("yes", "go ahead", "looks good", "approved", etc.) before writing ANY code files. If the user wants changes, revise the plan and ask again.

### Phase 4 — Build
Only after confirmation, execute ALL of the following steps. Do NOT skip any. Do NOT ask the user whether to proceed between steps — just do them all.

1. **Create TOOL.md FIRST** — Before writing any code, create `app/tools/custom/{tool-name}/TOOL.md` with complete frontmatter (name, display_name, description, icon, trigger) and body content. Commit it immediately.
2. **Write all code files** — routes.py, templates, static files, etc. following the Plugin Structure below.
3. **Run tests** — Run `pytest` to verify nothing is broken. If tests fail, fix the issues and re-run until they pass.
4. **Update TOOL.md** — Re-read your implementation and update TOOL.md with polished metadata reflecting what you actually built. Commit with message "Update TOOL.md with final tool metadata".
5. **Report completion** — Tell the user the tool is ready. Instruct them to preview it in the isolated environment to verify it meets their requirements. If not, they can send a message to continue modifying or optimizing. If it looks good, they can click the Publish button to add it to their application. Do NOT ask "want me to run tests?" or "should I verify?" — you must have already done it.

**Shortcut**: If the user's request is already very specific and detailed (e.g., includes exact features, UI layout, routes), you may compress Phases 1-3 into a brief summary: "Here's what I'll build: [summary]. Shall I proceed?" — then wait for confirmation.

## TOOL.md Format

Every tool MUST have a TOOL.md file with this format:

```
---
name: tool-slug-name
display_name: Human Readable Name
description: Short description of what the tool does
icon: bi-icon-name
trigger: keyword or pattern that activates this tool
---

# System Prompt

You are a specialized assistant that [does what the tool does].

## Instructions
- Step by step instructions for the AI
- How to handle different scenarios
- What format to respond in

## Examples
User: example input
Assistant: example output
```

## Plugin Structure (CRITICAL — follow exactly)

Custom tools are self-contained plugins. All files MUST go inside `app/tools/custom/{tool-name}/`:

```
app/tools/custom/{tool-name}/
├── routes.py          # Tool's own FastAPI router (router = APIRouter())
├── templates/
│   └── {tool-name}.html   # Jinja2 template extending base.html
├── static/            # Optional CSS/JS files
└── TOOL.md            # Tool manifest (REQUIRED)
```

### routes.py Template
```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from app.database import get_db
from app.auth.utils import get_current_user_optional

router = APIRouter(tags=["{tool-name}"])

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

@router.get("/", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse(
        "{tool-name}.html",
        {"request": request, "user": user, "page_title": "{Tool Display Name}"}
    )

# Add your API endpoints here...
```

### IMPORTANT RULES
- **NEVER** modify shared files like `app/tools/routes.py` or `app/templates/dashboard/tools/index.html`
- **NEVER** put files outside of `app/tools/custom/{tool-name}/`
- The router will be auto-registered at `/tools/{tool-name}` — do NOT add a prefix in your router
- Templates should extend `base.html` using the full path: `{% extends "base.html" %}`
- Use Bootstrap 5.3.2, Bootstrap Icons, and the existing CSS variables from the platform

## Guidelines
1. **FIRST**: Follow the planning workflow (Phases 1-3) before writing any code
2. Create and commit initial TOOL.md with frontmatter (name, display_name, description, icon, trigger)
3. The YAML frontmatter must include: name, display_name, description, icon, trigger
4. Use Bootstrap Icons (bi-*) for the icon field
5. Keep tool names as kebab-case slugs
6. Write clean, tested code
7. Commit your changes when ready with a descriptive message
8. **LAST STEP**: Update TOOL.md with polished metadata reflecting the actual implementation, then commit
9. **NEVER ask the user** whether to run tests, verify code, or do sanity checks. Just do them. Your job is to deliver a finished, tested tool — not to ask permission at every step.
10. When done, tell the user the tool is ready. Instruct them to preview it in the isolated environment to verify it meets their requirements. If not, they can send a message to continue modifying or optimizing. If it looks good, they can click the Publish button to add it to their application.
"""


class ToolBuilderSession:
    """An active tool builder session backed by a CLI agent in a worktree."""

    def __init__(self, session_id: str, db_session_id: int, user_id: int,
                 api_key: str, model: Optional[str] = None,
                 provider: str = "claude", auth_method: str = "api_key",
                 oauth_token: Optional[str] = None,
                 user_email: Optional[str] = None, user_name: Optional[str] = None,
                 ai_provider: Optional[str] = None):
        self.session_id = session_id  # UUID string
        self.db_session_id = db_session_id  # DB AiWorkspaceSession.id
        self.user_id = user_id
        self.claude_session_id = str(uuid.uuid4())  # For Claude CLI --session-id/--resume
        self._api_key = api_key
        self._model = model
        self._auth_method = auth_method
        self._oauth_token = oauth_token
        self._user_email = user_email
        self._user_name = user_name
        self._ai_provider = ai_provider  # AI backend for ChatHub provider (openai/anthropic/google)
        self.provider = provider

        # Load CLI provider adapter
        from app.ai_workspace.providers import get_provider
        self.cli_provider = get_provider(provider)

        self.worktree: Optional[SandboxInfo] = None  # sandbox_info, kept as 'worktree' attr for compat
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


class ToolBuilderManager:
    """Manages tool builder sessions with CLI agents in worktree sandboxes."""

    def __init__(self):
        self._sessions: Dict[int, ToolBuilderSession] = {}  # user_id -> session

    def get_session(self, user_id: int) -> Optional[ToolBuilderSession]:
        """Get the active session for a user."""
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
    ) -> ToolBuilderSession:
        """Create a new tool builder session with a worktree sandbox."""
        # Stop existing session if any
        if user_id in self._sessions:
            await self.discard(user_id)

        session_id = str(uuid.uuid4())

        # Create DB session record
        from app.database import SessionLocal, AiWorkspaceSession
        db = SessionLocal()
        try:
            db_session = AiWorkspaceSession(
                user_id=user_id,
                status="pending",
                prompt="[Tool Builder Session]",
                provider=provider,
                model=model,
                session_type="tool_builder",
            )
            db.add(db_session)
            db.commit()
            db.refresh(db_session)
            db_session_id = db_session.id
        finally:
            db.close()

        # Create Docker sandbox
        sandbox_info = sandbox_manager.create(session_id, user_id)

        # Create session object
        session = ToolBuilderSession(
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
        session.worktree = sandbox_info  # backward compat

        # Update DB with sandbox info and session UUID
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
        logger.info(f"Created tool builder session {session_id} for user {user_id} (preview deferred)")
        return session

    async def start_preview(self, user_id: int) -> dict:
        """Trigger Docker install + container on demand. Runs in threadpool (blocks up to 3 min)."""
        session = self._sessions.get(user_id)
        if not session or not session.sandbox_info:
            raise RuntimeError("No active session")
        info = sandbox_manager.start_preview(session.sandbox_info.session_id)
        # Update DB with container name now that it exists
        try:
            from app.database import SessionLocal, AiWorkspaceSession
            db = SessionLocal()
            db_sess = db.query(AiWorkspaceSession).filter(
                AiWorkspaceSession.id == session.db_session_id
            ).first()
            if db_sess:
                db_sess.worktree_path = info.container_name
                db.commit()
            db.close()
        except Exception as e:
            logger.warning(f"Failed to update container name in DB: {e}")
        return {"preview_url": info.preview_url, "preview_port": info.preview_port}

    async def send_message(self, user_id: int, message: str) -> bool:
        """Send a message to the agent by spawning a CLI turn in the worktree."""
        session = self._sessions.get(user_id)
        if not session or not session.sandbox_info:
            logger.warning(f"No active tool builder session for user {user_id}")
            return False

        if session.is_running:
            logger.warning(f"Session {session.session_id} is already running")
            return False

        try:
            # Save title on first turn
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

            # Broadcast user message
            await session.broadcast({"type": "user_message", "content": message})

            # Spawn CLI turn
            await self._run_turn(session, message, is_first=is_first)
            return True
        except Exception as e:
            logger.error(f"Failed to send message in tool builder session: {e}")
            await session.broadcast({"type": "error", "error": {"message": str(e)}})
            return False

    async def _run_turn(self, session: ToolBuilderSession, prompt: str, is_first: bool = False):
        """Spawn one CLI process for a single turn in the worktree directory."""
        session.turn_number += 1

        # For stateless providers, prepend conversation history on follow-up turns
        effective_prompt = prompt
        if not is_first and not session.cli_provider.supports_resume:
            history = self._build_conversation_history(session)
            if history:
                effective_prompt = history + effective_prompt

        # Build system context
        system_context = TOOL_BUILDER_SYSTEM_CONTEXT
        if session._user_name:
            system_context += f"\n\nYou are building tools on behalf of: {session._user_name}"
        if session._user_email:
            system_context += f" ({session._user_email})"

        # Use isolated worktree so the AI never edits the live project directly
        worktree_path = str(
            session.sandbox_info.worktree_path
            if session.sandbox_info and session.sandbox_info.worktree_path
            else PROJECT_ROOT
        )

        # Build CLI command via provider
        cmd = session.cli_provider.build_command(
            prompt=effective_prompt,
            session_uuid=session.claude_session_id,
            is_first=is_first,
            model=session._model,
            system_context=system_context
        )

        # Build environment
        base_env = os.environ.copy()
        env = session.cli_provider.build_env(
            base_env,
            api_key=session._api_key,
            auth_method=session._auth_method,
            oauth_token=session._oauth_token,
            ai_provider=session._ai_provider,
        )

        # Add API token for server access
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

        # Write instruction file for non-Claude providers (in worktree dir)
        session.cli_provider.prepare_session(worktree_path, system_context)

        # Spawn subprocess in worktree directory
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,  # KEY DIFFERENCE: run in worktree, not PROJECT_ROOT
            env=env,
            limit=10 * 1024 * 1024,
        )

        session.process = process
        session.is_running = True
        session.is_waiting = False

        # Start reading stdout and stderr concurrently
        session._read_task = asyncio.create_task(self._read_output(session))
        session._stderr_task = asyncio.create_task(self._read_stderr(session))

        logger.info(f"Tool builder turn started, session={session.session_id}, PID={process.pid}, cwd={worktree_path}")

    async def _read_stderr(self, session: ToolBuilderSession):
        """Read stderr line-by-line."""
        process = session.process
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.debug(f"ToolBuilder {session.session_id} stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stderr: {e}")

    async def _read_output(self, session: ToolBuilderSession):
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

                # Normalize non-Claude provider events
                if session.provider != "claude":
                    data = session.cli_provider.normalize_event(data)
                    if data is None:
                        continue

                # Extract role/content for DB persistence
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
                    # Extract tool_result from auto-submitted user messages
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

                # Skip system init events
                if msg_type == "system" and data.get("subtype") == "init":
                    continue

                # Persist to DB
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

                # Broadcast to WebSockets
                await session.broadcast(data)

                # After result event (Claude only), broadcast result_done marker
                if msg_type == "result" and session.provider == "claude":
                    await session.broadcast({"type": "result_done"})

            # Wait for stderr reader
            if stderr_task and not stderr_task.done():
                try:
                    await asyncio.wait_for(stderr_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            # Wait for exit code
            exit_code = await process.wait()

            # Only update state if this is still the active process
            if session.process is not process:
                return

            if exit_code == 0:
                session.is_running = False
                session.is_waiting = True
                # Clean up instruction files from whichever directory the session used
                _cleanup_path = (
                    str(session.sandbox_info.worktree_path)
                    if session.sandbox_info and session.sandbox_info.worktree_path
                    else str(PROJECT_ROOT)
                )
                session.cli_provider.cleanup_session(_cleanup_path)

                await session.broadcast({"type": "turn_end", "exit_code": 0})
                session.output_buffer.clear()
                logger.info(f"Tool builder turn completed, session={session.session_id}")
            else:
                session.is_running = False
                session.is_waiting = True  # Allow retry
                await session.broadcast({
                    "type": "error",
                    "error": {"message": f"Agent process exited with code {exit_code}"}
                })
                await session.broadcast({"type": "turn_end", "exit_code": exit_code})
                session.output_buffer.clear()

            # Update DB status
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

    def _build_conversation_history(self, session: ToolBuilderSession) -> str:
        """Build conversation history for stateless providers."""
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
        """Get sandbox status for a user's active session."""
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

    def _update_tool_md_frontmatter(self, session: "ToolBuilderSession", metadata: Dict) -> None:
        """Update TOOL.md frontmatter with user-provided modal form values before publishing."""
        import re

        # Find TOOL.md in the worktree
        worktree = session.sandbox_info.worktree_path
        if not worktree:
            return

        tool_md_path = None
        for candidate in ["TOOL.md", "tool.md"]:
            p = worktree / candidate
            if p.exists():
                tool_md_path = p
                break

        if not tool_md_path:
            return

        content = tool_md_path.read_text(encoding="utf-8")
        fm_match = re.match(r'^---\s*\n([\s\S]*?)\n---', content)
        if not fm_match:
            return

        yaml_text = fm_match.group(1)
        body = content[fm_match.end():]

        # Parse existing frontmatter fields
        fields = {}
        field_order = []
        for line in yaml_text.split('\n'):
            m = re.match(r'^(\w+)\s*:\s*(.+)$', line)
            if m:
                fields[m.group(1)] = m.group(2).strip().strip('"\'')
                field_order.append(m.group(1))
            elif line.strip():
                field_order.append(line)  # preserve comments etc.

        # Override with non-empty metadata values
        overrides = {
            "name": metadata.get("name", ""),
            "display_name": metadata.get("display_name", ""),
            "description": metadata.get("description", ""),
            "icon": metadata.get("icon", ""),
        }
        changed = False
        for key, val in overrides.items():
            if val and val != fields.get(key, ""):
                fields[key] = val
                if key not in field_order:
                    field_order.append(key)
                changed = True

        if not changed:
            return

        # Rebuild frontmatter
        lines = []
        for item in field_order:
            if item in fields:
                lines.append(f"{item}: {fields[item]}")
            else:
                lines.append(item)  # preserve raw lines

        new_content = "---\n" + "\n".join(lines) + "\n---" + body
        rel_path = tool_md_path.relative_to(worktree)

        sandbox_manager.write_file(session.session_id, str(rel_path), new_content)
        sandbox_manager.commit_file(session.session_id, str(rel_path), "Update TOOL.md metadata from publish form")

    async def publish(self, user_id: int, metadata: Optional[Dict] = None) -> dict:
        """Publish sandbox changes to host repo, extract tool definition, create BuiltTool record."""
        session = self._sessions.get(user_id)
        if not session or not session.sandbox_info:
            return {"error": "No active session"}

        # Update TOOL.md frontmatter with modal form values before publishing
        if metadata and metadata.get("name"):
            self._update_tool_md_frontmatter(session, metadata)

        # Try to find TOOL.md in changed files
        changed_files = sandbox_manager.get_changed_files(session.session_id)
        tool_md_content = None
        tool_file = None

        for f in changed_files:
            if f.upper().endswith("TOOL.MD"):
                tool_file = f
                tool_path = (session.sandbox_info.worktree_path or PROJECT_ROOT) / f
                if tool_path.exists():
                    tool_md_content = tool_path.read_text()
                break

        # Determine tool name for plugin directory
        tool_name = None
        if tool_md_content:
            import re as _re
            _fm = _re.match(r'^---\s*\n([\s\S]*?)\n---', tool_md_content)
            if _fm:
                for _line in _fm.group(1).split('\n'):
                    _m = _re.match(r'^name\s*:\s*(.+)$', _line)
                    if _m:
                        tool_name = _m.group(1).strip().strip('"\'')
                        break
        if not tool_name and metadata:
            tool_name = (metadata.get("name") or "").strip()

        # Publish sandbox changes to host repo (into app/tools/custom/{tool_name}/)
        result = sandbox_manager.publish(session.session_id, tool_name=tool_name or None)
        if not result.get("success"):
            return {"error": result.get("message", "Publish failed")}

        # Get the list of changed files for tracking
        changed_files_list = result.get("changed_files", [])

        # Create tool record: try TOOL.md first, fall back to user-provided metadata
        tool_id = None
        if tool_md_content:
            tool_id = self._create_tool_from_md(user_id, tool_md_content, files=changed_files_list)
        if not tool_id and metadata:
            tool_id = self._create_tool_from_metadata(user_id, metadata, files=changed_files_list)

        # Update DB session status to published
        try:
            from app.database import SessionLocal, AiWorkspaceSession
            db = SessionLocal()
            db_sess = db.query(AiWorkspaceSession).filter(
                AiWorkspaceSession.id == session.db_session_id
            ).first()
            if db_sess:
                db_sess.status = "published"
                db.commit()
            db.close()
        except Exception as e:
            logger.error(f"Failed to update session status to published: {e}")

        # Remove from active sessions (but keep DB record for history)
        if user_id in self._sessions:
            del self._sessions[user_id]

        return {
            "success": True,
            "message": result.get("message", "Published"),
            "commit_hash": result.get("commit_hash", ""),
            "merged_branch": result.get("merged_branch", ""),
            "skill_id": tool_id,
            "skill_file": tool_file,
        }

    def _create_tool_from_md(self, user_id: int, tool_md_content: str, files: list = None) -> Optional[int]:
        """Parse TOOL.md and create a BuiltTool record."""
        import re
        from app.database import SessionLocal, BuiltTool

        # Parse YAML frontmatter
        fm_match = re.match(r'^---\s*\n([\s\S]*?)\n---', tool_md_content)
        if not fm_match:
            return None

        yaml_text = fm_match.group(1)
        fields = {}
        for line in yaml_text.split('\n'):
            m = re.match(r'^(\w+)\s*:\s*(.+)$', line)
            if m:
                fields[m.group(1)] = m.group(2).strip().strip('"\'')

        name = fields.get("name", "unnamed-tool")
        files_json = json.dumps(files) if files else None

        db = SessionLocal()
        try:
            # Check for existing
            existing = db.query(BuiltTool).filter(
                BuiltTool.user_id == user_id,
                BuiltTool.name == name
            ).first()

            if existing:
                existing.display_name = fields.get("display_name", name)
                existing.description = fields.get("description", "")
                existing.icon = fields.get("icon", "bi-gear")
                existing.tool_md_content = tool_md_content
                existing.files = files_json
                existing.updated_at = datetime.utcnow()
                db.commit()
                return existing.id
            else:
                tool = BuiltTool(
                    user_id=user_id,
                    name=name,
                    display_name=fields.get("display_name", name),
                    description=fields.get("description", ""),
                    icon=fields.get("icon", "bi-gear"),
                    gradient_start=fields.get("gradient_start", "#6366f1"),
                    gradient_end=fields.get("gradient_end", "#8b5cf6"),
                    tool_md_content=tool_md_content,
                    files=files_json,
                    is_active=True,
                )
                db.add(tool)
                db.commit()
                db.refresh(tool)
                return tool.id
        except Exception as e:
            logger.error(f"Failed to create tool from TOOL.md: {e}")
            db.rollback()
            return None
        finally:
            db.close()

    def _create_tool_from_metadata(self, user_id: int, metadata: Dict, files: list = None) -> Optional[int]:
        """Create a BuiltTool record from user-provided form metadata."""
        from app.database import SessionLocal, BuiltTool

        name = metadata.get("name", "").strip()
        if not name:
            return None

        display_name = metadata.get("display_name", "") or name
        description = metadata.get("description", "")
        icon = metadata.get("icon", "bi-gear") or "bi-gear"
        gradient_start = metadata.get("gradient_start", "#6366f1")
        gradient_end = metadata.get("gradient_end", "#8b5cf6")
        files_json = json.dumps(files) if files else None

        db = SessionLocal()
        try:
            existing = db.query(BuiltTool).filter(
                BuiltTool.user_id == user_id,
                BuiltTool.name == name
            ).first()

            if existing:
                existing.display_name = display_name
                existing.description = description
                existing.icon = icon
                existing.files = files_json
                existing.updated_at = datetime.utcnow()
                db.commit()
                return existing.id
            else:
                # Generate a basic TOOL.md so the tool can be published to marketplace
                tool_md = f"---\nname: {name}\ndisplay_name: {display_name}\ndescription: {description}\nicon: {icon}\ntrigger: {name}\n---\n\n# {display_name}\n\n{description}\n"

                tool = BuiltTool(
                    user_id=user_id,
                    name=name,
                    display_name=display_name,
                    description=description,
                    icon=icon,
                    gradient_start=gradient_start,
                    gradient_end=gradient_end,
                    tool_md_content=tool_md,
                    files=files_json,
                    is_active=True,
                )
                db.add(tool)
                db.commit()
                db.refresh(tool)
                return tool.id
        except Exception as e:
            logger.error(f"Failed to create tool from metadata: {e}")
            db.rollback()
            return None
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
    ) -> "ToolBuilderSession":
        """Resume a historical session from DB, reattaching it as the active session."""
        # Discard any existing active session for this user
        if user_id in self._sessions:
            await self.discard(user_id)

        # Fetch DB messages to determine turn count
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

        # Create a new in-memory session object backed by the existing DB record
        session_id = str(uuid.uuid4())
        sandbox_info = sandbox_manager.create(session_id, user_id)

        session = ToolBuilderSession(
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
        # Restore claude session UUID so CLI --resume works
        if claude_session_uuid:
            session.claude_session_id = claude_session_uuid

        # Update DB with new sandbox branch/container and mark running
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
        logger.info(f"Resumed tool builder session db_id={db_id} as {session_id} for user {user_id}")
        return session

    async def discard(self, user_id: int) -> dict:
        """Discard worktree without merging, clean up session."""
        session = self._sessions.get(user_id)
        if not session:
            return {"error": "No active session"}

        # Kill running process if any
        if session.process and session.is_running:
            try:
                session.process.kill()
            except Exception:
                pass

        # Discard sandbox
        if session.sandbox_info:
            sandbox_manager.discard(session.session_id)

        # Update DB
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

        # Remove from active sessions
        if user_id in self._sessions:
            del self._sessions[user_id]

        return {"success": True, "message": "Session discarded"}

    async def stop_process(self, user_id: int) -> bool:
        """Stop the running CLI process without discarding the worktree."""
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


# Singleton
tool_builder_manager = ToolBuilderManager()
