"""
Claude Code Process Manager
Spawns and manages Claude Code CLI subprocesses with safety commits and DB backups.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class ActiveSession:
    """Represents an active Claude Code CLI session."""

    def __init__(self, session_id: int, user_id: int, process: asyncio.subprocess.Process):
        self.session_id = session_id
        self.user_id = user_id
        self.process = process
        self.websockets: Set[WebSocket] = set()
        self.output_buffer: list = []
        self.is_running = True
        self.is_waiting = False  # True when process finished responding, awaiting next stdin message
        self._read_task: Optional[asyncio.Task] = None

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


class ClaudeCodeManager:
    """
    Singleton manager for Claude Code CLI sessions.
    One active session per user enforced.
    """

    def __init__(self):
        self._sessions: Dict[int, ActiveSession] = {}  # user_id -> ActiveSession

    def get_active_session(self, user_id: int) -> Optional[ActiveSession]:
        """Get the active session for a user."""
        session = self._sessions.get(user_id)
        if session and session.is_running:
            return session
        return None

    def create_safety_commit(self) -> Optional[str]:
        """Create a safety git commit before running Claude Code. Returns commit hash or None."""
        try:
            # Check if there are changes to commit
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            if not result.stdout.strip():
                # No changes, return current HEAD
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT)
                )
                return head.stdout.strip() if head.returncode == 0 else None

            # Stage and commit
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT)
            )
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            msg = f"[Claude Code] Safety commit before session - {timestamp}"
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

            backup_dir = PROJECT_ROOT / "data" / "backups" / "claude_code"
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
        api_key: str,
        model: Optional[str] = None
    ) -> Optional[ActiveSession]:
        """Spawn a Claude Code CLI subprocess and start reading output."""
        # Enforce one session per user
        existing = self.get_active_session(user_id)
        if existing:
            logger.warning(f"User {user_id} already has an active session {existing.session_id}")
            return None

        # Build command — multi-turn via stdin streaming (no -p prompt)
        cmd = [
            "claude",
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions"
        ]
        if model:
            cmd.extend(["--model", model])

        # Set environment — must remove Claude Code nesting guard vars,
        # otherwise the child `claude` process refuses to start with:
        # "Claude Code cannot be launched inside another Claude Code session"
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        env["ANTHROPIC_API_KEY"] = api_key

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env
            )

            session = ActiveSession(session_id, user_id, process)
            self._sessions[user_id] = session

            # Start reading output in background
            session._read_task = asyncio.create_task(
                self._read_output(session)
            )

            # Send the first message via stdin
            first_msg = json.dumps({"type": "user_input", "content": prompt}) + "\n"
            process.stdin.write(first_msg.encode())
            await process.stdin.drain()

            logger.info(f"Started Claude Code session {session_id} for user {user_id}, PID={process.pid}")
            return session

        except FileNotFoundError:
            logger.error("Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code")
            return None
        except Exception as e:
            logger.error(f"Failed to start Claude Code session: {e}")
            return None

    async def send_message(self, user_id: int, message: str) -> bool:
        """Send a follow-up message to an active session via stdin."""
        session = self.get_active_session(user_id)
        if not session or not session.is_running:
            logger.warning(f"No active session for user {user_id}")
            return False
        if not session.is_waiting:
            logger.warning(f"Session {session.session_id} is not waiting for input")
            return False

        try:
            msg_line = json.dumps({"type": "user_input", "content": message}) + "\n"
            session.process.stdin.write(msg_line.encode())
            await session.process.stdin.drain()
            session.is_waiting = False

            # Broadcast the user message to WebSocket clients
            await session.broadcast({"type": "user_message", "content": message})

            logger.info(f"Sent follow-up to session {session.session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send message to session {session.session_id}: {e}")
            return False

    async def _read_output(self, session: ActiveSession):
        """Read stdout line-by-line, parse JSON, persist to DB, broadcast to WebSockets."""
        from app.database import SessionLocal, ClaudeCodeSession, ClaudeCodeMessage

        try:
            while True:
                line = await session.process.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    data = {"type": "raw", "content": line_str}

                # Extract role and content for DB persistence
                msg_type = data.get("type", "unknown")
                role = "system"
                content = ""

                if msg_type == "assistant":
                    role = "assistant"
                    # assistant messages have content blocks
                    if "message" in data:
                        blocks = data["message"].get("content", [])
                        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                        content = "\n".join(text_parts)
                    elif "content_block" in data:
                        content = data["content_block"].get("text", "")
                elif msg_type == "content_block_delta":
                    role = "assistant"
                    delta = data.get("delta", {})
                    content = delta.get("text", "")
                elif msg_type == "content_block_start":
                    role = "assistant"
                    block = data.get("content_block", {})
                    if block.get("type") == "tool_use":
                        role = "tool_use"
                        content = json.dumps({"tool": block.get("name", ""), "id": block.get("id", "")})
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
                    # Result event — turn complete, process awaits more stdin
                    session.is_waiting = True
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
                else:
                    content = line_str

                # Persist to database
                try:
                    db = SessionLocal()
                    msg = ClaudeCodeMessage(
                        session_id=session.session_id,
                        role=role,
                        content=content[:10000] if content else "",
                        message_type=msg_type,
                        event_data=json.dumps(data)[:5000]
                    )
                    db.add(msg)
                    db.commit()
                    db.close()
                except Exception as e:
                    logger.error(f"Failed to persist message: {e}")

                # Broadcast to WebSocket clients
                await session.broadcast(data)

                # After result event, broadcast a marker so frontend knows turn is done
                if msg_type == "result":
                    await session.broadcast({"type": "result_done"})

            # Process ended - read stderr
            stderr_data = await session.process.stderr.read()
            stderr_str = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else ""

            # Wait for exit code
            exit_code = await session.process.wait()
            session.is_running = False

            if stderr_str:
                logger.error(f"Session {session.session_id} stderr: {stderr_str[:1000]}")
                # Persist stderr as a system message so user sees the error in chat
                try:
                    db = SessionLocal()
                    err_msg = ClaudeCodeMessage(
                        session_id=session.session_id,
                        role="system",
                        content=stderr_str[:5000],
                        message_type="error"
                    )
                    db.add(err_msg)
                    db.commit()
                    db.close()
                except Exception:
                    pass
                await session.broadcast({
                    "type": "error",
                    "error": {"message": stderr_str[:2000]}
                })

            # Update session status in DB
            try:
                db = SessionLocal()
                db_session = db.query(ClaudeCodeSession).filter(
                    ClaudeCodeSession.id == session.session_id
                ).first()
                if db_session:
                    db_session.status = "completed" if exit_code == 0 else "failed"
                    db_session.ended_at = datetime.utcnow()
                    db.commit()
                db.close()
            except Exception as e:
                logger.error(f"Failed to update session status: {e}")

            # Notify WebSocket clients of session end
            await session.broadcast({
                "type": "session_end",
                "exit_code": exit_code,
                "stderr": stderr_str[:2000] if stderr_str else ""
            })

            logger.info(f"Session {session.session_id} ended with exit code {exit_code}")

        except asyncio.CancelledError:
            logger.info(f"Output reader cancelled for session {session.session_id}")
        except Exception as e:
            logger.error(f"Error reading output for session {session.session_id}: {e}")
            session.is_running = False
        finally:
            # Clean up from active sessions
            if session.user_id in self._sessions and self._sessions[session.user_id] is session:
                del self._sessions[session.user_id]

    async def stop_session(self, user_id: int) -> bool:
        """Stop a running session. SIGTERM -> wait 5s -> SIGKILL."""
        session = self._sessions.get(user_id)
        if not session or not session.is_running:
            return False

        session.is_running = False

        try:
            # Try graceful termination
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Force kill
                session.process.kill()
                await session.process.wait()

            # Cancel read task
            if session._read_task and not session._read_task.done():
                session._read_task.cancel()

            # Update DB
            from app.database import SessionLocal, ClaudeCodeSession
            try:
                db = SessionLocal()
                db_session = db.query(ClaudeCodeSession).filter(
                    ClaudeCodeSession.id == session.session_id
                ).first()
                if db_session:
                    db_session.status = "stopped"
                    db_session.ended_at = datetime.utcnow()
                    db.commit()
                db.close()
            except Exception as e:
                logger.error(f"Failed to update stopped session: {e}")

            # Notify clients
            await session.broadcast({"type": "session_end", "exit_code": -1, "stopped": True})

            logger.info(f"Stopped session {session.session_id} for user {user_id}")
            return True

        except Exception as e:
            logger.error(f"Error stopping session: {e}")
            return False
        finally:
            if user_id in self._sessions:
                del self._sessions[user_id]

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
claude_code_manager = ClaudeCodeManager()
