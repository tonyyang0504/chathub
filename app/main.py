"""
ChatHub - FastAPI Application Entry Point
Multi-tenant AI Bot Platform for WhatsApp, Telegram, Messenger, Line, WeChat and more
"""
import os
import sys
import asyncio
import time
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

# Fix for Windows asyncio subprocess issue
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .config import settings
from .database import engine, Base, get_db
from .middleware.rate_limit import limiter, rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from .auth.routes import router as auth_router
from .auth.utils import get_current_user_optional, get_current_user
from .bots.routes import router as bots_router
from .bots.manager import bot_manager
from .conversations.routes import router as conversations_router
from .analytics.routes import router as analytics_router
from .hubs.routes import router as hubs_router
from .hubs.scheduler import content_scheduler
from .hubs.analysis_scheduler import contact_analysis_scheduler
from .hubs.followup_scheduler import followup_send_scheduler
from .scripts.scheduler import script_scheduler
from .tools import tools_router, builder_router, marketplace_router, register_custom_tools
from .agents import agents_router
from .ai_workspace.routes import router as ai_workspace_router
from .ai_workspace.manager import ai_workspace_manager
from .chathub_agent.routes import router as chathub_agent_router
from .chathub_agent.manager import chathub_agent_manager
from .scripts.routes import router as scripts_router
from .metrics import metrics_endpoint
from .logging_config import setup_logging

# Register platform adapters
from .platforms.base import PlatformType
from .platforms.registry import platform_registry
from .platforms.whatsapp import WhatsAppAdapter
from .platforms.discord import DiscordAdapter
from .platforms.linkedin import LinkedInAdapter
from .platforms.telegram import TelegramAdapter
from .platforms.line import LineAdapter
from .platforms.line.routes import router as line_webhook_router
from .platforms.instagram import InstagramAdapter
from .platforms.messenger import MessengerAdapter
from .platforms.tinder import TinderAdapter
from .platforms.bumble import BumbleAdapter
platform_registry.register(PlatformType.WHATSAPP, WhatsAppAdapter)
platform_registry.register(PlatformType.DISCORD, DiscordAdapter)
platform_registry.register(PlatformType.LINKEDIN, LinkedInAdapter)
platform_registry.register(PlatformType.TELEGRAM, TelegramAdapter)
platform_registry.register(PlatformType.LINE, LineAdapter)
platform_registry.register(PlatformType.INSTAGRAM, InstagramAdapter)
platform_registry.register(PlatformType.MESSENGER, MessengerAdapter)
platform_registry.register(PlatformType.TINDER, TinderAdapter)
platform_registry.register(PlatformType.BUMBLE, BumbleAdapter)

# Detect if running as frozen executable (PyInstaller)
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    FROZEN = True
    BASE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).parent
    # Use AppData for user data on Windows
    if sys.platform == 'win32':
        import os as _os
        DATA_DIR = Path(_os.environ.get('APPDATA', '')) / 'ChatHub'
    else:
        DATA_DIR = APP_DIR / 'data'
else:
    # Running as script
    FROZEN = False
    BASE_DIR = Path(__file__).resolve().parent.parent
    APP_DIR = BASE_DIR
    DATA_DIR = BASE_DIR / 'data'

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)


# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Starting ChatHub...")

    # Store the main event loop for cross-thread WebSocket calls
    from app.conversations.routes import conversation_ws_manager
    main_loop = asyncio.get_running_loop()
    conversation_ws_manager.set_main_loop(main_loop)
    print("Main event loop stored for WebSocket manager.")

    # Create database tables
    Base.metadata.create_all(bind=engine)
    print("Database tables created.")

    # Create directories if they don't exist
    # Use DATA_DIR for user data (AppData on Windows when frozen)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)

    # Initialize logging system
    setup_logging()
    print("Logging system initialized.")

    # Clean up stale AI Workspace / ChatHub Agent sessions from previous crash
    await cleanup_stale_sessions()

    # Auto-recover bots that were marked as running
    await auto_recover_bots()

    # Start the content scheduler
    await content_scheduler.start()

    # Start the script scheduler
    await script_scheduler.start()

    # Start the contact analysis scheduler
    await contact_analysis_scheduler.start()

    # Start the follow-up send scheduler
    await followup_send_scheduler.start()

    # Start the message scheduler
    from app.bots.message_scheduler import message_scheduler
    await message_scheduler.start()

    yield

    # Shutdown
    print("Shutting down ChatHub...")

    # Stop the message scheduler
    await message_scheduler.stop()

    # Stop the follow-up send scheduler
    await followup_send_scheduler.stop()

    # Stop the contact analysis scheduler
    await contact_analysis_scheduler.stop()

    # Stop the script scheduler
    await script_scheduler.stop()

    # Stop the content scheduler
    await content_scheduler.stop()

    # Stop all AI Workspace sessions
    await ai_workspace_manager.stop_all()
    print("All AI Workspace sessions stopped.")

    # Stop all ChatHub Agent sessions
    await chathub_agent_manager.stop_all()
    print("All ChatHub Agent sessions stopped.")

    # Stop all running bots
    await bot_manager.stop_all_bots()
    print("All bots stopped.")


async def cleanup_stale_sessions():
    """Mark orphaned running sessions as stopped on startup (from previous crash)."""
    from .database import SessionLocal, AiWorkspaceSession, ChatHubAgentSession

    db = SessionLocal()
    try:
        stale_claude = db.query(AiWorkspaceSession).filter(
            AiWorkspaceSession.status.in_(["running", "pending"])
        ).all()
        for s in stale_claude:
            s.status = "stopped"
            s.ended_at = datetime.utcnow() if not s.ended_at else s.ended_at
        if stale_claude:
            db.commit()
            print(f"Cleaned up {len(stale_claude)} stale AI Workspace session(s).")

        stale_agent = db.query(ChatHubAgentSession).filter(
            ChatHubAgentSession.status.in_(["running", "pending"])
        ).all()
        for s in stale_agent:
            s.status = "stopped"
            s.ended_at = datetime.utcnow() if not s.ended_at else s.ended_at
        if stale_agent:
            db.commit()
            print(f"Cleaned up {len(stale_agent)} stale ChatHub Agent session(s).")
    except Exception as e:
        print(f"Error cleaning up stale sessions: {e}")
    finally:
        db.close()


async def auto_recover_bots():
    """Auto-recover bots that were marked as running when server starts."""
    from .database import SessionLocal, BotProfile
    from .auth.utils import decrypt_string
    from .config import settings

    print("Checking for bots to auto-recover...")

    db = SessionLocal()
    try:
        # Find all bots marked as running
        running_bots = db.query(BotProfile).filter(
            BotProfile.is_running == True
        ).all()

        if not running_bots:
            print("No bots to recover.")
            return

        print(f"Found {len(running_bots)} bot(s) to recover...")

        for bot in running_bots:
            try:
                print(f"Auto-recovering bot: {bot.name} (ID: {bot.id})")

                # Decrypt API key
                api_key = decrypt_string(bot.api_key_encrypted) if bot.api_key_encrypted else None

                if not api_key:
                    print(f"  Skipping bot {bot.id}: No API key configured")
                    # Mark as not running since we can't start it
                    bot.is_running = False
                    db.commit()
                    continue

                # Build config for bot
                config = {
                    "bot_profile_id": bot.id,
                    "platform_type": bot.platform_type or "whatsapp",
                    "ai_provider": bot.ai_provider or "openai",
                    "api_key": api_key,
                    "model": bot.model or "gpt-4o-mini",
                    "system_prompt": bot.system_prompt,
                    "temperature": bot.temperature if bot.temperature is not None else 0.7,
                    "max_tokens": bot.max_tokens if bot.max_tokens is not None else 1000,
                    "top_p": bot.top_p if bot.top_p is not None else 1.0,
                    "frequency_penalty": bot.frequency_penalty if bot.frequency_penalty is not None else 0.0,
                    "presence_penalty": bot.presence_penalty if bot.presence_penalty is not None else 0.0,
                    "max_history": bot.max_history or 20,
                    "response_delay_min": bot.response_delay_min or 3,
                    "response_delay_max": bot.response_delay_max or 8,
                    "group_chat_enabled": bot.group_chat_enabled if bot.group_chat_enabled is not None else True,
                    "headless": bot.headless if bot.headless is not None else False,
                }

                # Add proxy settings if enabled
                if bot.proxy_enabled and bot.proxy_url:
                    config["proxy_enabled"] = True
                    config["proxy_url"] = bot.proxy_url
                    if bot.proxy_username:
                        config["proxy_username"] = decrypt_string(bot.proxy_username)
                    if bot.proxy_password:
                        config["proxy_password"] = decrypt_string(bot.proxy_password)

                # Start the bot
                await bot_manager.start_bot(bot.id, config)
                print(f"  Bot {bot.name} recovery started")

            except Exception as e:
                print(f"  Error recovering bot {bot.id}: {e}")
                # Mark as not running on error
                bot.is_running = False
                db.commit()

    except Exception as e:
        print(f"Error during bot auto-recovery: {e}")
    finally:
        db.close()


