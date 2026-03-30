from app.database import SessionLocal, ActivityLog, BotProfile
import os

def get_activity_logs():
    db = SessionLocal()
    try:
        # Assuming user_id=1 for this session, as per GEMINI.md
        # Join ActivityLog with BotProfile to filter by user_id
        activity_logs = (
            db.query(ActivityLog)
            .join(BotProfile, ActivityLog.bot_profile_id == BotProfile.id)
            .filter(BotProfile.user_id == 1)
            .order_by(ActivityLog.timestamp.desc())
            .limit(20)
            .all()
        )
        if not activity_logs:
            print("No activity logs found for user_id=1.")
            return

        print("Recent Activity Logs for user_id=1:")
        for log in activity_logs:
            print(f"ID: {log.id}, Action: {log.action}, Details: {log.details}, Timestamp: {log.timestamp}")
    except Exception as e:
        print(f"Error retrieving activity logs: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    get_activity_logs()