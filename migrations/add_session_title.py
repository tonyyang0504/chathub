"""
Migration: Add title column to claude_code_sessions and chathub_agent_sessions.

AI-generated short title for easier session identification in the sidebar.
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

    tables = ['claude_code_sessions', 'chathub_agent_sessions']

    for path in paths_to_try:
        if not os.path.exists(path):
            continue

        print(f"Migrating database: {path}")
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        for table in tables:
            # Check if column already exists
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            if 'title' in columns:
                print(f"  Column 'title' already exists in {table}, skipping")
                continue

            cursor.execute(f"ALTER TABLE {table} ADD COLUMN title VARCHAR(200)")
            print(f"  Added 'title' column to {table}")

        conn.commit()
        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
