"""
Facebook OAuth Routes for Messenger and Instagram.

Implements the Facebook Login flow so users can connect their Facebook Pages
and Instagram accounts with a single click — no developer portal needed.

Flow:
1. User clicks "Connect Facebook" → GET /api/bots/{bot_id}/facebook-oauth-start
2. Redirects to Facebook Login → user authorizes
3. Facebook redirects back → GET /api/bots/facebook-oauth-callback
4. Exchange code for token → get user's pages → auto-subscribe webhooks
5. Redirect to bots page with success message
"""

import json
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, BotProfile
from app.auth.utils import encrypt_string

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Facebook OAuth"])
callback_router = APIRouter(tags=["Facebook OAuth"])

GRAPH_API = "https://graph.facebook.com/v21.0"
FACEBOOK_AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"

# Required scopes for Messenger + Instagram
SCOPES = "pages_show_list,pages_messaging,pages_manage_metadata,pages_read_engagement,instagram_basic,instagram_manage_messages"

# In-memory store for OAuth state tokens (maps state → {bot_id, user_id})
_oauth_states = {}


def _get_redirect_uri(request: Request) -> str:
    """Build the OAuth redirect URI from the current request."""
    # Use X-Forwarded headers if behind ngrok/proxy
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost:8000")
    return f"{scheme}://{host}/auth/facebook-oauth-callback"


