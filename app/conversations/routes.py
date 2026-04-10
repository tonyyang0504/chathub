"""
Conversation Routes
"""

import asyncio
import base64
import io
import logging
import mimetypes
import os
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Set
from datetime import datetime
from urllib.parse import unquote
from fastapi import APIRouter, Depends, HTTPException, status, Query, WebSocket, WebSocketDisconnect, File, Form, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel, field_serializer
from openai import OpenAI

from app.database import get_db, User, BotProfile, Conversation, Message
from app.auth.utils import get_current_user, decrypt_string, get_websocket_user
from app.auth.ownership import verify_conversation_ownership, verify_bot_ownership
from app.bots.whatsapp_bot import _analyze_image_with_ai, _analyze_document_with_ai, _media_url_to_path
from app.tools.event_bus import tool_event_bus
from app.platforms.message_handler import emit_event_sync

logger = logging.getLogger(__name__)


def _get_openai_client_for_bot(bot_profile: BotProfile):
    """Get an OpenAI client instance for a bot profile."""
    try:
        if not bot_profile.api_key_encrypted:
            logger.warning(f"Bot {bot_profile.id} has no API key configured")
            return None
        # Decrypt the API key
        api_key = decrypt_string(bot_profile.api_key_encrypted)
        return OpenAI(api_key=api_key)
    except Exception as e:
        logger.warning(f"Failed to create OpenAI client for bot {bot_profile.id}: {e}")
        return None


def _analyze_sent_media(file_path: str, content_type: str, caption: str, bot_profile: BotProfile) -> str:
    """
    Analyze sent media (image or document) using AI.

    Args:
        file_path: Path to the file
        content_type: MIME type of the file
        caption: Optional caption from the user
        bot_profile: Bot profile with OpenAI API key

    Returns:
        Analysis text or None if analysis fails
    """
    try:
        openai_client = _get_openai_client_for_bot(bot_profile)
        if not openai_client:
            return None

        # Analyze based on content type
        if content_type and content_type.startswith('image/'):
            analysis = _analyze_image_with_ai(openai_client, file_path, content_type, caption)
            if analysis:
                logger.info(f"Generated image analysis for sent media: {analysis[:100]}...")
            return analysis
        elif content_type and (content_type.startswith('application/') or content_type.startswith('text/')):
            analysis = _analyze_document_with_ai(openai_client, file_path, content_type, caption)
            if analysis:
                logger.info(f"Generated document analysis for sent media: {analysis[:100]}...")
            return analysis
        else:
            logger.info(f"Unsupported content type for analysis: {content_type}")
            return None
    except Exception as e:
        logger.warning(f"Failed to analyze sent media: {e}")
        return None
router = APIRouter(tags=["Conversations"])

# Default profile picture for AI/Human Agent messages
AI_AGENT_PROFILE_PIC = "/static/images/ai-agent.svg"

def _get_bot_sender_info(db: Session, bot_profile_id: int):
    """Get sender_id and sender_profile_pic for bot messages (AI Agent or Human Agent)."""
    bot_profile = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
    
    sender_id = None
    sender_profile_pic = AI_AGENT_PROFILE_PIC  # Default fallback
    
    if bot_profile:
        # Use phone number only (without @c.us suffix) for sender_id
        if bot_profile.whatsapp_phone:
            sender_id = bot_profile.whatsapp_phone.replace('+', '').replace(' ', '').replace('-', '')
        
        # Use bot's WhatsApp profile pic if available
        if bot_profile.whatsapp_profile_pic:
            sender_profile_pic = bot_profile.whatsapp_profile_pic
    
    return sender_id, sender_profile_pic




# ============== WebSocket Manager for Real-time Updates ==============

