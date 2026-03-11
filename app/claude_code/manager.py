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
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

import pexpect

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class ActiveSession:
    """Represents an active Claude Code CLI session."""

    def __init__(self, session_id: int, user_id: int, api_key: str, model: Optional[str] = None, auth_method: str = "api_key", oauth_token: Optional[str] = None, user_email: Optional[str] = None, user_name: Optional[str] = None, provider: str = "claude"):
        self.session_id = session_id
        self.user_id = user_id
        self.claude_session_id = str(uuid.uuid4())  # UUID for Claude CLI --session-id/--resume
        self._api_key = api_key
        self._model = model
        self._auth_method = auth_method
        self._oauth_token = oauth_token
        self._user_email = user_email
        self._user_name = user_name
        self.provider = provider
        # Load CLI provider adapter
        from .providers import get_provider
        self.cli_provider = get_provider(provider)
        self.process: Optional[asyncio.subprocess.Process] = None
        self.websockets: Set[WebSocket] = set()
        self.output_buffer: list = []
        self.is_running = False   # True while a CLI turn is actively running
        self.is_waiting = False   # True when turn finished, awaiting follow-up
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.turn_number = 0

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
        """Trigger `claude auth login` using pexpect for reliable PTY interaction.

        pexpect handles all PTY details — terminal settings, controlling terminal,
        input/output buffering — so the Ink UI properly accepts keyboard input.
        """
        try:
            # Kill any existing login process
            self._cleanup_login_pty()

            # Find claude binary path
            claude_path = shutil.which("claude")
            if not claude_path:
                return {"status": "error", "message": "Claude CLI not installed"}

            # Spawn via pexpect — handles PTY setup, controlling terminal, etc.
            child = pexpect.spawn(
                claude_path, ["auth", "login"],
                encoding="utf-8",
                timeout=15,
                dimensions=(24, 120),
            )

            # Wait for the OAuth URL to appear in the output
            loop = asyncio.get_event_loop()
            oauth_url = None
            url_pattern = re.compile(
                r'https://\S*oauth\S*authorize\S+|'
                r'https://\S*claude\S+/authorize\S+|'
                r'https://\S+/login\S*\?[^\s]+'
            )

            def _wait_for_url():
                """Read pexpect output until we find the OAuth URL."""
                nonlocal oauth_url
                buf = ""
                deadline = time.time() + 15.0
                while time.time() < deadline:
                    try:
                        # Read available data with short timeout
                        chunk = child.read_nonblocking(4096, timeout=0.5)
                        if chunk:
                            buf += chunk
                            # Strip ANSI escape codes for matching
                            clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', buf)
                            match = url_pattern.search(clean)
                            if match:
                                oauth_url = match.group(0)
                                return
                            visit_match = re.search(
                                r'(?:visit|open|go to|navigate to|browser)[:\s]+(https://\S+)',
                                clean, re.IGNORECASE
                            )
                            if visit_match:
                                oauth_url = visit_match.group(1)
                                return
                    except pexpect.TIMEOUT:
                        continue
                    except pexpect.EOF:
                        break
                if buf:
                    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', buf)
                    logger.info(f"Login output (no URL found): {clean[:1000]}")

            await loop.run_in_executor(None, _wait_for_url)

            if oauth_url:
                logger.info(f"OAuth URL captured: {oauth_url[:80]}...")

            # Start a background thread to continuously drain PTY output.
            # Ink renders spinners, cursor blinks, etc. to stdout. If nobody reads
            # from the PTY master, the buffer fills up and blocks Node.js's event
            # loop — preventing stdin (our code input) from being processed.
            stop_event = threading.Event()
            drain_thread = threading.Thread(
                target=self._drain_pexpect, args=(child, stop_event), daemon=True
            )
            drain_thread.start()

            self._login_pty = {
                "child": child,
                "drain_stop": stop_event,
                "drain_thread": drain_thread,
            }

            if oauth_url:
                return {"status": "ok", "oauth_url": oauth_url}
            else:
                return {"status": "ok", "message": "Browser opened for login"}
        except FileNotFoundError:
            return {"status": "error", "message": "Claude CLI not installed"}
        except Exception as e:
            logger.error(f"Failed to trigger membership login: {e}")
            return {"status": "error", "message": str(e)}

    def _drain_pexpect(self, child: pexpect.spawn, stop_event: threading.Event):
        """Background thread: continuously read pexpect output to prevent PTY buffer fill.

        Ink renders animations/spinners to stdout. If nobody reads from the PTY
        master, the buffer fills and blocks Node.js's single-threaded event loop,
        preventing stdin processing (our auth code input).
        """
        total_drained = 0
        while not stop_event.is_set():
            try:
                data = child.read_nonblocking(4096, timeout=0.5)
                if data:
                    total_drained += len(data)
            except pexpect.TIMEOUT:
                continue
            except (pexpect.EOF, OSError):
                break
        logger.info(f"Drain thread exiting, total bytes drained: {total_drained}")

    def _cleanup_login_pty(self):
        """Terminate login process, stop drain thread, and clean up."""
        if not hasattr(self, '_login_pty') or not self._login_pty:
            return
        pty_info = self._login_pty
        self._login_pty = None
        # Stop drain thread first
        stop = pty_info.get("drain_stop")
        if stop:
            stop.set()
        thread = pty_info.get("drain_thread")
        if thread:
            thread.join(timeout=2.0)
        # Kill child process
        child = pty_info.get("child")
        if child and child.isalive():
            try:
                child.terminate(force=True)
            except Exception:
                pass

    async def submit_login_code(self, code: str) -> dict:
        """Send the OAuth authorization code to the pexpect-managed login process.

        Uses pexpect.sendline() which properly writes through the PTY so Ink's
        TextInput component receives the characters as keyboard input.
        """
        if not hasattr(self, '_login_pty') or not self._login_pty:
            return {"status": "error", "message": "No login process running. Click 'Login with Claude' first."}

        child = self._login_pty.get("child")
        if not child or not child.isalive():
            self._cleanup_login_pty()
            return {"status": "error", "message": "Login process already exited. Click 'Login with Claude' to restart."}

        try:
            loop = asyncio.get_event_loop()

            def _send_and_wait():
                import termios

                # Stop the drain thread so we can read output after sending
                stop = self._login_pty.get("drain_stop") if self._login_pty else None
                if stop:
                    stop.set()
                thread = self._login_pty.get("drain_thread") if self._login_pty else None
                if thread:
                    thread.join(timeout=2.0)
                    logger.info(f"Drain thread stopped: {not thread.is_alive()}")

                # Check child process state
                logger.info(f"Child alive before send: {child.isalive()}, pid: {child.pid}")

                # Log PTY termios settings to detect raw mode
                try:
                    attrs = termios.tcgetattr(child.child_fd)
                    logger.info(f"PTY termios: iflag={hex(attrs[0])}, oflag={hex(attrs[1])}, cflag={hex(attrs[2])}, lflag={hex(attrs[3])}")
                except Exception as e:
                    logger.warning(f"Could not read PTY termios: {e}")

                # Flush any remaining buffered output
                flushed = ""
                try:
                    flushed = child.read_nonblocking(65536, timeout=0.5)
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass
                if flushed:
                    clean_flushed = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', flushed)
                    logger.info(f"Flushed {len(flushed)} bytes: {clean_flushed[:300]}")

                # Send auth code character-by-character with delays.
                # Ink's TextInput uses useInput which processes stdin data events
                # in Node.js's event loop. Bulk writes may not be processed correctly
                # — sending one char at a time simulates real keyboard input.
                stripped_code = code.strip()
                logger.info(f"Sending auth code char-by-char ({len(stripped_code)} chars)")
                for char in stripped_code:
                    child.send(char)
                    time.sleep(0.01)  # 10ms between characters
                time.sleep(0.1)  # 100ms pause before Enter
                child.send("\r")
                logger.info(f"Auth code sent, child alive: {child.isalive()}")

                # Wait for the process to finish (success or error)
                try:
                    # Look for success, error, or process exit
                    child.expect(
                        [pexpect.EOF, r'success', r'error', r'failed', r'invalid'],
                        timeout=20
                    )
                    before_text = repr(child.before[:200]) if child.before else None
                    after_text = repr(child.after[:200]) if child.after else None
                    logger.info(f"Login expect matched — before={before_text}, after={after_text}")
                except pexpect.TIMEOUT:
                    before_text = repr(child.before[:200]) if child.before else None
                    logger.warning(f"Timeout waiting for login response. child.before={before_text}")
                except pexpect.EOF:
                    before_text = repr(child.before[:200]) if child.before else None
                    logger.info(f"Login process EOF after code submission. child.before={before_text}")

                # Capture any remaining output
                try:
                    remaining = child.read_nonblocking(65536, timeout=1.0)
                    if remaining:
                        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', remaining)
                        logger.info(f"Remaining output: {clean[:500]}")
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass

            await asyncio.wait_for(
                loop.run_in_executor(None, _send_and_wait),
                timeout=25.0
            )

            # Clean up
            self._cleanup_login_pty()

            return {"status": "ok", "message": "Authorization code submitted"}
        except asyncio.TimeoutError:
            logger.warning("Timeout in submit_login_code")
            self._cleanup_login_pty()
            return {"status": "ok", "message": "Authorization code submitted (processing may still be in progress)"}
        except Exception as e:
            logger.error(f"Failed to submit login code: {e}")
            self._cleanup_login_pty()
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
        auth_method: str = "api_key",
        oauth_token: Optional[str] = None,
        user_email: Optional[str] = None,
        user_name: Optional[str] = None,
        file_paths: list = None,
        provider: str = "claude"
    ) -> Optional[ActiveSession]:
        """Create an ActiveSession and run the first turn."""
        session = ActiveSession(session_id, user_id, api_key, model, auth_method, oauth_token, user_email, user_name, provider=provider)
        self._sessions[user_id] = session

        # Persist the CLI session UUID to DB for future --resume
        try:
            from app.database import SessionLocal, ClaudeCodeSession
            db = SessionLocal()
            db_session = db.query(ClaudeCodeSession).filter(ClaudeCodeSession.id == session_id).first()
            if db_session:
                db_session.claude_session_uuid = session.claude_session_id
                db.commit()
            db.close()
        except Exception as e:
            logger.error(f"Failed to persist claude_session_uuid: {e}")

        try:
            await self._run_turn(session, prompt, is_first=True, file_paths=file_paths)
            return session
        except FileNotFoundError:
            cli_name = session.cli_provider.display_name
            logger.error(f"{cli_name} CLI not found")
            del self._sessions[user_id]
            return None
        except Exception as e:
            logger.error(f"Failed to start Claude Code session: {e}")
            if user_id in self._sessions and self._sessions[user_id] is session:
                del self._sessions[user_id]
            return None

    def _build_conversation_history(self, session: ActiveSession) -> str:
        """Build a conversation history summary for stateless providers (Codex/Gemini).

        Since these CLIs have no --resume support, follow-up turns lose all context.
        This reconstructs a condensed history from DB messages so the CLI understands
        what "it" and "the color" refer to in follow-up messages.
        """
        from app.database import SessionLocal, ClaudeCodeMessage
        try:
            db = SessionLocal()
            messages = db.query(ClaudeCodeMessage).filter(
                ClaudeCodeMessage.session_id == session.session_id
            ).order_by(ClaudeCodeMessage.id.asc()).all()
            db.close()

            if not messages:
                return ""

            history_parts = []
            current_turn = 0
            for msg in messages:
                # Include user messages and assistant results (skip tool_use/tool_result noise)
                if msg.message_type == "user_message" or (msg.role == "user" and msg.message_type == "text"):
                    current_turn += 1
                    content = msg.content[:500] if msg.content else ""
                    history_parts.append(f"[Turn {current_turn}] User: {content}")
                elif msg.message_type == "result" and msg.role == "assistant":
                    content = msg.content[:1000] if msg.content else ""
                    history_parts.append(f"[Turn {current_turn}] Assistant: {content}")
                elif msg.role == "tool_use" and msg.content:
                    # Include a brief note about tools used (truncated)
                    try:
                        tool_data = json.loads(msg.content)
                        tool_name = tool_data.get("tool", "unknown")
                        history_parts.append(f"[Turn {current_turn}] Used tool: {tool_name}")
                    except (json.JSONDecodeError, AttributeError):
                        pass

            if not history_parts:
                return ""

            # Cap total history to ~3000 chars to avoid bloating the prompt
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

    async def _run_turn(self, session: ActiveSession, prompt: str, is_first: bool = False, file_paths: list = None):
        """Spawn one CLI process for a single turn (prompt on command line)."""
        session.turn_number += 1

        # Prepend file context if files are attached
        effective_prompt = prompt
        if file_paths:
            file_lines = "\n".join(f"  - {p}" for p in file_paths)
            effective_prompt = (
                f"The user has attached the following files (available on the server filesystem):\n"
                f"{file_lines}\n\n"
                f"User's message: {prompt}"
            )

        # For stateless providers (no --resume), prepend conversation history on follow-up turns
        if not is_first and not session.cli_provider.supports_resume:
            history = self._build_conversation_history(session)
            if history:
                effective_prompt = history + effective_prompt

        # Build system context for the CLI
        system_context = (
            f"You are running inside the ChatHub project on behalf of user: "
            f"{session._user_name} (email: {session._user_email}, user_id: {session.user_id}). "
            f"You have direct access to the project codebase and SQLite database. "
            f"\n\n"
            f"DATABASE ACCESS (for reading/writing data):\n"
            f"Use direct database access via Python scripts with SQLAlchemy models from app/database.py. "
            f"Connection: from app.database import SessionLocal; db = SessionLocal(). "
            f"All models are in app/database.py. Always filter by user_id={session.user_id} for user-scoped resources.\n\n"
            f"SERVER API ACCESS (for actions requiring running bots):\n"
            f"Some operations (sending messages, starting/stopping bots) require the running server process. "
            f"Use curl with the auth token from $CHATHUB_API_TOKEN environment variable.\n"
            f"Base URL: $CHATHUB_API_URL\n"
            f"Auth header: -H 'Authorization: Bearer $CHATHUB_API_TOKEN'\n"
            f"Or use cookie: -b 'access_token=$CHATHUB_API_TOKEN'\n\n"
            f"KEY API ENDPOINTS:\n"
            f"- Send message: POST /api/conversations/{{conversation_id}}/send  Body: {{\"message\": \"text\"}}\n"
            f"- List bots: GET /api/bots\n"
            f"- Start bot: POST /api/bots/{{bot_id}}/start\n"
            f"- Stop bot: POST /api/bots/{{bot_id}}/stop\n"
            f"- List conversations: GET /api/conversations/bot/{{bot_id}}\n"
            f"- Export conversation: GET /api/conversations/{{id}}/export?format=csv|json\n"
            f"- Send file: POST /api/conversations/{{id}}/send-file  (multipart: file + caption)\n"
            f"- Hub contacts: GET /api/hubs/{{hub_id}}/contacts?search=&tags=&page=1&limit=20\n"
            f"- Analyze contact: POST /api/hubs/contacts/{{contact_id}}/analyze\n"
            f"- Analyze all contacts: POST /api/hubs/{{hub_id}}/contacts/analyze-all\n"
            f"- Create scheduled content: POST /api/hubs/{{hub_id}}/scheduled-content\n"
            f"- Generate content: POST /api/hubs/{{hub_id}}/content/generate  Body: {{\"topic\": \"\", \"tone\": \"\", \"target_audience\": \"\"}}\n"
            f"- Bot analytics: GET /api/analytics/bots/{{bot_id}}\n"
            f"- Overview stats: GET /api/analytics/overview\n"
            f"- Daily stats: GET /api/analytics/daily?days=7\n"
            f"- Activity feed: GET /api/analytics/activity?page=1&limit=20\n"
            f"- Hub message topics: GET/POST /api/hubs/{{hub_id}}/topics\n"
            f"- Scripts: GET/POST /scripts/api/{{hub_id}}/scripts\n\n"
            f"WHEN TO USE WHICH:\n"
            f"- Reading data (contacts, conversations, settings, history): Use direct DB access\n"
            f"- Sending WhatsApp messages: Use the server API (requires running bot's browser session)\n"
            f"- Starting/stopping bots: Use the server API\n"
            f"- Creating/modifying DB records (bots, hubs, agents, settings): Use direct DB access\n\n"
            f"TABLE OUTPUT: When displaying tabular data (CSV, DB results, etc.), show at most 5 rows by default "
            f"plus a summary (e.g., 'Showing 5 of 145 rows'). Show all rows only if the user explicitly asks for the full data."
        )

        # Use provider adapter to build command
        cmd = session.cli_provider.build_command(
            prompt=effective_prompt,
            session_uuid=session.claude_session_id,
            is_first=is_first,
            model=session._model,
            system_context=system_context
        )

        # Use provider adapter to build environment
        base_env = os.environ.copy()
        env = session.cli_provider.build_env(
            base_env,
            api_key=session._api_key,
            auth_method=session._auth_method,
            oauth_token=session._oauth_token
        )

        # Generate auth token so CLI can call server API for runtime operations
        from app.auth.utils import create_access_token
        from app.config import settings
        api_token = create_access_token(
            data={"sub": str(session.user_id)},
            expires_delta=timedelta(hours=24)
        )
        env["CHATHUB_API_TOKEN"] = api_token
        env["CHATHUB_API_URL"] = f"http://localhost:{settings.PORT}"

        # Write instruction file for non-Claude providers (AGENTS.md / GEMINI.md)
        session.cli_provider.prepare_session(str(PROJECT_ROOT), system_context)

        # P4: Verify instruction file was written for file-based providers
        if session.provider != "claude":
            instruction_files = {"codex": "AGENTS.md", "gemini": "GEMINI.md"}
            expected_file = instruction_files.get(session.provider)
            if expected_file:
                full_path = os.path.join(str(PROJECT_ROOT), expected_file)
                if os.path.exists(full_path):
                    logger.debug(f"Instruction file {expected_file} written ({os.path.getsize(full_path)} bytes)")
                else:
                    logger.warning(f"Instruction file {expected_file} not found after prepare_session — system context may be lost")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            env=env,
            limit=10 * 1024 * 1024,  # 10MB buffer — Claude can output very long JSON lines
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
        process = session.process  # Capture reference — may be overwritten by follow-up
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.debug(f"Session {session.session_id} stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stderr for session {session.session_id}: {e}")

    async def send_message(self, user_id: int, message: str, file_paths: list = None) -> bool:
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
            await self._run_turn(session, message, is_first=False, file_paths=file_paths)
            logger.info(f"Sent follow-up to session {session.session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send message to session {session.session_id}: {e}")
            return False

    async def _read_output(self, session: ActiveSession):
        """Read stdout line-by-line, parse JSON, persist to DB, broadcast to WebSockets."""
        from app.database import SessionLocal, ClaudeCodeSession, ClaudeCodeMessage

        # Capture process reference at start — session.process may be overwritten by a follow-up turn
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

                # Normalize non-Claude provider events to Claude format
                if session.provider != "claude":
                    data = session.cli_provider.normalize_event(data)
                    if data is None:
                        continue  # Provider says skip this event

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
                    skip_db = True  # Duplicates assistant/tool_use events during replay
                elif msg_type == "content_block_delta":
                    # High-frequency streaming event — skip DB persistence
                    # Exception: non-Claude providers send one delta per full text block, not streaming chunks
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
                    # Extract tool_result from auto-submitted user messages for artifact panel
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
                                # Save to DB
                                try:
                                    tr_db = SessionLocal()
                                    tr_msg = ClaudeCodeMessage(
                                        session_id=session.session_id,
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
                                # Broadcast to WebSocket clients
                                await session.broadcast(tool_result_event)
                    continue  # Skip the raw user event itself
                else:
                    content = line_str

                # Skip system/init event (large session metadata, no user value)
                if msg_type == "system" and data.get("subtype") == "init":
                    continue

                # Persist to database (skip high-frequency delta events)
                if not skip_db:
                    try:
                        db = SessionLocal()
                        event_json = json.dumps(data)
                        # Result events carry final assistant text needed for replay
                        max_event = 50000 if msg_type in ("result", "assistant") else 5000
                        msg = ClaudeCodeMessage(
                            session_id=session.session_id,
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

                # Broadcast to WebSocket clients
                await session.broadcast(data)

                # After result event, broadcast result_done marker
                if msg_type == "result":
                    await session.broadcast({"type": "result_done"})

            # Wait for stderr reader to finish
            if stderr_task and not stderr_task.done():
                try:
                    await asyncio.wait_for(stderr_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            # Wait for exit code
            exit_code = await process.wait()

            # Only update session state if this process is still the active one
            # (a follow-up turn may have already started a new process)
            if session.process is not process:
                logger.info(f"Skipping state update for superseded process in session {session.session_id}")
                return

            if exit_code == 0:
                # Turn completed successfully — session stays alive for follow-ups
                session.is_running = False
                session.is_waiting = True
                session.cli_provider.cleanup_session(str(PROJECT_ROOT))

                await session.broadcast({"type": "turn_end", "exit_code": 0})
                session.output_buffer.clear()
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
                session.cli_provider.cleanup_session(str(PROJECT_ROOT))

                # Clean up
                if session.user_id in self._sessions and self._sessions[session.user_id] is session:
                    del self._sessions[session.user_id]

        except asyncio.CancelledError:
            logger.info(f"Output reader cancelled for session {session.session_id}")
            session.cli_provider.cleanup_session(str(PROJECT_ROOT))
        except Exception as e:
            logger.error(f"Error reading output for session {session.session_id}: {e}")
            session.is_running = False
            session.is_waiting = False
            session.cli_provider.cleanup_session(str(PROJECT_ROOT))
            if session.user_id in self._sessions and self._sessions[session.user_id] is session:
                del self._sessions[session.user_id]

    async def resume_session(
        self,
        user_id: int,
        session_id: int,
        prompt: str,
        api_key: str,
        model: Optional[str] = None,
        auth_method: str = "api_key",
        oauth_token: Optional[str] = None,
        user_email: Optional[str] = None,
        user_name: Optional[str] = None,
        claude_session_uuid: Optional[str] = None,
        file_paths: list = None,
        provider: str = "claude"
    ) -> Optional[ActiveSession]:
        """Resume a stopped session by reusing its Claude CLI session UUID."""
        if not claude_session_uuid:
            logger.error(f"No claude_session_uuid for session {session_id}")
            return None

        session = ActiveSession(session_id, user_id, api_key, model, auth_method, oauth_token, user_email, user_name, provider=provider)
        # Reuse the old CLI session UUID so --resume picks up the conversation
        session.claude_session_id = claude_session_uuid
        self._sessions[user_id] = session

        try:
            # is_first=False triggers --resume instead of --session-id
            await self._run_turn(session, prompt, is_first=False, file_paths=file_paths)
            return session
        except Exception as e:
            logger.error(f"Failed to resume session: {e}")
            if user_id in self._sessions and self._sessions[user_id] is session:
                del self._sessions[user_id]
            return None

    async def stop_session(self, user_id: int) -> bool:
        """Stop a running or waiting session."""
        session = self._sessions.get(user_id)
        if not session:
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

            # Clean up instruction files
            session.cli_provider.cleanup_session(str(PROJECT_ROOT))

            # Notify clients
            await session.broadcast({"type": "session_end", "exit_code": -1, "stopped": True, "status": "stopped"})

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
