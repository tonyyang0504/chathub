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
                "required": ["path", "content"]
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
                "required": ["path", "old_text", "new_text"]
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
                "required": ["command"]
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
READ_ONLY_TOOLS = {"read_file", "list_files", "search_files"}


def get_tool_definitions(allowed_tools: list[str] | None = None) -> list[dict]:
    """Get tool definitions, optionally filtered by an allow-list."""
    if not allowed_tools:
        return TOOL_DEFINITIONS
    allowed_set = set(allowed_tools)
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed_set]
