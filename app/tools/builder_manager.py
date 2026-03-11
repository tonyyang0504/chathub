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

from .worktree_manager import worktree_manager, WorktreeInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

TOOL_BUILDER_SYSTEM_CONTEXT = """You are a Tool Builder agent for the ChatHub platform. You are working inside an isolated git worktree sandbox.

Your job is to BUILD custom tools (skills) by writing actual code files. You can:
- Create SKILL.md files that define tool behavior
- Write Python route handlers, templates, and utility modules
- Run tests to verify your code works
- Iterate on failures until everything passes

## SKILL.md Format

Every tool MUST have a SKILL.md file with this format:

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

## Guidelines
1. The YAML frontmatter must include: name, display_name, description, icon, trigger
2. Use Bootstrap Icons (bi-*) for the icon field
3. Keep tool names as kebab-case slugs
4. Write clean, tested code
5. Commit your changes when ready with a descriptive message
6. When done, tell the user the tool is ready to publish

## Project Structure
- Skills go in the project root as SKILL.md or in a subdirectory
- Python code goes in app/ directory structure
- Templates go in app/templates/
- Static files go in static/
"""


class ToolBuilderSession:
    """An active tool builder session backed by a CLI agent in a worktree."""

    def __init__(self, session_id: str, db_session_id: int, user_id: int,
                 api_key: str, model: Optional[str] = None,
                 provider: str = "claude", auth_method: str = "api_key",
                 oauth_token: Optional[str] = None,
                 user_email: Optional[str] = None, user_name: Optional[str] = None):
        self.session_id = session_id  # UUID string
        self.db_session_id = db_session_id  # DB ClaudeCodeSession.id
        self.user_id = user_id
        self.claude_session_id = str(uuid.uuid4())  # For Claude CLI --session-id/--resume
        self._api_key = api_key
        self._model = model
        self._auth_method = auth_method
        self._oauth_token = oauth_token
        self._user_email = user_email
        self._user_name = user_name
        self.provider = provider

        # Load CLI provider adapter
        from app.claude_code.providers import get_provider
        self.cli_provider = get_provider(provider)

        self.worktree: Optional[WorktreeInfo] = None
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
        if session and (session.is_running or session.is_waiting or session.worktree):
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
        user_name: Optional[str] = None
    ) -> ToolBuilderSession:
        """Create a new tool builder session with a worktree sandbox."""
        # Stop existing session if any
        if user_id in self._sessions:
            await self.discard(user_id)

        session_id = str(uuid.uuid4())

        # Create DB session record
        from app.database import SessionLocal, ClaudeCodeSession
        db = SessionLocal()
        try:
            db_session = ClaudeCodeSession(
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

        # Create worktree
        worktree_info = worktree_manager.create(session_id, user_id)

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
        )
        session.worktree = worktree_info

        # Update DB with worktree info and session UUID
        db = SessionLocal()
        try:
            db_sess = db.query(ClaudeCodeSession).filter(ClaudeCodeSession.id == db_session_id).first()
            if db_sess:
                db_sess.worktree_path = str(worktree_info.path)
                db_sess.worktree_branch = worktree_info.branch
                db_sess.claude_session_uuid = session.claude_session_id
                db_sess.status = "running"
                db_sess.started_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        self._sessions[user_id] = session
        logger.info(f"Created tool builder session {session_id} for user {user_id}, worktree at {worktree_info.path}")
        return session

    async def send_message(self, user_id: int, message: str) -> bool:
        """Send a message to the agent by spawning a CLI turn in the worktree."""
        session = self._sessions.get(user_id)
        if not session or not session.worktree:
            logger.warning(f"No active tool builder session for user {user_id}")
            return False

        if session.is_running:
            logger.warning(f"Session {session.session_id} is already running")
            return False

        try:
            # Broadcast user message
            await session.broadcast({"type": "user_message", "content": message})

            # Spawn CLI turn
            is_first = session.turn_number == 0
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

        worktree_path = str(session.worktree.path)

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
            oauth_token=session._oauth_token
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
        from app.database import SessionLocal, ClaudeCodeSession, ClaudeCodeMessage

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
                                    tr_msg = ClaudeCodeMessage(
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
                        msg = ClaudeCodeMessage(
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
                # Clean up instruction files in worktree
                if session.worktree:
                    session.cli_provider.cleanup_session(str(session.worktree.path))

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
                db_sess = db.query(ClaudeCodeSession).filter(
                    ClaudeCodeSession.id == session.db_session_id
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
        from app.database import SessionLocal, ClaudeCodeMessage
        try:
            db = SessionLocal()
            messages = db.query(ClaudeCodeMessage).filter(
                ClaudeCodeMessage.session_id == session.db_session_id
            ).order_by(ClaudeCodeMessage.id.asc()).all()
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
        """Get worktree status for a user's active session."""
        session = self._sessions.get(user_id)
        if not session or not session.worktree:
            return {"error": "No active session"}

        status = worktree_manager.get_status(session.session_id)
        changed_files = worktree_manager.get_changed_files(session.session_id)

        return {
            "session_id": session.session_id,
            "provider": session.provider,
            "is_running": session.is_running,
            "is_waiting": session.is_waiting,
            "turn_number": session.turn_number,
            "branch": session.worktree.branch,
            "worktree_path": str(session.worktree.path),
            "changed_files": changed_files,
            "diff_stat": status.get("diff_stat", ""),
            "log": status.get("log", ""),
        }

    async def publish(self, user_id: int) -> dict:
        """Merge worktree branch into main, extract skill, create ChatHubAgentSkill record."""
        session = self._sessions.get(user_id)
        if not session or not session.worktree:
            return {"error": "No active session"}

        # Try to find SKILL.md in changed files
        changed_files = worktree_manager.get_changed_files(session.session_id)
        skill_md_content = None
        skill_file = None

        for f in changed_files:
            if f.upper().endswith("SKILL.MD"):
                skill_file = f
                # Read the file from worktree
                skill_path = session.worktree.path / f
                if skill_path.exists():
                    skill_md_content = skill_path.read_text()
                break

        # Merge worktree
        result = worktree_manager.merge(session.session_id)
        if not result.success:
            return {"error": result.message}

        # Create skill record if SKILL.md was found
        skill_id = None
        if skill_md_content:
            skill_id = self._create_skill_from_md(user_id, skill_md_content)

        # Clean up session
        if user_id in self._sessions:
            del self._sessions[user_id]

        return {
            "success": True,
            "message": result.message,
            "commit_hash": result.commit_hash,
            "merged_branch": result.merged_branch,
            "skill_id": skill_id,
            "skill_file": skill_file,
        }

    def _create_skill_from_md(self, user_id: int, skill_md_content: str) -> Optional[int]:
        """Parse SKILL.md and create a ChatHubAgentSkill record."""
        import re
        from app.database import SessionLocal, ChatHubAgentSkill

        # Parse YAML frontmatter
        fm_match = re.match(r'^---\s*\n([\s\S]*?)\n---', skill_md_content)
        if not fm_match:
            return None

        yaml_text = fm_match.group(1)
        fields = {}
        for line in yaml_text.split('\n'):
            m = re.match(r'^(\w+)\s*:\s*(.+)$', line)
            if m:
                fields[m.group(1)] = m.group(2).strip().strip('"\'')

        name = fields.get("name", "unnamed-tool")

        db = SessionLocal()
        try:
            # Check for existing
            existing = db.query(ChatHubAgentSkill).filter(
                ChatHubAgentSkill.user_id == user_id,
                ChatHubAgentSkill.name == name
            ).first()

            if existing:
                existing.display_name = fields.get("display_name", name)
                existing.description = fields.get("description", "")
                existing.icon = fields.get("icon", "bi-gear")
                existing.skill_md_content = skill_md_content
                existing.updated_at = datetime.utcnow()
                db.commit()
                return existing.id
            else:
                skill = ChatHubAgentSkill(
                    user_id=user_id,
                    name=name,
                    display_name=fields.get("display_name", name),
                    description=fields.get("description", ""),
                    icon=fields.get("icon", "bi-gear"),
                    gradient_start=fields.get("gradient_start", "#6366f1"),
                    gradient_end=fields.get("gradient_end", "#8b5cf6"),
                    skill_md_content=skill_md_content,
                    is_active=True,
                    tier="managed"
                )
                db.add(skill)
                db.commit()
                db.refresh(skill)
                return skill.id
        except Exception as e:
            logger.error(f"Failed to create skill from SKILL.md: {e}")
            db.rollback()
            return None
        finally:
            db.close()

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

        # Discard worktree
        if session.worktree:
            worktree_manager.discard(session.session_id)

        # Update DB
        try:
            from app.database import SessionLocal, ClaudeCodeSession
            db = SessionLocal()
            db_sess = db.query(ClaudeCodeSession).filter(
                ClaudeCodeSession.id == session.db_session_id
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
