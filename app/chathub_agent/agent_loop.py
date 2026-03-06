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
        if self.skill_instructions:
            system_prompt += "\n\n" + self.skill_instructions

        self.conversation = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_prompt},
        ]
        await self._persist_message("user", initial_prompt)
        await self._agent_loop()

    async def handle_followup(self, message: str):
        """Handle a follow-up user message."""
        self.is_waiting = False
        self._stop_requested = False
        self.conversation.append({"role": "user", "content": message})
        await self._persist_message("user", message)
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
                response: AIResponse = await asyncio.to_thread(
                    self.provider.chat_completion,
                    messages=self.conversation,
                    tools=tools,
                    tool_choice="auto",
                    temperature=self.agent_config.get("temperature", 0.3),
                    max_tokens=self.agent_config.get("max_tokens", 8192),
                )
                elapsed_ms = int((time.time() - start) * 1000)

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
                            await self.broadcast({
                                "type": "approval_request",
                                "tool_call_id": tc.id,
                                "tool_name": tc.name,
                                "arguments": tc.arguments,
                            })
                            await self._persist_message(
                                "approval_request", f"Approval needed for {tc.name}",
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
        from app.chathub_agent.tool_defs import READ_ONLY_TOOLS
        if tool_name in READ_ONLY_TOOLS and self.agent_config.get("auto_approve_read", True):
            return False
        dangerous = self.agent_config.get("dangerous_tools") or ["exec_command"]
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
            "You are ChatHub Agent, an AI-powered coding assistant that operates directly "
            "in the user's workspace. You MUST use your tools to accomplish tasks — you are "
            "NOT a simple chatbot.\n\n"
            "## Tools Available\n"
            "- **read_file**: Read file contents (use this to understand code before changing it)\n"
            "- **write_file**: Create or overwrite files\n"
            "- **edit_file**: Replace specific text in a file (surgical edits)\n"
            "- **exec_command**: Run shell commands (git, python, npm, curl, sqlite3, etc.)\n"
            "- **list_files**: List files/directories with glob patterns\n"
            "- **search_files**: Search file contents with regex patterns\n\n"
            "## Critical Rules\n"
            "1. **ALWAYS use tools** to answer questions about the codebase, files, database, "
            "or system state. NEVER guess or make assumptions — look it up.\n"
            "2. When asked about the project, START by using list_files or search_files to explore.\n"
            "3. When asked to make changes, READ the relevant files first, then edit them.\n"
            "4. When asked about data (database contents, configs, etc.), use exec_command to query it.\n"
            "5. Explain what you're doing briefly, then ACT using tools.\n"
            "6. After making changes, verify they work (read the file back, run tests, etc.).\n\n"
            "## Workspace\n"
            "You are operating in a workspace directory. All file paths are relative to this workspace. "
            "Start by exploring the workspace structure if you need context."
        )
