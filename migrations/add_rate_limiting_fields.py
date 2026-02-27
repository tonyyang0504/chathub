"""
Migration: Add rate limiting fields to scheduled_contents table

This migration adds the following columns:
- sending_speed_mode: 'auto', 'custom', 'fast' (default: 'auto')
- delay_min: Minimum seconds between messages (default: 5)
- delay_max: Maximum seconds between messages (default: 15)
- batch_size: Messages per batch before pause (default: 20)
- batch_pause: Seconds to pause between batches (default: 180)
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
    cursor.execute("PRAGMA table_info(scheduled_contents)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    columns_to_add = [
        ("sending_speed_mode", "VARCHAR(20) DEFAULT 'auto'"),
        ("delay_min", "INTEGER DEFAULT 5"),
        ("delay_max", "INTEGER DEFAULT 15"),
        ("batch_size", "INTEGER DEFAULT 20"),
        ("batch_pause", "INTEGER DEFAULT 180"),
    ]

    added_count = 0
    for col_name, col_def in columns_to_add:
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE scheduled_contents ADD COLUMN {col_name} {col_def}")
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
