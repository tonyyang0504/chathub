"""
ChatHub Agent Manager
Singleton manager for ChatHub Agent sessions with safety commits and DB backups.
Follows the same pattern as ClaudeCodeManager but uses AgentLoop instead of subprocess.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class AgentSession:
    """Represents an active ChatHub Agent session."""

    def __init__(self, session_id: int, user_id: int, agent_loop):
        self.session_id = session_id
        self.user_id = user_id
        self.agent_loop = agent_loop  # AgentLoop instance
        self.websockets: Set[WebSocket] = set()
        self.output_buffer: list = []
        self.is_running = True
        self.is_waiting = False
        self._loop_task: Optional[asyncio.Task] = None

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


class ChatHubAgentManager:
    """
    Singleton manager for ChatHub Agent sessions.
    One active session per user enforced.
    """

    def __init__(self):
        self._sessions: Dict[int, AgentSession] = {}  # user_id -> AgentSession

    def get_active_session(self, user_id: int) -> Optional[AgentSession]:
        """Get the active session for a user."""
        session = self._sessions.get(user_id)
        if session and session.is_running:
            return session
        return None

    def create_safety_commit(self) -> Optional[str]:
        """Create a safety git commit before running agent. Returns commit hash or None."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            if not result.stdout.strip():
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT)
                )
                return head.stdout.strip() if head.returncode == 0 else None

            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            msg = f"[ChatHub Agent] Safety commit before session - {timestamp}"
            subprocess.run(
                ["git", "commit", "-m", msg],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            commit_hash = head.stdout.strip() if head.returncode == 0 else None
            logger.info(f"Safety commit created: {commit_hash}")
            return commit_hash
        except Exception as e:
            logger.error(f"Failed to create safety commit: {e}")
            return None

    def create_db_backup(self) -> Optional[str]:
        """Backup the SQLite database. Returns backup path or None."""
        try:
            from app.config import settings
            db_url = settings.DATABASE_URL
            db_path = db_url.replace("sqlite:///", "").replace("sqlite:", "")

            if not db_path or not os.path.exists(db_path):
                logger.warning(f"Database file not found at {db_path}")
                return None

            backup_dir = PROJECT_ROOT / "data" / "backups" / "chathub_agent"
            backup_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"app_db_backup_{timestamp}.db"
            shutil.copy2(db_path, str(backup_path))
            logger.info(f"DB backup created: {backup_path}")
            return str(backup_path)
        except Exception as e:
            logger.error(f"Failed to create DB backup: {e}")
            return None

    async def start_session(
        self,
        user_id: int,
        session_id: int,
        prompt: str,
        settings_dict: dict,
        agent_config_dict: dict,
        file_paths: list = None,
    ) -> Optional[AgentSession]:
        """Create an AgentLoop and start it in a background task."""
        # Enforce one session per user
        existing = self.get_active_session(user_id)
        if existing:
            logger.warning(f"User {user_id} already has an active session {existing.session_id}")
            return None

        try:
            from app.ai.factory import get_ai_provider
            from app.chathub_agent.tool_executor import ToolExecutor
            from app.chathub_agent.agent_loop import AgentLoop
            from app.chathub_agent.skills import SkillRegistry
            from app.database import SessionLocal

            # Create AI provider
            provider = get_ai_provider(
                provider_name=agent_config_dict.get("ai_provider", settings_dict.get("ai_provider", "openai")),
                api_key=settings_dict["api_key"],
                model=agent_config_dict.get("model", settings_dict.get("default_model")),
            )

            # Create tool executor
            workspace = agent_config_dict.get("workspace_path") or settings_dict.get("workspace_path") or str(PROJECT_ROOT)
            tool_executor = ToolExecutor(workspace_root=workspace)

            # Create session object
            session = AgentSession(session_id, user_id, None)
            self._sessions[user_id] = session

            # Create agent loop with session's broadcast method
            agent_loop = AgentLoop(
                session_id=session_id,
                provider=provider,
                tool_executor=tool_executor,
                agent_config=agent_config_dict,
                broadcast_fn=session.broadcast,
                db_factory=SessionLocal,
            )

            # Inject skill instructions
            registry = SkillRegistry()
            registry.load_all(workspace)
            enabled_skills = agent_config_dict.get("enabled_skills")
            if isinstance(enabled_skills, str):
                enabled_skills = json.loads(enabled_skills)
            agent_loop.skill_instructions = registry.get_system_prompt_injection(enabled_skills)

            session.agent_loop = agent_loop

            # Run agent loop in background task
            session._loop_task = asyncio.create_task(
                self._run_session(session, prompt, file_paths=file_paths)
            )

            logger.info(f"Started ChatHub Agent session {session_id} for user {user_id}")
            return session

        except Exception as e:
            logger.exception(f"Failed to start ChatHub Agent session: {e}")
            if user_id in self._sessions:
                del self._sessions[user_id]
            return None

    async def _run_session(self, session: AgentSession, prompt: str, file_paths: list = None):
        """Run the agent loop and handle completion."""
        try:
            effective_prompt = prompt
            if file_paths:
                file_lines = "\n".join(f"  - {p}" for p in file_paths)
                effective_prompt = (
                    f"The user has attached the following files (available on the server filesystem):\n"
                    f"{file_lines}\n\n"
                    f"User's message: {prompt}"
                )
            await session.agent_loop.run(effective_prompt)
            session.is_waiting = True
        except asyncio.CancelledError:
            logger.info(f"Agent loop cancelled for session {session.session_id}")
        except Exception as e:
            logger.error(f"Agent loop error for session {session.session_id}: {e}")
            await session.broadcast({"type": "error", "content": str(e)})
            await session.broadcast({"type": "session_end", "exit_code": 1})
            session.is_running = False
            self._update_session_status(session.session_id, "failed")
            if session.user_id in self._sessions and self._sessions[session.user_id] is session:
                del self._sessions[session.user_id]

    async def send_message(self, user_id: int, message: str, file_paths: list = None) -> bool:
        """Send a follow-up message to an active session."""
        session = self.get_active_session(user_id)
        if not session or not session.agent_loop:
            logger.warning(f"No active session for user {user_id}")
            return False
        if not session.agent_loop.is_waiting:
            logger.warning(f"Session {session.session_id} is not waiting for input")
            return False

        # Broadcast user message to WebSocket clients
        await session.broadcast({"type": "user_message", "content": message})

        # Run follow-up in background task
        session._loop_task = asyncio.create_task(
            self._run_followup(session, message, file_paths=file_paths)
        )
        return True

    async def _run_followup(self, session: AgentSession, message: str, file_paths: list = None):
        """Run a follow-up message through the agent loop."""
        try:
            effective_message = message
            if file_paths:
                file_lines = "\n".join(f"  - {p}" for p in file_paths)
                effective_message = (
                    f"The user has attached the following files (available on the server filesystem):\n"
                    f"{file_lines}\n\n"
                    f"User's message: {message}"
                )
            await session.agent_loop.handle_followup(effective_message)
        except Exception as e:
            logger.error(f"Follow-up error for session {session.session_id}: {e}")
            await session.broadcast({"type": "error", "content": str(e)})
        finally:
            session.is_waiting = True
            session.agent_loop.is_waiting = True

    async def resume_session(
        self,
        user_id: int,
        session_id: int,
        settings_dict: dict,
        agent_config_dict: dict,
    ) -> Optional[AgentSession]:
        """Reconstruct an AgentSession from DB-persisted messages (e.g. after server restart)."""
        # Don't resume if user already has an active session
        existing = self.get_active_session(user_id)
        if existing:
            logger.warning(f"User {user_id} already has an active session {existing.session_id}")
            return None

        try:
            from app.ai.factory import get_ai_provider
            from app.chathub_agent.tool_executor import ToolExecutor
            from app.chathub_agent.agent_loop import AgentLoop
            from app.chathub_agent.skills import SkillRegistry
            from app.database import SessionLocal, ChatHubAgentMessage

            # Create AI provider
            provider = get_ai_provider(
                provider_name=agent_config_dict.get("ai_provider", settings_dict.get("ai_provider", "openai")),
                api_key=settings_dict["api_key"],
                model=agent_config_dict.get("model", settings_dict.get("default_model")),
            )

            # Create tool executor
            workspace = agent_config_dict.get("workspace_path") or settings_dict.get("workspace_path") or str(PROJECT_ROOT)
            tool_executor = ToolExecutor(workspace_root=workspace)

            # Create session object
            session = AgentSession(session_id, user_id, None)
            self._sessions[user_id] = session

            # Create agent loop
            agent_loop = AgentLoop(
                session_id=session_id,
                provider=provider,
                tool_executor=tool_executor,
                agent_config=agent_config_dict,
                broadcast_fn=session.broadcast,
                db_factory=SessionLocal,
            )

            # Inject skill instructions
            registry = SkillRegistry()
            registry.load_all(workspace)
            enabled_skills = agent_config_dict.get("enabled_skills")
            if isinstance(enabled_skills, str):
                enabled_skills = json.loads(enabled_skills)
            agent_loop.skill_instructions = registry.get_system_prompt_injection(enabled_skills)

            session.agent_loop = agent_loop

            # Rebuild conversation from DB messages
            db = SessionLocal()
            try:
                db_messages = db.query(ChatHubAgentMessage).filter(
                    ChatHubAgentMessage.session_id == session_id
                ).order_by(ChatHubAgentMessage.created_at.asc()).all()

                # Build system prompt (same as AgentLoop.run)
                system_prompt = agent_config_dict.get("system_prompt") or agent_loop._default_system_prompt()
                user_ctx = agent_config_dict.get("user_context")
                if user_ctx:
                    system_prompt += (
                        f"\n\n## Current User\n"
                        f"- User ID: {user_ctx.get('user_id')}\n"
                        f"- Email: {user_ctx.get('email')}\n"
                        f"- Name: {user_ctx.get('name')}\n"
                        f"When querying the database for user-specific data, use user_id = {user_ctx.get('user_id')}."
                    )
                if agent_loop.skill_instructions:
                    system_prompt += "\n\n" + agent_loop.skill_instructions

                conversation = [{"role": "system", "content": system_prompt}]

                for msg in db_messages:
                    if msg.role == "user":
                        conversation.append({"role": "user", "content": msg.content or ""})
                    elif msg.role == "assistant":
                        conversation.append({"role": "assistant", "content": msg.content or ""})
                    elif msg.role == "tool_call":
                        # Append as tool_calls on the last assistant message
                        if conversation and conversation[-1]["role"] == "assistant":
                            if "tool_calls" not in conversation[-1]:
                                conversation[-1]["tool_calls"] = []
                            conversation[-1]["tool_calls"].append({
                                "id": msg.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": msg.tool_name,
                                    "arguments": msg.content or "{}",
                                },
                            })
                        else:
                            # No preceding assistant message — create one
                            conversation.append({
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": msg.tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": msg.tool_name,
                                        "arguments": msg.content or "{}",
                                    },
                                }],
                            })
                    elif msg.role == "tool_result":
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": msg.tool_call_id,
                            "content": msg.content or "",
                        })
                    # Skip system, approval_request roles

                # Pop the last user message if present — handle_followup() will re-append it
                if len(conversation) > 1 and conversation[-1].get("role") == "user":
                    conversation.pop()

                # Fix conversation ordering for OpenAI: tool results must immediately
                # follow the assistant message with tool_calls. Messages may be interleaved
                # in the DB (e.g., user sent messages while tool was executing).
                # Strategy: pull tool results out, then re-insert them right after their
                # parent assistant message, adding placeholders for any missing ones.

                # Collect all tool results by tool_call_id
                tool_results_by_id = {}
                for msg in conversation:
                    if msg.get("role") == "tool" and msg.get("tool_call_id"):
                        tool_results_by_id[msg["tool_call_id"]] = msg

                # Rebuild: skip inline tool results, insert them after their parent assistant msg
                fixed = []
                for msg in conversation:
                    if msg.get("role") == "tool":
                        continue  # Will be re-inserted after parent assistant message
                    fixed.append(msg)
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            if tc["id"] in tool_results_by_id:
                                fixed.append(tool_results_by_id[tc["id"]])
                            else:
                                fixed.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": "[Session interrupted — tool was not executed]",
                                })
                conversation = fixed

                agent_loop.set_conversation(conversation)
            finally:
                db.close()

            logger.info(f"Resumed ChatHub Agent session {session_id} for user {user_id} with {len(agent_loop.conversation)} messages")
            return session

        except Exception as e:
            logger.exception(f"Failed to resume ChatHub Agent session: {e}")
            if user_id in self._sessions:
                del self._sessions[user_id]
            return None

    def update_session_config(self, user_id: int, config_updates: dict):
        """Push config updates to a running session's agent loop."""
        session = self.get_active_session(user_id)
        if session and session.agent_loop:
            session.agent_loop.update_config(config_updates)

    def approve_tool(self, user_id: int, approved: bool) -> bool:
        """Approve or deny a pending tool call."""
        session = self.get_active_session(user_id)
        if not session or not session.agent_loop:
            return False
        session.agent_loop.approve(approved)
        return True

    async def stop_session(self, user_id: int) -> bool:
        """Stop a running session."""
        session = self._sessions.get(user_id)
        if not session:
            return False

        session.is_running = False

        # Stop agent loop
        if session.agent_loop:
            session.agent_loop.stop()

        # Cancel background task
        if session._loop_task and not session._loop_task.done():
            session._loop_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(session._loop_task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Update DB
        self._update_session_status(session.session_id, "stopped")

        # Notify clients
        await session.broadcast({"type": "session_end", "exit_code": -1, "stopped": True})

        logger.info(f"Stopped session {session.session_id} for user {user_id}")

        if user_id in self._sessions:
            del self._sessions[user_id]
        return True

    def _update_session_status(self, session_id: int, status: str):
        """Update session status in the database."""
        try:
            from app.database import SessionLocal
            from app.database import ChatHubAgentSession
            db = SessionLocal()
            try:
                db_session = db.query(ChatHubAgentSession).filter(
                    ChatHubAgentSession.id == session_id
                ).first()
                if db_session:
                    db_session.status = status
                    db_session.ended_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Failed to update session status: {e}")

    def rollback_session(self, git_hash: Optional[str], db_backup_path: Optional[str]) -> dict:
        """Rollback git state and/or DB to pre-session state."""
        result = {"git": False, "db": False}

        if git_hash:
            try:
                r = subprocess.run(
                    ["git", "reset", "--hard", git_hash],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT)
                )
                result["git"] = r.returncode == 0
                if result["git"]:
                    logger.info(f"Git rolled back to {git_hash}")
                else:
                    logger.error(f"Git rollback failed: {r.stderr}")
            except Exception as e:
                logger.error(f"Git rollback error: {e}")

        if db_backup_path and os.path.exists(db_backup_path):
            try:
                from app.config import settings
                db_url = settings.DATABASE_URL
                db_path = db_url.replace("sqlite:///", "").replace("sqlite:", "")
                if db_path and os.path.exists(db_path):
                    shutil.copy2(db_backup_path, db_path)
                    result["db"] = True
                    logger.info(f"DB restored from {db_backup_path}")
            except Exception as e:
                logger.error(f"DB rollback error: {e}")

        return result

    async def stop_all(self):
        """Stop all active sessions (shutdown hook)."""
        user_ids = list(self._sessions.keys())
        for user_id in user_ids:
            try:
                await self.stop_session(user_id)
            except Exception as e:
                logger.error(f"Error stopping session for user {user_id}: {e}")


# Singleton instance
chathub_agent_manager = ChatHubAgentManager()
