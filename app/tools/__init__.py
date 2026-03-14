"""
Tools Package - Modular tool pages for Hub functionality
"""

import importlib
import logging
from pathlib import Path

from .routes import router as tools_router
from .monitoring import ToolMonitor
from .builder_routes import router as builder_router
from .marketplace_routes import router as marketplace_router

logger = logging.getLogger(__name__)

CUSTOM_TOOLS_DIR = Path(__file__).parent / "custom"


def register_custom_tools(parent_router):
    """Discover and register all custom tool routers from app/tools/custom/*/routes.py."""
    if not CUSTOM_TOOLS_DIR.exists():
        return
    for tool_dir in sorted(CUSTOM_TOOLS_DIR.iterdir()):
        routes_file = tool_dir / "routes.py"
        if tool_dir.is_dir() and routes_file.exists():
            try:
                module = importlib.import_module(f"app.tools.custom.{tool_dir.name}.routes")
                if hasattr(module, "router"):
                    parent_router.include_router(module.router, prefix=f"/{tool_dir.name}")
                    logger.info(f"Registered custom tool: {tool_dir.name}")
            except Exception as e:
                logger.error(f"Failed to register custom tool '{tool_dir.name}': {e}")


def register_single_custom_tool(parent_router, tool_name: str):
    """Hot-register a single custom tool after publish (no restart needed)."""
    tool_dir = CUSTOM_TOOLS_DIR / tool_name
    routes_file = tool_dir / "routes.py"
    if not routes_file.exists():
        logger.warning(f"Cannot register custom tool '{tool_name}': no routes.py")
        return False

    # Check if already registered (avoid duplicate prefix)
    prefix = f"/{tool_name}"
    for route in parent_router.routes:
        if hasattr(route, 'path') and route.path.startswith(prefix):
            logger.info(f"Custom tool '{tool_name}' already registered, skipping")
            return True

    try:
        module_name = f"app.tools.custom.{tool_name}.routes"
        # Reload if previously imported (e.g. failed attempt)
        if module_name in importlib.sys.modules:
            module = importlib.reload(importlib.sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
        if hasattr(module, "router"):
            parent_router.include_router(module.router, prefix=prefix)
            logger.info(f"Hot-registered custom tool: {tool_name}")
            return True
    except Exception as e:
        logger.error(f"Failed to hot-register custom tool '{tool_name}': {e}")
    return False


__all__ = ['tools_router', 'ToolMonitor', 'builder_router', 'marketplace_router',
           'register_custom_tools', 'register_single_custom_tool']
