"""
Migration: Remove max_session_minutes column from claude_code_settings and chathub_agent_settings

This column was never read, enforced, or exposed in any UI. Removing dead code.
"""

import sqlite3
import os


def drop_column_if_exists(cursor, table_name, column_name):
    """Drop a column from a table if it exists (SQLite 3.35+)."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if column_name in existing_columns:
        cursor.execute(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
        print(f"  Dropped '{column_name}' from {table_name}")
    else:
        print(f"  '{column_name}' not found in {table_name}, skipping")


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

        # Check SQLite version supports DROP COLUMN (3.35.0+)
        version = sqlite3.sqlite_version_info
        if version < (3, 35, 0):
            print(f"  SQLite {sqlite3.sqlite_version} does not support DROP COLUMN (needs 3.35+), skipping")
            conn.close()
            continue

        drop_column_if_exists(cursor, 'claude_code_settings', 'max_session_minutes')
        drop_column_if_exists(cursor, 'chathub_agent_settings', 'max_session_minutes')

        conn.commit()
        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
