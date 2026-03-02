"""
Migration: Add auto-analysis fields to hubs table

This migration adds the following columns:
- auto_analysis_enabled: Whether auto-analysis is enabled for the hub
- auto_analysis_interval_hours: How often to check for new messages (hours)
- auto_analysis_min_new_messages: Minimum new messages to trigger re-analysis
- auto_analysis_last_run: Timestamp of last auto-analysis run
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
    cursor.execute("PRAGMA table_info(hubs)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    columns_to_add = [
        ("auto_analysis_enabled", "BOOLEAN DEFAULT 0"),
        ("auto_analysis_interval_hours", "INTEGER DEFAULT 24"),
        ("auto_analysis_min_new_messages", "INTEGER DEFAULT 5"),
        ("auto_analysis_last_run", "DATETIME DEFAULT NULL"),
    ]

    added_count = 0
    for col_name, col_def in columns_to_add:
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE hubs ADD COLUMN {col_name} {col_def}")
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
