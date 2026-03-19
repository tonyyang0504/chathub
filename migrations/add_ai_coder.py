"""
Migration: Add AI Coder support
- Creates code_modifications table
- Adds listing_type column to custom_tool_listings
"""

import os
import sys
import sqlite3

# Determine database path
if getattr(sys, 'frozen', False):
    db_path = os.path.join(os.environ.get('APPDATA', ''), 'ChatHub', 'app.db')
else:
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'app.db')

# Override with environment variable if set
env_db = os.environ.get('DATABASE_URL', '')
if env_db:
    env_db = env_db.replace('sqlite:///', '').replace('sqlite:', '')
    if env_db:
        db_path = env_db


def migrate():
    print(f"Using database: {db_path}")
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Create code_modifications table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS code_modifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES claude_code_sessions(id),
            title VARCHAR(200) NOT NULL,
            description TEXT,
            commit_hash VARCHAR(40),
            revert_commit_hash VARCHAR(40),
            files_changed TEXT,
            status VARCHAR(20) DEFAULT 'active',
            published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            reverted_at DATETIME
        )
    """)
    print("Created code_modifications table.")

    # 2. Add listing_type column to custom_tool_listings
    try:
        cursor.execute("ALTER TABLE custom_tool_listings ADD COLUMN listing_type VARCHAR(20) DEFAULT 'tool'")
        print("Added listing_type column to custom_tool_listings.")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("listing_type column already exists, skipping.")
        else:
            raise

    conn.commit()
    conn.close()
    print("Migration completed successfully.")


if __name__ == "__main__":
    migrate()
