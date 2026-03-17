"""
Migration: Add commit_hash column to built_tools table.
Stores the git commit hash from publish, used for git revert on uninstall.
"""
import os
import sys
import sqlite3

def get_db_path():
    appdata = os.environ.get("APPDATA")
    if appdata and os.path.exists(os.path.join(appdata, "ChatHub", "app.db")):
        return os.path.join(appdata, "ChatHub", "app.db")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")

def migrate():
    db_path = get_db_path()
    print(f"Database: {db_path}")

    if not os.path.exists(db_path):
        print("Database not found, skipping migration")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(built_tools)")
    columns = [row[1] for row in cursor.fetchall()]

    if "commit_hash" not in columns:
        cursor.execute("ALTER TABLE built_tools ADD COLUMN commit_hash VARCHAR(40)")
        conn.commit()
        print("Added commit_hash column to built_tools")
    else:
        print("commit_hash column already exists, skipping")

    conn.close()
    print("Migration complete")

if __name__ == "__main__":
    migrate()
