"""
Scheduled Content Scheduler

Background task that processes and sends scheduled content at the appropriate time.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.database import SessionLocal, ScheduledContent, Hub, Contact, BotProfile, Conversation
from app.bots.manager import bot_manager

logger = logging.getLogger(__name__)


class ContentScheduler:
    """Background scheduler for processing scheduled content."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 30  # Check every 30 seconds

    async def start(self):
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Content scheduler started.")

    async def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Content scheduler stopped.")

    async def _run_loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                await self._process_pending_content()
            except Exception as e:
                logger.info(f"Error in content scheduler: {e}")

            await asyncio.sleep(self._check_interval)

    async def _process_pending_content(self):
        """Process all pending content that should be sent now."""
        db = SessionLocal()
        try:
            # Find all pending content where scheduled_for <= now
            now = datetime.utcnow()
            pending_content = db.query(ScheduledContent).filter(
                ScheduledContent.status == "pending",
                ScheduledContent.scheduled_for <= now
            ).all()

            for content in pending_content:
                await self._send_content(db, content)

        except Exception as e:
            logger.info(f"Error processing pending content: {e}")
        finally:
            db.close()

    async def _send_content(self, db: Session, content: ScheduledContent) -> Tuple[bool, str]:
        """Send a single piece of scheduled content.

        Returns: (success: bool, error_message: str)
        """
        try:
            # Import the send function here to avoid circular imports
            from app.bots.whatsapp_bot import send_whatsapp_message

            # Check if content is still pending (prevent race conditions)
            if content.status != "pending":
                logger.info(f"Content {content.id}: Already processed (status={content.status}), skipping")
                return False, f"Content already {content.status}"

            # Mark as processing immediately to prevent duplicate sends
            content.status = "processing"
            db.commit()

            # Get the hub
            hub = db.query(Hub).filter(Hub.id == content.hub_id).first()
            if not hub:
                content.status = "failed"
                db.commit()
                logger.info(f"Content {content.id} failed: Hub not found")
                return False, "Hub not found"

            # Determine which bot to use
            bot_id = content.bot_profile_id
            logger.info(f"Content {content.id}: bot_profile_id from content = {bot_id}")

            if not bot_id:
                # Use the first available bot from the hub
                from app.database import HubBotMembership
                membership = db.query(HubBotMembership).filter(
                    HubBotMembership.hub_id == content.hub_id,
                    HubBotMembership.is_active == True
                ).first()
                if membership:
                    bot_id = membership.bot_profile_id
                    logger.info(f"Content {content.id}: Using bot {bot_id} from hub membership")
                else:
                    logger.info(f"Content {content.id}: No active bot membership found for hub {content.hub_id}")

            if not bot_id:
                content.status = "failed"
                db.commit()
                logger.info(f"Content {content.id} failed: No bot assigned to hub")
                return False, "No bot assigned to hub. Please add a bot to this hub first."

            # Check if bot instance exists and is running
            bot_instance = bot_manager.get_instance(bot_id)
            logger.info(f"Content {content.id}: bot_instance={bot_instance}, is_running={bot_instance.is_running if bot_instance else 'N/A'}")

            if not bot_instance:
                content.status = "pending"
                db.commit()
                logger.info(f"Content {content.id}: Bot {bot_id} instance not found")
                return False, f"Bot (ID: {bot_id}) is not started. Please start the bot first."

            if not bot_instance.is_running:
                content.status = "pending"
                db.commit()
                logger.info(f"Content {content.id}: Bot {bot_id} not running")
                return False, f"Bot (ID: {bot_id}) is not running. Please start the bot first."

            # Check if WhatsApp is connected
            if not bot_instance.whatsapp_connected:
                content.status = "pending"
                db.commit()
                logger.info(f"Content {content.id}: Bot {bot_id} WhatsApp not connected")
                return False, f"Bot (ID: {bot_id}) is not connected to WhatsApp. Please scan the QR code."

            # Determine recipients based on recipient_type
            recipients = []
            seen_ids = set()  # Deduplicate by chat ID

            recipient_type = getattr(content, 'recipient_type', 'broadcast') or 'broadcast'
            logger.info(f"Content {content.id}: recipient_type={recipient_type}")

            # Parse multi-select JSON fields
            contact_ids_list = None
            group_ids_list = None
            try:
                if content.contact_ids:
                    contact_ids_list = json.loads(content.contact_ids)
                    logger.info(f"Content {content.id}: contact_ids_list={contact_ids_list}")
                if content.group_ids:
                    group_ids_list = json.loads(content.group_ids)
                    logger.info(f"Content {content.id}: group_ids_list={group_ids_list}")
            except Exception as e:
                logger.info(f"Content {content.id}: Error parsing JSON: {e}")

            if recipient_type == 'broadcast_all':
                # Send to ALL contacts AND ALL groups
                logger.info(f"Content {content.id}: BROADCAST ALL mode - sending to all contacts and groups")

                # Add all hub contacts
                contacts = db.query(Contact).filter(Contact.hub_id == content.hub_id).all()
                logger.info(f"Content {content.id}: Found {len(contacts)} contacts in hub")
                for c in contacts:
                    clean_phone = ''.join(ch for ch in c.phone if ch.isdigit() or ch == '+')
                    if clean_phone.startswith('+'):
                        clean_phone = clean_phone[1:]
                    chat_id = f"{clean_phone}@c.us"
                    if clean_phone and chat_id not in seen_ids:
                        seen_ids.add(chat_id)
                        recipients.append({
                            'chat_id': chat_id,
                            'name': c.display_name or c.phone,
                            'is_group': False
                        })

                # Add all groups from hub bots
                from app.database import HubBotMembership
                bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
                    HubBotMembership.hub_id == content.hub_id,
                    HubBotMembership.is_active == True
                ).all()
                bot_ids = [b[0] for b in bot_ids]

                if bot_ids:
                    groups = db.query(Conversation).filter(
                        Conversation.bot_profile_id.in_(bot_ids),
                        Conversation.is_group == True
                    ).all()
                    logger.info(f"Content {content.id}: Found {len(groups)} groups from hub bots")

                    for g in groups:
                        if g.chat_id and g.chat_id not in seen_ids:
                            seen_ids.add(g.chat_id)
                            recipients.append({
                                'chat_id': g.chat_id,
                                'name': g.chat_name or g.chat_id,
                                'is_group': True
                            })

            elif recipient_type == 'groups' and group_ids_list:
                # Send to selected groups (multi-select)
                for group_info in group_ids_list:
                    group_id = group_info.get('id') if isinstance(group_info, dict) else group_info
                    group_name = group_info.get('name', group_id) if isinstance(group_info, dict) else group_id
                    if group_id:
                        if not group_id.endswith('@g.us'):
                            group_id = f"{group_id}@g.us"
                        if group_id not in seen_ids:
                            seen_ids.add(group_id)
                            recipients.append({
                                'chat_id': group_id,
                                'name': group_name,
                                'is_group': True
                            })

            elif recipient_type == 'group':
                # Legacy: Send to a single WhatsApp group
                group_id = content.group_id
                if group_id:
                    if not group_id.endswith('@g.us'):
                        group_id = f"{group_id}@g.us"
                    recipients.append({
                        'chat_id': group_id,
                        'name': content.group_name or group_id,
                        'is_group': True
                    })
                else:
                    content.status = "failed"
                    db.commit()
                    logger.info(f"Content {content.id} failed: No group_id specified")
                    return False, "No group specified"

            elif recipient_type == 'all_groups':
                # Send to all groups from hub bots
                from app.database import HubBotMembership
                bot_ids = db.query(HubBotMembership.bot_profile_id).filter(
                    HubBotMembership.hub_id == content.hub_id,
                    HubBotMembership.is_active == True
                ).all()
                bot_ids = [b[0] for b in bot_ids]

                if bot_ids:
                    groups = db.query(Conversation).filter(
                        Conversation.bot_profile_id.in_(bot_ids),
                        Conversation.is_group == True
                    ).all()

                    for g in groups:
                        if g.chat_id and g.chat_id not in seen_ids:
                            seen_ids.add(g.chat_id)
                            recipients.append({
                                'chat_id': g.chat_id,
                                'name': g.chat_name or g.chat_id,
                                'is_group': True
                            })

            elif recipient_type == 'contacts':
                # Send to selected contacts (multi-select)
                if not contact_ids_list or len(contact_ids_list) == 0:
                    content.status = "failed"
                    db.commit()
                    logger.info(f"Content {content.id} failed: No contacts selected (contact_ids_list={contact_ids_list})")
                    return False, "No contacts selected"

                logger.info(f"Content {content.id}: Querying contacts with IDs: {contact_ids_list}")
                contacts = db.query(Contact).filter(Contact.id.in_(contact_ids_list)).all()
                logger.info(f"Content {content.id}: Found {len(contacts)} contacts")
                for contact in contacts:
                    logger.info(f"Content {content.id}: Contact ID={contact.id}, name={contact.display_name}, phone={contact.phone}")
                    clean_phone = ''.join(c for c in contact.phone if c.isdigit() or c == '+')
                    if clean_phone.startswith('+'):
                        clean_phone = clean_phone[1:]
                    chat_id = f"{clean_phone}@c.us"
                    if chat_id not in seen_ids:
                        seen_ids.add(chat_id)
                        recipients.append({
                            'chat_id': chat_id,
                            'name': contact.display_name or contact.phone,
                            'is_group': False
                        })

            elif recipient_type == 'contact' and content.contact_id:
                # Legacy: Send to a single contact
                contact = db.query(Contact).filter(Contact.id == content.contact_id).first()
                if contact:
                    clean_phone = ''.join(c for c in contact.phone if c.isdigit() or c == '+')
                    if clean_phone.startswith('+'):
                        clean_phone = clean_phone[1:]
                    chat_id = f"{clean_phone}@c.us"
                    if chat_id not in seen_ids:
                        seen_ids.add(chat_id)
                        recipients.append({
                            'chat_id': chat_id,
                            'name': contact.display_name or contact.phone,
                            'is_group': False
                        })

            else:
                # Broadcast to all hub contacts (recipient_type='broadcast' or fallback)
                logger.info(f"Content {content.id}: BROADCAST mode - sending to all hub contacts")
                contacts = db.query(Contact).filter(Contact.hub_id == content.hub_id).all()
                logger.info(f"Content {content.id}: Found {len(contacts)} contacts in hub")
                for c in contacts:
                    clean_phone = ''.join(ch for ch in c.phone if ch.isdigit() or ch == '+')
                    if clean_phone.startswith('+'):
                        clean_phone = clean_phone[1:]
                    chat_id = f"{clean_phone}@c.us"
                    if clean_phone and chat_id not in seen_ids:
                        seen_ids.add(chat_id)
                        recipients.append({
                            'chat_id': chat_id,
                            'name': c.display_name or c.phone,
                            'is_group': False
                        })

            if not recipients:
                content.status = "failed"
                db.commit()
                logger.info(f"Content {content.id} failed: No recipients found")
                return False, "No recipients found"

            # Log all recipients before sending
            logger.info(f"Content {content.id}: Will send to {len(recipients)} recipient(s):")
            for r in recipients:
                logger.info(f"  - {r['name']} ({r['chat_id']})")

            # Send messages using the existing send_whatsapp_message function
            success_count = 0
            errors = []

            # Get all bot IDs in this hub for finding the right bot per recipient
            from app.database import HubBotMembership
            hub_bot_ids = [m.bot_profile_id for m in db.query(HubBotMembership).filter(
                HubBotMembership.hub_id == content.hub_id,
                HubBotMembership.is_active == True
            ).all()]

            for recipient in recipients:
                try:
                    chat_id = recipient['chat_id']
                    chat_name = recipient['name']

                    # Find which bot has a conversation with this contact
                    # This is important because each bot has its own WhatsApp session
                    send_bot_id = bot_id  # Default to the selected bot

                    if not recipient.get('is_group'):
                        # For contacts, find a bot that has a conversation with this phone
                        phone_to_find = chat_id.replace('@c.us', '').replace('@lid', '')
                        conv_with_contact = db.query(Conversation).filter(
                            Conversation.bot_profile_id.in_(hub_bot_ids),
                            Conversation.is_group == False,
                            Conversation.phone == phone_to_find
                        ).first()

                        if conv_with_contact:
                            send_bot_id = conv_with_contact.bot_profile_id
                            chat_name = conv_with_contact.chat_name or chat_name  # Use the conversation's chat_name
                            logger.info(f"Content {content.id}: Found conversation with {chat_name} in bot {send_bot_id}")

                            # Verify this bot is running
                            conv_bot_instance = bot_manager.get_instance(send_bot_id)
                            if not conv_bot_instance or not conv_bot_instance.is_running or not conv_bot_instance.whatsapp_connected:
                                logger.info(f"Content {content.id}: Bot {send_bot_id} with conversation is not running, trying default bot")
                                send_bot_id = bot_id
                        else:
                            logger.info(f"Content {content.id}: No existing conversation found for phone {phone_to_find}, using default bot {bot_id}")

                    # Use the existing WhatsApp message sending function
                    result = await send_whatsapp_message(
                        bot_profile_id=send_bot_id,
                        chat_id=chat_id,
                        chat_name=chat_name,
                        message=content.content
                    )

                    if result:
                        success_count += 1
                        logger.info(f"Content {content.id}: Sent to {chat_name} via bot {send_bot_id} ({'group' if recipient['is_group'] else 'contact'})")
                    else:
                        errors.append(f"Failed to send to {chat_name}")
                except Exception as e:
                    errors.append(f"Error sending to {recipient['name']}: {str(e)}")
                    logger.info(f"Content {content.id}: Error sending to {recipient['name']}: {e}")

            if success_count > 0:
                content.status = "sent"
                content.sent_at = datetime.utcnow()
                db.commit()
                return True, f"Sent to {success_count} recipient(s)"
            else:
                content.status = "failed"
                db.commit()
                return False, "; ".join(errors) if errors else "Failed to send"

        except Exception as e:
            logger.info(f"Error sending content {content.id}: {e}")
            content.status = "failed"
            db.commit()
            return False, str(e)

    async def send_immediate(self, content_id: int) -> Tuple[bool, Optional[str]]:
        """Send content immediately (for 'send immediately' option).

        Returns: (success: bool, error_message: str or None)
        """
        db = SessionLocal()
        try:
            content = db.query(ScheduledContent).filter(
                ScheduledContent.id == content_id
            ).first()

            if not content:
                return False, "Content not found"

            success, message = await self._send_content(db, content)
            return success, message if not success else None

        except Exception as e:
            logger.info(f"Error sending immediate content: {e}")
            return False, str(e)
        finally:
            db.close()


# Global scheduler instance
content_scheduler = ContentScheduler()
