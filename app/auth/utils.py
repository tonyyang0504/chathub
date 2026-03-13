"""
Authentication Utilities
- Password hashing
- JWT token management
- Current user dependency
- WebSocket authentication
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet
import base64

from app.config import settings
from app.database import get_db, User

logger = logging.getLogger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Bearer
security = HTTPBearer(auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    """Decode a JWT token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user from JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = None

    # Try to get token from Authorization header
    if credentials:
        token = credentials.credentials
    # Try to get token from cookie
    elif settings.COOKIE_NAME in request.cookies:
        token = request.cookies.get(settings.COOKIE_NAME)

    if not token:
        raise credentials_exception

    payload = decode_token(token)
    if payload is None:
        raise credentials_exception

    user_id_str = payload.get("sub")
    if user_id_str is None:
        raise credentials_exception

    try:
        user_id = int(user_id_str)
    except (ValueError, TypeError):
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    return user


async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user if authenticated, None otherwise."""
    try:
        return await get_current_user(request, credentials, db)
    except HTTPException:
        return None


# ============== Encryption Utilities ==============

def get_fernet() -> Fernet:
    """Get Fernet instance for encryption."""
    key = settings.get_encryption_key()
    # Fernet requires a 32-byte base64-encoded key
    fernet_key = base64.urlsafe_b64encode(key[:32])
    return Fernet(fernet_key)


def encrypt_string(plaintext: str) -> str:
    """Encrypt a string."""
    f = get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_string(ciphertext: str) -> str:
    """Decrypt a string."""
    f = get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


# ============== WebSocket Authentication ==============

async def get_websocket_user(
    websocket: WebSocket,
    db: Session
) -> Optional[User]:
    """
    Authenticate a WebSocket connection using query param or cookie token.

    Attempts to get token from:
    1. Query parameter 'token'
    2. Cookie 'access_token'

    Args:
        websocket: The WebSocket connection
        db: Database session

    Returns:
        User if authenticated, None otherwise
    """
    token = None

    # Try to get token from query parameter
    token = websocket.query_params.get("token")

    # Try to get token from cookies if not in query params
    if not token:
        token = websocket.cookies.get(settings.COOKIE_NAME)

    if not token:
        logger.debug("WebSocket auth: No token found in query params or cookies")
        return None

    # Decode and validate token
    payload = decode_token(token)
    if payload is None:
        logger.warning("WebSocket auth: Invalid token")
        return None

    user_id_str = payload.get("sub")
    if user_id_str is None:
        logger.warning("WebSocket auth: No user ID in token payload")
        return None

    try:
        user_id = int(user_id_str)
    except (ValueError, TypeError):
        logger.warning("WebSocket auth: Invalid user ID format in token")
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        logger.warning(f"WebSocket auth: User {user_id} not found")
        return None

    if not user.is_active:
        logger.warning(f"WebSocket auth: User {user_id} is inactive")
        return None

    logger.debug(f"WebSocket auth: Successfully authenticated user {user_id}")
    return user


async def require_websocket_auth(
    websocket: WebSocket,
    db: Session
) -> User:
    """
    Require authentication for a WebSocket connection.
    Closes the connection with code 4001 if not authenticated.

    Args:
        websocket: The WebSocket connection
        db: Database session

    Returns:
        User if authenticated

    Raises:
        WebSocketException if not authenticated (connection will be closed)
    """
    user = await get_websocket_user(websocket, db)
    if user is None:
        logger.warning("WebSocket: Authentication required but not provided")
        await websocket.close(code=4001, reason="Authentication required")
        raise Exception("WebSocket authentication failed")
    return user
