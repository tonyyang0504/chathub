"""
Scheduled Content Scheduler

Background task that processes and sends scheduled content at the appropriate time.
"""
import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional, Tuple, Dict

from sqlalchemy.orm import Session
from app.tools.monitoring import ToolMonitor


def calculate_sending_params(recipient_count: int, sending_speed_mode: str = "auto",
                             delay_min: int = None, delay_max: int = None,
                             batch_size: int = None, batch_pause: int = None) -> Dict:
    """
    Calculate rate limiting parameters based on mode and recipient count.

    For 'auto' mode, parameters scale based on recipient count:
    - 1-10 recipients: 3-5 sec delay, no batching
    - 11-50 recipients: 5-10 sec delay, batch of 15, 2 min pause
    - 51-100 recipients: 8-15 sec delay, batch of 20, 3 min pause
    - 100+ recipients: 10-20 sec delay, batch of 25, 5 min pause

    For 'custom' mode, uses provided parameters.
    For 'fast' mode, uses minimal delays (2-3 sec, no batching).
    """
    if sending_speed_mode == "fast":
        return {
            'delay_min': 2,
            'delay_max': 3,
            'batch_size': None,  # No batching
            'batch_pause': 0
        }

    if sending_speed_mode == "custom":
        return {
            'delay_min': delay_min or 5,
            'delay_max': delay_max or 15,
            'batch_size': batch_size if batch_size and batch_size > 0 else None,
            'batch_pause': batch_pause or 180
        }

    # Auto mode - scale based on recipient count
    if recipient_count <= 10:
        return {
            'delay_min': 3,
            'delay_max': 5,
            'batch_size': None,
            'batch_pause': 0
        }
    elif recipient_count <= 50:
        return {
            'delay_min': 5,
            'delay_max': 10,
            'batch_size': 15,
            'batch_pause': 120  # 2 minutes
        }
    elif recipient_count <= 100:
        return {
            'delay_min': 8,
            'delay_max': 15,
            'batch_size': 20,
            'batch_pause': 180  # 3 minutes
        }
    else:
        return {
            'delay_min': 10,
            'delay_max': 20,
            'batch_size': 25,
            'batch_pause': 300  # 5 minutes
        }


def convert_to_whatsapp_markdown(text: str) -> str:
    """
    Convert standard markdown to WhatsApp-compatible format.

    Conversions:
    - **bold** or __bold__ -> *bold*
    - ~~strikethrough~~ -> ~strikethrough~
    - ### headers -> *header* (bold)
    """
    if not text:
        return text

    # Remove markdown headers (###, ##, #) and make them bold
    text = re.sub(r'^#{1,6}\s*(.+)$', r'*\1*', text, flags=re.MULTILINE)

    # Convert **bold** to *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)

    # Convert __bold__ to *bold*
    text = re.sub(r'__(.+?)__', r'*\1*', text)

    # Convert ~~strikethrough~~ to ~strikethrough~
    text = re.sub(r'~~(.+?)~~', r'~\1~', text)

    # Clean up any double asterisks that might remain
    text = re.sub(r'\*\*+', '*', text)

    return text

from app.database import SessionLocal, ScheduledContent, Hub, Contact, BotProfile, Conversation
from app.bots.manager import bot_manager
from app.tools.event_bus import tool_event_bus

logger = logging.getLogger(__name__)


