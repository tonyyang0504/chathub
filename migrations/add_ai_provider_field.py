"""
Migration: Add ai_provider field to bot_profiles, hubs, and ai_agents tables.

This migration adds support for multiple AI providers (OpenAI, Anthropic, Google, etc.)
by adding an ai_provider column to relevant tables.

Run this migration after updating the codebase:
    python migrations/add_ai_provider_field.py
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text, inspect
from app.database import engine


def run_migration():
    """Add ai_provider column to relevant tables."""
    with engine.connect() as conn:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        changes_made = []

        # Add ai_provider to bot_profiles
        if 'bot_profiles' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('bot_profiles')]

            if 'ai_provider' not in existing_columns:
                try:
                    conn.execute(text(
                        "ALTER TABLE bot_profiles ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'openai'"
                    ))
                    conn.commit()
                    changes_made.append("Added ai_provider to bot_profiles")
                    print("Added ai_provider column to bot_profiles table")
                except Exception as e:
                    print(f"Could not add ai_provider to bot_profiles: {e}")

        # Add ai_provider to hubs
        if 'hubs' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('hubs')]

            if 'ai_provider' not in existing_columns:
                try:
                    conn.execute(text(
                        "ALTER TABLE hubs ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'openai'"
                    ))
                    conn.commit()
                    changes_made.append("Added ai_provider to hubs")
                    print("Added ai_provider column to hubs table")
                except Exception as e:
                    print(f"Could not add ai_provider to hubs: {e}")

        # Add ai_provider to ai_agents
        if 'ai_agents' in existing_tables:
            existing_columns = [col['name'] for col in inspector.get_columns('ai_agents')]

            if 'ai_provider' not in existing_columns:
                try:
                    conn.execute(text(
                        "ALTER TABLE ai_agents ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'openai'"
                    ))
                    conn.commit()
                    changes_made.append("Added ai_provider to ai_agents")
                    print("Added ai_provider column to ai_agents table")
                except Exception as e:
                    print(f"Could not add ai_provider to ai_agents: {e}")

        if changes_made:
            print(f"\nMigration complete. Changes made:")
            for change in changes_made:
                print(f"  - {change}")
        else:
            print("No changes needed. All columns already exist.")


if __name__ == "__main__":
    print("Running AI Provider Migration...")
    print("=" * 50)
    run_migration()
    print("=" * 50)
    print("Done.")