class ConversationWebSocketManager:
    """Manages WebSocket connections for real-time conversation updates."""

    def __init__(self):
        # Map of conversation_id -> set of websocket connections
        self.active_connections: Dict[int, Set[WebSocket]] = {}
        # Map of bot_id -> set of websocket connections (for chat list updates)
        self.bot_connections: Dict[int, Set[WebSocket]] = {}
        # Store the main event loop for cross-thread calls
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

    def set_main_loop(self, loop: asyncio.AbstractEventLoop):
        """Store the main event loop for cross-thread async calls."""
        self._main_loop = loop
        logger.info("Main event loop stored for WebSocket manager")

    def get_main_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Get the stored main event loop."""
        return self._main_loop

    async def connect(self, websocket: WebSocket, conversation_id: int = None, bot_id: int = None):
        await websocket.accept()
        if conversation_id:
            if conversation_id not in self.active_connections:
                self.active_connections[conversation_id] = set()
            self.active_connections[conversation_id].add(websocket)
            logger.info(f"WebSocket connected for conversation {conversation_id}")
        if bot_id:
            if bot_id not in self.bot_connections:
                self.bot_connections[bot_id] = set()
            self.bot_connections[bot_id].add(websocket)
            logger.info(f"WebSocket connected for bot {bot_id} chat list")

    def disconnect(self, websocket: WebSocket, conversation_id: int = None, bot_id: int = None):
        if conversation_id and conversation_id in self.active_connections:
            self.active_connections[conversation_id].discard(websocket)
            if not self.active_connections[conversation_id]:
                del self.active_connections[conversation_id]
        if bot_id and bot_id in self.bot_connections:
            self.bot_connections[bot_id].discard(websocket)
            if not self.bot_connections[bot_id]:
                del self.bot_connections[bot_id]

    async def broadcast_message(self, conversation_id: int, message: dict):
        """Broadcast a new message to all clients watching this conversation."""
        if conversation_id in self.active_connections:
            disconnected = []
            for ws in self.active_connections[conversation_id]:
                try:
                    await ws.send_json({"type": "new_message", "message": message})
                except Exception as e:
                    logger.error(f"Error sending to websocket: {e}")
                    disconnected.append(ws)
            for ws in disconnected:
                self.active_connections[conversation_id].discard(ws)

    async def broadcast_chat_update(self, bot_id: int, conversation: dict):
        """Broadcast chat list update to all clients watching this bot."""
        if bot_id in self.bot_connections:
            disconnected = []
            for ws in self.bot_connections[bot_id]:
                try:
                    await ws.send_json({"type": "chat_update", "conversation": conversation})
                except Exception as e:
                    logger.error(f"Error sending chat update: {e}")
                    disconnected.append(ws)
            for ws in disconnected:
                self.bot_connections[bot_id].discard(ws)

    async def broadcast_typing(self, conversation_id: int, is_typing: bool, bot_name: str = "Bot"):
        """Broadcast typing indicator to all clients watching this conversation."""
        if conversation_id in self.active_connections:
            disconnected = []
            for ws in self.active_connections[conversation_id]:
                try:
                    await ws.send_json({
                        "type": "typing",
                        "is_typing": is_typing,
                        "bot_name": bot_name
                    })
                except Exception as e:
                    logger.error(f"Error sending typing indicator: {e}")
                    disconnected.append(ws)
            for ws in disconnected:
                self.active_connections[conversation_id].discard(ws)


# Global WebSocket manager instance
conversation_ws_manager = ConversationWebSocketManager()


# ============== Schemas ==============

class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    sender_name: Optional[str]
    sender_profile_pic: Optional[str] = None
    timestamp: datetime
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    file_size: Optional[int] = None
    file_pages: Optional[int] = None

    class Config:
        from_attributes = True

    @field_serializer('timestamp')
    def serialize_timestamp(self, value: datetime) -> str:
        """Serialize timestamp as ISO format with UTC suffix."""
        if value:
            return value.isoformat() + "Z"
        return None


class ConversationResponse(BaseModel):
    id: int
    chat_id: str
    chat_name: Optional[str]
    display_name: Optional[str] = None
    phone: Optional[str] = None
    is_group: bool
    profile_pic: Optional[str] = None
    message_count: int
    last_message_at: Optional[datetime]
    created_at: datetime
    last_message: Optional[str] = None
    human_takeover: bool = False
    dm_approved: bool = True
    model_override: Optional[str] = None

    class Config:
        from_attributes = True

    @field_serializer('last_message_at', 'created_at')
    def serialize_timestamp(self, value: datetime) -> str:
        """Serialize timestamp as ISO format with UTC suffix."""
        if value:
            return value.isoformat() + "Z"
        return None


class SendMessageRequest(BaseModel):
    message: str


class ConversationListResponse(BaseModel):
    conversations: List[ConversationResponse]
    total: int


class MessageListResponse(BaseModel):
    messages: List[MessageResponse]
    total: int
    conversation: ConversationResponse


# ============== Helper Functions ==============

def get_conversation(
    conversation_id: int,
    user: User,
    db: Session
) -> Conversation:
    """Get conversation and verify ownership."""
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


# ============== Routes ==============


@router.get("/bot/{bot_id}", response_model=ConversationListResponse)
async def list_conversations(
    bot_id: int,
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List conversations for a bot."""
    # Verify bot ownership
    bot = db.query(BotProfile).filter(
        BotProfile.id == bot_id,
        BotProfile.user_id == current_user.id
    ).first()

    if not bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bot not found"
        )

    # Build query
    query = db.query(Conversation).filter(Conversation.bot_profile_id == bot_id)

    if search:
        query = query.filter(Conversation.chat_name.ilike(f"%{search}%"))

    # Get total
    total = query.count()

    # Get conversations with last message
    conversations = query.order_by(
        desc(Conversation.last_message_at)
    ).offset(offset).limit(limit).all()

    # Add last message preview
    result = []
    for conv in conversations:
        last_msg = db.query(Message).filter(
            Message.conversation_id == conv.id
        ).order_by(desc(Message.timestamp)).first()

        conv_response = ConversationResponse(
            id=conv.id,
            chat_id=conv.chat_id,
            chat_name=conv.chat_name,
            display_name=conv.display_name,
            phone=conv.phone,
            is_group=conv.is_group,
            profile_pic=conv.profile_pic,
            message_count=conv.message_count or 0,
            last_message_at=conv.last_message_at,
            created_at=conv.created_at,
            last_message=last_msg.content[:100] if last_msg else None,
            human_takeover=conv.human_takeover or False,
            dm_approved=conv.dm_approved if hasattr(conv, 'dm_approved') and conv.dm_approved is not None else True,
            model_override=getattr(conv, 'model_override', None),
        )
        result.append(conv_response)

    return ConversationListResponse(
        conversations=result,
        total=total
    )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation_detail(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get conversation details."""
    conversation = get_conversation(conversation_id, current_user, db)

    last_msg = db.query(Message).filter(
        Message.conversation_id == conversation.id
    ).order_by(desc(Message.timestamp)).first()

    return ConversationResponse(
        id=conversation.id,
        chat_id=conversation.chat_id,
        chat_name=conversation.chat_name,
        display_name=conversation.display_name,
        phone=conversation.phone,
        is_group=conversation.is_group,
        profile_pic=conversation.profile_pic,
        message_count=conversation.message_count or 0,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        last_message=last_msg.content[:100] if last_msg else None,
        human_takeover=conversation.human_takeover or False
    )


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
async def get_messages(
    conversation_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get messages for a conversation with pagination for infinite scroll.

    - offset=0: Returns the most recent `limit` messages
    - offset=100: Returns the next older `limit` messages
    - Messages are always returned in chronological order (oldest first) for display
    """
    conversation = get_conversation(conversation_id, current_user, db)

    # Get total message count
    total = db.query(func.count(Message.id)).filter(
        Message.conversation_id == conversation_id
    ).scalar() or 0

    # For infinite scroll: offset means "skip N most recent messages"
    # We want to return messages in chronological order for display
    # Calculate how many to skip from the START (oldest messages)
    skip_from_start = max(0, total - limit - offset)
    actual_limit = min(limit, total - offset) if total > offset else 0

    # Get messages in chronological order
    # Use id as secondary sort to preserve insertion order when timestamps are identical
    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.timestamp, Message.id).offset(skip_from_start).limit(actual_limit).all()

    return MessageListResponse(
        messages=[MessageResponse.model_validate(m) for m in messages],
        total=total,
        conversation=ConversationResponse(
            id=conversation.id,
            chat_id=conversation.chat_id,
            chat_name=conversation.chat_name,
            display_name=conversation.display_name,
            phone=conversation.phone,
            is_group=conversation.is_group,
            profile_pic=conversation.profile_pic,
            message_count=conversation.message_count or 0,
            last_message_at=conversation.last_message_at,
            created_at=conversation.created_at
        )
    )


