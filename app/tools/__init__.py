"""
Tools Package - Modular tool pages for Hub functionality
"""

import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path

from .routes import router as tools_router
from .monitoring import ToolMonitor
from .builder_routes import router as builder_router
from .marketplace_routes import router as marketplace_router
from .coder_routes import router as coder_router

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
    sys.modules[name] = module  # Register in sys.modules for proper cleanup later
    spec.loader.exec_module(module)
    return module


def _get_inactive_tool_names() -> set[str]:
    """Get names of tools marked inactive in DB. Lazy-imports to avoid circular deps."""
    try:
        from app.database import SessionLocal, BuiltTool
        db = SessionLocal()
        try:
            inactive = db.query(BuiltTool.name).filter(
                (BuiltTool.is_active == False) | (BuiltTool.is_deleted == True)
            ).all()
            return {row[0] for row in inactive}
        finally:
            db.close()
    except Exception:
        return set()


def register_custom_tools(parent_router):
    """Discover and register all custom tool routers from app/tools/custom/*/routes.py."""
    if not CUSTOM_TOOLS_DIR.exists():
        return

    inactive_names = _get_inactive_tool_names()

    for tool_dir in sorted(CUSTOM_TOOLS_DIR.iterdir()):
        routes_file = tool_dir / "routes.py"
        if not (tool_dir.is_dir() and routes_file.exists()):
            continue
        if tool_dir.name in _registered_tools:
            continue
        if tool_dir.name in inactive_names:
            logger.info(f"Skipping inactive custom tool: {tool_dir.name}")
            continue
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
            # Mark the tool as inactive in DB so it doesn't retry on every startup
            try:
                from app.database import SessionLocal, BuiltTool
                db = SessionLocal()
                try:
                    tool_record = db.query(BuiltTool).filter(BuiltTool.name == tool_dir.name).first()
                    if tool_record:
                        tool_record.is_active = False
                        db.commit()
                        logger.info(f"Marked failed tool '{tool_dir.name}' as inactive")
                finally:
                    db.close()
            except Exception:
                pass


def register_custom_tools_on_app(app):
    """Register any NEW custom tool routers directly on the running FastAPI app.

    Unlike register_custom_tools() which adds to a source router before
    include_router(), this adds routes directly to the live app so they
    take effect immediately — even after startup.
    """
    if not CUSTOM_TOOLS_DIR.exists():
        return

    inactive_names = _get_inactive_tool_names()

    for tool_dir in sorted(CUSTOM_TOOLS_DIR.iterdir()):
        routes_file = tool_dir / "routes.py"
        if not (tool_dir.is_dir() and routes_file.exists()):
            continue
        if tool_dir.name in _registered_tools:
            continue
        if tool_dir.name in inactive_names:
            continue
        try:
            module = _load_module_from_file(
                f"app.tools.custom.{tool_dir.name}.routes", routes_file
            )
            if hasattr(module, "router"):
                app.include_router(module.router, prefix=f"/tools/{tool_dir.name}", tags=["Tools"])
                _registered_tools.add(tool_dir.name)
                logger.info(f"Live-registered custom tool on app: {tool_dir.name}")
        except Exception as e:
            logger.error(f"Failed to live-register custom tool '{tool_dir.name}': {e}")


def unregister_custom_tool(parent_router, tool_name: str):
    """Remove a custom tool's routes from the parent router at runtime."""
    prefix = f"/tools/{tool_name}"
    parent_router.routes = [r for r in parent_router.routes
                            if not (hasattr(r, 'path') and r.path.startswith(prefix))]
    _registered_tools.discard(tool_name)
    # Clean up Python module cache
    keys_to_remove = [k for k in sys.modules if k.startswith(f"app.tools.custom.{tool_name}")]
    for k in keys_to_remove:
        del sys.modules[k]
    logger.info(f"Unregistered custom tool: {tool_name}")


def get_active_widgets(page: str, db) -> list[dict]:
    """Return widget endpoints for all active custom tools that provide widgets for the given page."""
    from app.database import BuiltTool
    tools = db.query(BuiltTool).filter(
        BuiltTool.is_active == True,
        BuiltTool.is_deleted == False,
        BuiltTool.widgets != None
    ).all()

    widgets = []
    for tool in tools:
        try:
            tool_widgets = json.loads(tool.widgets) if tool.widgets else []
        except (json.JSONDecodeError, TypeError):
            continue
        for w in tool_widgets:
            if w.get("page") == page:
                tool_dir = CUSTOM_TOOLS_DIR / tool.name
                if not tool_dir.exists():
                    continue
                endpoint = w.get("endpoint", f"/api/widget/{page}")
                widgets.append({
                    "tool_name": tool.name,
                    "display_name": tool.display_name or tool.name,
                    "endpoint": f"/tools/{tool.name}{endpoint}"
                })
    return widgets


__all__ = ['tools_router', 'ToolMonitor', 'builder_router', 'marketplace_router', 'coder_router',
           'register_custom_tools', 'register_custom_tools_on_app', 'unregister_custom_tool', 'get_active_widgets']
