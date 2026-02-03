"""
Ownership Verification Utilities

Provides helper functions to verify user ownership of various resources.
These functions help enforce multi-tenancy security across the application.
"""

from typing import List, Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.database import User, BotProfile, Conversation, Hub, HubBotMembership


def verify_bot_ownership(bot_id: int, user: User, db: Session) -> BotProfile:
    """
    Verify that the user owns the specified bot.

    Args:
        bot_id: The bot profile ID to verify
        user: The current authenticated user
        db: Database session

    Returns:
        BotProfile if ownership is verified

    Raises:
        HTTPException 404 if bot not found or not owned by user
    """
    bot = db.query(BotProfile).filter(
        BotProfile.id == bot_id,
        BotProfile.user_id == user.id
    ).first()

    if not bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bot not found"
        )

    return bot


def verify_conversation_ownership(conversation_id: int, user: User, db: Session) -> Conversation:
    """
    Verify that the user owns the specified conversation.
    A user owns a conversation if they own the bot that the conversation belongs to.

    Args:
        conversation_id: The conversation ID to verify
        user: The current authenticated user
        db: Database session

    Returns:
        Conversation if ownership is verified

    Raises:
        HTTPException 404 if conversation not found or not owned by user
    """
    conversation = db.query(Conversation).join(BotProfile).filter(
        Conversation.id == conversation_id,
        BotProfile.user_id == user.id
    ).first()

    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )

    return conversation


def verify_hub_ownership(hub_id: int, user: User, db: Session) -> Hub:
    """
    Verify that the user owns the specified hub.

    Args:
        hub_id: The hub ID to verify
        user: The current authenticated user
        db: Database session

    Returns:
        Hub if ownership is verified

    Raises:
        HTTPException 404 if hub not found or not owned by user
    """
    hub = db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == user.id
    ).first()

    if not hub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hub not found"
        )

    return hub


def get_user_hub_ids(user: User, db: Session) -> List[int]:
    """
    Get all hub IDs owned by the user.

    Args:
        user: The current authenticated user
        db: Database session

    Returns:
        List of hub IDs owned by the user
    """
    hubs = db.query(Hub.id).filter(Hub.user_id == user.id).all()
    return [h[0] for h in hubs]


def get_user_bot_ids(user: User, db: Session) -> List[int]:
    """
    Get all bot IDs owned by the user.

    Args:
        user: The current authenticated user
        db: Database session

    Returns:
        List of bot IDs owned by the user
    """
    bots = db.query(BotProfile.id).filter(BotProfile.user_id == user.id).all()
    return [b[0] for b in bots]


def user_owns_bot(bot_id: int, user: User, db: Session) -> bool:
    """
    Check if user owns a specific bot without raising exception.

    Args:
        bot_id: The bot profile ID to check
        user: The current authenticated user
        db: Database session

    Returns:
        True if user owns the bot, False otherwise
    """
    return db.query(BotProfile).filter(
        BotProfile.id == bot_id,
        BotProfile.user_id == user.id
    ).first() is not None


def user_owns_hub(hub_id: int, user: User, db: Session) -> bool:
    """
    Check if user owns a specific hub without raising exception.

    Args:
        hub_id: The hub ID to check
        user: The current authenticated user
        db: Database session

    Returns:
        True if user owns the hub, False otherwise
    """
    return db.query(Hub).filter(
        Hub.id == hub_id,
        Hub.user_id == user.id
    ).first() is not None


def user_owns_conversation(conversation_id: int, user: User, db: Session) -> bool:
    """
    Check if user owns a specific conversation without raising exception.

    Args:
        conversation_id: The conversation ID to check
        user: The current authenticated user
        db: Database session

    Returns:
        True if user owns the conversation, False otherwise
    """
    return db.query(Conversation).join(BotProfile).filter(
        Conversation.id == conversation_id,
        BotProfile.user_id == user.id
    ).first() is not None
