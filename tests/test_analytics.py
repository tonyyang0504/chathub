
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from app.database import BotProfile, ActivityLog, User

@pytest.fixture
def test_user_db(db: Session):
    # registered_user fixture already creates a user via API
    user = db.query(User).filter(User.email == "test@example.com").first()
    return user

@pytest.fixture
def test_bot(db: Session, test_user_db):
    bot = BotProfile(
        user_id=test_user_db.id,
        name="Test Bot",
        api_key_encrypted="encrypted",
        platform_type="whatsapp"
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot

@pytest.fixture
def sample_activities(db: Session, test_bot):
    now = datetime.utcnow()
    activities = [
        ActivityLog(bot_profile_id=test_bot.id, action="bot_started", details="Bot started successfully", timestamp=now - timedelta(hours=2)),
        ActivityLog(bot_profile_id=test_bot.id, action="message_sent", details="Sent to 12345: Hello", timestamp=now - timedelta(hours=1)),
        ActivityLog(bot_profile_id=test_bot.id, action="bot_stopped", details="Bot stopped manually", timestamp=now),
    ]
    db.add_all(activities)
    db.commit()
    return activities

def test_get_activity_log(client: TestClient, auth_headers, registered_user, sample_activities, test_bot):
    response = client.get("/api/analytics/activity", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "activities" in data
    assert "total" in data
    assert data["total"] >= 3
    actions = [a["action"] for a in data["activities"]]
    assert "bot_stopped" in actions
    assert "message_sent" in actions
    assert "bot_started" in actions

def test_get_activity_log_filter_bot(client: TestClient, auth_headers, registered_user, sample_activities, test_bot):
    response = client.get(f"/api/analytics/activity?bot_id={test_bot.id}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert all(a["bot_profile_id"] == test_bot.id for a in data["activities"])

def test_get_activity_log_filter_action(client: TestClient, auth_headers, registered_user, sample_activities):
    response = client.get("/api/analytics/activity?action=message_sent", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert all(a["action"] == "message_sent" for a in data["activities"])
    assert len(data["activities"]) == 1

def test_get_activity_log_filter_search(client: TestClient, auth_headers, registered_user, sample_activities):
    response = client.get("/api/analytics/activity?search=successfully", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["activities"]) == 1
    assert "successfully" in data["activities"][0]["details"]

def test_get_activity_log_filter_dates(client: TestClient, auth_headers, registered_user, sample_activities):
    start = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
    response = client.get(f"/api/analytics/activity?start_date={start}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # Should only see the latest activity (bot_stopped)
    assert len(data["activities"]) == 1
    assert data["activities"][0]["action"] == "bot_stopped"

def test_get_activity_log_pagination(client: TestClient, auth_headers, registered_user, sample_activities):
    response = client.get("/api/analytics/activity?limit=2&page=1", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["activities"]) == 2
    assert data["page"] == 1
    assert data["limit"] == 2
    
    response = client.get("/api/analytics/activity?limit=2&page=2", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 2
    assert len(data["activities"]) >= 1
