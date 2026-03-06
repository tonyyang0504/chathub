"""Tool execution engine with path security and sandboxed command execution."""

import asyncio
import glob
import os


class ToolExecutor:
    """Executes tools (file I/O, commands, search) within a secured workspace."""

    def __init__(self, workspace_root: str):
        self.workspace_root = os.path.abspath(workspace_root)

    def _resolve_path(self, path: str) -> str:
        """Resolve path relative to workspace_root, rejecting traversal attempts."""
        if os.path.isabs(path):
            resolved = os.path.abspath(path)
        else:
            resolved = os.path.abspath(os.path.join(self.workspace_root, path))

        # Security: ensure resolved path is within workspace_root
        try:
            common = os.path.commonpath([self.workspace_root, resolved])
        except ValueError:
            raise PermissionError(
                f"Path '{path}' resolves outside workspace: {resolved}"
            )

        if common != self.workspace_root:
            raise PermissionError(
                f"Path '{path}' resolves outside workspace: {resolved}"
            )

        return resolved

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """Dispatch tool call to the appropriate handler."""
        handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "exec_command": self._exec_command,
            "list_files": self._list_files,
            "search_files": self._search_files,
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return f"Error: unknown tool '{tool_name}'"

        try:
            return await handler(**arguments)
        except PermissionError as e:
            return f"Permission denied: {e}"
        except FileNotFoundError as e:
            return f"File not found: {e}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    async def _read_file(
        self, path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        """Read a file with optional line offset/limit. Returns numbered lines."""
        resolved = self._resolve_path(path)

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        start = (offset or 1) - 1  # 1-based to 0-based
        end = start + limit if limit else len(lines)
        selected = lines[start:end]

        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i}: {line.rstrip()}")

        return "\n".join(numbered)

    async def _write_file(
        self, path: str, content: str, create_dirs: bool = False
    ) -> str:
        """Write content to a file, optionally creating parent directories."""
        resolved = self._resolve_path(path)

        if create_dirs:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)

        with open(resolved, "w", encoding="utf-8") as f:
            written = f.write(content)

        return f"Wrote {written} bytes to {path}"

    async def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace old_text with new_text in a file. Fails if not found or ambiguous."""
        resolved = self._resolve_path(path)

        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return "Error: old_text not found in file"
        if count > 1:
            return f"Error: old_text found {count} times (must be unique)"

        new_content = content.replace(old_text, new_text, 1)

        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)

        return f"Successfully edited {path}"

    async def _exec_command(
        self, command: str, workdir: str | None = None, timeout: int = 120
    ) -> str:
        """Execute a shell command with timeout. Returns stdout, stderr, and exit code."""
        cwd = self._resolve_path(workdir) if workdir else self.workspace_root

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Error: command timed out after {timeout}s"

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        max_output = 10000
        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output] + "\n... (truncated)"
        if len(stderr_text) > max_output:
            stderr_text = stderr_text[:max_output] + "\n... (truncated)"

        parts = [f"Exit code: {proc.returncode}"]
        if stdout_text.strip():
            parts.append(f"STDOUT:\n{stdout_text}")
        if stderr_text.strip():
            parts.append(f"STDERR:\n{stderr_text}")

        return "\n".join(parts)

    async def _list_files(
        self, path: str = ".", pattern: str = "*", max_results: int = 100
    ) -> str:
        """List files matching a glob pattern within the workspace."""
        resolved = self._resolve_path(path)
        search_pattern = os.path.join(resolved, "**", pattern)

        matches = []
        for match in glob.glob(search_pattern, recursive=True):
            try:
                rel = os.path.relpath(match, self.workspace_root)
            except ValueError:
                continue
            matches.append(rel)
            if len(matches) >= max_results:
                break

        if not matches:
            return "No files found"

        return "\n".join(sorted(matches))

    async def _search_files(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str | None = None,
        max_results: int = 50,
    ) -> str:
        """Search file contents using grep. Returns matching lines."""
        resolved = self._resolve_path(path)

        cmd_parts = ["grep", "-rn", "--"]
        cmd_parts.append(pattern)
        cmd_parts.append(resolved)

        if file_pattern:
            cmd_parts = ["grep", "-rn", f"--include={file_pattern}", "--"]
            cmd_parts.append(pattern)
            cmd_parts.append(resolved)

        # Build shell-safe command
        import shlex

        cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        cmd += f" | head -n {max_results}"

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode("utf-8", errors="replace").strip()

        if not output:
            return "No matches found"

        # Make paths relative to workspace
        lines = []
        for line in output.split("\n"):
            line = line.replace(self.workspace_root + "/", "")
            lines.append(line)

        return "\n".join(lines)
