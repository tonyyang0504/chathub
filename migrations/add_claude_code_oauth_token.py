"""
Migration: Add oauth_token_encrypted field to claude_code_settings table

This migration adds:
- oauth_token_encrypted: TEXT (Fernet-encrypted setup-token for membership auth)
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
        cursor.execute("PRAGMA table_info(claude_code_settings)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'oauth_token_encrypted' not in existing_columns:
            cursor.execute("ALTER TABLE claude_code_settings ADD COLUMN oauth_token_encrypted TEXT")
            conn.commit()
            print("  Added 'oauth_token_encrypted' column to claude_code_settings")
        else:
            print("  'oauth_token_encrypted' column already exists")

        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