class GroupMemberResponse(BaseModel):
    """Response model for a group member."""
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    sender_phone: Optional[str] = None
    sender_profile_pic: Optional[str] = None
    message_count: int = 0


@router.get("/{conversation_id}/members")
async def get_conversation_members(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get unique members (senders) from a group conversation.
    Returns distinct senders with their profile info and message counts.
    """
    conversation = get_conversation(conversation_id, current_user, db)

    # Query distinct senders from messages
    # Group by sender_id (or sender_name as fallback) to get unique senders
    members_query = db.query(
        Message.sender_id,
        Message.sender_name,
        Message.sender_phone,
        Message.sender_profile_pic,
        func.count(Message.id).label('message_count')
    ).filter(
        Message.conversation_id == conversation_id,
        Message.role == 'user'  # Only incoming messages have sender info
    ).group_by(
        func.coalesce(Message.sender_id, Message.sender_name)
    ).order_by(
        desc(func.count(Message.id))  # Most active senders first
    ).all()

    members = []
    for m in members_query:
        # Skip if no sender info at all
        if not m.sender_id and not m.sender_name:
            continue
        members.append(GroupMemberResponse(
            sender_id=m.sender_id,
            sender_name=m.sender_name,
            sender_phone=m.sender_phone,
            sender_profile_pic=m.sender_profile_pic,
            message_count=m.message_count
        ))

    return {
        "members": [m.model_dump() for m in members],
        "total": len(members),
        "is_group": conversation.is_group
    }


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a conversation and all its messages."""
    conversation = get_conversation(conversation_id, current_user, db)

    db.delete(conversation)
    db.commit()
    emit_event_sync("conversation.deleted", db=db, conversation_id=conversation_id)

    return {"message": "Conversation deleted"}


@router.delete("/{conversation_id}/messages")
async def clear_messages(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Clear all messages in a conversation."""
    conversation = get_conversation(conversation_id, current_user, db)

    db.query(Message).filter(Message.conversation_id == conversation_id).delete()
    conversation.message_count = 0
    conversation.last_message_at = None
    db.commit()
    emit_event_sync("conversation.cleared", db=db, conversation_id=conversation_id)

    return {"message": "Messages cleared"}


@router.get("/{conversation_id}/export")
async def export_conversation(
    conversation_id: int,
    format: str = Query("json", pattern="^(json|txt)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export conversation messages."""
    conversation = get_conversation(conversation_id, current_user, db)

    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.timestamp).all()

    if format == "json":
        return {
            "conversation": {
                "id": conversation.id,
                "chat_name": conversation.chat_name,
                "is_group": conversation.is_group
            },
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "sender": m.sender_name,
                    "timestamp": m.timestamp.isoformat() + "Z"
                }
                for m in messages
            ]
        }
    else:
        # Plain text format
        lines = [f"Conversation: {conversation.chat_name}", "=" * 50, ""]
        for m in messages:
            sender = m.sender_name or m.role.capitalize()
            lines.append(f"[{m.timestamp.strftime('%Y-%m-%d %H:%M')}] {sender}:")
            lines.append(m.content)
            lines.append("")

        return {"content": "\n".join(lines)}


