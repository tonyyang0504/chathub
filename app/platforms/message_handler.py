"""
Shared Message Processing Logic
Platform-agnostic utilities for message handling that any adapter can use.

These functions are extracted patterns from whatsapp_bot.py. The WhatsApp adapter
continues to use the original implementations in whatsapp_bot.py directly.
New platform adapters should use these shared utilities instead of duplicating logic.

Covers:
- Conversation creation/update in DB
- Message storage (incoming & outgoing)
- Message deduplication
- AI response generation (conversation history + provider call)
- Media analysis (image vision + document extraction)
- WebSocket real-time notifications
- Human takeover detection
"""

import asyncio
import base64
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import quote

from app.tools.event_bus import tool_event_bus

logger = logging.getLogger(__name__)


def emit_event_sync(event_name: str, **kwargs):
    """Emit event from sync code (schedules on the event loop)."""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(tool_event_bus.emit(event_name, **kwargs))
        else:
            loop.run_until_complete(tool_event_bus.emit(event_name, **kwargs))
    except Exception:
        pass


async def emit_message_received(db, bot_profile_id, conversation, message):
    """Emit message.received event to tool event bus."""
    try:
        await tool_event_bus.emit("message.received", db=db, bot_profile_id=bot_profile_id, conversation=conversation, message=message)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_session():
    """Get a database session context manager.

    Usage:
        with get_db_session() as db:
            db.query(...)
    """
    from app.database import SessionLocal

    class _DBContext:
        def __enter__(self):
            self.db = SessionLocal()
            return self.db

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type:
                self.db.rollback()
            self.db.close()
            return False

    return _DBContext()


def find_or_create_conversation(
    db,
    bot_profile_id: int,
    chat_id: str,
    chat_name: str,
    *,
    is_group: bool = False,
    phone: str = None,
    display_name: str = None,
    profile_pic: str = None,
) -> "Conversation":
    """Find an existing conversation or create a new one.

    Looks up by (bot_profile_id, chat_id). Creates if not found.

    Args:
        db: SQLAlchemy session
        bot_profile_id: Bot database ID
        chat_id: Platform-specific chat identifier
        chat_name: Human-readable name for this chat
        is_group: Whether this is a group chat
        phone: Contact phone number (if applicable)
        display_name: Saved display name
        profile_pic: URL or base64 of profile picture

    Returns:
        Conversation model instance (already in session)
    """
    from app.database import Conversation

    conversation = db.query(Conversation).filter(
        Conversation.bot_profile_id == bot_profile_id,
        Conversation.chat_id == chat_id,
    ).first()

    if conversation:
        # Update mutable fields if provided
        if chat_name and conversation.chat_name != chat_name:
            conversation.chat_name = chat_name
        if display_name and conversation.display_name != display_name:
            conversation.display_name = display_name
        if profile_pic and conversation.profile_pic != profile_pic:
            conversation.profile_pic = profile_pic
        if phone and conversation.phone != phone:
            conversation.phone = phone
        db.flush()
        return conversation

    # Check if DM pairing is enabled for this bot (new private DMs start unapproved)
    dm_approved = True  # Default: auto-approve
    if not is_group:
        from app.database import BotProfile
        bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
        if bot and bot.dm_pairing_enabled:
            dm_approved = False

    # Create new conversation
    conversation = Conversation(
        bot_profile_id=bot_profile_id,
        chat_id=chat_id,
        chat_name=chat_name,
        display_name=display_name or chat_name,
        phone=phone or "",
        is_group=is_group,
        profile_pic=profile_pic or "",
        message_count=0,
        last_message_at=datetime.utcnow(),
        dm_approved=dm_approved,
    )
    db.add(conversation)
    db.flush()
    emit_event_sync("conversation.created", db=db, bot_profile_id=bot_profile_id, conversation=conversation)
    logger.info(
        f"Bot {bot_profile_id}: Created conversation '{chat_name}' "
        f"(chat_id={chat_id}, group={is_group}, dm_approved={dm_approved})"
    )
    return conversation


