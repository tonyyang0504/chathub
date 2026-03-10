"""Tool execution engine with path security and sandboxed command execution."""

import asyncio
import glob
import os
import re


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
            "read_url": self._read_url,
            "web_search": self._web_search,
            "browser_read": self._browser_read,
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

    async def _read_url(self, url: str, max_length: int = 15000) -> str:
        """Fetch a URL and return its content. Extracts text from HTML pages."""
        import httpx

        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=30.0,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return f"Error: HTTP {e.response.status_code} fetching {url}"
        except httpx.RequestError as e:
            return f"Error fetching URL: {e}"

        content_type = resp.headers.get("content-type", "")
        raw = resp.text

        # Non-HTML: return raw content
        if "html" not in content_type.lower():
            if len(raw) > max_length:
                raw = raw[:max_length] + "\n... (truncated)"
            return raw

        # HTML: extract readable text
        text = self._extract_text_from_html(raw)

        if len(text) > max_length:
            text = text[:max_length] + "\n... (truncated)"

        return text

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        """Extract readable text from HTML, stripping scripts/styles/nav."""
        # Remove script and style blocks
        html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<nav[^>]*>.*?</nav>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<header[^>]*>.*?</header>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<footer[^>]*>.*?</footer>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<!--.*?-->', ' ', html, flags=re.DOTALL)

        # Convert common block elements to newlines
        html = re.sub(r'<(?:br|hr)[^>]*/?>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'<(?:p|div|h[1-6]|li|tr|blockquote|section|article)[^>]*>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'<(?:td|th)[^>]*>', '\t', html, flags=re.IGNORECASE)

        # Strip remaining tags
        text = re.sub(r'<[^>]+>', ' ', html)

        # Decode common HTML entities
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')

        # Collapse whitespace
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n[ \t]+', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    async def _get_browser(self):
        """Get or create a shared Playwright browser instance."""
        if not hasattr(self, '_playwright') or self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def _browser_read(self, url: str, wait_seconds: float = 3, max_length: int = 15000) -> str:
        """Navigate to a URL in a real browser and return the visible page text."""
        try:
            browser = await self._get_browser()
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(int(wait_seconds * 1000))

                # Extract visible text from the page
                text = await page.evaluate("""() => {
                    // Remove non-visible elements
                    const remove = document.querySelectorAll('script, style, noscript, iframe, svg');
                    remove.forEach(el => el.remove());
                    return document.body ? document.body.innerText : document.documentElement.innerText || '';
                }""")

                if len(text) > max_length:
                    text = text[:max_length] + "\n... (truncated)"

                return text.strip() if text.strip() else "Page loaded but no visible text content found."
            finally:
                await page.close()
        except Exception as e:
            return f"Error reading page: {type(e).__name__}: {e}"

    async def _web_search(self, query: str, max_results: int = 10) -> str:
        """Search Google using a real browser and return results."""
        import urllib.parse
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en&gl=us"

        try:
            browser = await self._get_browser()
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # Extract search results using JS
                results = await page.evaluate("""(maxResults) => {
                    const results = [];
                    // Google search result containers
                    const items = document.querySelectorAll('div.g, div[data-hveid] div.g');
                    for (const item of items) {
                        if (results.length >= maxResults) break;
                        const titleEl = item.querySelector('h3');
                        const linkEl = item.querySelector('a[href]');
                        const snippetEl = item.querySelector('div[data-sncf], div.VwiC3b, span.aCOpRe, div[style*="-webkit-line-clamp"]');
                        if (titleEl && linkEl) {
                            results.push({
                                title: titleEl.innerText,
                                url: linkEl.href,
                                snippet: snippetEl ? snippetEl.innerText : ''
                            });
                        }
                    }
                    // Fallback: if no structured results found, get all visible text
                    if (results.length === 0) {
                        return [{title: '_fallback_', url: '', snippet: document.body ? document.body.innerText.slice(0, 5000) : 'No content found'}];
                    }
                    return results;
                }""", max_results)

                if not results:
                    return "No search results found."

                # Check for fallback
                if len(results) == 1 and results[0].get('title') == '_fallback_':
                    return f"Could not parse structured results. Page text:\n\n{results[0]['snippet']}"

                # Format results
                lines = []
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. {r['title']}")
                    lines.append(f"   URL: {r['url']}")
                    if r.get('snippet'):
                        lines.append(f"   {r['snippet']}")
                    lines.append("")

                return "\n".join(lines).strip()
            finally:
                await page.close()
        except Exception as e:
            return f"Error performing search: {type(e).__name__}: {e}"
