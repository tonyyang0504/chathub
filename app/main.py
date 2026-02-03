"""
WhatsApp Bot Dashboard - FastAPI Application Entry Point
"""
import os
import sys
import asyncio
import time
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
from .auth.routes import router as auth_router
from .auth.utils import get_current_user_optional, get_current_user
from .bots.routes import router as bots_router
from .bots.manager import bot_manager
from .conversations.routes import router as conversations_router
from .analytics.routes import router as analytics_router
from .hubs.routes import router as hubs_router
from .hubs.scheduler import content_scheduler
from .tools import tools_router
from .agents import agents_router


# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Starting WhatsApp Bot Dashboard...")

    # Store the main event loop for cross-thread WebSocket calls
    from app.conversations.routes import conversation_ws_manager
    main_loop = asyncio.get_running_loop()
    conversation_ws_manager.set_main_loop(main_loop)
    print("Main event loop stored for WebSocket manager.")

    # Create database tables
    Base.metadata.create_all(bind=engine)
    print("Database tables created.")

    # Create directories if they don't exist
    os.makedirs("sessions", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    # Auto-recover bots that were marked as running
    await auto_recover_bots()

    # Start the content scheduler
    await content_scheduler.start()

    yield

    # Shutdown
    print("Shutting down WhatsApp Bot Dashboard...")

    # Stop the content scheduler
    await content_scheduler.stop()

    # Stop all running bots
    await bot_manager.stop_all_bots()
    print("All bots stopped.")


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
                api_key = decrypt_string(bot.openai_api_key_encrypted) if bot.openai_api_key_encrypted else None

                if not api_key:
                    print(f"  Skipping bot {bot.id}: No API key configured")
                    # Mark as not running since we can't start it
                    bot.is_running = False
                    db.commit()
                    continue

                # Build config for bot
                config = {
                    "bot_profile_id": bot.id,
                    "openai_api_key": api_key,
                    "openai_model": bot.openai_model or "gpt-4o-mini",
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
    title="WhatsApp Bot Dashboard",
    description="Multi-tenant WhatsApp Bot Management Platform",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS (use CORS_ORIGINS env var, defaults to "*" for development)
cors_origins = settings.CORS_ORIGINS.split(",") if settings.CORS_ORIGINS != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get paths
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = BASE_DIR / "uploads"

# Create uploads directory if not exists
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
(UPLOADS_DIR / "messages").mkdir(parents=True, exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount uploads directory for serving uploaded files (legacy path)
app.mount("/uploads/messages", StaticFiles(directory=str(UPLOADS_DIR / "messages")), name="uploads_messages")

# Bot media files directory
BOT_SESSIONS_DIR = BASE_DIR / "data" / "sessions"


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
app.include_router(tools_router, tags=["Tools"])
app.include_router(agents_router, tags=["Agents"])


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
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "WhatsApp Bot Dashboard",
        "version": "1.0.0"
    }


# Error handlers
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle HTTP exceptions"""
    # For API routes, return JSON
    if "/api/" in str(request.url):
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