def save_user_message(
    db,
    conversation_id: int,
    content: str,
    *,
    sender_name: str = "",
    sender_id: str = "",
    sender_phone: str = "",
    sender_profile_pic: str = "",
    platform_message_id: str = None,
    timestamp: datetime = None,
    file_url: str = None,
    file_type: str = None,
    file_name: str = None,
    file_size: int = None,
    file_pages: int = None,
    media_analysis: str = None,
) -> "Message":
    """Save an incoming user message to the database.

    Args:
        db: SQLAlchemy session
        conversation_id: FK to conversations table
        content: Message text
        sender_name: Display name of sender
        sender_id: Platform-specific sender identifier
        sender_phone: Phone number if available
        sender_profile_pic: Sender's profile picture
        platform_message_id: Platform's unique message ID (for dedup)
        timestamp: Message timestamp (defaults to now)
        file_url: URL/path to attached media
        file_type: MIME type of attachment
        file_name: Original filename
        file_size: File size in bytes
        file_pages: Page count (for PDFs)
        media_analysis: AI-generated description of media

    Returns:
        Message model instance
    """
    from app.database import Message

    # Dedup: skip if message with same platform_message_id already exists
    if platform_message_id:
        existing = db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.whatsapp_message_id == platform_message_id,
        ).first()
        if existing:
            return existing

    # For voice messages: use transcription as display content
    if media_analysis and file_type and file_type.startswith("audio/"):
        content = f"🎤 {media_analysis}"

    msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=content,
        sender_name=sender_name,
        sender_id=sender_id,
        sender_phone=sender_phone,
        sender_profile_pic=sender_profile_pic,
        whatsapp_message_id=platform_message_id,
        timestamp=timestamp or datetime.utcnow(),
        file_url=file_url,
        file_type=file_type,
        file_name=file_name,
        file_size=file_size,
        file_pages=file_pages,
        media_analysis=media_analysis,
    )
    db.add(msg)
    db.flush()
    emit_event_sync("message.received", db=db, conversation_id=conversation_id, message=msg)
    return msg


def save_assistant_message(
    db,
    conversation_id: int,
    content: str,
    *,
    sender_name: str = "AI Agent",
    sender_id: str = "",
    sender_profile_pic: str = "/static/images/ai-agent.svg",
    timestamp: datetime = None,
) -> "Message":
    """Save an outgoing AI/assistant message to the database.

    Args:
        db: SQLAlchemy session
        conversation_id: FK to conversations table
        content: Response text
        sender_name: Bot display name
        sender_id: Bot's platform identifier
        sender_profile_pic: Bot's profile picture

    Returns:
        Message model instance
    """
    from app.database import Message

    msg = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        sender_name=sender_name,
        sender_id=sender_id,
        sender_profile_pic=sender_profile_pic,
        timestamp=timestamp or datetime.utcnow(),
    )
    db.add(msg)
    db.flush()
    return msg


def update_conversation_stats(
    db,
    conversation_id: int,
    increment_count: int = 1,
):
    """Increment message count and update last_message_at."""
    from app.database import Conversation

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv:
        conv.message_count = (conv.message_count or 0) + increment_count
        conv.last_message_at = datetime.utcnow()
        db.flush()


# ---------------------------------------------------------------------------
# Message deduplication
# ---------------------------------------------------------------------------

def is_duplicate_message(
    db,
    conversation_id: int,
    *,
    platform_message_id: str = None,
    content: str = None,
    window_minutes: int = 5,
) -> Tuple[bool, Optional["Message"]]:
    """Check if a message is a duplicate.

    Two-tier dedup:
    1. Platform message ID (exact match)
    2. Content + time window fallback

    Args:
        db: SQLAlchemy session
        conversation_id: Conversation to check within
        platform_message_id: Platform's unique message ID
        content: Message text (fallback dedup)
        window_minutes: Time window for content-based dedup

    Returns:
        Tuple of (is_duplicate, existing_message_or_None)
    """
    from app.database import Message

    # Primary: platform message ID
    if platform_message_id:
        existing = db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.whatsapp_message_id == platform_message_id,
        ).first()
        if existing:
            return True, existing

    # Fallback: content + time window
    if content:
        cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
        existing = db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.role == "user",
            Message.content == content,
            Message.timestamp > cutoff,
        ).order_by(Message.timestamp.desc()).first()
        if existing:
            return True, existing

    return False, None


