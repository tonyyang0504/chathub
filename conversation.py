"""
Conversation History Manager for WhatsApp Bot.
Handles CRUD operations for chat messages using SQLite.
"""

from datetime import datetime
from typing import List, Dict, Optional
from database import Database


class ConversationManager:
    """Manages conversation history in SQLite database."""

    def __init__(self, database: Database):
        """
        Initialize conversation manager.

        Args:
            database: Database instance
        """
        self.db = database

    def ensure_chat_exists(self, chat_id: str, is_group: bool = False, chat_name: str = None):
        """
        Ensure a chat record exists in the database.

        Args:
            chat_id: Unique chat identifier
            is_group: Whether this is a group chat
            chat_name: Optional name for the chat
        """
        existing = self.db.fetchone(
            "SELECT id FROM chats WHERE chat_id = ?",
            (chat_id,)
        )

        if not existing:
            self.db.execute(
                """
                INSERT INTO chats (chat_id, is_group, chat_name)
                VALUES (?, ?, ?)
                """,
                (chat_id, is_group, chat_name)
            )
        else:
            # Update the updated_at timestamp
            self.db.execute(
                "UPDATE chats SET updated_at = ? WHERE chat_id = ?",
                (datetime.now().isoformat(), chat_id)
            )

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        sender_name: str = None,
        sender_id: str = None,
        is_group: bool = False
    ) -> int:
        """
        Add a message to conversation history.

        Args:
            chat_id: Unique chat identifier
            role: Message role ('user', 'assistant', 'system')
            content: Message content
            sender_name: Name of the sender (for group chats)
            sender_id: ID of the sender
            is_group: Whether this is a group chat

        Returns:
            ID of the inserted message
        """
        # Ensure chat exists
        self.ensure_chat_exists(chat_id, is_group)

        cursor = self.db.execute(
            """
            INSERT INTO messages (chat_id, role, content, sender_name, sender_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, role, content, sender_name, sender_id, datetime.now().isoformat())
        )

        return cursor.lastrowid

    def get_history(self, chat_id: str, limit: int = 20) -> List[Dict]:
        """
        Get conversation history for a chat.

        Args:
            chat_id: Unique chat identifier
            limit: Maximum number of messages to retrieve

        Returns:
            List of message dictionaries with role and content
        """
        rows = self.db.fetchall(
            """
            SELECT role, content, sender_name, timestamp
            FROM messages
            WHERE chat_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (chat_id, limit)
        )

        # Reverse to get chronological order and format for OpenAI
        messages = []
        for row in reversed(rows):
            msg = {
                "role": row["role"],
                "content": row["content"]
            }
            # For group chats, prepend sender name to content
            if row["sender_name"] and row["role"] == "user":
                msg["content"] = f"[{row['sender_name']}]: {row['content']}"
            messages.append(msg)

        return messages

    def get_full_history(self, chat_id: str) -> List[Dict]:
        """
        Get full conversation history for a chat.

        Args:
            chat_id: Unique chat identifier

        Returns:
            List of all messages with full details
        """
        rows = self.db.fetchall(
            """
            SELECT id, role, content, sender_name, sender_id, timestamp
            FROM messages
            WHERE chat_id = ?
            ORDER BY timestamp ASC
            """,
            (chat_id,)
        )

        return [dict(row) for row in rows]

    def clear(self, chat_id: str):
        """
        Clear all messages for a chat.

        Args:
            chat_id: Unique chat identifier
        """
        self.db.execute(
            "DELETE FROM messages WHERE chat_id = ?",
            (chat_id,)
        )

    def delete_chat(self, chat_id: str):
        """
        Delete a chat and all its messages.

        Args:
            chat_id: Unique chat identifier
        """
        self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))

    def get_all_chats(self) -> List[Dict]:
        """
        Get all chat records.

        Returns:
            List of chat dictionaries
        """
        rows = self.db.fetchall(
            """
            SELECT chat_id, is_group, chat_name, created_at, updated_at
            FROM chats
            ORDER BY updated_at DESC
            """
        )

        return [dict(row) for row in rows]

    def get_chat_info(self, chat_id: str) -> Optional[Dict]:
        """
        Get information about a specific chat.

        Args:
            chat_id: Unique chat identifier

        Returns:
            Chat info dictionary or None
        """
        row = self.db.fetchone(
            "SELECT * FROM chats WHERE chat_id = ?",
            (chat_id,)
        )

        return dict(row) if row else None

    def get_message_count(self, chat_id: str) -> int:
        """
        Get the number of messages in a chat.

        Args:
            chat_id: Unique chat identifier

        Returns:
            Message count
        """
        row = self.db.fetchone(
            "SELECT COUNT(*) as count FROM messages WHERE chat_id = ?",
            (chat_id,)
        )

        return row["count"] if row else 0
