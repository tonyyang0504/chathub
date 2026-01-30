"""
SQLite Database Management for WhatsApp Bot.
Handles database connection and schema creation.
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional
import threading


class Database:
    """SQLite database manager with thread-safe connections."""

    _local = threading.local()

    def __init__(self, db_path: str = "data/conversations.db"):
        """
        Initialize database manager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._ensure_directory()
        self.create_tables()

    def _ensure_directory(self):
        """Ensure the directory for the database file exists."""
        directory = os.path.dirname(self.db_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

    @property
    def connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    @property
    def cursor(self) -> sqlite3.Cursor:
        """Get cursor from current connection."""
        return self.connection.cursor()

    def create_tables(self):
        """Create database schema if not exists."""
        cursor = self.cursor

        # Chats table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                is_group BOOLEAN DEFAULT FALSE,
                chat_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                sender_name TEXT,
                sender_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
            )
        """)

        # Index for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_chat_id
            ON messages(chat_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
            ON messages(timestamp)
        """)

        self.connection.commit()

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """
        Execute a query with parameters.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            Cursor with results
        """
        cursor = self.cursor
        cursor.execute(query, params)
        self.connection.commit()
        return cursor

    def executemany(self, query: str, params_list: list) -> sqlite3.Cursor:
        """
        Execute a query with multiple parameter sets.

        Args:
            query: SQL query string
            params_list: List of parameter tuples

        Returns:
            Cursor with results
        """
        cursor = self.cursor
        cursor.executemany(query, params_list)
        self.connection.commit()
        return cursor

    def fetchone(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """
        Execute query and fetch one result.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            Single row or None
        """
        cursor = self.cursor
        cursor.execute(query, params)
        return cursor.fetchone()

    def fetchall(self, query: str, params: tuple = ()) -> list:
        """
        Execute query and fetch all results.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            List of rows
        """
        cursor = self.cursor
        cursor.execute(query, params)
        return cursor.fetchall()

    def close(self):
        """Close the database connection."""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None