def has_response_after(
    db,
    conversation_id: int,
    after_message_id: int,
) -> bool:
    """Check if there's an assistant response after a given message.

    Used with dedup to decide whether to skip AI generation entirely
    (response exists) or just skip storing the message (no response yet).

    Args:
        db: SQLAlchemy session
        conversation_id: Conversation to check
        after_message_id: Message ID to check after

    Returns:
        True if an assistant message exists after the given message
    """
    from app.database import Message

    return db.query(Message).filter(
        Message.conversation_id == conversation_id,
        Message.role == "assistant",
        Message.id > after_message_id,
    ).first() is not None


# ---------------------------------------------------------------------------
# AI response helpers
# ---------------------------------------------------------------------------

def build_ai_messages(
    db,
    conversation_id: int,
    system_prompt: str,
    max_history: int = 20,
    *,
    is_group: bool = False,
    current_message: "Message" = None,
) -> List[Dict[str, Any]]:
    """Build the messages array for an AI chat completion call.

    Fetches conversation history from DB and formats it as OpenAI-compatible
    messages list with system prompt.

    Args:
        db: SQLAlchemy session
        conversation_id: Conversation to pull history from
        system_prompt: The bot's system prompt
        max_history: Maximum history messages to include
        is_group: If True, prefix user messages with sender name
        current_message: The current message being processed (optional,
            will be included with actual image if it has one)

    Returns:
        List of message dicts: [{"role": "system"|"user"|"assistant", "content": ...}]
    """
    from app.database import Message

    messages = [{"role": "system", "content": system_prompt}]

    # Fetch recent history
    history = db.query(Message).filter(
        Message.conversation_id == conversation_id,
    ).order_by(Message.timestamp.desc()).limit(max_history).all()

    history.reverse()  # Oldest first

    for msg in history:
        # Skip the current message if provided (we'll add it separately)
        if current_message and msg.id == current_message.id:
            continue

        sender_prefix = msg.sender_name if is_group and msg.role == "user" else None
        built = _build_message_dict(msg, sender_prefix=sender_prefix, include_image=False)
        messages.append(built)

    # Add current message with image included
    if current_message:
        sender_prefix = current_message.sender_name if is_group else None
        built = _build_message_dict(
            current_message, sender_prefix=sender_prefix, include_image=True
        )
        messages.append(built)

    return messages


def _build_message_dict(
    msg,
    sender_prefix: str = None,
    include_image: bool = False,
) -> Dict[str, Any]:
    """Build an OpenAI-compatible message dict from a Message model.

    For history messages (include_image=False): uses stored media_analysis text.
    For current message (include_image=True): includes actual base64 image.

    Args:
        msg: Message model with content, role, file_url, file_type, media_analysis
        sender_prefix: Optional sender name to prefix (for group chats)
        include_image: If True, include actual image; if False, use stored analysis

    Returns:
        OpenAI message dict
    """
    content = msg.content or ""
    if sender_prefix and msg.role == "user":
        content = f"[{sender_prefix}]: {content}"

    # Image attachment
    if msg.file_url and msg.file_type and msg.file_type.startswith("image/"):
        has_analysis = hasattr(msg, "media_analysis") and msg.media_analysis

        if has_analysis and not include_image:
            text = (
                f"{content}\n[Image: {msg.media_analysis}]"
                if content
                else f"[Image: {msg.media_analysis}]"
            )
            return {"role": msg.role, "content": text}

        if include_image or not has_analysis:
            try:
                from app.bots.whatsapp_bot import _media_url_to_path

                file_path = _media_url_to_path(msg.file_url)
                if file_path.exists():
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    image_b64 = base64.b64encode(image_data).decode("utf-8")
                    parts = [
                        {"type": "text", "text": content or "Please describe or respond to this image."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{msg.file_type};base64,{image_b64}",
                                "detail": "auto",
                            },
                        },
                    ]
                    return {"role": msg.role, "content": parts}
            except Exception as e:
                logger.error(f"Error building image message: {e}")

    # Audio/voice attachment — show transcription as the message
    elif msg.file_type and msg.file_type.startswith("audio/"):
        has_analysis = hasattr(msg, "media_analysis") and msg.media_analysis
        if has_analysis:
            text = f'[Voice message: "{msg.media_analysis}"]'
            return {"role": msg.role, "content": text}
        else:
            return {"role": msg.role, "content": content or "[Voice message - not transcribed]"}

    # Document attachment
    elif msg.file_type and not msg.file_type.startswith("image/") and (msg.file_url or msg.file_name):
        has_analysis = hasattr(msg, "media_analysis") and msg.media_analysis
        doc_label = _get_document_type_label(msg.file_type)
        if has_analysis:
            text = (
                f"{content}\n[{doc_label}: {msg.media_analysis}]"
                if content
                else f"[{doc_label}: {msg.media_analysis}]"
            )
            return {"role": msg.role, "content": text}
        else:
            note = f"[{doc_label} attached: {msg.file_name or 'document'}]"
            text = f"{content}\n{note}" if content else note
            return {"role": msg.role, "content": text}

    return {"role": msg.role, "content": content}


