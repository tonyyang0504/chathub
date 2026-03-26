
from app.database import SessionLocal, ToolExecution, Hub, BotProfile
from sqlalchemy import desc
import json

def get_recent_tool_executions():
    db = SessionLocal()
    try:
        user_id = 1
        # Get user's hub IDs
        hub_ids = [h.id for h in db.query(Hub).filter(Hub.user_id == user_id).all()]
        
        # Fetch latest 10 tool executions
        query = db.query(ToolExecution)
        if hub_ids:
            query = query.filter((ToolExecution.hub_id.in_(hub_ids)) | (ToolExecution.user_id == user_id))
        else:
            query = query.filter(ToolExecution.user_id == user_id)
            
        executions = query.order_by(desc(ToolExecution.created_at)).limit(10).all()
        
        if not executions:
            print("No tool executions found.")
            return

        print(f"{'ID':<5} | {'Tool Type':<20} | {'Operation':<20} | {'Status':<8} | {'Created At'}")
        print("-" * 100)
        for e in executions:
            print(f"{e.id:<5} | {e.tool_type:<20} | {e.operation:<20} | {e.status:<8} | {e.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
            # print(f"  Input: {e.input_data}")
            # print(f"  Output: {e.output_data}")
            
    finally:
        db.close()

if __name__ == "__main__":
    get_recent_tool_executions()
