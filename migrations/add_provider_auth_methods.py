"""
Migration: Add per-provider auth method fields to AiWorkspaceSettings.

Adds codex_auth_method and gemini_auth_method columns so each provider
can independently use API key or membership/OAuth authentication.
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

    columns = [
        ("codex_auth_method", "VARCHAR(20) DEFAULT 'api_key'"),
        ("gemini_auth_method", "VARCHAR(20) DEFAULT 'api_key'"),
    ]

    for path in paths_to_try:
        if not os.path.exists(path):
            continue

        print(f"Migrating database: {path}")
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='claude_code_settings'")
            if not cursor.fetchone():
                print("  Table claude_code_settings does not exist, skipping")
                continue

            cursor.execute("PRAGMA table_info(claude_code_settings)")
            existing = {col[1] for col in cursor.fetchall()}

            for col_name, col_type in columns:
                if col_name in existing:
                    print(f"  Column {col_name} already exists, skipping")
                else:
                    cursor.execute(f"ALTER TABLE claude_code_settings ADD COLUMN {col_name} {col_type}")
                    print(f"  Added column {col_name}")

            conn.commit()
            print("  Done!")
        except Exception as e:
            conn.rollback()
            print(f"  Error: {e}")
        finally:
            conn.close()


if __name__ == "__main__":
    migrate()