def _get_document_type_label(file_type: str) -> str:
    """Get a human-readable label for a MIME type."""
    labels = {
        "application/pdf": "PDF Document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word Document",
        "application/msword": "Word Document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel Spreadsheet",
        "application/vnd.ms-excel": "Excel Spreadsheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint Presentation",
        "text/csv": "CSV File",
        "text/plain": "Text File",
        "application/json": "JSON File",
        "text/html": "HTML File",
        "application/xml": "XML File",
    }
    return labels.get(file_type, "Document")


# ---------------------------------------------------------------------------
# Media handling
# ---------------------------------------------------------------------------

def save_media_file(
    base64_data: str,
    media_type: str,
    bot_profile_id: int,
    chat_name: str,
    direction: str = "received",
    original_filename: str = None,
) -> Optional[Dict[str, Any]]:
    """Save a media file (image, document, audio) to the bot's session folder.

    Args:
        base64_data: Base64-encoded file content
        media_type: MIME type (e.g., 'image/jpeg', 'application/pdf')
        bot_profile_id: Bot database ID
        chat_name: Chat name (used for folder organization)
        direction: 'received' or 'sent'
        original_filename: Original filename if known

    Returns:
        Dict with file_url, file_type, file_name, file_size, file_pages, local_file_path
        or None on error
    """
    try:
        file_bytes = base64.b64decode(base64_data)
        file_size = len(file_bytes)
        mime_type = media_type or "application/octet-stream"

        ext_map = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp",
            "video/mp4": ".mp4", "video/webm": ".webm", "video/3gpp": ".3gp",
            "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel": ".xls",
        }
        extension = ext_map.get(mime_type, ".bin")

        # Generate filename
        unique_id = uuid.uuid4().hex[:12]
        if original_filename:
            clean_name = original_filename
            download_match = re.match(r'Download\s+"([^"]+)"', original_filename)
            if download_match:
                clean_name = download_match.group(1)
            else:
                clean_name = re.sub(r'^["\'\s]+|["\'\s]+$', "", clean_name)
            safe_name = re.sub(r"[^\w\-_\.]", "_", clean_name)
            filename = f"{unique_id}_{safe_name}"
        else:
            prefix = "incoming" if direction == "received" else "outgoing"
            filename = f"{prefix}_{media_type.split('/')[-1]}_{unique_id}{extension}"

        # Type subfolder
        if mime_type.startswith("image/") or mime_type.startswith("video/"):
            type_folder = "images"
        elif mime_type.startswith("audio/"):
            type_folder = "audio"
        else:
            type_folder = "documents"

        # Sanitize chat name
        safe_chat_name = re.sub(r"[^\w\-_ ]", "_", chat_name).strip() or "unknown"

        # Save file
        import sys
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent.parent.parent
        else:
            base_dir = Path(__file__).resolve().parent.parent
        sessions_dir = base_dir / "data" / "sessions"
        bot_media_dir = sessions_dir / f"bot_{bot_profile_id}" / "conversations" / safe_chat_name / type_folder / direction
        bot_media_dir.mkdir(parents=True, exist_ok=True)

        file_path = bot_media_dir / filename
        with open(file_path, "wb") as f:
            f.write(file_bytes)

        encoded_chat_name = quote(safe_chat_name, safe="")
        file_url = f"/media/bot_{bot_profile_id}/conversations/{encoded_chat_name}/{type_folder}/{direction}/{filename}"

        logger.info(f"Bot {bot_profile_id}: Saved {direction} media for '{chat_name}': {file_url} ({file_size} bytes)")

        # PDF page count
        file_pages = None
        if mime_type == "application/pdf":
            try:
                from pypdf import PdfReader
                with open(file_path, "rb") as pdf_file:
                    file_pages = len(PdfReader(pdf_file).pages)
            except Exception:
                pass

        return {
            "file_url": file_url,
            "file_type": mime_type,
            "file_name": original_filename or filename,
            "file_size": file_size,
            "file_pages": file_pages,
            "local_file_path": str(file_path),
        }

    except Exception as e:
        logger.error(f"Error saving media: {e}", exc_info=True)
        return None


