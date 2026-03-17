"""
Tools Package - Modular tool pages for Hub functionality
"""

import importlib
import importlib.util
import logging
from pathlib import Path

from .routes import router as tools_router
from .monitoring import ToolMonitor
from .builder_routes import router as builder_router
from .marketplace_routes import router as marketplace_router

logger = logging.getLogger(__name__)

CUSTOM_TOOLS_DIR = Path(__file__).parent / "custom"

# Track registered tool names to avoid duplicates on hot-reload
_registered_tools: set[str] = set()


def _load_module_from_file(name: str, file_path: Path):
    """Load a Python module from file path (works with kebab-case directory names)."""
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register_custom_tools(parent_router):
    """Discover and register all custom tool routers from app/tools/custom/*/routes.py."""
    if not CUSTOM_TOOLS_DIR.exists():
        return
    for tool_dir in sorted(CUSTOM_TOOLS_DIR.iterdir()):
        routes_file = tool_dir / "routes.py"
        if tool_dir.is_dir() and routes_file.exists() and tool_dir.name not in _registered_tools:
            try:
                module = _load_module_from_file(
                    f"app.tools.custom.{tool_dir.name}.routes", routes_file
                )
                if hasattr(module, "router"):
                    parent_router.include_router(module.router, prefix=f"/{tool_dir.name}")
                    _registered_tools.add(tool_dir.name)
                    logger.info(f"Registered custom tool: {tool_dir.name}")
            except Exception as e:
                logger.error(f"Failed to register custom tool '{tool_dir.name}': {e}")


__all__ = ['tools_router', 'ToolMonitor', 'builder_router', 'marketplace_router', 'register_custom_tools']
