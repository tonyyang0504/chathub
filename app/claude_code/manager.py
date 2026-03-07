"""
Claude Code Process Manager
Spawns and manages Claude Code CLI subprocesses with safety commits and DB backups.

Uses `-p "prompt"` CLI invocation (one process per turn) with `--session-id` / `--resume`
for multi-turn conversations. This avoids stdin pipe issues with `--input-format stream-json`.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class ActiveSession:
    """Represents an active Claude Code CLI session."""

    def __init__(self, session_id: int, user_id: int, api_key: str, model: Optional[str] = None, auth_method: str = "api_key"):
        self.session_id = session_id
        self.user_id = user_id
        self.claude_session_id = str(uuid.uuid4())  # UUID for Claude CLI --session-id/--resume
        self._api_key = api_key
        self._model = model
        self._auth_method = auth_method
        self.process: Optional[asyncio.subprocess.Process] = None
        self.websockets: Set[WebSocket] = set()
        self.output_buffer: list = []
        self.is_running = False   # True while a CLI turn is actively running
        self.is_waiting = False   # True when turn finished, awaiting follow-up
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


class ClaudeCodeManager:
    """
    Singleton manager for Claude Code CLI sessions.
    One active session per user enforced.
    """

    def __init__(self):
        self._sessions: Dict[int, ActiveSession] = {}  # user_id -> ActiveSession

    def get_active_session(self, user_id: int) -> Optional[ActiveSession]:
        """Get the active session for a user (running or waiting for follow-up)."""
        session = self._sessions.get(user_id)
        if session and (session.is_running or session.is_waiting):
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

    async def check_membership_status(self) -> dict:
        """Check Claude membership auth status via `claude auth status --json`."""
        try:
            process = await asyncio.create_subprocess_exec(
                "claude", "auth", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            output = stdout.decode("utf-8", errors="replace").strip()
            if output:
                try:
                    data = json.loads(output)
                    return {
                        "loggedIn": data.get("loggedIn", data.get("authenticated", False)),
                        "email": data.get("email", ""),
                        "subscriptionType": data.get("subscriptionType", data.get("planType", ""))
                    }
                except json.JSONDecodeError:
                    # Some versions output non-JSON — check for keywords
                    logged_in = "logged in" in output.lower() or "authenticated" in output.lower()
                    return {"loggedIn": logged_in, "email": "", "subscriptionType": "", "raw": output}
            return {"loggedIn": False, "email": "", "subscriptionType": ""}
        except FileNotFoundError:
            return {"loggedIn": False, "email": "", "subscriptionType": "", "error": "Claude CLI not installed"}
        except asyncio.TimeoutError:
            return {"loggedIn": False, "email": "", "subscriptionType": "", "error": "Timeout checking status"}
        except Exception as e:
            logger.error(f"Failed to check membership status: {e}")
            return {"loggedIn": False, "email": "", "subscriptionType": "", "error": str(e)}

    async def trigger_membership_login(self) -> dict:
        """Trigger `claude auth login` which opens browser for OAuth."""
        try:
            process = await asyncio.create_subprocess_exec(
                "claude", "auth", "login",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # Don't wait for completion — login happens in browser
            return {"status": "ok", "message": "Browser opened for login"}
        except FileNotFoundError:
            return {"status": "error", "message": "Claude CLI not installed"}
        except Exception as e:
            logger.error(f"Failed to trigger membership login: {e}")
            return {"status": "error", "message": str(e)}

    async def trigger_membership_logout(self) -> dict:
        """Run `claude auth logout`."""
        try:
            process = await asyncio.create_subprocess_exec(
                "claude", "auth", "logout",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(process.communicate(), timeout=10.0)
            return {"status": "ok"}
        except FileNotFoundError:
            return {"status": "error", "message": "Claude CLI not installed"}
        except Exception as e:
            logger.error(f"Failed to trigger membership logout: {e}")
            return {"status": "error", "message": str(e)}

    async def start_session(
        self,
        user_id: int,
        session_id: int,
        prompt: str,
        api_key: str,
        model: Optional[str] = None,
        auth_method: str = "api_key"
    ) -> Optional[ActiveSession]:
        """Create an ActiveSession and run the first turn."""
        # Enforce one session per user
        existing = self.get_active_session(user_id)
        if existing:
            logger.warning(f"User {user_id} already has an active session {existing.session_id}")
            return None

        session = ActiveSession(session_id, user_id, api_key, model, auth_method)
        self._sessions[user_id] = session

        try:
            await self._run_turn(session, prompt, is_first=True)
            return session
        except FileNotFoundError:
            logger.error("Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code")
            del self._sessions[user_id]
            return None
        except Exception as e:
            logger.error(f"Failed to start Claude Code session: {e}")
            if user_id in self._sessions and self._sessions[user_id] is session:
                del self._sessions[user_id]
            return None

    async def _run_turn(self, session: ActiveSession, prompt: str, is_first: bool = False):
        """Spawn one CLI process for a single turn (prompt on command line)."""
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions"
        ]

        if session._model:
            cmd.extend(["--model", session._model])

        if is_first:
            cmd.extend(["--session-id", session.claude_session_id])
        else:
            cmd.extend(["--resume", session.claude_session_id])

        # Set environment
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        if session._auth_method == "membership":
            # Don't set ANTHROPIC_API_KEY — let CLI use stored membership credentials
            env.pop("ANTHROPIC_API_KEY", None)
        else:
            env["ANTHROPIC_API_KEY"] = session._api_key

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            env=env
        )

        session.process = process
        session.is_running = True
        session.is_waiting = False

        # Start reading stdout and stderr concurrently
        session._read_task = asyncio.create_task(self._read_output(session))
        session._stderr_task = asyncio.create_task(self._read_stderr(session))

        logger.info(f"Turn started for session {session.session_id}, PID={process.pid}, first={is_first}")

    async def _read_stderr(self, session: ActiveSession):
        """Read stderr line-by-line to prevent pipe deadlock and log debug output."""
        try:
            while True:
                line = await session.process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.debug(f"Session {session.session_id} stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stderr for session {session.session_id}: {e}")

    async def send_message(self, user_id: int, message: str) -> bool:
        """Send a follow-up message by spawning a new CLI process with --resume."""
        session = self._sessions.get(user_id)
        if not session:
            logger.warning(f"No active session for user {user_id}")
            return False
        if not session.is_waiting:
            logger.warning(f"Session {session.session_id} is not waiting for input")
            return False

        try:
            # Broadcast the user message to WebSocket clients
            await session.broadcast({"type": "user_message", "content": message})

            # Spawn a new turn
            await self._run_turn(session, message, is_first=False)
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
                skip_db = False  # Skip high-frequency events

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
                elif msg_type == "content_block_delta":
                    # High-frequency streaming event — skip DB persistence
                    skip_db = True
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
                else:
                    content = line_str

                # Persist to database (skip high-frequency delta events)
                if not skip_db:
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

                # After result event, broadcast result_done marker
                if msg_type == "result":
                    await session.broadcast({"type": "result_done"})

            # Wait for stderr reader to finish
            if session._stderr_task and not session._stderr_task.done():
                try:
                    await asyncio.wait_for(session._stderr_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            # Wait for exit code
            exit_code = await session.process.wait()

            if exit_code == 0:
                # Turn completed successfully — session stays alive for follow-ups
                session.is_running = False
                session.is_waiting = True
                await session.broadcast({"type": "turn_end", "exit_code": 0})
                logger.info(f"Turn completed for session {session.session_id}, waiting for follow-up")
            else:
                # Process failed — session is done
                session.is_running = False
                session.is_waiting = False

                # Update session status in DB
                try:
                    db = SessionLocal()
                    db_session = db.query(ClaudeCodeSession).filter(
                        ClaudeCodeSession.id == session.session_id
                    ).first()
                    if db_session:
                        db_session.status = "failed"
                        db_session.ended_at = datetime.utcnow()
                        db.commit()
                    db.close()
                except Exception as e:
                    logger.error(f"Failed to update session status: {e}")

                await session.broadcast({
                    "type": "session_end",
                    "exit_code": exit_code,
                    "stderr": ""
                })

                logger.info(f"Session {session.session_id} ended with exit code {exit_code}")

                # Clean up
                if session.user_id in self._sessions and self._sessions[session.user_id] is session:
                    del self._sessions[session.user_id]

        except asyncio.CancelledError:
            logger.info(f"Output reader cancelled for session {session.session_id}")
        except Exception as e:
            logger.error(f"Error reading output for session {session.session_id}: {e}")
            session.is_running = False
            session.is_waiting = False
            if session.user_id in self._sessions and self._sessions[session.user_id] is session:
                del self._sessions[session.user_id]

    async def stop_session(self, user_id: int) -> bool:
        """Stop a running or waiting session."""
        session = self._sessions.get(user_id)
        if not session:
            return False
        if not session.is_running and not session.is_waiting:
            return False

        was_waiting = session.is_waiting
        session.is_running = False
        session.is_waiting = False

        try:
            # If there's a running process, terminate it
            if session.process and not was_waiting:
                try:
                    session.process.terminate()
                    try:
                        await asyncio.wait_for(session.process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        session.process.kill()
                        await session.process.wait()
                except ProcessLookupError:
                    pass

            # Cancel read tasks
            if session._read_task and not session._read_task.done():
                session._read_task.cancel()
            if session._stderr_task and not session._stderr_task.done():
                session._stderr_task.cancel()

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
