"""
CLI Provider Adapters for Claude Code, OpenAI Codex, and Google Gemini CLI.

Each provider knows how to:
- Build the CLI command for a prompt
- Set up the environment variables
- Normalize stream-JSON events to Claude's internal format
"""

import os
import shutil
from abc import ABC, abstractmethod
from typing import Optional


class CLIProvider(ABC):
    """Base class for CLI provider adapters."""
    name: str
    display_name: str
    brand_color: str

    @abstractmethod
    def build_command(self, prompt: str, session_uuid: str, is_first: bool,
                      model: Optional[str] = None, system_context: Optional[str] = None) -> list:
        """Build the CLI command list."""
        pass

    @abstractmethod
    def build_env(self, base_env: dict, api_key: str, **kwargs) -> dict:
        """Modify environment for this provider."""
        pass

    @abstractmethod
    def normalize_event(self, raw: dict) -> Optional[dict]:
        """Convert a raw JSON event to Claude's stream-json format. Return None to skip."""
        pass

    def get_cli_binary(self) -> Optional[str]:
        """Return the path to the CLI binary, or None if not found."""
        return shutil.which(self.name)


class ClaudeProvider(CLIProvider):
    """Claude Code CLI — pass-through (native format)."""
    name = "claude"
    display_name = "Claude Code"
    brand_color = "#da6a46"

    def build_command(self, prompt, session_uuid, is_first, model=None, system_context=None):
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions"
        ]
        if system_context:
            cmd.extend(["--append-system-prompt", system_context])
        if model:
            cmd.extend(["--model", model])
        if is_first:
            cmd.extend(["--session-id", session_uuid])
        else:
            cmd.extend(["--resume", session_uuid])
        return cmd

    def build_env(self, base_env, api_key, auth_method="api_key", oauth_token=None, **kwargs):
        env = dict(base_env)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        if auth_method == "membership":
            env.pop("ANTHROPIC_API_KEY", None)
            if oauth_token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
        else:
            if api_key:
                env["ANTHROPIC_API_KEY"] = api_key
        return env

    def normalize_event(self, raw):
        # Already in Claude format — pass through
        return raw


class CodexProvider(CLIProvider):
    """OpenAI Codex CLI adapter."""
    name = "codex"
    display_name = "OpenAI Codex"
    brand_color = "#10a37f"

    def build_command(self, prompt, session_uuid, is_first, model=None, system_context=None):
        cmd = ["codex", "exec", "--full-auto", "--json"]
        if model:
            cmd.extend(["--model", model])
        effective_prompt = prompt
        if system_context:
            effective_prompt = f"[System context: {system_context}]\n\n{prompt}"
        cmd.append(effective_prompt)
        return cmd

    def build_env(self, base_env, api_key, **kwargs):
        env = dict(base_env)
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return env

    def normalize_event(self, raw):
        """Normalize Codex JSONL events to Claude stream-json format.

        Actual Codex `exec --json` JSONL events:
        - thread.started, turn.started → lifecycle, skip
        - item.started {item.type: command_execution} → tool_use
        - item.completed {item.type: command_execution} → tool_result
        - item.completed {item.type: agent_message} → result (final text)
        - turn.completed → skip (process exit handles session lifecycle)
        - error / turn.failed → error
        """
        evt_type = raw.get("type", "")

        # Skip lifecycle events
        if evt_type in ("thread.started", "turn.started", "turn.completed"):
            return None

        # Command started → tool_use
        if evt_type == "item.started":
            item = raw.get("item", {})
            if item.get("type") == "command_execution":
                return {
                    "type": "tool_use",
                    "tool": {"name": "execute_command"},
                    "input": {"command": item.get("command", "")}
                }
            return None

        # Item completed — command result or agent message
        if evt_type == "item.completed":
            item = raw.get("item", {})
            if item.get("type") == "command_execution":
                return {
                    "type": "tool_result",
                    "content": item.get("aggregated_output", "")
                }
            if item.get("type") == "agent_message":
                return {
                    "type": "result",
                    "result": item.get("text", "")
                }
            return None

        # Error events
        if evt_type in ("error", "turn.failed"):
            return {
                "type": "error",
                "error": {"message": raw.get("message", raw.get("error", str(raw)))}
            }

        return None


