"""
ChatHub Agent Loop - Core tool-use agent loop.
Calls AI provider, handles tool calls with approval flow, loops until text-only response.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

from app.ai.providers.base import AIResponse

MAX_ITERATIONS = 50
logger = logging.getLogger(__name__)


class AgentLoop:
    """Core agent loop that drives AI + tool-use conversations."""

    def __init__(
        self,
        session_id: int,
        provider,
        tool_executor,
        agent_config: dict,
        broadcast_fn: Callable,
        db_factory: Callable,
    ):
        self.session_id = session_id
        self.provider = provider  # AIProvider instance
        self.tool_executor = tool_executor  # ToolExecutor instance
        self.agent_config = agent_config  # dict with system_prompt, temperature, max_tokens, allowed_tools, dangerous_tools, auto_approve_read
        self.broadcast = broadcast_fn  # async callable
        self.db_factory = db_factory  # SessionLocal callable
        self.conversation: list[dict] = []
        self.is_running = False
        self.is_waiting = False
        self._approval_event = asyncio.Event()
        self._approval_granted = False
        self._stop_requested = False
        self.total_turns = 0
        self.total_tool_calls = 0
        self.total_tokens = 0
        self.skill_instructions = ""  # Injected by skills system

    async def run(self, initial_prompt: str):
        """Start the agent loop with an initial user prompt."""
        self.is_running = True
        self.is_waiting = False
        self._stop_requested = False

        # Build system prompt
        system_prompt = self.agent_config.get("system_prompt") or self._default_system_prompt()

        # Inject user context
        user_ctx = self.agent_config.get("user_context")
        if user_ctx:
            system_prompt += (
                f"\n\n## Current User\n"
                f"- User ID: {user_ctx.get('user_id')}\n"
                f"- Email: {user_ctx.get('email')}\n"
                f"- Name: {user_ctx.get('name')}\n"
                f"When querying the database for user-specific data, use user_id = {user_ctx.get('user_id')}."
            )

        if self.skill_instructions:
            system_prompt += "\n\n" + self.skill_instructions

        self.conversation = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_prompt},
        ]
        await self._agent_loop()

    async def handle_followup(self, message: str):
        """Handle a follow-up user message."""
        self.is_waiting = False
        self._stop_requested = False
        self.conversation.append({"role": "user", "content": message})
        await self._agent_loop()

    async def _agent_loop(self):
        """Main loop: call AI, handle tool calls, repeat until text-only response."""
        for iteration in range(MAX_ITERATIONS):
            if self._stop_requested:
                await self.broadcast({"type": "assistant_text", "content": "Session stopped by user."})
                await self.broadcast({"type": "turn_complete"})
                self.is_waiting = True
                return

            try:
                # Get tool definitions (filtered by allowed_tools)
                from app.chathub_agent.tool_defs import get_tool_definitions
                tools = get_tool_definitions(self.agent_config.get("allowed_tools"))

                # Call AI provider (sync method, run in thread)
                start = time.time()
                logger.info(f"Calling provider with {len(tools)} tools, {len(self.conversation)} messages")
                response: AIResponse = await asyncio.to_thread(
                    self.provider.chat_completion,
                    messages=self.conversation,
                    tools=tools,
                    tool_choice="auto",
                    temperature=self.agent_config.get("temperature", 0.3),
                    max_tokens=self.agent_config.get("max_tokens", 8192),
                )
                elapsed_ms = int((time.time() - start) * 1000)
                logger.info(f"Provider response: finish_reason={response.finish_reason}, tool_calls={len(response.tool_calls)}, content_len={len(response.content or '')}")

                # Track tokens
                tokens = response.usage.get("total_tokens", 0)
                self.total_tokens += tokens
                self.total_turns += 1

                # Handle tool calls
                if response.tool_calls:
                    # Build assistant message with tool calls for conversation history
                    assistant_msg = {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in response.tool_calls
                        ],
                    }
                    self.conversation.append(assistant_msg)

                    if response.content:
                        await self.broadcast({"type": "assistant_text", "content": response.content})
                        await self._persist_message(
                            "assistant", response.content,
                            tokens_used=tokens, execution_time_ms=elapsed_ms,
                        )

                    for tc in response.tool_calls:
                        self.total_tool_calls += 1

                        await self.broadcast({
                            "type": "tool_call",
                            "tool_call_id": tc.id,
                            "tool_name": tc.name,
                            "arguments": tc.arguments,
                        })
                        await self._persist_message(
                            "tool_call", json.dumps(tc.arguments),
                            tool_name=tc.name, tool_call_id=tc.id,
                        )

                        # Check if approval is needed
                        if self._needs_approval(tc.name):
                            # Extract meaningful description with proper fallbacks
                            description = tc.arguments.get("description", "")
                            if not description:
                                if tc.name == "exec_command":
                                    cmd = tc.arguments.get("command", "")
                                    description = f"Run: {cmd[:80]}{'…' if len(cmd) > 80 else ''}" if cmd else "Run a shell command"
                                elif tc.name == "write_file":
                                    description = f"Write file: {tc.arguments.get('path', 'unknown')}"
                                elif tc.name == "edit_file":
                                    description = f"Edit file: {tc.arguments.get('path', 'unknown')}"
                                else:
                                    description = f"Use {tc.name}"

                            await self.broadcast({
                                "type": "approval_request",
                                "tool_call_id": tc.id,
                                "tool_name": tc.name,
                                "arguments": tc.arguments,
                                "description": description,
                            })
                            await self._persist_message(
                                "approval_request", description,
                                tool_name=tc.name, tool_call_id=tc.id,
                            )
                            approved = await self._wait_for_approval()
                            if not approved:
                                result_text = "Tool execution denied by user."
                                self.conversation.append({
                                    "role": "tool",
                                    "tool_call_id": tc.id,
                                    "content": result_text,
                                })
                                await self.broadcast({
                                    "type": "tool_result",
                                    "tool_call_id": tc.id,
                                    "tool_name": tc.name,
                                    "content": result_text,
                                    "denied": True,
                                })
                                await self._persist_message(
                                    "tool_result", result_text,
                                    tool_name=tc.name, tool_call_id=tc.id,
                                )
                                continue

                        # Execute tool
                        exec_start = time.time()
                        try:
                            result_text = await self.tool_executor.execute(tc.name, tc.arguments)
                        except Exception as e:
                            result_text = f"Error executing {tc.name}: {str(e)}"
                        exec_ms = int((time.time() - exec_start) * 1000)

                        self.conversation.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })
                        await self.broadcast({
                            "type": "tool_result",
                            "tool_call_id": tc.id,
                            "tool_name": tc.name,
                            "content": result_text,
                        })
                        await self._persist_message(
                            "tool_result", result_text,
                            tool_name=tc.name, tool_call_id=tc.id,
                            execution_time_ms=exec_ms,
                        )

                    continue  # Loop back for AI to process results

                else:
                    # Text-only response -- turn is complete
                    content = response.content or ""
                    self.conversation.append({"role": "assistant", "content": content})
                    await self.broadcast({"type": "assistant_text", "content": content})
                    await self._persist_message(
                        "assistant", content,
                        tokens_used=tokens, execution_time_ms=elapsed_ms,
                    )
                    await self.broadcast({"type": "turn_complete"})
                    self.is_waiting = True
                    return

            except Exception as e:
                logger.exception(f"Agent loop error: {e}")
                await self.broadcast({"type": "error", "content": str(e)})
                await self._persist_message("system", f"Error: {str(e)}")
                self.is_waiting = True
                return

        # Max iterations reached
        await self.broadcast({"type": "assistant_text", "content": "Maximum iterations reached."})
        await self.broadcast({"type": "turn_complete"})
        self.is_waiting = True

    def _needs_approval(self, tool_name: str) -> bool:
        """Check if a tool requires user approval before execution."""
        if self.agent_config.get("auto_approve_all", True):
            return False
        from app.chathub_agent.tool_defs import READ_ONLY_TOOLS
        if tool_name in READ_ONLY_TOOLS and self.agent_config.get("auto_approve_read", True):
            return False
        # "ask every tool" mode: auto_approve_read=False means ask for everything
        if not self.agent_config.get("auto_approve_read", True):
            return True
        # "auto_approve_reads" mode: only dangerous tools need approval
        dangerous = self.agent_config.get("dangerous_tools") or ["exec_command", "write_file", "edit_file"]
        if isinstance(dangerous, str):
            dangerous = json.loads(dangerous)
        return tool_name in dangerous

    async def _wait_for_approval(self, timeout: int = 300) -> bool:
        """Wait for user to approve or deny a tool call."""
        self._approval_event.clear()
        try:
            await asyncio.wait_for(self._approval_event.wait(), timeout=timeout)
            return self._approval_granted
        except asyncio.TimeoutError:
            return False

    def approve(self, granted: bool):
        """Approve or deny a pending tool call."""
        self._approval_granted = granted
        self._approval_event.set()

    def set_conversation(self, conversation: list[dict]):
        """Set conversation history (for session resume)."""
        self.conversation = conversation
        self.is_running = True
        self.is_waiting = True

    def update_config(self, new_config: dict):
        """Hot-reload config for the running agent loop."""
        self.agent_config.update(new_config)

    def stop(self):
        """Request the agent loop to stop."""
        self._stop_requested = True
        self._approval_event.set()  # Unblock any waiting approval

    async def _persist_message(self, role: str, content: str, **kwargs):
        """Persist a message to the database."""
        try:
            from app.database import SessionLocal, ChatHubAgentMessage
            db = SessionLocal()
            try:
                msg = ChatHubAgentMessage(
                    session_id=self.session_id,
                    role=role,
                    content=content[:10000] if content else "",
                    tool_name=kwargs.get("tool_name"),
                    tool_call_id=kwargs.get("tool_call_id"),
                    tokens_used=kwargs.get("tokens_used", 0),
                    execution_time_ms=kwargs.get("execution_time_ms", 0),
                )
                db.add(msg)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Failed to persist message: {e}")

    @staticmethod
    def _default_system_prompt() -> str:
        return (
            "You are ChatHub Agent, an AI assistant with full access to this computer's shell, "
            "filesystem, and the ChatHub codebase/database. You MUST use your tools to accomplish "
            "tasks — you are NOT a chatbot.\n\n"

            "## About ChatHub\n"
            "ChatHub is a multi-platform AI bot management platform built with:\n"
            "- **Backend**: FastAPI + SQLAlchemy + SQLite\n"
            "- **Database**: SQLite at `data/app.db`\n"
            "- **Main code**: `app/` directory\n"
            "- **Database models** in `app/database.py`: User, BotProfile, Conversation, Message, "
            "Hub, HubBotMembership, Contact, ScheduledContent, AIAgent, and more\n"
            "- **AI Providers**: OpenAI, Anthropic, Google, DeepSeek, Qwen (in `app/ai/providers/`)\n"
            "- **Bot platforms**: WhatsApp, Telegram, Instagram, Messenger, LINE, etc.\n"
            "- **Templates**: `app/templates/` (Jinja2 HTML)\n"
            "- **Static files**: `static/` (CSS, JS)\n\n"

            "## Tools Available\n"
            "- **read_file**: Read file contents\n"
            "- **write_file**: Create or overwrite files\n"
            "- **edit_file**: Replace specific text in a file\n"
            "- **exec_command**: Run ANY shell command (ls, cat, git, python, sqlite3, npm, curl, "
            "open, brew, system commands, etc.)\n"
            "- **list_files**: List files/directories with glob patterns\n"
            "- **search_files**: Search file contents with regex\n"
            "- **read_url**: Fetch a URL and extract readable text (for static pages, APIs, docs)\n"
            "- **web_search**: Search Google and return structured results (titles, URLs, snippets). "
            "ALWAYS use this instead of exec_command with curl for web searches.\n"
            "- **browser_read**: Open a URL in a real headless browser and return the visible text. "
            "Use this for JavaScript-heavy pages that read_url can't handle.\n\n"

            "## Web Access\n"
            "- **Google searches**: ALWAYS use `web_search` tool. NEVER use exec_command with curl for Google.\n"
            "- **Reading web pages**: Use `read_url` first. If the result is empty/garbled (JS-rendered page), "
            "fall back to `browser_read`.\n"
            "- **Opening desktop browsers** (Firefox, Chrome, Safari): Use exec_command with `open -a`.\n"
            "- Read-only web tools (read_url, web_search, browser_read) don't need user approval.\n\n"

            "## Database Access\n"
            "Two methods available:\n"
            "1. **Quick queries**: `exec_command` with `sqlite3 data/app.db \"SQL HERE\"`\n"
            "2. **Python scripts** (preferred for complex operations): Use SQLAlchemy models from "
            "`app/database.py`. Connection: `from app.database import SessionLocal; db = SessionLocal()`. "
            "All models are in `app/database.py`. Always filter by user_id for user-scoped resources.\n\n"

            "## Server API Access\n"
            "Some operations (sending messages, starting/stopping bots) require the running server. "
            "Use `exec_command` with curl:\n"
            "- Auth: `-b 'access_token=TOKEN'` (get token from current session)\n"
            "- Base URL: `http://localhost:8000`\n\n"
            "**Key API Endpoints:**\n"
            "- Send message: `POST /api/conversations/{conversation_id}/send` Body: `{\"message\": \"text\"}`\n"
            "- List bots: `GET /api/bots`\n"
            "- Start bot: `POST /api/bots/{bot_id}/start`\n"
            "- Stop bot: `POST /api/bots/{bot_id}/stop`\n"
            "- List conversations: `GET /api/conversations/bot/{bot_id}`\n\n"

            "## When to Use Which\n"
            "- **Reading data** (contacts, conversations, settings, history): Direct DB access\n"
            "- **Sending messages**: Server API (requires running bot's session)\n"
            "- **Starting/stopping bots**: Server API\n"
            "- **Creating/modifying DB records** (bots, hubs, agents, settings): Direct DB access\n\n"

            "## How to Operate\n"
            "1. **ALWAYS use tools.** Never say you don't have access — you DO.\n"
            "2. **Explore code**: Use `list_files`, `read_file`, `search_files`\n"
            "3. **Make changes**: Read files first, then use `edit_file` or `write_file`\n"
            "4. **Run commands**: Use `exec_command` for git, python, tests, etc.\n"
            "5. **Verify changes**: Read back files or run tests after modifying code\n\n"

            "## Output Formatting\n"
            "- Use **markdown tables** when presenting tabular data (query results, CSV data, comparisons)\n"
            "- Use **code blocks** with language tags for code snippets\n"
            "- Use **structured headings** for multi-part answers\n"
            "- Keep responses concise and action-oriented\n\n"

            "## Behavior\n"
            "- Act autonomously. When the user asks something, use tools immediately.\n"
            "- NEVER ask for clarification if you can figure it out by exploring.\n"
            "- If you don't know a table name, run: `sqlite3 data/app.db \".tables\"`\n"
            "- If you don't know a table schema, run: `sqlite3 data/app.db \"PRAGMA table_info(table_name);\"`\n"
            "- For user-specific queries, use the user_id provided in the Current User section.\n"
            "- Chain multiple tool calls as needed — explore first, then act.\n"
            "- If a command fails with a permission or access error, try an alternative approach before giving up.\n"
            "- Read-only tools (read_file, list_files, search_files, read_url, web_search, browser_read) don't need approval.\n"
            "- NEVER ask the user for permission or confirmation in your text responses. "
            "The system has a built-in approval UI that automatically prompts the user when needed. "
            "Just call the tool directly — the approval flow is handled for you.\n"
            "- NEVER suggest or display commands for the user to run. ALWAYS execute them yourself using exec_command.\n"
            "- You have full system access. Tasks like listing files, reading CSVs, checking installed apps, "
            "opening browsers — use exec_command for ALL of these.\n"
            "- Do NOT say things like 'I need your approval' or 'If you approve, I will run...' — "
            "just call the tool immediately."
        )
