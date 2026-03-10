"""
Built-in tool definitions for ChatHub Agent in OpenAI function-calling format.
"""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path. Returns the file text with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file to read"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating it if it doesn't exist or overwriting if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "A short, clear, human-readable description of what this file change does "
                            "(e.g. 'Save search results to output.json', 'Fix the typo in the config file'). "
                            "Describe the PURPOSE, not the mechanics."
                        )
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file to write"
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write to the file"
                    },
                    "create_dirs": {
                        "type": "boolean",
                        "description": "Create parent directories if they don't exist",
                        "default": False
                    }
                },
                "required": ["description", "path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a file by replacing an exact string match with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "A short, clear, human-readable description of what this file change does "
                            "(e.g. 'Save search results to output.json', 'Fix the typo in the config file'). "
                            "Describe the PURPOSE, not the mechanics."
                        )
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file to edit"
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find and replace (must be unique in the file)"
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The text to replace old_text with"
                    }
                },
                "required": ["description", "path", "old_text", "new_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "Execute any shell command and return its output. Use for ALL system operations: "
"listing files, reading data, running scripts, git, python, package managers, system info, "
"opening apps, curl requests, and anything else the user needs done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "A short, clear, human-readable description of what this command does, written in active voice. "
                            "For simple commands keep it brief (5-10 words, e.g. 'List files in current directory'). "
                            "For complex commands add enough context so a non-technical user understands the intent "
                            "(e.g. 'Check what song is currently playing on Spotify'). "
                            "Never include the raw command text — describe the PURPOSE, not the syntax."
                        )
                    },
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute"
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory for the command"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 120
                    }
                },
                "required": ["description", "command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories matching a pattern. Returns file paths relative to the given directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list",
                        "default": "."
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match files",
                        "default": "*"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 100
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Fetch a URL and return its content. For HTML pages, extracts clean readable text "
"(strips scripts, styles, nav). For non-HTML (JSON, CSV, plain text), returns raw content. "
"Use this instead of curl for reading web pages, Google Docs/Sheets, articles, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch"
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 15000)",
                        "default": 15000
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search Google and return the top results with titles, URLs, and snippets. "
"Uses a real browser (Playwright) so it works with JavaScript-heavy pages. "
"Use this for any web search query instead of trying to curl Google.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 10)",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": "Navigate to a URL in a real browser and return the visible page text. "
"Use this for pages that require JavaScript rendering (Google results, dynamic web apps, SPAs). "
"More powerful than read_url because it executes JavaScript and renders the page like a real browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to and read"
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait for page to load (default 3)",
                        "default": 3
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 15000)",
                        "default": 15000
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a text pattern in files. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for"
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in",
                        "default": "."
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob pattern to filter files (e.g. '*.py')"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching lines to return",
                        "default": 50
                    }
                },
                "required": ["pattern"]
            }
        }
    }
]

# Tools that require explicit user approval before execution
TOOLS_REQUIRING_APPROVAL = {"exec_command", "write_file", "edit_file"}

# Tools that only read data and are safe to auto-approve
READ_ONLY_TOOLS = {"read_file", "list_files", "search_files", "read_url", "web_search", "browser_read"}


def get_tool_definitions(allowed_tools: list[str] | None = None) -> list[dict]:
    """Get tool definitions, optionally filtered by an allow-list."""
    if not allowed_tools:
        return TOOL_DEFINITIONS
    allowed_set = set(allowed_tools)
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed_set]
