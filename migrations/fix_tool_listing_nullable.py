"""
Migration: Make custom_tool_listings.tool_id nullable.

The model defines tool_id as nullable=True, but the DB column was created
with NOT NULL. This fixes it so modification listings (listing_type='mod')
can be created without a tool_id.
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

    for path in paths_to_try:
        if not os.path.exists(path):
            continue

        print(f"Migrating database: {path}")
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        try:
            # Check if table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='custom_tool_listings'")
            if not cursor.fetchone():
                print("  Table custom_tool_listings does not exist, skipping")
                continue

            # SQLite doesn't support ALTER COLUMN, so we recreate the table
            # 1. Get current schema
            cursor.execute("PRAGMA table_info(custom_tool_listings)")
            columns = cursor.fetchall()
            col_names = [c[1] for c in columns]

            # Check if tool_id is already nullable
            tool_id_col = [c for c in columns if c[1] == 'tool_id']
            if tool_id_col and tool_id_col[0][3] == 0:  # notnull == 0 means nullable
                print("  tool_id is already nullable, skipping")
                continue

            print("  Recreating table to make tool_id nullable...")

            # 2. Rename old table
            cursor.execute("ALTER TABLE custom_tool_listings RENAME TO _custom_tool_listings_old")

            # 3. Create new table with tool_id nullable
            cursor.execute("""
                CREATE TABLE custom_tool_listings (
                    id INTEGER NOT NULL PRIMARY KEY,
                    author_id INTEGER NOT NULL,
                    tool_id INTEGER,
                    name VARCHAR(200) NOT NULL UNIQUE,
                    display_name VARCHAR(200),
                    description TEXT,
                    long_description TEXT,
                    category VARCHAR(50) DEFAULT 'automation',
                    version VARCHAR(20) DEFAULT '1.0.0',
                    icon VARCHAR(50) DEFAULT 'bi-gear',
                    gradient_start VARCHAR(7) DEFAULT '#6366f1',
                    gradient_end VARCHAR(7) DEFAULT '#8b5cf6',
                    install_count INTEGER DEFAULT 0,
                    listing_type VARCHAR(20) DEFAULT 'tool',
                    status VARCHAR(20) DEFAULT 'draft',
                    tool_md_content TEXT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(author_id) REFERENCES users (id) ON DELETE CASCADE,
                    FOREIGN KEY(tool_id) REFERENCES built_tools (id) ON DELETE CASCADE
                )
            """)

            # 4. Copy data
            cols = ', '.join(col_names)
            cursor.execute(f"INSERT INTO custom_tool_listings ({cols}) SELECT {cols} FROM _custom_tool_listings_old")

            # 5. Drop old table
            cursor.execute("DROP TABLE _custom_tool_listings_old")

            # 6. Recreate indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_custom_tool_listings_id ON custom_tool_listings (id)")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_custom_tool_listings_tool_id ON custom_tool_listings (tool_id)")

            conn.commit()
            print("  Done! tool_id is now nullable.")

        except Exception as e:
            conn.rollback()
            print(f"  Error: {e}")
        finally:
            conn.close()


if __name__ == "__main__":
    migrate()