class ContentScheduler:
    """Background scheduler for processing scheduled content."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 30  # Check every 30 seconds
        self._active_sends: Dict[int, asyncio.Task] = {}  # content_id -> task
        self._cancelled_content: set = set()  # Set of cancelled content IDs

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

        # Cancel all active sends
        for content_id, task in self._active_sends.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._active_sends.clear()
        self._cancelled_content.clear()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Content scheduler stopped.")

    def cancel_content(self, content_id: int):
        """Cancel a running content send."""
        self._cancelled_content.add(content_id)
        if content_id in self._active_sends:
            self._active_sends[content_id].cancel()
            logger.info(f"Cancelled active send for content {content_id}")

    def is_cancelled(self, content_id: int) -> bool:
        """Check if a content has been cancelled."""
        return content_id in self._cancelled_content

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

        Supports multi-bot sending modes:
        - "all": Each hub bot sends to its own contacts/groups
        - "selected": Each selected bot sends to its own contacts/groups
        - "any": Any available bot sends to all recipients (legacy behavior)

        Returns: (success: bool, error_message: str)
        """
        # Track execution time for logging
        start_time = time.time()

        try:
            # Import the send function here to avoid circular imports
            from app.bots.whatsapp_bot import send_whatsapp_message
            # HubBotMembership no longer needed - we use user's bots directly

            # Check if content is still pending (prevent race conditions)
            if content.status != "pending":
                return False, f"Content already {content.status}"

            # Check if already cancelled
            if self.is_cancelled(content.id):
                content.status = "cancelled"
                db.commit()
                return False, "Content was cancelled"

            # Mark as sending immediately to prevent duplicate sends
            content.status = "sending"
            db.commit()

            hub = db.query(Hub).filter(Hub.id == content.hub_id).first()
            if not hub:
                content.status = "failed"
                db.commit()
                return False, "Hub not found"

            # Get all bot phone numbers to avoid sending to bots themselves
            all_bot_phones = set()
            all_bots_list = db.query(BotProfile).filter(
                BotProfile.whatsapp_phone.isnot(None),
                BotProfile.whatsapp_phone != ''
            ).all()
            for bot in all_bots_list:
                normalized = ''.join(c for c in bot.whatsapp_phone if c.isdigit())
                if normalized:
                    all_bot_phones.add(normalized)
            logger.info(f"Scheduled content {content.id}: Bot phone numbers to exclude: {all_bot_phones}")

            # Parse bot_send_mode and bot_profile_ids
            bot_send_mode = content.bot_send_mode or "any"
            bot_profile_ids_list = None
            try:
                if content.bot_profile_ids:
                    bot_profile_ids_list = json.loads(content.bot_profile_ids)
            except:
                pass

            # Get all user's bots (via hub owner) instead of requiring hub membership
            # This allows dynamic bot selection without pre-configuring hub memberships
            user_bots = db.query(BotProfile.id).filter(
                BotProfile.user_id == hub.user_id
            ).all()
            user_bot_ids = [b[0] for b in user_bots]

            # Determine which bots will participate
            sending_bots = []
            if bot_send_mode == "all":
                # All user's bots will send to their own contacts
                sending_bots = user_bot_ids
                logger.info(f"Scheduled content {content.id}: Mode 'all' - using all user bots: {sending_bots}")
            elif bot_send_mode == "selected" and bot_profile_ids_list:
                # Selected bots will send to their own contacts (verified to be user's bots)
                sending_bots = [bid for bid in bot_profile_ids_list if bid in user_bot_ids]
                logger.info(f"Scheduled content {content.id}: Mode 'selected' - using selected bots: {sending_bots}")
            elif content.bot_profile_id:
                # Legacy: single bot specified
                sending_bots = [content.bot_profile_id]
                logger.info(f"Scheduled content {content.id}: Legacy mode - using single bot: {sending_bots}")
            else:
                # "any" mode - use all user's bots with fallback logic
                # Each bot sends to its OWN contacts, but we track already-sent contacts
                # to avoid duplicates and ensure all contacts are reached
                sending_bots = user_bot_ids
                logger.info(f"Scheduled content {content.id}: Mode 'any' - using all user bots with fallback: {sending_bots}")

            if not sending_bots:
                content.status = "failed"
                db.commit()
                return False, "No running bot available in this hub. Please select and start a bot first."

            # Parse multi-select JSON fields for recipients
            contact_ids_list = None
            group_ids_list = None
            try:
                if content.contact_ids:
                    contact_ids_list = json.loads(content.contact_ids)
                if content.group_ids:
                    group_ids_list = json.loads(content.group_ids)
            except:
                pass

            recipient_type = getattr(content, 'recipient_type', 'broadcast') or 'broadcast'

            # For "all" or "selected" mode, we iterate through each bot and send to their OWN contacts
            # For "any" mode with broadcast/broadcast_all, we use fallback logic:
            #   - Each bot sends to contacts it has access to
            #   - Track already-sent contacts to avoid duplicates
            #   - Continue to next bot for remaining contacts until all are covered
            total_success_count = 0
            total_errors = []

            # Track already-sent contacts/groups in "any" mode to avoid duplicates
            already_sent_chat_ids = set()

            for sending_bot_id in sending_bots:
                # Check if this bot is running and connected
                bot_instance = bot_manager.get_instance(sending_bot_id)
                if not bot_instance or not bot_instance.is_running or not bot_instance.whatsapp_connected:
                    logger.info(f"Scheduled content {content.id}: Bot {sending_bot_id} not running/connected, skipping")
                    if bot_send_mode == "any":
                        # In "any" mode, try next bot
                        continue
                    else:
                        # In "all"/"selected" mode, log but continue to other bots
                        total_errors.append(f"Bot {sending_bot_id} not running/connected")
                        continue

                # Build recipients for THIS specific bot
                recipients = []
                seen_ids = set()

                if recipient_type == 'broadcast_all':
                    # Send to ALL contacts AND ALL groups THIS bot has conversations with
                    # Get all contact conversations for this bot
                    contact_convs = db.query(Conversation).filter(
                        Conversation.bot_profile_id == sending_bot_id,
                        Conversation.is_group == False,
                        Conversation.phone.isnot(None),
                        Conversation.phone != ""
                    ).all()

                    for conv in contact_convs:
                        clean_phone = ''.join(ch for ch in conv.phone if ch.isdigit() or ch == '+')
                        if clean_phone.startswith('+'):
                            clean_phone = clean_phone[1:]
                        chat_id = f"{clean_phone}@c.us"
                        if clean_phone in all_bot_phones:
                            continue
                        if len(clean_phone) < 5:
                            continue
                        # In "any" mode, skip contacts already sent by previous bots
                        if bot_send_mode == "any" and chat_id in already_sent_chat_ids:
                            continue
                        if clean_phone and chat_id not in seen_ids:
                            seen_ids.add(chat_id)
                            recipients.append({
                                'chat_id': chat_id,
                                'name': conv.chat_name or conv.phone,
                                'is_group': False
                            })

                    # Add ALL groups this bot has access to
                    groups = db.query(Conversation).filter(
                        Conversation.bot_profile_id == sending_bot_id,
                        Conversation.is_group == True,
                        Conversation.chat_id.isnot(None)
                    ).all()

                    for g in groups:
                        # In "any" mode, skip groups already sent by previous bots
                        if bot_send_mode == "any" and g.chat_id in already_sent_chat_ids:
                            continue
                        if g.chat_id and g.chat_id not in seen_ids:
                            seen_ids.add(g.chat_id)
                            recipients.append({
                                'chat_id': g.chat_id,
                                'name': g.chat_name or g.chat_id,
                                'is_group': True
                            })

                elif recipient_type == 'groups' and group_ids_list:
                    # Send to selected groups - filter by what THIS bot has access to
                    bot_group_ids = set()
                    bot_groups = db.query(Conversation.chat_id).filter(
                        Conversation.bot_profile_id == sending_bot_id,
                        Conversation.is_group == True
                    ).all()
                    bot_group_ids = {g[0] for g in bot_groups}

                    for group_info in group_ids_list:
                        group_id = group_info.get('id') if isinstance(group_info, dict) else group_info
                        group_name = group_info.get('name', group_id) if isinstance(group_info, dict) else group_id
                        if group_id:
                            if not group_id.endswith('@g.us'):
                                group_id = f"{group_id}@g.us"
                            # Only send if this bot has access to this group
                            if group_id in bot_group_ids and group_id not in seen_ids:
                                seen_ids.add(group_id)
                                recipients.append({
                                    'chat_id': group_id,
                                    'name': group_name,
                                    'is_group': True
                                })

                elif recipient_type == 'group':
                    # Legacy: Send to a single group (check if bot has access)
                    group_id = content.group_id
                    if group_id:
                        if not group_id.endswith('@g.us'):
                            group_id = f"{group_id}@g.us"
                        # Check if this bot has this group
                        has_group = db.query(Conversation).filter(
                            Conversation.bot_profile_id == sending_bot_id,
                            Conversation.is_group == True,
                            Conversation.chat_id == group_id
                        ).first()
                        if has_group:
                            recipients.append({
                                'chat_id': group_id,
                                'name': content.group_name or group_id,
                                'is_group': True
                            })

                elif recipient_type == 'all_groups':
                    # Send to ALL groups THIS bot has access to (from conversations)
                    groups = db.query(Conversation).filter(
                        Conversation.bot_profile_id == sending_bot_id,
                        Conversation.is_group == True,
                        Conversation.chat_id.isnot(None)
                    ).all()

                    for g in groups:
                        # In "any" mode, skip groups already sent by previous bots
                        if bot_send_mode == "any" and g.chat_id in already_sent_chat_ids:
                            continue
                        if g.chat_id and g.chat_id not in seen_ids:
                            seen_ids.add(g.chat_id)
                            recipients.append({
                                'chat_id': g.chat_id,
                                'name': g.chat_name or g.chat_id,
                                'is_group': True
                            })

                elif recipient_type == 'contacts':
                    # Send to selected contacts (phone numbers or IDs)
                    logger.info(f"Scheduled content {content.id}: recipient_type='contacts', contact_ids_list={contact_ids_list}")
                    if contact_ids_list:
                        # Get this bot's valid conversations
                        valid_convs = db.query(Conversation).filter(
                            Conversation.bot_profile_id == sending_bot_id,
                            Conversation.is_group == False,
                            Conversation.phone.isnot(None),
                            Conversation.phone != ""
                        ).all()
                        valid_phone_map = {c.phone: c for c in valid_convs}
                        logger.info(f"Scheduled content {content.id}: Bot {sending_bot_id} has {len(valid_convs)} valid conversations, phones: {list(valid_phone_map.keys())}")

                        for contact_ref in contact_ids_list:
                            # contact_ref could be a phone number (string or int) or contact ID (int)
                            phone = None
                            display_name = None

                            # Helper to find phone in valid_phone_map (handles +prefix variations)
                            def find_phone_in_map(phone_val):
                                if phone_val in valid_phone_map:
                                    return phone_val
                                # Try with + prefix
                                if f"+{phone_val}" in valid_phone_map:
                                    return f"+{phone_val}"
                                # Try without + prefix
                                if phone_val.startswith('+') and phone_val[1:] in valid_phone_map:
                                    return phone_val[1:]
                                return None

                            if isinstance(contact_ref, str):
                                # It's a phone number string
                                matched_phone = find_phone_in_map(contact_ref)
                                if matched_phone:
                                    phone = matched_phone
                                    conv = valid_phone_map.get(phone)
                                    display_name = conv.chat_name if conv else phone
                            elif isinstance(contact_ref, int):
                                # Could be a phone number stored as int, or a contact ID
                                phone_str = str(contact_ref)
                                matched_phone = find_phone_in_map(phone_str)
                                if matched_phone:
                                    phone = matched_phone
                                    conv = valid_phone_map.get(phone)
                                    display_name = conv.chat_name if conv else phone
                                else:
                                    # Try as contact ID - look up in Contact table
                                    contact = db.query(Contact).filter(Contact.id == contact_ref).first()
                                    if contact:
                                        phone = contact.phone
                                        display_name = contact.display_name or contact.phone

                            if phone and phone in valid_phone_map:
                                clean_phone = ''.join(c for c in phone if c.isdigit() or c == '+')
                                if clean_phone.startswith('+'):
                                    clean_phone = clean_phone[1:]
                                chat_id = f"{clean_phone}@c.us"
                                if clean_phone in all_bot_phones:
                                    continue
                                if len(clean_phone) < 5:
                                    continue
                                # In "any" mode, skip contacts already sent by previous bots
                                if bot_send_mode == "any" and chat_id in already_sent_chat_ids:
                                    continue
                                if chat_id not in seen_ids:
                                    seen_ids.add(chat_id)
                                    recipients.append({
                                        'chat_id': chat_id,
                                        'name': display_name or phone,
                                        'is_group': False
                                    })

                elif recipient_type == 'contact' and content.contact_id:
                    # Legacy: Single contact - check if this bot has conversation
                    contact = db.query(Contact).filter(Contact.id == content.contact_id).first()
                    if contact:
                        has_conv = db.query(Conversation).filter(
                            Conversation.bot_profile_id == sending_bot_id,
                            Conversation.phone == contact.phone,
                            Conversation.is_group == False
                        ).first()
                        if has_conv:
                            clean_phone = ''.join(c for c in contact.phone if c.isdigit() or c == '+')
                            if clean_phone.startswith('+'):
                                clean_phone = clean_phone[1:]
                            chat_id = f"{clean_phone}@c.us"
                            if clean_phone not in all_bot_phones and len(clean_phone) >= 5:
                                recipients.append({
                                    'chat_id': chat_id,
                                    'name': contact.display_name or contact.phone,
                                    'is_group': False
                                })

                else:
                    # Broadcast to ALL contacts THIS bot has conversations with
                    conversations = db.query(Conversation).filter(
                        Conversation.bot_profile_id == sending_bot_id,
                        Conversation.is_group == False,
                        Conversation.phone.isnot(None),
                        Conversation.phone != ""
                    ).all()

                    for conv in conversations:
                        clean_phone = ''.join(ch for ch in conv.phone if ch.isdigit() or ch == '+')
                        if clean_phone.startswith('+'):
                            clean_phone = clean_phone[1:]
                        chat_id = f"{clean_phone}@c.us"
                        if clean_phone in all_bot_phones:
                            continue
                        if len(clean_phone) < 5:
                            continue
                        # In "any" mode, skip contacts already sent by previous bots
                        if bot_send_mode == "any" and chat_id in already_sent_chat_ids:
                            continue
                        if clean_phone and chat_id not in seen_ids:
                            seen_ids.add(chat_id)
                            recipients.append({
                                'chat_id': chat_id,
                                'name': conv.chat_name or conv.phone,
                                'is_group': False
                            })

                if bot_send_mode == "any":
                    logger.info(f"Scheduled content {content.id}: Bot {sending_bot_id} has {len(recipients)} recipients (already sent: {len(already_sent_chat_ids)})")
                else:
                    logger.info(f"Scheduled content {content.id}: Bot {sending_bot_id} has {len(recipients)} recipients")

                if not recipients:
                    if bot_send_mode == "any":
                        # Try next bot for remaining contacts
                        logger.info(f"Scheduled content {content.id}: Bot {sending_bot_id} has no new recipients, trying next bot")
                        continue
                    else:
                        # This bot has no recipients, continue to next
                        continue

                # Send messages for this bot with rate limiting
                whatsapp_message = convert_to_whatsapp_markdown(content.content)

                # Calculate rate limiting parameters
                sending_speed_mode = getattr(content, 'sending_speed_mode', 'auto') or 'auto'
                rate_params = calculate_sending_params(
                    recipient_count=len(recipients),
                    sending_speed_mode=sending_speed_mode,
                    delay_min=getattr(content, 'delay_min', None),
                    delay_max=getattr(content, 'delay_max', None),
                    batch_size=getattr(content, 'batch_size', None),
                    batch_pause=getattr(content, 'batch_pause', None)
                )

                logger.info(f"Scheduled content {content.id}: Rate limiting - mode={sending_speed_mode}, "
                           f"delay={rate_params['delay_min']}-{rate_params['delay_max']}s, "
                           f"batch_size={rate_params['batch_size']}, batch_pause={rate_params['batch_pause']}s")

                messages_in_batch = 0

                for idx, recipient in enumerate(recipients):
                    # Check for cancellation before each send
                    if self.is_cancelled(content.id):
                        logger.info(f"Scheduled content {content.id}: Cancelled during sending at recipient {idx + 1}/{len(recipients)}")
                        content.status = "cancelled"
                        db.commit()
                        # Clean up cancelled content from tracking
                        self._cancelled_content.discard(content.id)
                        return False, f"Cancelled after sending to {total_success_count} recipients"

                    try:
                        chat_id = recipient['chat_id']
                        chat_name = recipient['name']

                        logger.info(f"Scheduled content {content.id}: Sending to {chat_name} ({chat_id}) via bot {sending_bot_id} [{idx + 1}/{len(recipients)}]")

                        result = await send_whatsapp_message(
                            bot_profile_id=sending_bot_id,
                            chat_id=chat_id,
                            chat_name=chat_name,
                            message=whatsapp_message
                        )

                        if result:
                            total_success_count += 1
                            messages_in_batch += 1
                            # Track sent chat_ids in "any" mode to avoid duplicates
                            if bot_send_mode == "any":
                                already_sent_chat_ids.add(chat_id)
                            # Update contact's last_interaction_at for non-group recipients
                            if not recipient.get('is_group'):
                                phone = chat_id.replace('@c.us', '').replace('@lid', '')
                                contact_to_update = db.query(Contact).filter(
                                    Contact.hub_id == content.hub_id,
                                    Contact.phone == phone
                                ).first()
                                if contact_to_update:
                                    contact_to_update.last_interaction_at = datetime.utcnow()
                        else:
                            total_errors.append(f"Failed to send to {chat_name} via bot {sending_bot_id}")

                        # Apply rate limiting delay (except for last message)
                        if idx < len(recipients) - 1:
                            # Check for batch pause
                            if rate_params['batch_size'] and messages_in_batch >= rate_params['batch_size']:
                                batch_num = (idx + 1) // rate_params['batch_size']
                                logger.info(f"Scheduled content {content.id}: Batch {batch_num} complete, pausing for {rate_params['batch_pause']} seconds...")
                                await asyncio.sleep(rate_params['batch_pause'])
                                messages_in_batch = 0
                            else:
                                # Random delay between messages
                                delay = random.uniform(rate_params['delay_min'], rate_params['delay_max'])
                                await asyncio.sleep(delay)

                    except Exception as e:
                        total_errors.append(f"Error sending to {recipient['name']}: {str(e)}")

                # In "any" mode with broadcast/broadcast_all/all_groups, continue to next bot for remaining contacts/groups
                # For other recipient types (selected contacts, selected groups), break after first bot
                if bot_send_mode == "any" and recipient_type not in ('broadcast', 'broadcast_all', 'all_groups'):
                    break

            # Log summary for "any" mode with fallback
            if bot_send_mode == "any" and recipient_type in ('broadcast', 'broadcast_all', 'all_groups'):
                logger.info(f"Scheduled content {content.id}: 'any' mode fallback complete - sent to {len(already_sent_chat_ids)} unique recipients")

            # Build bot name for logging (before success/fail branching)
            bot_name_for_log = None
            if bot_send_mode == "all":
                bot_name_for_log = "All Bots"
            elif bot_send_mode == "any":
                bot_name_for_log = "Any Available Bot"
            elif bot_profile_ids_list:
                bot_names = []
                for bid in bot_profile_ids_list:
                    bot = db.query(BotProfile).filter(BotProfile.id == bid).first()
                    if bot:
                        bot_names.append(bot.name)
                bot_name_for_log = ", ".join(bot_names) if bot_names else None

            if total_success_count > 0:
                content.status = "sent"
                content.sent_at = datetime.utcnow()
                db.commit()
                try:
                    import asyncio
                    asyncio.ensure_future(tool_event_bus.emit("content.sent", db=db, content_id=content.id, hub_id=content.hub_id))
                except Exception:
                    pass
                # Clean up from cancelled tracking (in case it was added but we finished anyway)
                self._cancelled_content.discard(content.id)

                # Handle recurring content - create next occurrence
                if content.schedule_type == "recurring" and content.recurring_frequency:
                    await self._create_next_recurring(db, content)

                # Log successful execution to ToolMonitor
                execution_time_ms = int((time.time() - start_time) * 1000)
                ToolMonitor.log_execution(
                    db=db,
                    tool_type='scheduled_content',
                    operation='send_content',
                    hub_id=content.hub_id,
                    input_data={
                        'content_id': content.id,
                        'content_type': content.content_type,
                        'recipient_type': recipient_type,
                        'bot_send_mode': bot_send_mode,
                        'bot_name': bot_name_for_log,
                        'schedule_type': content.schedule_type or 'immediate',
                        'content_preview': content.content[:100] if content.content else None
                    },
                    output_data={
                        'success_count': total_success_count,
                        'total_recipients': len(already_sent_chat_ids) if bot_send_mode == "any" else total_success_count,
                        'errors': total_errors[:5] if total_errors else []
                    },
                    status='success',
                    execution_time_ms=execution_time_ms,
                    triggered_by='scheduled',
                    related_entity_type='scheduled_content',
                    related_entity_id=content.id
                )

                return True, f"Sent to {total_success_count} recipient(s)"
            else:
                content.status = "failed"
                db.commit()

                # Log failed execution to ToolMonitor
                execution_time_ms = int((time.time() - start_time) * 1000)
                if total_errors:
                    error_message = "; ".join(total_errors)
                elif bot_send_mode == "selected":
                    error_message = "No recipients found. The selected bot(s) have no conversations with contacts in this hub. Try using 'Any Available Bot' or select a bot that has chatted with contacts."
                else:
                    error_message = "No recipients found. Make sure the hub has contacts and the bots have conversations with them."
                ToolMonitor.log_execution(
                    db=db,
                    tool_type='scheduled_content',
                    operation='send_content',
                    hub_id=content.hub_id,
                    input_data={
                        'content_id': content.id,
                        'content_type': content.content_type,
                        'recipient_type': recipient_type,
                        'bot_send_mode': bot_send_mode,
                        'bot_name': bot_name_for_log,
                        'schedule_type': content.schedule_type or 'immediate',
                        'content_preview': content.content[:100] if content.content else None
                    },
                    output_data={
                        'success_count': 0,
                        'errors': total_errors[:5] if total_errors else []
                    },
                    status='error',
                    error_message=error_message,
                    execution_time_ms=execution_time_ms,
                    triggered_by='scheduled',
                    related_entity_type='scheduled_content',
                    related_entity_id=content.id
                )

                return False, error_message

        except Exception as e:
            content.status = "failed"
            db.commit()

            # Log exception to ToolMonitor
            execution_time_ms = int((time.time() - start_time) * 1000)
            ToolMonitor.log_execution(
                db=db,
                tool_type='scheduled_content',
                operation='send_content',
                hub_id=content.hub_id,
                input_data={
                    'content_id': content.id,
                    'content_type': getattr(content, 'content_type', None),
                    'bot_send_mode': getattr(content, 'bot_send_mode', None),
                    'schedule_type': getattr(content, 'schedule_type', 'immediate'),
                    'content_preview': content.content[:100] if content.content else None
                },
                status='error',
                error_message=str(e),
                execution_time_ms=execution_time_ms,
                triggered_by='scheduled',
                related_entity_type='scheduled_content',
                related_entity_id=content.id
            )

            return False, str(e)

    async def _create_next_recurring(self, db: Session, content: ScheduledContent):
        """Create the next occurrence for recurring content."""
        try:
            from datetime import timedelta

            # Calculate next scheduled time based on frequency
            current_scheduled = content.scheduled_for or datetime.utcnow()
            frequency = content.recurring_frequency

            if frequency == "daily":
                next_scheduled = current_scheduled + timedelta(days=1)
            elif frequency == "weekly":
                next_scheduled = current_scheduled + timedelta(weeks=1)
            elif frequency == "monthly":
                # Add roughly a month (30 days)
                next_scheduled = current_scheduled + timedelta(days=30)
            else:
                logger.info(f"Unknown recurring frequency: {frequency}")
                return

            # Check if next occurrence is past the end date
            if content.recurring_end_date and next_scheduled > content.recurring_end_date:
                logger.info(f"Recurring content {content.id}: Next occurrence {next_scheduled} is past end date {content.recurring_end_date}, not creating")
                return

            # Create new content for next occurrence
            next_content = ScheduledContent(
                hub_id=content.hub_id,
                bot_profile_id=content.bot_profile_id,
                bot_profile_ids=content.bot_profile_ids,
                bot_send_mode=content.bot_send_mode,
                contact_id=content.contact_id,
                content=content.content,
                content_type=content.content_type,
                topic=content.topic,
                scheduled_for=next_scheduled,
                schedule_type="recurring",
                recurring_frequency=content.recurring_frequency,
                recurring_time=content.recurring_time,
                recurring_start_date=content.recurring_start_date,
                recurring_end_date=content.recurring_end_date,
                recipient_type=content.recipient_type,
                group_id=content.group_id,
                group_name=content.group_name,
                contact_ids=content.contact_ids,
                group_ids=content.group_ids,
                status="pending",
                # Preserve rate limiting settings
                sending_speed_mode=content.sending_speed_mode,
                delay_min=content.delay_min,
                delay_max=content.delay_max,
                batch_size=content.batch_size,
                batch_pause=content.batch_pause
            )
            db.add(next_content)
            db.commit()
            logger.info(f"Recurring content {content.id}: Created next occurrence {next_content.id} scheduled for {next_scheduled}")

        except Exception as e:
            logger.error(f"Error creating next recurring content: {e}")

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
