"""
Tests for authentication endpoints.
"""

import pytest


class TestRegistration:
    """Tests for user registration."""

    def test_register_success(self, client):
        """Test successful user registration."""
        response = client.post("/auth/register", json={
            "email": "newuser@example.com",
            "password": "securepassword123"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "newuser@example.com"
        assert "id" in data

    def test_register_weak_password(self, client):
        """Test registration with weak password."""
        response = client.post("/auth/register", json={
            "email": "test@example.com",
            "password": "short"
        })
        assert response.status_code == 400
        assert "12 characters" in response.json()["detail"]

    def test_register_duplicate_email(self, client, registered_user):
        """Test registration with existing email."""
        response = client.post("/auth/register", json={
            "email": "test@example.com",
            "password": "anotherpassword123"
        })
        assert response.status_code == 400
        assert "already registered" in response.json()["detail"]

    def test_register_invalid_email(self, client):
        """Test registration with invalid email format."""
        response = client.post("/auth/register", json={
            "email": "not-an-email",
            "password": "securepassword123"
        })
        assert response.status_code == 422  # Validation error


class TestLogin:
    """Tests for user login."""

    def test_login_success(self, client, registered_user):
        """Test successful login."""
        response = client.post("/auth/login", json={
            "email": "test@example.com",
            "password": "securepassword123"
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password(self, client):
        """Test login with wrong password returns error."""
        # First register a user
        client.post("/auth/register", json={
            "email": "wrongpwd@example.com",
            "password": "securepassword123"
        })
        # Then try to login with wrong password
        response = client.post("/auth/login", json={
            "email": "wrongpwd@example.com",
            "password": "wrongpassword123"
        })
        # Should return 401 Unauthorized
        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["detail"]

    def test_login_nonexistent_user(self, client):
        """Test login with non-existent user returns error."""
        response = client.post("/auth/login", json={
            "email": "nonexistent@example.com",
            "password": "somepassword123"
        })
        # Should return 401 Unauthorized
        assert response.status_code == 401


class TestCurrentUser:
    """Tests for current user endpoint."""

    def test_get_me_authenticated(self, client, auth_headers):
        """Test getting current user info when authenticated."""
        response = client.get("/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "test@example.com"

    def test_get_me_unauthenticated(self, client):
        """Test getting current user info without authentication."""
        response = client.get("/auth/me", follow_redirects=False)
        # Without auth, should get 401 or redirect to login
        assert response.status_code in [401, 302, 307]


class TestLogout:
    """Tests for logout."""

    def test_logout(self, client, auth_headers):
        """Test logout clears session."""
        response = client.post("/auth/logout", headers=auth_headers)
        assert response.status_code == 200
        assert "Logged out" in response.json()["message"]