@router.get("/{bot_id}/facebook-oauth-start")
async def facebook_oauth_start(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Start Facebook OAuth flow — redirects to Facebook Login."""
    if not settings.FACEBOOK_APP_ID or not settings.FACEBOOK_APP_SECRET:
        raise HTTPException(status_code=500, detail="Facebook App credentials not configured")

    # Verify bot exists (skip user auth — OAuth state token provides security)
    bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Generate CSRF state token
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"bot_id": bot_id, "user_id": bot.user_id}

    redirect_uri = _get_redirect_uri(request)

    params = {
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "response_type": "code",
    }

    auth_url = f"{FACEBOOK_AUTH_URL}?{urlencode(params)}"
    logger.info(f"Bot {bot_id}: Starting Facebook OAuth, redirect_uri={redirect_uri}")

    return RedirectResponse(url=auth_url)


@callback_router.get("/facebook-oauth-callback")
async def facebook_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Handle Facebook OAuth callback — exchange code for tokens."""

    # Handle errors from Facebook
    if error:
        logger.warning(f"Facebook OAuth error: {error} — {error_description}")
        return _render_result_page(False, f"Facebook authorization failed: {error_description or error}")

    if not code or not state:
        return _render_result_page(False, "Missing authorization code or state")

    # Validate CSRF state
    oauth_data = _oauth_states.pop(state, None)
    if not oauth_data:
        return _render_result_page(False, "Invalid or expired state token. Please try again.")

    bot_id = oauth_data["bot_id"]
    user_id = oauth_data["user_id"]

    bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
    if not bot:
        return _render_result_page(False, "Bot not found")

    redirect_uri = _get_redirect_uri(request)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Exchange code for User Access Token
            token_resp = await client.get(
                f"{GRAPH_API}/oauth/access_token",
                params={
                    "client_id": settings.FACEBOOK_APP_ID,
                    "client_secret": settings.FACEBOOK_APP_SECRET,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            if token_resp.status_code != 200:
                error_data = token_resp.json().get("error", {})
                return _render_result_page(False, f"Token exchange failed: {error_data.get('message', token_resp.text[:200])}")

            user_token = token_resp.json().get("access_token")
            if not user_token:
                return _render_result_page(False, "No access token received from Facebook")

            logger.info(f"Bot {bot_id}: Got user access token")

            # Step 2: Exchange for long-lived token (60 days)
            ll_resp = await client.get(
                f"{GRAPH_API}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.FACEBOOK_APP_ID,
                    "client_secret": settings.FACEBOOK_APP_SECRET,
                    "fb_exchange_token": user_token,
                },
            )
            if ll_resp.status_code == 200:
                ll_data = ll_resp.json()
                user_token = ll_data.get("access_token", user_token)
                logger.info(f"Bot {bot_id}: Exchanged for long-lived token")

            # Step 3: Get user's Facebook Pages with Page Access Tokens
            pages_resp = await client.get(
                f"{GRAPH_API}/me/accounts",
                params={
                    "access_token": user_token,
                    "fields": "id,name,access_token,picture",
                },
            )
            if pages_resp.status_code != 200:
                return _render_result_page(False, "Failed to fetch Facebook Pages")

            pages = pages_resp.json().get("data", [])
            if not pages:
                return _render_result_page(False, "No Facebook Pages found. Create a page first.")

            # If multiple pages, use the first one (or show a page selector)
            # For now, auto-select the first page
            page = pages[0]
            page_id = page["id"]
            page_name = page["name"]
            page_token = page["access_token"]

            logger.info(f"Bot {bot_id}: Selected page '{page_name}' (id={page_id})")

            # Step 4: Subscribe the page to webhook events
            sub_resp = await client.post(
                f"{GRAPH_API}/{page_id}/subscribed_apps",
                params={
                    "access_token": page_token,
                    "subscribed_fields": "messages,messaging_postbacks,message_reads",
                },
            )
            if sub_resp.status_code == 200:
                logger.info(f"Bot {bot_id}: Page '{page_name}' subscribed to webhook events")
            else:
                logger.warning(f"Bot {bot_id}: Webhook subscription response: {sub_resp.text[:200]}")

            # Step 5: Store everything in platform_config
            platform_config = json.loads(bot.platform_config or "{}")
            platform_config["platform_token_encrypted"] = encrypt_string(page_token)
            platform_config["app_secret"] = settings.FACEBOOK_APP_SECRET
            platform_config["page_id"] = page_id
            platform_config["page_name"] = page_name
            platform_config["webhook_verify_token"] = "chathub_verify"
            # Instagram needs the page_id as instagram_page_id
            if bot.platform_type == "instagram":
                platform_config["instagram_page_id"] = page_id
                platform_config["instagram_app_secret"] = settings.FACEBOOK_APP_SECRET
            bot.platform_config = json.dumps(platform_config)

            # Save page name to account display fields
            bot.whatsapp_name = page_name

            db.commit()
            logger.info(f"Bot {bot_id}: Facebook OAuth complete — page '{page_name}' connected")

            logger.info(f"Bot {bot_id}: Facebook OAuth complete — redirecting to bots page")

            # Also stop the bot if it was running without credentials
            # (it will be restarted from the bots page)
            bot.is_running = False
            db.commit()

            return _render_result_page(
                True,
                f"Connected to Facebook Page: {page_name}",
                bot_id=bot_id,
            )

    except httpx.HTTPError as e:
        logger.error(f"Bot {bot_id}: Facebook OAuth HTTP error: {e}")
        return _render_result_page(False, f"Connection error: {e}")
    except Exception as e:
        logger.error(f"Bot {bot_id}: Facebook OAuth error: {e}", exc_info=True)
        return _render_result_page(False, f"Unexpected error: {e}")


def _render_result_page(success: bool, message: str, bot_id: int = None) -> HTMLResponse:
    """Render result page that auto-redirects to bots page."""
    if success and bot_id:
        # Auto-redirect to bots page with autostart param
        html = f"""<!DOCTYPE html>
<html>
<head><title>Connected! - ChatHub</title></head>
<body>
    <p>Connected! Redirecting...</p>
    <script>window.location.href = '/dashboard/bots?autostart={bot_id}';</script>
</body>
</html>"""
        return HTMLResponse(content=html)

    # Error page
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Connection Failed - ChatHub</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
        .card {{ background: white; border-radius: 16px; padding: 40px; text-align: center; max-width: 400px; box-shadow: 0 4px 24px rgba(0,0,0,0.1); }}
        .icon {{ font-size: 4rem; color: #dc3545; }}
        h2 {{ margin: 16px 0 8px; }}
        p {{ color: #666; }}
        .btn {{ display: inline-block; margin-top: 16px; padding: 10px 24px; background: #dc3545; color: white; border: none; border-radius: 20px; cursor: pointer; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="card">
        <i class="bi bi-x-circle-fill icon"></i>
        <h2>Connection Failed</h2>
        <p>{message}</p>
        <a href="/dashboard/bots" class="btn">Go to Bots</a>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)
