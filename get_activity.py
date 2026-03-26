
from app.database import SessionLocal, ActivityLog, BotProfile
from sqlalchemy import desc

def get_recent_activity():
    db = SessionLocal()
    try:
        # Get user's bot IDs (assuming user_id=1 for this agent)
        user_id = 1
        bot_ids = db.query(BotProfile.id).filter(BotProfile.user_id == user_id).all()
        bot_ids = [b[0] for b in bot_ids]
        
        if not bot_ids:
            print("No bots found for user 1.")
            return

        # Fetch latest 10 activities
        activities = db.query(ActivityLog).filter(ActivityLog.bot_profile_id.in_(bot_ids)).order_by(desc(ActivityLog.timestamp)).limit(10).all()
        
        if not activities:
            print("No activity logs found.")
            return

        print(f"{'ID':<5} | {'Bot ID':<8} | {'Action':<20} | {'Timestamp':<20} | {'Details'}")
        print("-" * 80)
        for a in activities:
            details = (a.details[:50] + '...') if a.details and len(a.details) > 50 else a.details
            print(f"{a.id:<5} | {a.bot_profile_id:<8} | {a.action:<20} | {a.timestamp.strftime('%Y-%m-%d %H:%M:%S'):<20} | {details}")
            
    finally:
        db.close()

if __name__ == "__main__":
    get_recent_activity()
