"""
Authentication Module
"""

from app.auth.routes import router as auth_router
from app.auth.utils import get_current_user, create_access_token

__all__ = ["auth_router", "get_current_user", "create_access_token"]
