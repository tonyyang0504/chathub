"""
Migration: Add suggested_metadata column to claude_code_sessions.

Stores AI-generated metadata (name, display_name, description, icon) as JSON
so the Publish modal can be pre-populated instantly without an extra AI call.
"""

import sqlite3
import os


def migrate():
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'app.db')

    appdata = os.environ.get('APPDATA', '')
    appdata_db = os.path.join(appdata, 'ChatHub', 'app.db') if appdata else None

    paths_to_try = [db_path]
    if appdata_db and os.path.exists(appdata_db):
        paths_to_try.insert(0, appdata_db)

    for path in paths_to_try:
        if not os.path.exists(path):
            continue

        print(f"Migrating database: {path}")
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(claude_code_sessions)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'suggested_metadata' in columns:
            print("  Column 'suggested_metadata' already exists, skipping")
        else:
            cursor.execute("ALTER TABLE claude_code_sessions ADD COLUMN suggested_metadata TEXT")
            print("  Added 'suggested_metadata' column to claude_code_sessions")

        conn.commit()
        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
