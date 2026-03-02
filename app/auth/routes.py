"""
Authentication Routes
- Login
- Register
- Logout
- Current user info
"""

import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from app.config import settings
from app.database import get_db, User
from app.middleware.rate_limit import limiter
from app.auth.utils import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    get_current_user_optional
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Authentication"])

# Detect if running as frozen executable (PyInstaller)
if getattr(sys, 'frozen', False):
    # Running as compiled executable - templates are in _MEIPASS/app/templates
    _BASE_DIR = Path(sys._MEIPASS)
else:
    # Running as script - templates are relative to project root
    _BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "app" / "templates"))

# Add cache_bust to prevent browser caching
templates.env.globals['cache_bust'] = str(int(time.time()))


# ============== Pydantic Schemas ==============

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    name: Optional[str]
    is_active: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ============== Page Routes ==============

@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: Optional[User] = Depends(get_current_user_optional)
):
    """Login page."""
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("auth/login.html", {"request": request})


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    user: Optional[User] = Depends(get_current_user_optional)
):
    """Registration page."""
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("auth/register.html", {"request": request})


# ============== API Routes ==============

@router.post("/register", response_model=UserResponse)
@limiter.limit("3/minute")
async def register(
    request: Request,
    user_data: UserCreate,
    db: Session = Depends(get_db)
):
    """Register a new user."""
    # Check if email already exists
    existing_user = db.query(User).filter(User.email == user_data.email).first()
    if existing_user:
        logger.warning(f"Registration attempt with existing email: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Validate password strength
    if len(user_data.password) < 12:
        logger.warning(f"Registration attempt with weak password: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 12 characters"
        )

    # Create user
    user = User(
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        name=user_data.name
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info(f"New user registered: {user.email} (id={user.id})")
    return user


@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    user_data: UserLogin,
    db: Session = Depends(get_db)
):
    """Login and get access token."""
    # Find user
    user = db.query(User).filter(User.email == user_data.email).first()
    if not user:
        logger.warning(f"Login attempt with unknown email: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    # Verify password
    if not verify_password(user_data.password, user.password_hash):
        logger.warning(f"Login attempt with invalid password for: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    # Check if active
    if not user.is_active:
        logger.warning(f"Login attempt for disabled account: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    # Create token (sub must be a string for JWT)
    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    # Set cookie
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax"
    )

    logger.info(f"User logged in: {user.email} (id={user.id})")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": UserResponse.model_validate(user)
    }


@router.post("/logout")
async def logout(response: Response):
    """Logout and clear token cookie."""
    response.delete_cookie(key="access_token")
    logger.info("User logged out")
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user info."""
    return current_user


@router.put("/me")
async def update_me(
    name: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update current user info."""
    if name is not None:
        current_user.name = name
    db.commit()
    db.refresh(current_user)
    return UserResponse.model_validate(current_user)


@router.post("/change-password")
async def change_password(
    current_password: str,
    new_password: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Change user password."""
    # Verify current password
    if not verify_password(current_password, current_user.password_hash):
        logger.warning(f"Password change attempt with wrong current password: {current_user.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )

    # Validate new password strength
    if len(new_password) < 12:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 12 characters"
        )

    # Ensure new password is different from current
    if current_password == new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from current password"
        )

    # Update password
    current_user.password_hash = get_password_hash(new_password)
    db.commit()

    logger.info(f"Password changed for user: {current_user.email} (id={current_user.id})")
    return {"message": "Password changed successfully"}
