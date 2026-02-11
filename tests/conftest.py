"""
Pytest fixtures for testing.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.main import app
from app.database import Base, get_db
from app.middleware.rate_limit import limiter


# Test database URL (in-memory SQLite)
SQLALCHEMY_DATABASE_URL = "sqlite://"

# Create test engine with in-memory SQLite
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    """Create a fresh database for each test."""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db):
    """Create a test client with database override and rate limiting disabled."""
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    # Disable rate limiting for tests
    limiter.enabled = False

    # Create client with follow_redirects disabled to prevent redirect loops on 401s
    with TestClient(app, follow_redirects=False) as c:
        yield c

    # Re-enable rate limiting after tests
    limiter.enabled = True
    app.dependency_overrides.clear()


@pytest.fixture
def registered_user(client):
    """Create and return a registered user."""
    response = client.post("/auth/register", json={
        "email": "test@example.com",
        "password": "securepassword123"
    })
    return response.json()


@pytest.fixture
def auth_token(client, registered_user):
    """Get an auth token for the registered user."""
    response = client.post("/auth/login", json={
        "email": "test@example.com",
        "password": "securepassword123"
    })
    return response.json()["access_token"]


@pytest.fixture
def auth_headers(auth_token):
    """Return authorization headers."""
    return {"Authorization": f"Bearer {auth_token}"}