def analyze_media_with_ai(
    ai_provider,
    file_path: str,
    file_type: str,
    user_message: str = "",
) -> Optional[str]:
    """Analyze any media file using AI and return a description.

    Routes to image analysis (vision) or document analysis (text extraction + summarization).

    Args:
        ai_provider: AI provider instance (from app.ai.factory)
        file_path: Path to the media file
        file_type: MIME type
        user_message: User's accompanying message (for context)

    Returns:
        AI-generated analysis text, or None on error
    """
    if file_type.startswith("image/"):
        return _analyze_image(ai_provider, file_path, file_type, user_message)
    elif file_type.startswith("audio/"):
        return transcribe_audio(file_path, ai_provider)
    else:
        return _analyze_document(ai_provider, file_path, file_type, user_message)


def transcribe_audio(file_path: str, ai_provider=None) -> Optional[str]:
    """Transcribe an audio file to text using OpenAI Whisper API.

    Uses the bot's AI provider to get the API key. Falls back to trying
    the OpenAI API directly if the provider has an OpenAI-compatible key.

    Args:
        file_path: Path to the audio file (.ogg, .mp3, .m4a, .wav, etc.)
        ai_provider: AI provider instance (to get API key)

    Returns:
        Transcribed text, or None on error
    """
    try:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Audio file not found: {file_path}")
            return None

        # Get API key from provider
        api_key = None
        if ai_provider and hasattr(ai_provider, 'api_key'):
            api_key = ai_provider.api_key
        elif ai_provider and hasattr(ai_provider, 'client'):
            api_key = getattr(ai_provider.client, 'api_key', None)

        if not api_key:
            logger.warning("No API key available for audio transcription")
            return None

        import httpx

        # Use OpenAI Whisper API
        with open(path, "rb") as f:
            response = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (path.name, f, "audio/ogg")},
                data={"model": "whisper-1"},
                timeout=60.0,
            )

        if response.status_code == 200:
            text = response.json().get("text", "").strip()
            if text:
                logger.info(f"Audio transcribed ({len(text)} chars): {text[:80]}...")
                return text
            return None
        else:
            logger.warning(f"Whisper API error {response.status_code}: {response.text[:200]}")
            return None

    except Exception as e:
        logger.error(f"Audio transcription error: {e}")
        return None


