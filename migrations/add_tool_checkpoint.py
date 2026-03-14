"""
Migration: Add 'publish_commit_hash' and 'pre_publish_db_backup' columns to built_tools table.
Enables git-level rollback and DB restore on tool uninstall.
"""

import os
import sqlite3


def get_db_path():
    """Get the database path, handling both dev and production (AppData) locations."""
    appdata = os.environ.get('APPDATA')
    if appdata:
        db_path = os.path.join(appdata, 'ChatHub', 'app.db')
        if os.path.exists(db_path):
            return db_path

    # Fallback to local data/app.db
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, '..', 'data', 'app.db')


def migrate():
    db_path = get_db_path()
    print(f"Migrating database: {db_path}")

    if not os.path.exists(db_path):
        print("Database not found, skipping migration.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(built_tools)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'publish_commit_hash' not in columns:
        cursor.execute("ALTER TABLE built_tools ADD COLUMN publish_commit_hash VARCHAR(40)")
        print("Added 'publish_commit_hash' column to built_tools table.")
    else:
        print("Column 'publish_commit_hash' already exists, skipping.")

    if 'pre_publish_db_backup' not in columns:
        cursor.execute("ALTER TABLE built_tools ADD COLUMN pre_publish_db_backup VARCHAR(500)")
        print("Added 'pre_publish_db_backup' column to built_tools table.")
    else:
        print("Column 'pre_publish_db_backup' already exists, skipping.")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
