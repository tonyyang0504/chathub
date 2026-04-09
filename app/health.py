"""
Health Check API
Auto-diagnoses all ChatHub components: bots, AI providers, database, system resources.
"""

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

import psutil
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from app.database import get_db, User, BotProfile, Conversation, Message
from app.auth.utils import get_current_user, decrypt_string
from app.bots.manager import bot_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Health Check"])


@router.get("/api/health")
async def get_health_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get comprehensive health status of all ChatHub components."""
    start = time.time()

    checks = {
        "system": _check_system(),
        "database": _check_database(db),
        "bots": _check_bots(current_user, db),
        "ai_providers": _check_ai_providers(current_user, db),
    }

    # Overall status
    all_statuses = []
    for section in checks.values():
        if isinstance(section, dict) and "status" in section:
            all_statuses.append(section["status"])
        elif isinstance(section, list):
            for item in section:
                if isinstance(item, dict) and "status" in item:
                    all_statuses.append(item["status"])

    if "red" in all_statuses:
        overall = "red"
    elif "yellow" in all_statuses:
        overall = "yellow"
    else:
        overall = "green"

    return {
        "overall": overall,
        "checks": checks,
        "duration_ms": round((time.time() - start) * 1000),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@router.post("/api/health/fix/{fix_type}")
async def apply_fix(
    fix_type: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply an auto-fix for a specific issue."""
    if fix_type == "recover_bots":
        # Reset stale is_running flags and recover bots
        stale = db.query(BotProfile).filter(
            BotProfile.user_id == current_user.id,
            BotProfile.is_running == True,
        ).all()
        fixed = 0
        for bot in stale:
            if not bot_manager.is_bot_healthy(bot.id):
                bot.is_running = False
                fixed += 1
        db.commit()
        return {"message": f"Reset {fixed} stale bot(s)", "fixed": fixed}

    elif fix_type == "clear_sessions":
        # Clear orphaned session directories
        sessions_dir = Path("data/sessions")
        if sessions_dir.exists():
            bot_ids = {b.id for b in db.query(BotProfile.id).filter(BotProfile.user_id == current_user.id).all()}
            cleared = 0
            for d in sessions_dir.iterdir():
                if d.is_dir() and d.name.startswith("bot_"):
                    try:
                        bid = int(d.name.replace("bot_", ""))
                        if bid not in bot_ids:
                            shutil.rmtree(d)
                            cleared += 1
                    except (ValueError, OSError):
                        pass
            return {"message": f"Cleared {cleared} orphaned session(s)", "fixed": cleared}

    elif fix_type == "cleanup_logs":
        # Delete old log files
        logs_dir = Path("logs/app")
        if logs_dir.exists():
            deleted = 0
            cutoff = datetime.utcnow() - timedelta(days=7)
            for f in logs_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
                    f.unlink()
                    deleted += 1
            return {"message": f"Deleted {deleted} old log file(s)", "fixed": deleted}

    return {"message": "Unknown fix type", "fixed": 0}


def _check_system() -> Dict[str, Any]:
    """Check system resources."""
    # Disk space
    disk = shutil.disk_usage(".")
    disk_pct = (disk.used / disk.total) * 100

    # Memory
    mem = psutil.virtual_memory()

    # CPU
    cpu_pct = psutil.cpu_percent(interval=0.1)

    # Process info
    process = psutil.Process(os.getpid())
    proc_mem = process.memory_info().rss / (1024 * 1024)  # MB

    # Data directory size
    data_size = 0
    data_dir = Path("data")
    if data_dir.exists():
        for f in data_dir.rglob("*"):
            if f.is_file():
                data_size += f.stat().st_size
    data_size_mb = data_size / (1024 * 1024)

    # Log size
    log_size = 0
    logs_dir = Path("logs")
    if logs_dir.exists():
        for f in logs_dir.rglob("*"):
            if f.is_file():
                log_size += f.stat().st_size
    log_size_mb = log_size / (1024 * 1024)

    # Status
    status = "green"
    issues = []
    if disk_pct > 90:
        status = "red"
        issues.append("Disk usage above 90%")
    elif disk_pct > 75:
        status = "yellow"
        issues.append("Disk usage above 75%")
    if mem.percent > 90:
        status = "red"
        issues.append("Memory usage above 90%")
    elif mem.percent > 75:
        if status != "red":
            status = "yellow"
        issues.append("Memory usage above 75%")

    return {
        "status": status,
        "issues": issues,
        "disk": {
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
            "percent": round(disk_pct, 1),
        },
        "memory": {
            "total_gb": round(mem.total / (1024**3), 1),
            "used_gb": round(mem.used / (1024**3), 1),
            "percent": round(mem.percent, 1),
        },
        "cpu_percent": round(cpu_pct, 1),
        "process_memory_mb": round(proc_mem, 1),
        "data_size_mb": round(data_size_mb, 1),
        "log_size_mb": round(log_size_mb, 1),
    }