# ============== WebSocket Endpoints ==============

@router.websocket("/{conversation_id}/ws")
async def websocket_conversation(websocket: WebSocket, conversation_id: int):
    """WebSocket endpoint for real-time conversation updates."""
    # Authenticate the WebSocket connection
    db = next(get_db())
    try:
        user = await get_websocket_user(websocket, db)
        if not user:
            await websocket.close(code=4001, reason="Authentication required")
            return

        # Verify conversation ownership
        conversation = db.query(Conversation).join(BotProfile).filter(
            Conversation.id == conversation_id,
            BotProfile.user_id == user.id
        ).first()

        if not conversation:
            await websocket.close(code=4004, reason="Conversation not found")
            return

        await conversation_ws_manager.connect(websocket, conversation_id=conversation_id)
        try:
            while True:
                # Keep connection alive, handle pings
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            conversation_ws_manager.disconnect(websocket, conversation_id=conversation_id)
            logger.info(f"WebSocket disconnected for conversation {conversation_id}")
        except Exception as e:
            logger.error(f"WebSocket error for conversation {conversation_id}: {e}")
            conversation_ws_manager.disconnect(websocket, conversation_id=conversation_id)
    finally:
        db.close()


@router.websocket("/bot/{bot_id}/ws")
async def websocket_bot_chats(websocket: WebSocket, bot_id: int):
    """WebSocket endpoint for real-time chat list updates."""
    # Authenticate the WebSocket connection
    db = next(get_db())
    try:
        user = await get_websocket_user(websocket, db)
        if not user:
            await websocket.close(code=4001, reason="Authentication required")
            return

        # Verify bot ownership
        bot = db.query(BotProfile).filter(
            BotProfile.id == bot_id,
            BotProfile.user_id == user.id
        ).first()

        if not bot:
            await websocket.close(code=4004, reason="Bot not found")
            return

        await conversation_ws_manager.connect(websocket, bot_id=bot_id)
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            conversation_ws_manager.disconnect(websocket, bot_id=bot_id)
            logger.info(f"WebSocket disconnected for bot {bot_id} chat list")
        except Exception as e:
            logger.error(f"WebSocket error for bot {bot_id}: {e}")
            conversation_ws_manager.disconnect(websocket, bot_id=bot_id)
    finally:
        db.close()


# ============== AI Email Reply Generation ==============

