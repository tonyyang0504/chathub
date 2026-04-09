"""
Email Platform Adapter
IMAP/SMTP-based integration for receiving and sending emails.

Requires:
- IMAP server + port (for receiving)
- SMTP server + port (for sending)
- Email address
- Password / App Password (for Gmail, use App Password)

The adapter polls the IMAP inbox for new unread emails and replies
via SMTP with AI-generated responses. Email threading is preserved
via In-Reply-To and References headers.
"""

import asyncio
import email
import email.utils
import imaplib
import logging
import os
import re
import smtplib
import tempfile
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Dict, Any, List, Optional

from app.platforms.base import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformType,
    AuthMethod,
)
from app.platforms.message_handler import (
    get_db_session,
    find_or_create_conversation,
    save_user_message,
    save_assistant_message,
    update_conversation_stats,
    is_duplicate_message,
    has_response_after,
    build_ai_messages,
    broadcast_user_message,
    broadcast_assistant_message,
    broadcast_typing,
    is_human_takeover_active,
    is_sender_approved,
)

logger = logging.getLogger(__name__)

# Per-bot state
_bot_state: Dict[int, Dict[str, Any]] = {}

# Common IMAP/SMTP server presets
SERVER_PRESETS = {
    "gmail.com": {"imap": "imap.gmail.com", "imap_port": 993, "smtp": "smtp.gmail.com", "smtp_port": 587},
    "outlook.com": {"imap": "outlook.office365.com", "imap_port": 993, "smtp": "smtp.office365.com", "smtp_port": 587},
    "hotmail.com": {"imap": "outlook.office365.com", "imap_port": 993, "smtp": "smtp.office365.com", "smtp_port": 587},
    "yahoo.com": {"imap": "imap.mail.yahoo.com", "imap_port": 993, "smtp": "smtp.mail.yahoo.com", "smtp_port": 587},
}


def _get_state(bot_profile_id: int) -> Dict[str, Any]:
    if bot_profile_id not in _bot_state:
        _bot_state[bot_profile_id] = {
            "email_address": None,
            "password": None,
            "imap_server": None,
            "imap_port": 993,
            "smtp_server": None,
            "smtp_port": 587,
            "instance": None,
            "seen_uids": set(),
        }
    return _bot_state[bot_profile_id]


def cleanup_bot_state(bot_profile_id: int):
    _bot_state.pop(bot_profile_id, None)


