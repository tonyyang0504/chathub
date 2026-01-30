"""
Database migration script to add missing columns.
Run this once to update the database schema.
"""

import sqlite3
import os

# Find the database file - check multiple possible locations
script_dir = os.path.dirname(os.path.abspath(__file__))
possible_paths = [
    os.path.join(script_dir, 'whatsapp_bot.db'),
    os.path.join(script_dir, 'data', 'app.db'),
    os.path.join(script_dir, 'data', 'bot.db'),
    './whatsapp_bot.db',
]

db_path = None
for path in possible_paths:
    if os.path.exists(path):
        db_path = path
        break

if not db_path:
    print(f"Database not found. Checked: {possible_paths}")
    exit(1)

print(f"Migrating database: {db_path}")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get existing columns
cursor.execute("PRAGMA table_info(bot_profiles)")
existing_columns = [row[1] for row in cursor.fetchall()]
print(f"Existing columns: {existing_columns}")

# Columns to add
columns_to_add = [
    ('headless', 'BOOLEAN DEFAULT 0'),
    ('proxy_enabled', 'BOOLEAN DEFAULT 0'),
    ('proxy_url', 'VARCHAR(500)'),
    ('proxy_username', 'VARCHAR(255)'),
    ('proxy_password', 'VARCHAR(255)')
]

for col_name, col_type in columns_to_add:
    if col_name not in existing_columns:
        try:
            cursor.execute(f'ALTER TABLE bot_profiles ADD COLUMN {col_name} {col_type}')
            print(f'Added column: {col_name}')
        except Exception as e:
            print(f'Error adding {col_name}: {e}')
    else:
        print(f'Column {col_name} already exists')

conn.commit()
conn.close()

print('Migration complete!')
