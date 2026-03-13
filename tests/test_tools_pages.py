"""Tests for tools page routes and content."""


class TestToolsPages:
    """Tests for rendered tools pages."""

    def test_reply_assistant_requires_auth(self, client):
        """Reply Assistant page shows login template when unauthenticated."""
        response = client.get("/tools/reply-assistant")
        assert response.status_code == 200
        assert "Sign In" in response.text

    def test_reply_assistant_renders_for_authenticated_user(self, client, auth_headers):
        """Reply Assistant page renders correctly for authenticated users."""
        response = client.get("/tools/reply-assistant", headers=auth_headers)
        assert response.status_code == 200
        assert "Reply Assistant" in response.text
        assert "Generate Reply Options" in response.text

    def test_tools_index_includes_reply_assistant_card(self, client, auth_headers):
        """Tools index includes Reply Assistant navigation card."""
        response = client.get("/tools", headers=auth_headers)
        assert response.status_code == 200
        assert "/tools/reply-assistant" in response.text
        assert "Reply Assistant" in response.text
