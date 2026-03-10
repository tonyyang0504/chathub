"""
Migration: Add coding CLI provider fields
Adds multi-provider support (Claude, Codex, Gemini) to claude_code_sessions and claude_code_settings.
"""

import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings


def get_db_path():
    db_url = settings.DATABASE_URL
    return db_url.replace("sqlite:///", "").replace("sqlite:", "")


def column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate():
    db_path = get_db_path()
    print(f"Database: {db_path}")

    if not os.path.exists(db_path):
        print("Database file not found!")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # claude_code_sessions: add provider
    if not column_exists(cursor, "claude_code_sessions", "provider"):
        cursor.execute("ALTER TABLE claude_code_sessions ADD COLUMN provider VARCHAR(20) DEFAULT 'claude'")
        print("Added: claude_code_sessions.provider")
    else:
        print("Skipped: claude_code_sessions.provider (already exists)")

    # claude_code_settings: add provider fields
    new_settings_columns = [
        ("openai_api_key_encrypted", "TEXT"),
        ("gemini_api_key_encrypted", "TEXT"),
        ("codex_default_model", "VARCHAR(100) DEFAULT 'codex-mini'"),
        ("gemini_default_model", "VARCHAR(100) DEFAULT 'gemini-2.5-pro'"),
        ("default_provider", "VARCHAR(20) DEFAULT 'claude'"),
    ]

    for col_name, col_type in new_settings_columns:
        if not column_exists(cursor, "claude_code_settings", col_name):
            cursor.execute(f"ALTER TABLE claude_code_settings ADD COLUMN {col_name} {col_type}")
            print(f"Added: claude_code_settings.{col_name}")
        else:
            print(f"Skipped: claude_code_settings.{col_name} (already exists)")

    conn.commit()
    conn.close()
    print("Migration completed successfully!")


if __name__ == "__main__":
    migrate()