@router.post("/{conversation_id}/generate-reply")
async def generate_email_reply(
    conversation_id: int,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate an AI email reply based on thread history and selected tone."""
    conversation = verify_conversation_ownership(conversation_id, current_user, db)

    bot_profile = db.query(BotProfile).filter(BotProfile.id == conversation.bot_profile_id).first()
    if not bot_profile:
        raise HTTPException(status_code=404, detail="Bot profile not found")

    if not bot_profile.api_key_encrypted:
        raise HTTPException(status_code=400, detail="No AI API key configured on this bot")

    tone = body.get("tone", "professional")
    valid_tones = ["professional", "friendly", "formal", "casual", "concise"]
    if tone not in valid_tones:
        tone = "professional"

    # Get conversation history
    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.timestamp.asc()).all()

    if not messages:
        raise HTTPException(status_code=400, detail="No messages in this conversation")

    # Build email thread context (strip HTML markers)
    thread_context = []
    for msg in messages:
        content = msg.content or ""
        if "<!--EMAIL_HTML-->" in content:
            content = content.split("<!--EMAIL_HTML-->")[0].strip()
        # Strip Subject: prefix
        content = content.replace("Subject:", "").strip() if content.startswith("Subject:") else content
        role_label = "Sender" if msg.role == "user" else "Our Reply"
        sender = msg.sender_name or ("Sender" if msg.role == "user" else "Bot")
        thread_context.append(f"[{role_label} - {sender}]:\n{content}")

    thread_text = "\n\n---\n\n".join(thread_context)

    tone_instructions = {
        "professional": "Write in a professional, business-appropriate tone. Be clear, polite, and direct.",
        "friendly": "Write in a warm, friendly tone. Be approachable and personable while staying helpful.",
        "formal": "Write in a formal, respectful tone. Use proper language and maintain a serious, courteous demeanor.",
        "casual": "Write in a casual, relaxed tone. Be conversational and natural, like chatting with a friend.",
        "concise": "Write a very brief, to-the-point reply. Keep it short — maximum 2-3 sentences.",
    }

    system_prompt = f"""You are an email reply assistant. Generate a reply to the latest email in the thread below.

Tone: {tone_instructions.get(tone, tone_instructions['professional'])}

Rules:
- Output format: first line is the subject (prefixed with "Subject: "), then a blank line, then the reply body
- For replies, the subject should be "Re: <original subject>" unless a new subject is more appropriate
- Be contextually relevant to the conversation thread
- Keep the reply focused and natural
- Do not include email headers or signatures beyond the subject line"""

    ai_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Email thread:\n\n{thread_text}\n\nGenerate a reply to the latest email:"}
    ]

    try:
        from app.ai.factory import get_ai_provider
        from app.auth.utils import decrypt_string

        api_key = decrypt_string(bot_profile.api_key_encrypted)
        provider = get_ai_provider(bot_profile.ai_provider, api_key, bot_profile.model)

        import asyncio
        response = await asyncio.to_thread(provider.chat_completion, ai_messages)
        reply_text = response.content if hasattr(response, 'content') else str(response)

        # Parse subject and body from AI output
        subject_line = ""
        body = reply_text
        if reply_text.startswith("Subject:"):
            lines = reply_text.split("\n", 1)
            subject_line = lines[0].replace("Subject:", "").strip()
            body = lines[1].strip() if len(lines) > 1 else ""

        return {"reply": body, "subject": subject_line}

    except Exception as e:
        logger.error(f"AI reply generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate reply: {str(e)}")


# ============== Manual Message Sending ==============

@router.post("/{conversation_id}/send")
async def send_manual_message(
    conversation_id: int,
    request: SendMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Send a manual message and mark as human takeover."""
    from app.platforms.send import send_message as platform_send_message

    # Verify conversation ownership
    conversation = verify_conversation_ownership(conversation_id, current_user, db)

    bot_profile = db.query(BotProfile).filter(BotProfile.id == conversation.bot_profile_id).first()
    if not bot_profile:
        raise HTTPException(status_code=404, detail="Bot profile not found")

    # Check if bot is running
    from app.bots.manager import bot_manager
    bot_instance = bot_manager.get_instance(bot_profile.id)
    if not bot_instance or not bot_instance.is_running:
        # For email and other non-realtime platforms, check DB flag as fallback
        if not bot_profile.is_running:
            raise HTTPException(status_code=400, detail="Bot is not running. Please start the bot first.")

    # Send the message via the correct platform adapter
    try:
        success = await platform_send_message(
            bot_profile_id=bot_profile.id,
            chat_id=conversation.chat_id,
            chat_name=conversation.chat_name,
            message=request.message,
            platform_type=bot_profile.platform_type or "whatsapp",
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to send message. Check server logs.")

    except Exception as e:
        logger.error(f"Error sending manual message: {e}")
        raise HTTPException(status_code=500, detail=f"Error sending message: {str(e)}")

    # Store the message in database
    # Get bot sender info for Human Agent (same as AI Agent since sent from same WhatsApp account)
    sender_id, sender_profile_pic = _get_bot_sender_info(db, conversation.bot_profile_id)
    
    new_msg = Message(
        conversation_id=conversation.id,
        role="assistant",  # Human replies appear as bot messages
        content=request.message,
        sender_name="Human Agent",
        sender_id=sender_id,
        sender_profile_pic=sender_profile_pic,
        timestamp=datetime.utcnow()
    )
    db.add(new_msg)

    # Mark conversation as human takeover
    conversation.human_takeover = True
    conversation.human_takeover_at = datetime.utcnow()
    conversation.message_count = (conversation.message_count or 0) + 1
    conversation.last_message_at = datetime.utcnow()

    db.commit()
    db.refresh(new_msg)
    emit_event_sync("message.sent", db=db, bot_profile_id=conversation.bot_profile_id, conversation_id=conversation_id, content=request.message)

    # Build message data for response
    message_data = {
        "id": new_msg.id,
        "role": new_msg.role,
        "content": new_msg.content,
        "sender_name": new_msg.sender_name,
        "timestamp": new_msg.timestamp.isoformat() + "Z"
    }

    # NOTE: Don't broadcast via WebSocket here - the frontend already shows the message
    # via optimistic update. Broadcasting would cause duplicate messages.

    return {"success": True, "message": message_data}


@router.post("/{conversation_id}/resume-ai")
async def resume_ai_bot(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Resume AI bot for this conversation (disable human takeover)."""
    # Verify conversation ownership
    conversation = verify_conversation_ownership(conversation_id, current_user, db)

    conversation.human_takeover = False
    conversation.human_takeover_at = None
    db.commit()
    emit_event_sync("conversation.ai_resumed", db=db, conversation_id=conversation_id)

    return {"success": True, "message": "AI bot resumed"}


@router.post("/{conversation_id}/approve-sender")
async def approve_sender(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Approve an unknown sender (DM pairing)."""
    conversation = verify_conversation_ownership(conversation_id, current_user, db)
    conversation.dm_approved = True
    db.commit()
    return {"success": True, "message": "Sender approved"}


@router.post("/{conversation_id}/reject-sender")
async def reject_sender(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Reject an unknown sender (DM pairing)."""
    conversation = verify_conversation_ownership(conversation_id, current_user, db)
    conversation.dm_approved = False
    db.commit()
    return {"success": True, "message": "Sender rejected"}


@router.post("/{conversation_id}/set-model")
async def set_conversation_model(
    conversation_id: int,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Set a model override for a specific conversation."""
    conversation = verify_conversation_ownership(conversation_id, current_user, db)
    model = body.get("model")  # None = clear override, use bot default
    conversation.model_override = model
    db.commit()
    return {"success": True, "model_override": model}


@router.post("/{conversation_id}/send-file")
async def send_file_message(
    conversation_id: int,
    file: UploadFile = File(...),
    caption: str = Form(default=""),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Send a file/image via WhatsApp."""
    logger.info(f"=== FILE UPLOAD START ===")
    logger.info(f"Conversation ID: {conversation_id}")
    logger.info(f"File object: {file}")
    logger.info(f"Filename: {file.filename}")
    logger.info(f"Content-Type header: {file.content_type}")
    logger.info(f"Caption: {caption}")

    # Validate we have a file
    if not file or not file.filename:
        logger.error("No file or filename provided")
        raise HTTPException(status_code=400, detail="No file provided")

    # Verify conversation ownership
    conversation = verify_conversation_ownership(conversation_id, current_user, db)

    bot_profile = db.query(BotProfile).filter(BotProfile.id == conversation.bot_profile_id).first()
    if not bot_profile:
        logger.error(f"Bot profile {conversation.bot_profile_id} not found")
        raise HTTPException(status_code=404, detail="Bot profile not found")

    # Check if bot is running
    from app.bots.manager import bot_manager
    bot_instance = bot_manager.get_instance(bot_profile.id)
    if not bot_instance or not bot_instance.is_running:
        logger.error(f"Bot {bot_profile.id} is not running")
        raise HTTPException(status_code=400, detail="Bot is not running. Please start the bot first.")
    if not bot_instance.whatsapp_connected:
        logger.error(f"WhatsApp not connected for bot {bot_profile.id}")
        raise HTTPException(status_code=400, detail="WhatsApp is not connected. Please scan QR code first.")

    # Get file extension from filename
    original_filename = file.filename or "file"
    file_ext = os.path.splitext(original_filename)[1].lower()

    # If no extension, try to determine from content type
    if not file_ext:
        ext_map = {
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'video/mp4': '.mp4',
            'video/3gpp': '.3gp',
            'video/quicktime': '.mov',
            'audio/mpeg': '.mp3',
            'audio/ogg': '.ogg',
            'audio/wav': '.wav',
            'application/pdf': '.pdf',
            'text/plain': '.txt',
            'text/csv': '.csv',
        }
        file_ext = ext_map.get(file.content_type, '.bin')

    logger.info(f"File extension: {file_ext}")

    # Determine content type
    content_type = file.content_type
    if not content_type or content_type == 'application/octet-stream':
        guessed_type, _ = mimetypes.guess_type(original_filename)
        content_type = guessed_type or 'application/octet-stream'

    logger.info(f"Determined content type: {content_type}")

    # Allowed file extensions
    allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.3gp', '.mov',
                         '.mp3', '.ogg', '.wav', '.pdf', '.doc', '.docx', '.xls', '.xlsx',
                         '.txt', '.csv', '.bin'}

    if file_ext and file_ext not in allowed_extensions:
        logger.error(f"File type {file_ext} not supported")
        raise HTTPException(status_code=400, detail=f"File type {file_ext} not supported")

    # Determine max file size based on type
    max_size = 100 * 1024 * 1024  # 100MB default
    if content_type and (content_type.startswith('image/') or content_type.startswith('video/')):
        max_size = 16 * 1024 * 1024  # 16MB for media

    # Read file content first
    try:
        content = await file.read()
        file_size = len(content)
        logger.info(f"Read file content: {file_size} bytes")

        if file_size == 0:
            logger.error("File is empty")
            raise HTTPException(status_code=400, detail="File is empty")

        if file_size > max_size:
            logger.error(f"File too large: {file_size} > {max_size}")
            raise HTTPException(status_code=400, detail=f"File too large. Max size: {max_size // (1024*1024)}MB")
    except HTTPException:
        raise
    except Exception as read_err:
        logger.error(f"Error reading file: {read_err}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Error reading file: {str(read_err)}")

    # Save file to bot's session folder using the same structure as received files
    saved_path = None
    file_url = None
    file_info = None
    try:
        # Import the save function from whatsapp_bot
        from app.bots.whatsapp_bot import _save_outgoing_media

        # Convert raw bytes to base64 data URL format
        base64_data = f"data:{content_type};base64,{base64.b64encode(content).decode()}"

        # Determine media type from content_type
        if content_type.startswith('image/') or content_type.startswith('video/'):
            media_type = 'image' if content_type.startswith('image/') else 'video'
        elif content_type.startswith('audio/'):
            media_type = 'audio'
        else:
            media_type = 'document'

        # Save using the same structure as received files
        file_info = _save_outgoing_media(
            base64_data=base64_data,
            media_type=media_type,
            bot_profile_id=bot_profile.id,
            chat_name=conversation.chat_name or 'Unknown',
            original_filename=original_filename
        )

        if file_info:
            file_url = file_info.get('file_url')
            # Get the actual file path for WhatsApp sending
            # PREFER local_file_path (always correct) over reconstructing from URL
            local_path = file_info.get('local_file_path')
            if local_path:
                saved_path = Path(local_path)
            else:
                # Fallback: reconstruct from URL using cross-platform helper
                saved_path = _media_url_to_path(file_url)
            logger.info(f"Saved outgoing file to {saved_path}, URL: {file_url}")
        else:
            logger.error("Failed to save outgoing file")
            raise HTTPException(status_code=500, detail="Failed to save file")

        # Send via the correct platform adapter
        from app.platforms.send import send_file as platform_send_file

        logger.info(f"Sending file: bot={bot_profile.id}, chat={conversation.chat_name} ({conversation.chat_id})")

        try:
            success = await platform_send_file(
                bot_profile_id=bot_profile.id,
                chat_id=conversation.chat_id,
                file_path=str(saved_path),
                caption=caption,
                file_type=content_type or '',
                chat_name=conversation.chat_name or '',
                platform_type=bot_profile.platform_type or "whatsapp",
            )
            logger.info(f"Platform send_file returned: {success}")
        except Exception as send_err:
            logger.error(f"Exception in platform send_file: {send_err}", exc_info=True)
            success = False

        if not success:
            # Clean up file on failure
            if saved_path and saved_path.exists():
                try:
                    saved_path.unlink()
                except:
                    pass
            raise HTTPException(status_code=500, detail="Failed to send file via WhatsApp. Check server logs.")

        # Extract PDF page count if applicable
        file_pages = None
        if content_type == 'application/pdf':
            try:
                try:
                    from pypdf import PdfReader
                except ImportError:
                    from PyPDF2 import PdfReader
                pdf_reader = PdfReader(io.BytesIO(content))
                file_pages = len(pdf_reader.pages)
                logger.info(f"PDF has {file_pages} pages")
            except Exception as pdf_err:
                logger.warning(f"Could not extract PDF page count: {pdf_err}")

        # Store message in database with file info
        message_content = caption if caption else ""

        # Get bot sender info for Human Agent
        sender_id, sender_profile_pic = _get_bot_sender_info(db, conversation.bot_profile_id)

        # Analyze the sent media (image or document)
        media_analysis = None
        if saved_path and saved_path.exists():
            logger.info(f"Analyzing sent media: {saved_path}")
            media_analysis = _analyze_sent_media(str(saved_path), content_type, caption, bot_profile)
            if media_analysis:
                logger.info(f"Media analysis completed for sent file")

        new_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=message_content,
            sender_name="Human Agent",
            sender_id=sender_id,
            sender_profile_pic=sender_profile_pic,
            timestamp=datetime.utcnow(),
            file_url=file_url,
            file_name=original_filename,
            file_type=content_type,
            file_size=file_size,
            file_pages=file_pages,
            media_analysis=media_analysis
        )
        db.add(new_msg)

        # Mark conversation as human takeover
        conversation.human_takeover = True
        conversation.human_takeover_at = datetime.utcnow()
        conversation.message_count = (conversation.message_count or 0) + 1
        conversation.last_message_at = datetime.utcnow()

        db.commit()
        db.refresh(new_msg)

        logger.info(f"=== FILE UPLOAD SUCCESS ===")

        return {
            "success": True,
            "message": {
                "id": new_msg.id,
                "role": new_msg.role,
                "content": new_msg.content,
                "sender_name": new_msg.sender_name,
                "timestamp": new_msg.timestamp.isoformat() + "Z",
                "file_url": new_msg.file_url,
                "file_name": new_msg.file_name,
                "file_type": new_msg.file_type,
                "file_size": new_msg.file_size,
                "file_pages": new_msg.file_pages,
                "media_analysis": new_msg.media_analysis
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"=== FILE UPLOAD ERROR ===")
        logger.error(f"Error sending file: {e}", exc_info=True)
        # Clean up file on error
        if saved_path and saved_path.exists():
            try:
                saved_path.unlink()
            except:
                pass
        raise HTTPException(status_code=500, detail=f"Error sending file: {str(e)}")


class ForwardFileRequest(BaseModel):
    """Request model for forwarding a file."""
    file_url: str
    file_name: str
    file_type: str
    caption: str = ""


@router.post("/{conversation_id}/forward-file")
async def forward_file_message(
    conversation_id: int,
    request: ForwardFileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Forward a file/image to another conversation."""
    logger.info(f"=== FORWARD FILE START ===")
    logger.info(f"Target conversation ID: {conversation_id}")
    logger.info(f"File URL: {request.file_url}")
    logger.info(f"File name: {request.file_name}")
    logger.info(f"File type: {request.file_type}")

    # Verify conversation ownership
    conversation = verify_conversation_ownership(conversation_id, current_user, db)

    bot_profile = db.query(BotProfile).filter(BotProfile.id == conversation.bot_profile_id).first()
    if not bot_profile:
        raise HTTPException(status_code=404, detail="Bot profile not found")

    # Check if bot is running
    from app.bots.manager import bot_manager
    bot_instance = bot_manager.get_instance(bot_profile.id)
    if not bot_instance or not bot_instance.is_running:
        raise HTTPException(status_code=400, detail="Bot is not running. Please start the bot first.")
    if not bot_instance.whatsapp_connected:
        raise HTTPException(status_code=400, detail="WhatsApp is not connected. Please scan QR code first.")

    # Get the file path from the URL
    # file_url format: /media/bot_X/conversations/... -> data/sessions/bot_X/conversations/...
    base_dir = Path(__file__).resolve().parent.parent.parent
    # Convert media URL to actual file path and URL-decode
    url_path = request.file_url.lstrip('/')
    if url_path.startswith('media/'):
        relative_path = unquote(url_path.replace('media/', 'data/sessions/', 1))
    else:
        relative_path = unquote(url_path)
    file_path = base_dir / relative_path

    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        raise HTTPException(status_code=404, detail="Source file not found")

    logger.info(f"Source file path: {file_path}")

    # Send the file via the correct platform adapter
    from app.platforms.send import send_file as platform_send_file

    try:
        success = await platform_send_file(
            bot_profile_id=bot_profile.id,
            chat_id=conversation.chat_id,
            file_path=str(file_path),
            caption=request.caption,
            file_type=request.file_type,
            chat_name=conversation.chat_name or '',
            platform_type=bot_profile.platform_type or "whatsapp",
        )
        logger.info(f"Platform send_file returned: {success}")
    except Exception as send_err:
        logger.error(f"Exception in platform send_file: {send_err}", exc_info=True)
        success = False

    if not success:
        raise HTTPException(status_code=500, detail="Failed to forward file.")

    # Get file size
    file_size = file_path.stat().st_size

    # Extract PDF page count if applicable
    file_pages = None
    if request.file_type == 'application/pdf':
        try:
            from pypdf import PdfReader
            pdf_reader = PdfReader(str(file_path))
            file_pages = len(pdf_reader.pages)
        except Exception as pdf_err:
            logger.warning(f"Could not extract PDF page count: {pdf_err}")

    # Store forwarded message in database
    # Get bot sender info for Human Agent
    sender_id, sender_profile_pic = _get_bot_sender_info(db, conversation.bot_profile_id)

    # Analyze the forwarded media (image or document)
    media_analysis = None
    if file_path.exists():
        logger.info(f"Analyzing forwarded media: {file_path}")
        media_analysis = _analyze_sent_media(str(file_path), request.file_type, request.caption, bot_profile)
        if media_analysis:
            logger.info(f"Media analysis completed for forwarded file")

    new_msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=request.caption,
        sender_name="Human Agent",
        sender_id=sender_id,
        sender_profile_pic=sender_profile_pic,
        timestamp=datetime.utcnow(),
        file_url=request.file_url,
        file_name=request.file_name,
        file_type=request.file_type,
        file_size=file_size,
        file_pages=file_pages,
        media_analysis=media_analysis
    )
    db.add(new_msg)

    # Mark conversation as human takeover
    conversation.human_takeover = True
    conversation.human_takeover_at = datetime.utcnow()
    conversation.message_count = (conversation.message_count or 0) + 1
    conversation.last_message_at = datetime.utcnow()

    db.commit()
    db.refresh(new_msg)

    logger.info(f"=== FORWARD FILE SUCCESS ===")

    return {
        "success": True,
        "message": {
            "id": new_msg.id,
            "role": new_msg.role,
            "content": new_msg.content,
            "sender_name": new_msg.sender_name,
            "timestamp": new_msg.timestamp.isoformat() + "Z",
            "file_url": new_msg.file_url,
            "file_name": new_msg.file_name,
            "file_type": new_msg.file_type,
            "file_size": new_msg.file_size,
            "file_pages": new_msg.file_pages,
            "media_analysis": new_msg.media_analysis
        }
    }
