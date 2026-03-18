"""
Migration: Add widgets and is_deleted columns to built_tools table.
- widgets: JSON list of widget declarations for dashboard/page extension points
- is_deleted: Soft-delete flag to preserve tool_md_content for potential reinstall
"""
import os
import sqlite3


def get_db_path():
    appdata = os.environ.get("APPDATA")
    if appdata and os.path.exists(os.path.join(appdata, "ChatHub", "app.db")):
        return os.path.join(appdata, "ChatHub", "app.db")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")


def migrate():
    db_path = get_db_path()
    print(f"Database: {db_path}")

    if not os.path.exists(db_path):
        print("Database not found, skipping migration")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check existing columns
    cursor.execute("PRAGMA table_info(built_tools)")
    columns = [row[1] for row in cursor.fetchall()]

    if "widgets" not in columns:
        cursor.execute("ALTER TABLE built_tools ADD COLUMN widgets TEXT")
        conn.commit()
        print("Added widgets column to built_tools")
    else:
        print("widgets column already exists, skipping")

    if "is_deleted" not in columns:
        cursor.execute("ALTER TABLE built_tools ADD COLUMN is_deleted BOOLEAN DEFAULT 0")
        conn.commit()
        print("Added is_deleted column to built_tools")
    else:
        print("is_deleted column already exists, skipping")

    conn.close()
    print("Migration complete")


if __name__ == "__main__":
    migrate()