# Create FastAPI application
app = FastAPI(
    title="ChatHub",
    description="Multi-tenant AI Bot Platform for WhatsApp, Telegram, Messenger, Line, WeChat and more",
    version="1.0.0",
    lifespan=lifespan
)

# Add rate limiter to app state
app.state.limiter = limiter

# Add rate limit exception handler
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Configure CORS (use CORS_ORIGINS env var, defaults to "*" for development)
cors_origins = settings.CORS_ORIGINS.split(",") if settings.CORS_ORIGINS != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get paths - use the already-defined BASE_DIR which handles frozen executables correctly
# BASE_DIR is set at top of file: sys._MEIPASS for frozen, project root for script
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "static"

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Bot media files directory - use DATA_DIR for user data (AppData on Windows when frozen)
BOT_SESSIONS_DIR = DATA_DIR / "sessions"


# Serve bot media files from /media/bot_{bot_id}/conversations/{chat_name}/{type_folder}/{direction}/{filename}
@app.get("/media/bot_{bot_id}/conversations/{chat_name}/{type_folder}/{direction}/{filename:path}")
async def serve_bot_media(bot_id: int, chat_name: str, type_folder: str, direction: str, filename: str):
    """
    Serve media files from bot session folders.
    Files are stored at: data/sessions/bot_{id}/conversations/{chat_name}/{type_folder}/{direction}/{filename}
    Type folders: images, documents, audio
    Direction: sent, received
    """
    from urllib.parse import unquote

    # Validate type_folder to prevent directory traversal
    allowed_type_folders = {'images', 'documents', 'audio'}
    if type_folder not in allowed_type_folders:
        raise HTTPException(status_code=404, detail="Invalid type folder")

    # Validate direction
    allowed_directions = {'sent', 'received'}
    if direction not in allowed_directions:
        raise HTTPException(status_code=404, detail="Invalid direction")

    # Decode chat_name from URL
    decoded_chat_name = unquote(chat_name)

    # Construct file path
    file_path = BOT_SESSIONS_DIR / f"bot_{bot_id}" / "conversations" / decoded_chat_name / type_folder / direction / filename

    # Check if file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Determine media type for response
    import mimetypes
    media_type, _ = mimetypes.guess_type(str(file_path))

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=filename
    )


# Setup templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Add global cache_bust variable (timestamp) to prevent browser caching
# This updates on every server restart
CACHE_BUST = str(int(time.time()))
templates.env.globals['cache_bust'] = CACHE_BUST


