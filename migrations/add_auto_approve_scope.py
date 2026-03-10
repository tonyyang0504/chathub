"""
Migration: Add auto_approve_scope column to claude_code_settings
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

        cursor.execute("PRAGMA table_info(claude_code_settings)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'auto_approve_scope' not in existing_columns:
            cursor.execute("ALTER TABLE claude_code_settings ADD COLUMN auto_approve_scope BOOLEAN DEFAULT 0")
            print("  Added 'auto_approve_scope' column")
        else:
            print("  'auto_approve_scope' already exists, skipping")

        conn.commit()
        conn.close()

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
