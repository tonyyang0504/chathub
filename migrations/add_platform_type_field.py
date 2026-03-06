"""
Migration: Add platform_type field to bot_profiles table

This migration adds:
- platform_type: VARCHAR(50) DEFAULT 'whatsapp'

All existing bots default to 'whatsapp' since that's the only platform
that was supported before this change.
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
        cursor.execute("PRAGMA table_info(bot_profiles)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'platform_type' not in existing_columns:
            cursor.execute("ALTER TABLE bot_profiles ADD COLUMN platform_type VARCHAR(50) DEFAULT 'whatsapp'")
            conn.commit()
            print("  Added 'platform_type' column to bot_profiles")
        else:
            print("  'platform_type' column already exists")

        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