# Include routers
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(bots_router, prefix="/api/bots", tags=["Bots"])
app.include_router(conversations_router, prefix="/api/conversations", tags=["Conversations"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(hubs_router, prefix="/api/hubs", tags=["Hubs"])
app.include_router(scripts_router, tags=["Scripts"])
register_custom_tools(tools_router)  # Auto-discover app/tools/custom/*/routes.py
app.include_router(tools_router, tags=["Tools"])
app.include_router(builder_router, tags=["Tool Builder"])
app.include_router(marketplace_router, tags=["Marketplace"])
app.include_router(ai_workspace_router, tags=["AI Workspace"])
app.include_router(chathub_agent_router, tags=["ChatHub Agent"])
app.include_router(agents_router, tags=["Agents"])
app.include_router(line_webhook_router, tags=["LINE"])

# Prometheus metrics endpoint
app.get("/metrics")(metrics_endpoint)


# Root redirect
@app.get("/")
async def root():
    """Redirect to dashboard or login"""
    return RedirectResponse(url="/dashboard")


# Dashboard routes
@app.get("/dashboard")
async def dashboard(
    request: Request,
    user = Depends(get_current_user)
):
    """Main dashboard page"""
    return templates.TemplateResponse(
        "dashboard/index.html",
        {"request": request, "user": user, "active_page": "dashboard"}
    )


@app.get("/dashboard/bots")
async def dashboard_bots(
    request: Request,
    user = Depends(get_current_user)
):
    """Bot management page"""
    return templates.TemplateResponse(
        "dashboard/bots.html",
        {"request": request, "user": user, "active_page": "bots"}
    )


@app.get("/dashboard/conversations")
async def dashboard_conversations(
    request: Request,
    user = Depends(get_current_user)
):
    """Conversations page"""
    return templates.TemplateResponse(
        "dashboard/conversations.html",
        {"request": request, "user": user, "active_page": "conversations"}
    )


@app.get("/dashboard/analytics")
async def dashboard_analytics(
    request: Request,
    user = Depends(get_current_user)
):
    """Analytics page"""
    return templates.TemplateResponse(
        "dashboard/analytics.html",
        {"request": request, "user": user, "active_page": "analytics"}
    )


@app.get("/dashboard/settings")
async def dashboard_settings(
    request: Request,
    user = Depends(get_current_user)
):
    """Settings page"""
    return templates.TemplateResponse(
        "dashboard/settings.html",
        {"request": request, "user": user, "active_page": "settings"}
    )


@app.get("/dashboard/hubs")
async def dashboard_hubs(
    request: Request,
    user = Depends(get_current_user)
):
    """Hubs management page"""
    return templates.TemplateResponse(
        "dashboard/hubs.html",
        {"request": request, "user": user, "active_page": "hubs"}
    )


# Health check endpoint
@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    """Comprehensive health check endpoint"""
    from sqlalchemy import text

    checks = {
        "database": "unknown",
        "bot_manager": "unknown"
    }

    # Check database
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        checks["database"] = f"unhealthy: {str(e)}"

    # Check bot manager
    try:
        running = len(bot_manager.get_all_running())
        checks["bot_manager"] = f"healthy ({running} bots running)"
    except Exception as e:
        checks["bot_manager"] = f"unhealthy: {str(e)}"

    overall = "healthy" if all("healthy" in str(v) for v in checks.values()) else "degraded"

    return {
        "status": overall,
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "checks": checks
    }


# Error handlers
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle HTTP exceptions"""
    # For API routes and auth POST endpoints, return JSON
    url_str = str(request.url)
    is_api_route = "/api/" in url_str
    is_auth_api = "/auth/" in url_str and request.method == "POST"

    if is_api_route or is_auth_api:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail}
        )

    # For 401 on non-API routes, redirect to login
    if exc.status_code == 401:
        return RedirectResponse(url="/auth/login")

    # For 404 on non-API routes, show error page
    if exc.status_code == 404:
        return templates.TemplateResponse(
            "errors/404.html",
            {"request": request},
            status_code=404
        )

    # Default: return JSON response
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )
