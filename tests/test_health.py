"""
Tests for health check endpoint.
"""

import pytest


class TestHealthCheck:
    """Tests for health check endpoint."""

    def test_health_check_returns_status(self, client):
        """Test that health check returns status information."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] in ["healthy", "degraded"]

    def test_health_check_includes_service_info(self, client):
        """Test that health check includes service information."""
        response = client.get("/health")
        data = response.json()
        assert data["service"] == "ChatHub"
        assert "version" in data

    def test_health_check_includes_checks(self, client):
        """Test that health check includes component checks."""
        response = client.get("/health")
        data = response.json()
        assert "checks" in data
        assert "database" in data["checks"]
        assert "bot_manager" in data["checks"]

    def test_health_check_database_healthy(self, client):
        """Test that database check is healthy."""
        response = client.get("/health")
        data = response.json()
        assert "healthy" in data["checks"]["database"]