class EmailAdapter(PlatformAdapter):
    """Email adapter using IMAP for receiving and SMTP for sending.

    Integration type: Polling-based.
    - Polls IMAP inbox every 30 seconds for new unread emails
    - Sends replies via SMTP
    - Auth: Email + Password/App Password
    """

    @property
    def platform_type(self) -> PlatformType:
        return PlatformType.EMAIL

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_groups=False,
            supports_media=True,
            supports_file_send=True,
            supports_reactions=False,
            supports_read_receipts=False,
            supports_typing_indicator=False,
            supports_history_sync=False,
            supports_contacts_list=False,
            supports_groups_list=False,
            supports_profile_pic=False,
            auth_method=AuthMethod.CREDENTIALS,
            max_message_length=50000,
            supported_media_types=[
                "image/jpeg", "image/png", "image/gif",
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/plain", "text/csv",
            ],
        )

    async def run(self, instance) -> None:
        """Start the email bot — connect to IMAP and poll for new emails."""
        bot_id = instance.bot_profile_id
        config = instance.config

        email_address = config.get("email_address", "")
        password = config.get("platform_token") or config.get("password", "")
        imap_server = config.get("imap_server", "")
        smtp_server = config.get("smtp_server", "")
        imap_port = int(config.get("imap_port", 993))
        smtp_port = int(config.get("smtp_port", 587))

        # Auto-detect servers from email domain
        if email_address and (not imap_server or not smtp_server):
            domain = email_address.split("@")[-1].lower()
            preset = SERVER_PRESETS.get(domain)
            if preset:
                if not imap_server:
                    imap_server = preset["imap"]
                    imap_port = preset["imap_port"]
                if not smtp_server:
                    smtp_server = preset["smtp"]
                    smtp_port = preset["smtp_port"]

        if not email_address or not password:
            await instance.notify_status({
                "error": "Email address and password are required",
                "message": "Enter your email address and password (App Password for Gmail).",
            })
            instance.is_running = False
            return

        if not imap_server or not smtp_server:
            await instance.notify_status({
                "error": "IMAP/SMTP servers not configured",
                "message": "Enter IMAP and SMTP server addresses, or use a supported email provider (Gmail, Outlook, Yahoo).",
            })
            instance.is_running = False
            return

        state = _get_state(bot_id)
        state["email_address"] = email_address
        state["password"] = password
        state["imap_server"] = imap_server
        state["imap_port"] = imap_port
        state["smtp_server"] = smtp_server
        state["smtp_port"] = smtp_port
        state["instance"] = instance

        # Test IMAP connection
        try:
            imap = await asyncio.to_thread(self._connect_imap, state)
            imap.logout()
        except Exception as e:
            await instance.notify_status({
                "error": f"IMAP connection failed: {e}",
                "message": "Check your email, password, and IMAP server settings. For Gmail, use an App Password.",
            })
            instance.is_running = False
            try:
                from app.database import SessionLocal, BotProfile as BPModel
                _db = SessionLocal()
                try:
                    _bot = _db.query(BPModel).filter(BPModel.id == bot_id).first()
                    if _bot:
                        _bot.is_running = False
                        _db.commit()
                finally:
                    _db.close()
            except Exception:
                pass
            return

        # Save account info
        instance.whatsapp_connected = True
        try:
            from app.database import SessionLocal, BotProfile as BPModel
            _db = SessionLocal()
            try:
                _bot = _db.query(BPModel).filter(BPModel.id == bot_id).first()
                if _bot:
                    _bot.whatsapp_name = email_address
                    _db.commit()
            finally:
                _db.close()
        except Exception:
            pass

        await instance.notify_status({
            "message": f"Connected to email: {email_address}",
            "connected": True,
            "account_info": {"name": email_address},
        })

        logger.info(f"Bot {bot_id}: Email connected as {email_address} (IMAP: {imap_server}, SMTP: {smtp_server})")

        # Mark all existing unread as seen (don't process backlog)
        try:
            logger.info(f"Bot {bot_id}: Marking existing unread emails as seen (skipping backlog)")
            await asyncio.to_thread(self._mark_existing_as_seen, state)
        except Exception as e:
            logger.warning(f"Bot {bot_id}: Failed to mark existing emails: {e}")

        # Polling loop
        poll_interval = 30  # seconds
        try:
            while instance.is_running and not instance.stopped_by_user:
                try:
                    # Fetch new emails in thread, process async
                    new_emails = await asyncio.to_thread(self._fetch_new_emails, bot_id, state)
                    for email_data in new_emails:
                        try:
                            await self._handle_email(bot_id, state, **email_data)
                        except Exception as e:
                            logger.error(f"Bot {bot_id}: Email handling error: {e}", exc_info=True)
                except Exception as e:
                    logger.error(f"Bot {bot_id}: Email poll error: {e}", exc_info=True)

                # Process outbound messages
                if instance.has_outbound_messages():
                    for msg in instance.get_outbound_messages():
                        try:
                            await self.send_message(
                                bot_id,
                                msg["chat_id"],
                                msg.get("chat_name", ""),
                                msg["message"],
                            )
                        except Exception as e:
                            logger.error(f"Bot {bot_id}: Outbound send error: {e}")

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info(f"Bot {bot_id}: Email adapter cancelled")
        finally:
            instance.whatsapp_connected = False
            state["instance"] = None
            logger.info(f"Bot {bot_id}: Email adapter stopped")

    def _connect_imap(self, state: dict) -> imaplib.IMAP4_SSL:
        """Connect to IMAP server."""
        imap = imaplib.IMAP4_SSL(state["imap_server"], state["imap_port"])
        imap.login(state["email_address"], state["password"])
        return imap

    def _mark_existing_as_seen(self, state: dict):
        """Record baseline UID. Process emails from last 2 minutes to catch recent ones."""
        imap = self._connect_imap(state)
        imap.select("INBOX")

        # Get all UIDs
        status, data = imap.uid("search", None, "ALL")
        if status == "OK" and data[0]:
            uids = data[0].split()
            if uids:
                # Set threshold to 10 UIDs before the latest — catches very recent emails
                latest_uid = int(uids[-1])
                state["last_uid"] = max(0, latest_uid - 10)
                logger.info(f"Email: Baseline UID {state['last_uid']} (latest: {latest_uid}) — will process last ~10 emails + all new")
            else:
                state["last_uid"] = 0
        else:
            state["last_uid"] = 0
        imap.logout()

    def _fetch_new_emails(self, bot_id: int, state: dict) -> list:
        """Fetch new emails by UID (runs in thread). Returns list of email dicts."""
        results = []
        try:
            imap = self._connect_imap(state)
            imap.select("INBOX")

            # Search for UIDs greater than the last seen UID
            last_uid = state.get("last_uid", 0)
            search_uid = str(last_uid + 1)
            status, data = imap.uid("search", None, f"UID {search_uid}:*")
            if status != "OK" or not data[0]:
                imap.logout()
                return results

            uid_list = data[0].split()
            # Filter out UIDs we've already seen (including last_uid itself which may match)
            new_uids = [uid for uid in uid_list if int(uid) > last_uid]
            if not new_uids:
                imap.logout()
                return results

            # Limit to 10 per poll to avoid overload
            for uid in new_uids[:10]:
                uid_int = int(uid)
                if uid_int > state.get("last_uid", 0):
                    state["last_uid"] = uid_int

                status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
                from_name = email.utils.parseaddr(msg.get("From", ""))[0] or from_addr
                subject = self._decode_header(msg.get("Subject", ""))
                message_id = msg.get("Message-ID", "")
                date_str = msg.get("Date", "")
                references = msg.get("References", "")

                # Skip emails from self
                if from_addr.lower() == state["email_address"].lower():
                    continue

                body = self._extract_body(msg)
                if not body:
                    body = f"[Email with subject: {subject}]"

                content = f"Subject: {subject}\n\n{body}" if subject else body

                try:
                    timestamp = email.utils.parsedate_to_datetime(date_str) if date_str else datetime.utcnow()
                except Exception:
                    timestamp = datetime.utcnow()

                results.append({
                    "from_addr": from_addr,
                    "from_name": from_name,
                    "subject": subject,
                    "content": content,
                    "message_id": message_id,
                    "references": references,
                    "timestamp": timestamp,
                })

            logger.info(f"Bot {bot_id}: Fetched {len(results)} new email(s) (UIDs > {last_uid})")

            imap.logout()
        except Exception as e:
            logger.error(f"Bot {bot_id}: IMAP fetch error: {e}")

        return results

    async def _handle_email(self, bot_id: int, state: dict, from_addr: str, from_name: str,
                             subject: str, content: str, message_id: str, references: str,
                             timestamp: datetime):
        """Handle a single inbound email."""
        instance = state.get("instance")
        if not instance:
            return

        with get_db_session() as db:
            from app.database import BotProfile
            bot = db.query(BotProfile).filter(BotProfile.id == bot_id).first()
            if not bot:
                return

            # Find or create conversation (use email address as chat_id)
            conversation = find_or_create_conversation(
                db=db,
                bot_profile_id=bot_id,
                chat_id=from_addr,
                chat_name=from_name,
                phone=from_addr,
                is_group=False,
            )

            # Save user message
            save_user_message(
                db,
                conversation.id,
                content,
                sender_name=from_name,
                sender_id=from_addr,
                platform_message_id=message_id,
                timestamp=timestamp,
            )

            update_conversation_stats(db, conversation.id)
            db.commit()  # Commit the message before AI response attempt

            logger.info(f"Bot {bot_id}: Saved email from {from_addr} ({from_name}), subject: {subject}")

            # Check human takeover and DM approval
            if is_human_takeover_active(db, conversation.id):
                return
            if not is_sender_approved(db, conversation.id):
                return
            if not bot.is_active:
                return

            # Generate AI response
            try:
                from app.auth.utils import decrypt_string
                ai_key = decrypt_string(bot.api_key_encrypted) if bot.api_key_encrypted else None
                if not ai_key:
                    logger.warning(f"Bot {bot_id}: No AI API key configured")
                    return

                ai_messages = build_ai_messages(
                    db,
                    conversation.id,
                    bot.system_prompt or "You are a helpful email assistant. Reply concisely and professionally.",
                    bot.max_history or 20,
                )

                from app.ai.factory import get_ai_provider
                provider = get_ai_provider(bot.ai_provider, ai_key, bot.model)
                reply = await provider.chat_completion(ai_messages)

                if reply:
                    import random
                    delay = random.uniform(
                        bot.response_delay_min or 1,
                        bot.response_delay_max or 3,
                    )
                    await asyncio.sleep(delay)

                    # Send reply email
                    sent = await asyncio.to_thread(
                        self._send_email_sync, state, from_addr, subject, reply, message_id, references
                    )
                    if sent:
                        save_assistant_message(db, conversation.id, reply)
                        update_conversation_stats(db, conversation.id)
                        logger.info(f"Bot {bot_id}: AI replied to {from_addr}")

            except Exception as e:
                logger.error(f"Bot {bot_id}: AI response error: {e}", exc_info=True)

    def _send_email_sync(self, state: dict, to_addr: str, subject: str, body: str,
                          in_reply_to: str = "", references: str = "") -> bool:
        """Send an email reply via SMTP (runs in thread)."""
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = state["email_address"]
            msg["To"] = to_addr
            msg["Subject"] = f"Re: {subject}" if subject and not subject.startswith("Re:") else (subject or "Re:")
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to

            # Plain text part
            msg.attach(MIMEText(body, "plain", "utf-8"))

            # HTML part (basic formatting)
            html_body = body.replace("\n", "<br>")
            msg.attach(MIMEText(f"<html><body><p>{html_body}</p></body></html>", "html", "utf-8"))

            # Connect and send
            smtp = smtplib.SMTP(state["smtp_server"], state["smtp_port"])
            smtp.ehlo()
            smtp.starttls()
            smtp.login(state["email_address"], state["password"])
            smtp.sendmail(state["email_address"], to_addr, msg.as_string())
            smtp.quit()

            logger.info(f"Email sent to {to_addr}: {subject}")
            return True

        except Exception as e:
            logger.error(f"SMTP send error to {to_addr}: {e}")
            return False

    async def send_message(self, bot_profile_id: int, chat_id: str, chat_name: str, message: str) -> bool:
        """Send an email message."""
        state = _get_state(bot_profile_id)
        if not state.get("email_address"):
            return False
        return await asyncio.to_thread(
            self._send_email_sync, state, chat_id, "", message
        )

    async def send_file(self, bot_profile_id: int, chat_id: str, chat_name: str, file_path: str, caption: str = "") -> bool:
        """Send an email with a file attachment."""
        state = _get_state(bot_profile_id)
        if not state.get("email_address"):
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = state["email_address"]
            msg["To"] = chat_id
            msg["Subject"] = caption or "File attachment"

            if caption:
                msg.attach(MIMEText(caption, "plain", "utf-8"))

            # Attach file
            with open(file_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(file_path)}")
                msg.attach(part)

            smtp = smtplib.SMTP(state["smtp_server"], state["smtp_port"])
            smtp.ehlo()
            smtp.starttls()
            smtp.login(state["email_address"], state["password"])
            smtp.sendmail(state["email_address"], chat_id, msg.as_string())
            smtp.quit()

            return True
        except Exception as e:
            logger.error(f"Email send_file error: {e}")
            return False

    async def cleanup(self, bot_id: int) -> None:
        cleanup_bot_state(bot_id)

    async def get_contacts(self, bot_id: int) -> List[Dict[str, Any]]:
        return []

    async def get_groups(self, bot_id: int) -> List[Dict[str, Any]]:
        return []

    @staticmethod
    def _decode_header(header_value: str) -> str:
        """Decode email header (handles encoded words)."""
        if not header_value:
            return ""
        decoded_parts = email.header.decode_header(header_value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return " ".join(result)

    @staticmethod
    def _extract_body(msg: email.message.Message) -> str:
        """Extract plain text body from email message."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))

                if content_type == "text/plain" and "attachment" not in disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace").strip()

            # Fallback to HTML
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")
                        # Strip HTML tags (basic)
                        text = re.sub(r'<[^>]+>', '', html)
                        text = re.sub(r'\s+', ' ', text).strip()
                        return text
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace").strip()

        return ""
