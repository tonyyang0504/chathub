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
        cmd = ["codex", "--full-auto"]
        if model:
            cmd.extend(["--model", model])
        # Codex doesn't have --append-system-prompt, but we can prepend context to prompt
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
        """Normalize Codex JSONL events to Claude stream-json format."""
        evt_type = raw.get("type", "")

        # Codex outputs lines of text as the agent works
        if evt_type == "message":
            role = raw.get("role", "assistant")
            content = raw.get("content", "")
            if role == "user":
                return None  # Skip echoed user messages
            return {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": content + "\n"}
            }

        # Command execution events
        if evt_type == "function_call":
            name = raw.get("name", "command")
            args = raw.get("arguments", "")
            return {
                "type": "tool_use",
                "tool": {"name": name},
                "input": {"command": args}
            }

        if evt_type == "function_call_output":
            output = raw.get("output", "")
            return {
                "type": "tool_result",
                "content": output
            }

        # Final result
        if evt_type == "result":
            content = raw.get("content", raw.get("message", ""))
            return {
                "type": "result",
                "result": content,
                "cost_usd": raw.get("cost_usd"),
                "duration_ms": raw.get("duration_ms"),
                "duration_api_ms": raw.get("duration_api_ms")
            }

        # Error
        if evt_type == "error":
            return {
                "type": "error",
                "error": {"message": raw.get("message", raw.get("error", str(raw)))}
            }

        # Pass through unknown events as raw content
        if "content" in raw or "text" in raw:
            text = raw.get("content", raw.get("text", ""))
            if text:
                return {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text}
                }

        # Skip unrecognized events
        return None


class GeminiProvider(CLIProvider):
    """Google Gemini CLI adapter."""
    name = "gemini"
    display_name = "Gemini CLI"
    brand_color = "#4285f4"

    def build_command(self, prompt, session_uuid, is_first, model=None, system_context=None):
        cmd = ["gemini", "--sandbox=false"]
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
        """Normalize Gemini CLI events to Claude stream-json format."""
        evt_type = raw.get("type", "")

        # Gemini's stream-json is similar to Claude's format
        if evt_type in ("content_block_start", "content_block_delta", "content_block_stop",
                        "assistant", "tool_use", "tool_result", "result", "error", "system"):
            return raw  # Pass through compatible events

        # Text content
        if evt_type == "message" or evt_type == "text":
            content = raw.get("content", raw.get("text", ""))
            if content:
                return {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": content}
                }

        # Function/tool calls
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
        if evt_type == "done" or evt_type == "final":
            return {
                "type": "result",
                "result": raw.get("content", raw.get("text", "")),
                "cost_usd": raw.get("cost_usd"),
                "duration_ms": raw.get("duration_ms"),
                "duration_api_ms": raw.get("duration_api_ms")
            }

        # Pass through anything with content
        if "content" in raw or "text" in raw:
            text = raw.get("content", raw.get("text", ""))
            if text:
                return {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text}
                }

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
