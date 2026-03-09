"""
Migration: Create Claude Code tables

Creates the following tables:
- claude_code_sessions: Tracks CLI subprocess sessions
- claude_code_messages: Individual stream events from CLI
- claude_code_settings: Per-user configuration
"""

import sqlite3
import os


def get_db_path():
    """Get the database path, checking AppData on Windows first."""
    # Check AppData (Windows server deployment)
    if os.name == 'nt':
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            appdata_db = os.path.join(appdata, 'ChatHub', 'app.db')
            if os.path.exists(appdata_db):
                return appdata_db

    # Default path
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'app.db')
    return db_path


def migrate():
    db_path = get_db_path()

    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return False

    print(f"Using database: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get existing tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in cursor.fetchall()}

    # Create claude_code_sessions
    if 'claude_code_sessions' not in existing_tables:
        cursor.execute('''
            CREATE TABLE claude_code_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status VARCHAR(20) DEFAULT 'pending',
                prompt TEXT NOT NULL,
                git_commit_hash VARCHAR(40),
                db_backup_path VARCHAR(500),
                pid INTEGER,
                model VARCHAR(100),
                rolled_back BOOLEAN DEFAULT 0,
                rolled_back_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                started_at DATETIME,
                ended_at DATETIME
            )
        ''')
        cursor.execute('CREATE INDEX ix_claude_code_sessions_user_id ON claude_code_sessions(user_id)')
        cursor.execute('CREATE INDEX ix_claude_code_sessions_status ON claude_code_sessions(status)')
        print("Created claude_code_sessions table")
    else:
        print("claude_code_sessions table already exists")

    # Create claude_code_messages
    if 'claude_code_messages' not in existing_tables:
        cursor.execute('''
            CREATE TABLE claude_code_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES claude_code_sessions(id) ON DELETE CASCADE,
                role VARCHAR(20) NOT NULL,
                content TEXT,
                message_type VARCHAR(50),
                event_data TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX ix_claude_code_messages_session_id ON claude_code_messages(session_id)')
        print("Created claude_code_messages table")
    else:
        print("claude_code_messages table already exists")

    # Create claude_code_settings
    if 'claude_code_settings' not in existing_tables:
        cursor.execute('''
            CREATE TABLE claude_code_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                anthropic_api_key_encrypted TEXT,
                default_model VARCHAR(100) DEFAULT 'sonnet',
                auto_commit BOOLEAN DEFAULT 1,
                auto_backup_db BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("Created claude_code_settings table")
    else:
        print("claude_code_settings table already exists")

    conn.commit()
    conn.close()
    print("Migration completed successfully")
    return True


if __name__ == '__main__':
    migrate()
