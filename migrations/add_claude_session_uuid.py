"""
Migration: Add claude_session_uuid field to claude_code_sessions table

This migration adds:
- claude_session_uuid: VARCHAR(36) — stores the Claude CLI session UUID for --resume support
"""

import sqlite3
import os


def migrate():
    # Get database path
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'app.db')

    # Also check AppData path (Windows production)
    appdata = os.environ.get('APPDATA', '')
    appdata_db = os.path.join(appdata, 'ChatHub', 'app.db') if appdata else None

    # Try both paths
    paths_to_try = [db_path]
    if appdata_db and os.path.exists(appdata_db):
        paths_to_try.insert(0, appdata_db)

    for path in paths_to_try:
        if not os.path.exists(path):
            continue

        print(f"Migrating database: {path}")
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        # Check if column already exists
        cursor.execute("PRAGMA table_info(claude_code_sessions)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'claude_session_uuid' not in existing_columns:
            cursor.execute("ALTER TABLE claude_code_sessions ADD COLUMN claude_session_uuid VARCHAR(36)")
            conn.commit()
            print("  Added 'claude_session_uuid' column to claude_code_sessions")
        else:
            print("  'claude_session_uuid' column already exists")

        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
