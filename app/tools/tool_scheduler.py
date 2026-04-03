"""
Tool Scheduler

Runs custom tool scheduled tasks based on cron expressions declared in TOOL.md.
Uses APScheduler for cron parsing and execution.

Usage in TOOL.md:
    events:
      scheduled: "0 9 * * *"           # Cron expression
      scheduled_handler: daily_report   # Function name in routes.py
"""

import asyncio
import logging
from typing import Dict, Callable, Optional

logger = logging.getLogger(__name__)

# Scheduled handlers: {tool_name: {"cron": str, "handler": Callable}}
_scheduled_tasks: Dict[str, dict] = {}
_scheduler = None


def register_scheduled_task(tool_name: str, cron_expr: str, handler: Callable):
    """Register a tool's scheduled task.

    Args:
        tool_name: Tool's kebab-case name
        cron_expr: Cron expression (e.g., "0 9 * * *")
        handler: Async function to call on schedule
    """
    _scheduled_tasks[tool_name] = {
        "cron": cron_expr,
        "handler": handler,
    }
    logger.info(f"Tool '{tool_name}' registered scheduled task: {cron_expr}")


async def start_scheduler():
    """Start the APScheduler with all registered tool tasks."""
    global _scheduler

    if not _scheduled_tasks:
        logger.info("No scheduled tool tasks registered")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        _scheduler = AsyncIOScheduler()

        for tool_name, task in _scheduled_tasks.items():
            try:
                trigger = CronTrigger.from_crontab(task["cron"])
                _scheduler.add_job(
                    _run_scheduled_handler,
                    trigger=trigger,
                    args=[tool_name, task["handler"]],
                    id=f"tool_{tool_name}",
                    name=f"Tool: {tool_name}",
                    replace_existing=True,
                )
                logger.info(f"Scheduled tool '{tool_name}' with cron: {task['cron']}")
            except Exception as e:
                logger.error(f"Failed to schedule tool '{tool_name}': {e}")

        _scheduler.start()
        logger.info(f"Tool scheduler started with {len(_scheduled_tasks)} tasks")
    except ImportError:
        logger.warning("APScheduler not installed — tool scheduling disabled")
    except Exception as e:
        logger.error(f"Failed to start tool scheduler: {e}")


async def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Tool scheduler stopped")


async def _run_scheduled_handler(tool_name: str, handler: Callable):
    """Execute a scheduled tool handler with error handling."""
    try:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(db=db)
            else:
                handler(db=db)

            # Log successful execution
            from app.tools.monitoring import ToolMonitor
            ToolMonitor.log_execution(
                db=db,
                tool_type=tool_name,
                operation="scheduled_task",
                status="success",
                triggered_by="scheduled",
            )
            db.commit()
        except Exception as e:
            logger.error(f"Scheduled task for tool '{tool_name}' failed: {e}", exc_info=True)
            try:
                from app.tools.monitoring import ToolMonitor
                ToolMonitor.log_execution(
                    db=db,
                    tool_type=tool_name,
                    operation="scheduled_task",
                    status="error",
                    error_message=str(e)[:500],
                    triggered_by="scheduled",
                )
                db.commit()
            except Exception:
                pass
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Scheduled task DB error for tool '{tool_name}': {e}")