def _check_database(db: Session) -> Dict[str, Any]:
    """Check database health."""
    status = "green"
    issues = []

    try:
        # Test connection
        db.execute(text("SELECT 1"))

        # Get counts
        bot_count = db.query(func.count(BotProfile.id)).scalar() or 0
        conv_count = db.query(func.count(Conversation.id)).scalar() or 0
        msg_count = db.query(func.count(Message.id)).scalar() or 0

        # Check for stale is_running flags
        stale_running = db.query(func.count(BotProfile.id)).filter(
            BotProfile.is_running == True
        ).scalar() or 0
        running_in_memory = len(bot_manager.get_all_running())
        stale_count = max(0, stale_running - running_in_memory)

        if stale_count > 0:
            status = "yellow"
            issues.append(f"{stale_count} bot(s) marked as running but not in memory")

        # DB file size
        db_size = 0
        db_path = Path("data/app.db")
        if db_path.exists():
            db_size = db_path.stat().st_size / (1024 * 1024)

        return {
            "status": status,
            "issues": issues,
            "connected": True,
            "bots": bot_count,
            "conversations": conv_count,
            "messages": msg_count,
            "stale_running": stale_count,
            "db_size_mb": round(db_size, 1),
        }
    except Exception as e:
        return {
            "status": "red",
            "issues": [f"Database error: {str(e)}"],
            "connected": False,
        }


def _check_bots(user: User, db: Session) -> List[Dict[str, Any]]:
    """Check health of all user's bots."""
    bots = db.query(BotProfile).filter(BotProfile.user_id == user.id).all()
    results = []

    for bot in bots:
        health = bot_manager.get_health_status(bot.id)
        platform_type = bot.platform_type or "whatsapp"

        status = "green"
        issues = []

        if bot.is_running and not health.get("has_instance"):
            status = "red"
            issues.append("Marked running but no instance in memory")
        elif bot.is_running and not health.get("task_active"):
            status = "red"
            issues.append("Instance exists but task not active")
        elif bot.is_running and health.get("error"):
            status = "yellow"
            issues.append(f"Error: {health['error'][:80]}")
        elif not bot.is_running:
            status = "gray"

        # Check if credentials exist
        has_ai_key = bool(bot.api_key_encrypted)
        if not has_ai_key:
            if status == "green":
                status = "yellow"
            issues.append("No AI API key configured")

        results.append({
            "id": bot.id,
            "name": bot.name,
            "platform_type": platform_type,
            "status": status,
            "issues": issues,
            "is_running": bot.is_running,
            "connected": health.get("whatsapp_connected", False),
            "error": health.get("error"),
            "last_active": bot.last_active.isoformat() + "Z" if bot.last_active else None,
            "has_ai_key": has_ai_key,
            "needs_recovery": health.get("needs_recovery", False),
        })

    return results


def _check_ai_providers(user: User, db: Session) -> List[Dict[str, Any]]:
    """Check AI provider configurations for user's bots."""
    bots = db.query(BotProfile).filter(BotProfile.user_id == user.id).all()
    providers_seen = {}

    for bot in bots:
        provider = bot.ai_provider or "openai"
        if provider in providers_seen:
            providers_seen[provider]["bot_count"] += 1
            continue

        status = "green"
        issues = []

        if not bot.api_key_encrypted:
            status = "yellow"
            issues.append("No API key configured")
        else:
            try:
                key = decrypt_string(bot.api_key_encrypted)
                if not key or len(key) < 10:
                    status = "yellow"
                    issues.append("API key appears invalid (too short)")
                elif key.startswith("sk-") or key.startswith("AIza") or len(key) > 20:
                    # Looks like a valid key format
                    pass
                else:
                    status = "yellow"
                    issues.append("API key format unrecognized")
            except Exception:
                status = "red"
                issues.append("Failed to decrypt API key")

        providers_seen[provider] = {
            "provider": provider,
            "status": status,
            "issues": issues,
            "bot_count": 1,
            "model": bot.model or "default",
        }

    return list(providers_seen.values())