def text_to_speech(text: str, ai_provider=None, voice: str = "alloy", output_path: str = None) -> Optional[str]:
    """Convert text to speech using OpenAI TTS API.

    Args:
        text: Text to convert to speech
        ai_provider: AI provider instance (to get API key)
        voice: Voice name (alloy, echo, fable, onyx, nova, shimmer)
        output_path: Where to save the audio file (auto-generated if None)

    Returns:
        Path to the generated audio file, or None on error
    """
    try:
        if not text or len(text.strip()) < 2:
            return None

        # Get API key
        api_key = None
        if ai_provider and hasattr(ai_provider, 'api_key'):
            api_key = ai_provider.api_key
        elif ai_provider and hasattr(ai_provider, 'client'):
            api_key = getattr(ai_provider.client, 'api_key', None)

        if not api_key:
            logger.warning("No API key available for TTS")
            return None

        import httpx
        import tempfile

        response = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "tts-1",
                "input": text[:4096],  # TTS has a 4096 char limit
                "voice": voice,
                "response_format": "opus",  # Small file, good for voice notes
            },
            timeout=60.0,
        )

        if response.status_code == 200:
            if not output_path:
                fd, output_path = tempfile.mkstemp(suffix=".ogg", prefix="tts_")
                import os
                os.close(fd)

            with open(output_path, "wb") as f:
                f.write(response.content)

            logger.info(f"TTS generated: {len(response.content)} bytes → {output_path}")
            return output_path
        else:
            logger.warning(f"TTS API error {response.status_code}: {response.text[:200]}")
            return None

    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None


def _analyze_image(ai_provider, file_path: str, file_type: str, user_message: str = "") -> Optional[str]:
    """Analyze an image using AI vision."""
    try:
        if not ai_provider.supports_vision:
            return "[Image received - AI provider does not support image analysis]"

        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Image file not found: {file_path}")
            return None

        with open(path, "rb") as f:
            image_data = f.read()
        image_b64 = base64.b64encode(image_data).decode("utf-8")

        prompt = (
            "Describe this image in 2-3 sentences. Focus on the key elements, context, and any text visible."
        )
        if user_message:
            prompt = (
                f"The user sent this image with the message: '{user_message}'. "
                "Describe what's in the image in 2-3 sentences, focusing on details relevant to their message."
            )

        response = ai_provider.analyze_image(
            image_data=f"data:{file_type};base64,{image_b64}",
            prompt=prompt,
            detail="low",
            max_tokens=200,
        )
        return response.content

    except Exception as e:
        logger.error(f"Error analyzing image: {e}")
        return None


def _analyze_document(ai_provider, file_path: str, file_type: str, user_message: str = "") -> Optional[str]:
    """Analyze a document using text extraction + AI summarization."""
    try:
        # Delegate to whatsapp_bot's existing extraction (it handles all doc types)
        from app.bots.whatsapp_bot import _extract_document_text

        text_content = _extract_document_text(file_path, file_type)
        if not text_content:
            return None

        if user_message:
            prompt = (
                f'The user sent a document with the message: "{user_message}"\n\n'
                f"Document content:\n{text_content}\n\n"
                "Provide a concise summary (2-4 sentences) focusing on key information relevant to the user's message."
            )
        else:
            prompt = (
                "Summarize this document in 2-4 sentences. Focus on the main topic, key points, "
                f"and any important data or conclusions.\n\nDocument content:\n{text_content}"
            )

        response = ai_provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
        )
        return response.content

    except Exception as e:
        logger.error(f"Error analyzing document: {e}")
        return None


# ---------------------------------------------------------------------------
# WebSocket real-time notifications
# ---------------------------------------------------------------------------

