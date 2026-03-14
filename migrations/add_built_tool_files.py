"""
Migration: Add 'files' column to built_tools table.
Stores a JSON list of relative file paths for clean uninstall/reset.
"""

import os
import sqlite3
import sys

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

    # Check if column already exists
    cursor.execute("PRAGMA table_info(built_tools)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'files' not in columns:
        cursor.execute("ALTER TABLE built_tools ADD COLUMN files TEXT")
        conn.commit()
        print("Added 'files' column to built_tools table.")
    else:
        print("Column 'files' already exists, skipping.")

    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
