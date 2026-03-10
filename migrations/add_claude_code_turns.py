"""
Migration: Add claude_code_turns table for per-turn review feature.

Tracks git diffs for each turn so users can review, accept, or reject changes.
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

        # Check if table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='claude_code_turns'")
        if cursor.fetchone():
            print("  Table 'claude_code_turns' already exists, skipping")
            conn.close()
            continue

        cursor.execute("""
            CREATE TABLE claude_code_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES claude_code_sessions(id) ON DELETE CASCADE,
                turn_number INTEGER NOT NULL,
                pre_turn_hash VARCHAR(40),
                post_turn_hash VARCHAR(40),
                diff_summary TEXT,
                diff_text TEXT,
                files_changed INTEGER DEFAULT 0,
                review_status VARCHAR(20) DEFAULT 'pending',
                reviewed_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("CREATE INDEX ix_claude_code_turns_session_id ON claude_code_turns(session_id)")
        cursor.execute("CREATE INDEX ix_claude_code_turns_id ON claude_code_turns(id)")

        conn.commit()
        conn.close()
        print("  Created table 'claude_code_turns'")

    print("Migration complete.")
    return True


if __name__ == '__main__':
    migrate()
