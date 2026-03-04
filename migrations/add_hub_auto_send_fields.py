"""
Migration: Add auto-send follow-up fields to hubs table

This migration adds the following columns:
- auto_send_enabled: Whether auto-send is enabled for the hub
- auto_send_interval_minutes: How often to check for pending contacts (minutes)
- auto_send_tone: Message tone (friendly, professional, casual)
- auto_send_speed_mode: Sending speed mode (auto, custom, fast)
- auto_send_delay_min: Min seconds between sends
- auto_send_delay_max: Max seconds between sends
- auto_send_batch_size: Messages per batch before pause
- auto_send_batch_pause: Seconds to pause between batches
- auto_send_filters: JSON filter conditions
- auto_send_last_run: Timestamp of last auto-send run
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
        ("auto_send_enabled", "BOOLEAN DEFAULT 0"),
        ("auto_send_interval_minutes", "INTEGER DEFAULT 15"),
        ("auto_send_tone", "VARCHAR(20) DEFAULT 'friendly'"),
        ("auto_send_speed_mode", "VARCHAR(20) DEFAULT 'auto'"),
        ("auto_send_delay_min", "INTEGER DEFAULT 5"),
        ("auto_send_delay_max", "INTEGER DEFAULT 15"),
        ("auto_send_batch_size", "INTEGER DEFAULT 20"),
        ("auto_send_batch_pause", "INTEGER DEFAULT 180"),
        ("auto_send_bot_ids", "TEXT DEFAULT NULL"),
        ("auto_send_filters", "TEXT DEFAULT NULL"),
        ("auto_send_last_run", "DATETIME DEFAULT NULL"),
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

    # Drop old delay_seconds column data by ignoring it (SQLite can't drop columns easily)
    # The old column will remain but is no longer used by the application
    if "auto_send_delay_seconds" in existing_columns:
        print("Note: auto_send_delay_seconds is deprecated and replaced by speed_mode/delay_min/delay_max/batch_size/batch_pause")

    conn.commit()
    conn.close()

    if added_count > 0:
        print(f"\nMigration complete: {added_count} columns added")
    else:
        print("\nNo changes needed - all columns already exist")

    return True

if __name__ == "__main__":
    migrate()