class GeminiProvider(CLIProvider):
    """Google Gemini CLI adapter."""
    name = "gemini"
    display_name = "Gemini CLI"
    brand_color = "#4285f4"

    def build_command(self, prompt, session_uuid, is_first, model=None, system_context=None):
        cmd = ["gemini", "--yolo", "--output-format", "stream-json"]
        if model:
            cmd.extend(["--model", model])
        effective_prompt = prompt
        if system_context:
            effective_prompt = f"[System context: {system_context}]\n\n{prompt}"
        cmd.extend(["-p", effective_prompt])
        return cmd

    def build_env(self, base_env, api_key, **kwargs):
        env = dict(base_env)
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        return env

    def normalize_event(self, raw):
        """Normalize Gemini CLI stream-json events to Claude stream-json format.

        Gemini CLI stream-json events:
        - {"type": "init", "session_id": ..., "model": ...} → skip
        - {"type": "message", "role": "user", "content": ...} → skip (echo)
        - {"type": "message", "role": "assistant", "content": ..., "delta": true} → content_block_delta
        - {"type": "action", "tool_name": ..., "input": ...} → tool_use
        - {"type": "action_result", "tool_name": ..., "output": ...} → tool_result
        - {"type": "result", "status": ..., "stats": ...} → result
        - {"type": "error", ...} → error
        """
        evt_type = raw.get("type", "")

        # Skip init and user echo events
        if evt_type == "init":
            return None
        if evt_type == "message" and raw.get("role") == "user":
            return None

        # Assistant message → content_block_delta
        if evt_type == "message" and raw.get("role") == "assistant":
            content = raw.get("content", "")
            if content:
                return {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": content}
                }
            return None

        # Tool/action calls
        if evt_type == "action":
            name = raw.get("tool_name", raw.get("name", "tool"))
            inp = raw.get("input", raw.get("arguments", {}))
            return {
                "type": "tool_use",
                "tool": {"name": name},
                "input": inp if isinstance(inp, dict) else {"command": str(inp)}
            }

        if evt_type == "action_result":
            return {
                "type": "tool_result",
                "content": raw.get("output", raw.get("content", ""))
            }

        # Function call variants
        if evt_type in ("function_call", "tool_call"):
            name = raw.get("name", raw.get("function", {}).get("name", "tool"))
            args = raw.get("arguments", raw.get("args", raw.get("function", {}).get("arguments", "")))
            return {
                "type": "tool_use",
                "tool": {"name": name},
                "input": args if isinstance(args, dict) else {"command": str(args)}
            }

        if evt_type in ("function_call_output", "tool_result_output"):
            return {
                "type": "tool_result",
                "content": raw.get("output", raw.get("content", ""))
            }

        # Final result
        if evt_type == "result":
            stats = raw.get("stats", {})
            return {
                "type": "result",
                "result": raw.get("content", raw.get("text", "")),
                "duration_ms": stats.get("duration_ms"),
                "stats": stats
            }

        # Error events
        if evt_type == "error":
            return {
                "type": "error",
                "error": {"message": raw.get("message", raw.get("error", str(raw)))}
            }

        # Pass through Claude-compatible events
        if evt_type in ("content_block_start", "content_block_delta", "content_block_stop",
                        "assistant", "tool_use", "tool_result", "system"):
            return raw

        return None


# Provider registry
PROVIDERS = {
    "claude": ClaudeProvider(),
    "codex": CodexProvider(),
    "gemini": GeminiProvider(),
}


def get_provider(name: str) -> CLIProvider:
    """Get a CLI provider by name. Defaults to Claude."""
    return PROVIDERS.get(name, PROVIDERS["claude"])