def broadcast_user_message(
    conversation_id: int,
    bot_profile_id: int,
    message: "Message",
    conversation: "Conversation",
):
    """Broadcast a new user message via WebSocket.

    Safely handles cross-thread calls (bot thread -> async main loop).

    Args:
        conversation_id: Conversation ID for message stream subscribers
        bot_profile_id: Bot ID for chat list subscribers
        message: The Message model instance
        conversation: The Conversation model instance
    """
    try:
        from app.conversations.routes import conversation_ws_manager

        loop = conversation_ws_manager.get_main_loop()
        if not loop or not loop.is_running():
            return

        msg_data = {
            "id": message.id,
            "role": "user",
            "content": message.content,
            "sender_name": message.sender_name,
            "sender_id": message.sender_id,
            "sender_profile_pic": message.sender_profile_pic,
            "timestamp": message.timestamp.isoformat() + "Z",
        }

        future = asyncio.run_coroutine_threadsafe(
            conversation_ws_manager.broadcast_message(conversation_id, msg_data),
            loop,
        )
        future.result(timeout=2)

        # Chat list update
        conv_data = {
            "id": conversation.id,
            "chat_name": conversation.chat_name,
            "last_message": (message.content or "")[:50],
            "timestamp": message.timestamp.isoformat() + "Z",
            "profile_pic": conversation.profile_pic,
        }
        future2 = asyncio.run_coroutine_threadsafe(
            conversation_ws_manager.broadcast_chat_update(bot_profile_id, conv_data),
            loop,
        )
        future2.result(timeout=2)

    except Exception as e:
        logger.error(f"WebSocket broadcast error (user msg): {e}")


def broadcast_assistant_message(
    conversation_id: int,
    message: "Message",
):
    """Broadcast an AI response via WebSocket.

    Args:
        conversation_id: Conversation ID for subscribers
        message: The assistant Message model instance
    """
    try:
        from app.conversations.routes import conversation_ws_manager

        loop = conversation_ws_manager.get_main_loop()
        if not loop or not loop.is_running():
            return

        msg_data = {
            "id": message.id,
            "role": "assistant",
            "content": message.content,
            "sender_name": message.sender_name,
            "sender_id": message.sender_id,
            "sender_profile_pic": message.sender_profile_pic,
            "timestamp": message.timestamp.isoformat() + "Z",
        }

        future = asyncio.run_coroutine_threadsafe(
            conversation_ws_manager.broadcast_message(conversation_id, msg_data),
            loop,
        )
        future.result(timeout=2)

    except Exception as e:
        logger.error(f"WebSocket broadcast error (assistant msg): {e}")


def broadcast_typing(conversation_id: int, is_typing: bool, sender: str = "Bot"):
    """Broadcast typing indicator via WebSocket.

    Args:
        conversation_id: Conversation ID for subscribers
        is_typing: True to show typing, False to hide
        sender: Who is typing
    """
    try:
        from app.conversations.routes import conversation_ws_manager

        loop = conversation_ws_manager.get_main_loop()
        if not loop or not loop.is_running():
            return

        future = asyncio.run_coroutine_threadsafe(
            conversation_ws_manager.broadcast_typing(conversation_id, is_typing, sender),
            loop,
        )
        future.result(timeout=2)

    except Exception as e:
        logger.error(f"WebSocket typing broadcast error: {e}")


# ---------------------------------------------------------------------------
# Human takeover
# ---------------------------------------------------------------------------

def is_human_takeover_active(db, conversation_id: int) -> bool:
    """Check if human takeover is active for a conversation.

    When active, AI responses should be suppressed.

    Args:
        db: SQLAlchemy session
        conversation_id: Conversation to check

    Returns:
        True if human has taken over the conversation
    """
    from app.database import Conversation

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    return bool(conv and conv.human_takeover)


def is_sender_approved(db, conversation_id: int) -> bool:
    """Check if sender is approved for DM pairing.

    Returns True if:
    - The conversation is approved (dm_approved = True, default)
    - DM pairing is not relevant (groups are always approved)
    """
    from app.database import Conversation
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        return True  # No conversation = allow (shouldn't happen)
    if conv.is_group:
        return True  # Groups are always approved
    return bool(conv.dm_approved)


# ---------------------------------------------------------------------------
# Activity logging
# ---------------------------------------------------------------------------

def log_activity(
    db,
    bot_profile_id_or_user_id: int,
    action: str,
    details: str = "",
):
    """Log an activity event.

    Args:
        db: SQLAlchemy session
        bot_profile_id_or_user_id: Bot profile ID (ActivityLog uses bot_profile_id)
        action: Action type string
        details: Human-readable details
    """
    from app.database import ActivityLog

    log = ActivityLog(
        bot_profile_id=bot_profile_id_or_user_id,
        action=action,
        details=details,
    )
    db.add(log)
    db.flush()
