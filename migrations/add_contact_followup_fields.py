"""
Migration: Add follow-up tracking fields to contacts table

This migration adds the following columns:
- followup_status: Status of follow-up (pending, sent, responded, dismissed)
- followup_sent_at: Timestamp when follow-up was sent
- followup_message: Last sent follow-up message text
- followup_attempts: Number of follow-up attempts made
- followup_last_attempt_at: Timestamp of last follow-up attempt
"""

import sqlite3
import os

def migrate():
    # Get database path
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'app.db')

    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check which columns already exist
    cursor.execute("PRAGMA table_info(contacts)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    columns_to_add = [
        ("followup_status", "VARCHAR(20) DEFAULT NULL"),
        ("followup_sent_at", "DATETIME DEFAULT NULL"),
        ("followup_message", "TEXT DEFAULT NULL"),
        ("followup_attempts", "INTEGER DEFAULT 0"),
        ("followup_last_attempt_at", "DATETIME DEFAULT NULL"),
    ]

    added_count = 0
    for col_name, col_def in columns_to_add:
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE contacts ADD COLUMN {col_name} {col_def}")
                print(f"Added column: {col_name}")
                added_count += 1
            except sqlite3.OperationalError as e:
                print(f"Error adding column {col_name}: {e}")
        else:
            print(f"Column already exists: {col_name}")

    conn.commit()
    conn.close()

    if added_count > 0:
        print(f"\nMigration complete: {added_count} columns added")
    else:
        print("\nNo changes needed - all columns already exist")

    return True

if __name__ == "__main__":
    migrate()
