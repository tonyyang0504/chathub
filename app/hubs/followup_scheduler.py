"""
Follow-up Send Scheduler

Background task that monitors contact_followup hubs with auto_send_enabled
and automatically sends follow-up messages to matching contacts.
"""
import asyncio
import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional, Set

from app.database import SessionLocal, Contact, Hub, AIAgent, Conversation, BotProfile, HubBotMembership
from app.tools.monitoring import ToolMonitor
from app.tools.event_bus import tool_event_bus

logger = logging.getLogger(__name__)

# Thread pool for blocking AI calls
_ai_executor = ThreadPoolExecutor(max_workers=2)


class FollowupSendScheduler:
    """Background scheduler that auto-sends follow-up messages for enabled hubs."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 10  # Main loop sleep (seconds)
        self._processing_hubs: Set[int] = set()  # Hub IDs currently being processed

    async def start(self):
        """Start the scheduler."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Follow-up send scheduler started.")

    async def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._processing_hubs.clear()
        logger.info("Follow-up send scheduler stopped.")

    async def _run_loop(self):
        """Main scheduler loop — checks for hubs that need processing."""
        while self._running:
            try:
                await self._check_hubs()
            except Exception as e:
                logger.error(f"Error in followup send scheduler loop: {e}")
            await asyncio.sleep(self._check_interval)

    async def _check_hubs(self):
        """Find auto_send_enabled hubs and process them if interval has elapsed."""
        db = SessionLocal()
        try:
            hubs = db.query(Hub).filter(
                Hub.auto_send_enabled == True,
                Hub.task_type == "contact_followup"
            ).all()

            for hub in hubs:
                if hub.id in self._processing_hubs:
                    continue

                interval_minutes = hub.auto_send_interval_minutes or 15
                if hub.auto_send_last_run:
                    elapsed = (datetime.utcnow() - hub.auto_send_last_run).total_seconds() / 60
                    if elapsed < interval_minutes:
                        continue

                # Spawn processing task for this hub
                self._processing_hubs.add(hub.id)
                asyncio.create_task(self._process_hub(hub.id, hub.user_id))
        except Exception as e:
            logger.error(f"Error checking followup send hubs: {e}")
        finally:
            db.close()

    async def _process_hub(self, hub_id: int, user_id: int):
        """Process a single hub — find matching contacts and send follow-ups."""
        from app.bots.manager import BotManager
        from app.bots.whatsapp_bot import send_whatsapp_message
        from app.auth.utils import decrypt_string
        from app.ai.providers import get_ai_provider

        db = SessionLocal()
        try:
            hub = db.query(Hub).filter(Hub.id == hub_id).first()
            if not hub or not hub.auto_send_enabled:
                return

            # --- Resolve AI config (agent → hub → any user hub) ---
            followup_agent = db.query(AIAgent).filter(
                AIAgent.hub_id == hub_id,
                AIAgent.agent_type.in_(["followup", "generator"]),
                AIAgent.is_active == True
            ).first()

            api_key = None
            if followup_agent and followup_agent.api_key_encrypted:
                api_key = decrypt_string(followup_agent.api_key_encrypted)
            if not api_key and hub.api_key_encrypted:
                api_key = decrypt_string(hub.api_key_encrypted)
            if not api_key:
                for h in db.query(Hub).filter(Hub.user_id == user_id).all():
                    if h.api_key_encrypted:
                        api_key = decrypt_string(h.api_key_encrypted)
                        break

            if not api_key:
                logger.warning(f"Hub {hub_id}: no API key — skipping auto-send")
                return

            ai_provider_name = "openai"
            ai_model = "gpt-4o-mini"
            if followup_agent and followup_agent.ai_provider:
                ai_provider_name = followup_agent.ai_provider
            elif hub.ai_provider:
                ai_provider_name = hub.ai_provider
            if followup_agent and followup_agent.model:
                ai_model = followup_agent.model
            elif hub.model:
                ai_model = hub.model

            additional_instructions = ""
            if followup_agent and followup_agent.additional_instructions:
                additional_instructions = followup_agent.additional_instructions

            provider = get_ai_provider(ai_provider_name, api_key, ai_model)
            bot_manager = BotManager()
            tone = hub.auto_send_tone or "friendly"

            # --- Parse filters ---
            filters = {}
            if hub.auto_send_filters:
                try:
                    filters = json.loads(hub.auto_send_filters)
                except (json.JSONDecodeError, TypeError):
                    pass

            # --- Parse selected bot IDs ---
            selected_bot_ids = []
            if hub.auto_send_bot_ids:
                try:
                    selected_bot_ids = json.loads(hub.auto_send_bot_ids)
                except (json.JSONDecodeError, TypeError):
                    pass

            # --- Build contact query ---
            # contact_followup hubs aggregate from all contact_analyzer hubs
            user_hub_ids = [h.id for h in db.query(Hub).filter(Hub.user_id == user_id).all()]
            ca_hub_ids = [h.id for h in db.query(Hub).filter(
                Hub.id.in_(user_hub_ids),
                Hub.task_type == "contact_analyzer"
            ).all()]

            if not ca_hub_ids:
                logger.info(f"Hub {hub_id}: no contact_analyzer hubs — skipping")
                return

            from sqlalchemy import func
            from app.database import Message

            query = db.query(Contact).filter(
                Contact.follow_up_needed == True,
                (Contact.followup_status.is_(None)) | (Contact.followup_status == 'pending'),
                Contact.hub_id.in_(ca_hub_ids)
            )

            urgency = filters.get("urgency", "")
            sentiment = filters.get("sentiment", "")
            engagement = filters.get("engagement", "")
            date_filter = filters.get("date", "")

            if urgency:
                query = query.filter(Contact.urgency == urgency)
            if sentiment:
                query = query.filter(Contact.sentiment == sentiment)

            if engagement == "high":
                query = query.filter(Contact.engagement_score >= 70)
            elif engagement == "medium":
                query = query.filter(Contact.engagement_score >= 40, Contact.engagement_score <= 69)
            elif engagement == "low":
                query = query.filter(Contact.engagement_score <= 39)

            # Filter contacts to those with conversations through selected bots
            if selected_bot_ids:
                phones_with_bots = db.query(Conversation.phone).filter(
                    Conversation.bot_profile_id.in_(selected_bot_ids),
                    Conversation.is_group == False
                ).distinct().subquery()
                query = query.filter(Contact.phone.in_(phones_with_bots))

            if date_filter:
                now = datetime.utcnow()
                last_msg_subq = db.query(
                    Conversation.phone,
                    func.max(Message.timestamp).label('last_msg_at')
                ).join(Message, Message.conversation_id == Conversation.id).filter(
                    Conversation.is_group == False
                ).group_by(Conversation.phone).subquery()
                query = query.outerjoin(last_msg_subq, Contact.phone == last_msg_subq.c.phone)

                if date_filter == "24h":
                    query = query.filter(last_msg_subq.c.last_msg_at >= now - timedelta(hours=24))
                elif date_filter == "7d":
                    query = query.filter(last_msg_subq.c.last_msg_at >= now - timedelta(days=7))
                elif date_filter == "30d":
                    query = query.filter(last_msg_subq.c.last_msg_at >= now - timedelta(days=30))
                elif date_filter == "over7d":
                    query = query.filter(last_msg_subq.c.last_msg_at <= now - timedelta(days=7))
                elif date_filter == "over30d":
                    query = query.filter(last_msg_subq.c.last_msg_at <= now - timedelta(days=30))
                elif date_filter == "custom":
                    custom_days = int(filters.get("custom_days", 7))
                    direction = filters.get("custom_days_direction", "over")
                    cutoff = now - timedelta(days=custom_days)
                    if direction == "within":
                        query = query.filter(last_msg_subq.c.last_msg_at >= cutoff)
                    else:
                        query = query.filter(last_msg_subq.c.last_msg_at <= cutoff)

            contacts = query.all()
            if not contacts:
                hub.auto_send_last_run = datetime.utcnow()
                db.commit()
                logger.info(f"Hub {hub_id}: no matching contacts for auto-send")
                return

            # Calculate rate limiting parameters
            from app.hubs.scheduler import calculate_sending_params
            speed_mode = getattr(hub, 'auto_send_speed_mode', 'auto') or 'auto'
            rate_params = calculate_sending_params(
                recipient_count=len(contacts),
                sending_speed_mode=speed_mode,
                delay_min=getattr(hub, 'auto_send_delay_min', None),
                delay_max=getattr(hub, 'auto_send_delay_max', None),
                batch_size=getattr(hub, 'auto_send_batch_size', None),
                batch_pause=getattr(hub, 'auto_send_batch_pause', None)
            )

            logger.info(f"Hub {hub_id}: auto-sending to {len(contacts)} contacts "
                        f"(speed={speed_mode}, delay={rate_params['delay_min']}-{rate_params['delay_max']}s, "
                        f"batch_size={rate_params['batch_size']}, batch_pause={rate_params['batch_pause']}s)")
            sent_count = 0
            failed_count = 0
            messages_in_batch = 0

            for idx, contact in enumerate(contacts):
                if not self._running:
                    break
                # Re-check hub is still enabled
                db.refresh(hub)
                if not hub.auto_send_enabled:
                    break

                try:
                    # Find running bot with conversation history
                    conv_bot_ids = [bid[0] for bid in db.query(Conversation.bot_profile_id).filter(
                        Conversation.phone == contact.phone,
                        Conversation.is_group == False
                    ).distinct().all()]

                    # Restrict to selected bots if configured
                    if selected_bot_ids:
                        conv_bot_ids = [bid for bid in conv_bot_ids if bid in selected_bot_ids]

                    selected_bot_id = None
                    for bid in conv_bot_ids:
                        bot = db.query(BotProfile).filter(
                            BotProfile.id == bid,
                            BotProfile.user_id == user_id
                        ).first()
                        if bot:
                            instance = bot_manager.get_instance(bid)
                            if instance and instance.is_running:
                                selected_bot_id = bid
                                break

                    if not selected_bot_id:
                        continue

                    # Build follow-up message
                    key_topics = []
                    if contact.key_topics:
                        try:
                            key_topics = json.loads(contact.key_topics) if isinstance(contact.key_topics, str) else contact.key_topics
                        except Exception:
                            key_topics = []

                    tags = [t.tag for t in contact.tags] if contact.tags else []

                    system_prompt = f"""You are generating a follow-up message for a WhatsApp contact.

CONTACT PROFILE:
- Name: {contact.display_name or 'Unknown'}
- Description: {contact.description or 'No description available'}
- Predicted Intent: {contact.predicted_intent or 'Unknown'}
- Follow-up Reason: {contact.follow_up_reason or 'General follow-up'}
- Key Topics: {', '.join(key_topics) if key_topics else 'None identified'}
- Sentiment: {contact.sentiment or 'neutral'}
- Urgency: {contact.urgency or 'low'}
- Engagement Score: {contact.engagement_score or 0}%
- Tags: {', '.join(tags) if tags else 'None'}
- Last Interaction: {contact.last_interaction_at.strftime('%Y-%m-%d') if contact.last_interaction_at else 'Unknown'}

Generate a personalized, natural follow-up message that:
1. References their specific situation from the description
2. Addresses their predicted intent
3. Matches their sentiment (warm for positive, professional for neutral, empathetic for negative)
4. Is appropriately urgent based on urgency level
5. Uses a {tone} tone
6. Is concise and actionable (2-4 sentences)
7. Does not use markdown formatting (no asterisks, underscores, etc.)

Return only the message text, no explanations or quotes."""

                    if additional_instructions:
                        system_prompt += f"\n\nAdditional Instructions:\n{additional_instructions}"

                    is_reasoning_model = ai_model.startswith(('gpt-5', 'o1', 'o3'))
                    max_tokens = 4000 if is_reasoning_model else 300

                    loop = asyncio.get_event_loop()
                    ai_response = await loop.run_in_executor(
                        _ai_executor,
                        lambda: provider.chat_completion(
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": "Generate a follow-up message for this contact."}
                            ],
                            max_tokens=max_tokens,
                            temperature=0.7
                        )
                    )

                    generated_message = ai_response.content.strip()
                    if generated_message.startswith('"') and generated_message.endswith('"'):
                        generated_message = generated_message[1:-1]

                    # Send via bot
                    chat_id = f"{contact.phone}@c.us" if not contact.phone.endswith("@c.us") else contact.phone
                    chat_name = contact.display_name or contact.phone
                    success = await send_whatsapp_message(selected_bot_id, chat_id, chat_name, generated_message)

                    if success:
                        contact.followup_status = 'sent'
                        contact.followup_sent_at = datetime.utcnow()
                        contact.followup_message = generated_message
                        contact.followup_attempts = (contact.followup_attempts or 0) + 1
                        contact.followup_last_attempt_at = datetime.utcnow()
                        db.commit()
                        try:
                            asyncio.ensure_future(tool_event_bus.emit("followup.sent", db=db, contact_id=contact.id, hub_id=hub_id))
                        except Exception:
                            pass

                        ToolMonitor.log_execution(
                            db=db,
                            tool_type="contact_followup",
                            operation="send_followup",
                            hub_id=contact.hub_id,
                            user_id=user_id,
                            input_data={"contact_id": contact.id, "bot_id": selected_bot_id, "message_length": len(generated_message)},
                            output_data={"status": "sent", "contact_phone": contact.phone},
                            status="success",
                            triggered_by="auto_send"
                        )
                        sent_count += 1
                        messages_in_batch += 1
                    else:
                        failed_count += 1
                        ToolMonitor.log_execution(
                            db=db,
                            tool_type="contact_followup",
                            operation="send_followup",
                            hub_id=contact.hub_id,
                            user_id=user_id,
                            input_data={"contact_id": contact.id, "bot_id": selected_bot_id},
                            output_data={"error": "send_whatsapp_message returned False"},
                            status="error",
                            error_message="Failed to send message via bot",
                            triggered_by="auto_send"
                        )

                except Exception as e:
                    failed_count += 1
                    logger.error(f"Hub {hub_id}: error sending to contact {contact.id}: {e}")
                    ToolMonitor.log_execution(
                        db=db,
                        tool_type="contact_followup",
                        operation="send_followup",
                        hub_id=contact.hub_id if contact else hub_id,
                        user_id=user_id,
                        input_data={"contact_id": contact.id},
                        output_data={"error": str(e)},
                        status="error",
                        error_message=str(e),
                        triggered_by="auto_send"
                    )

                # Rate limiting delay (except after last contact)
                if idx < len(contacts) - 1:
                    if rate_params['batch_size'] and messages_in_batch >= rate_params['batch_size']:
                        batch_num = (idx + 1) // rate_params['batch_size']
                        logger.info(f"Hub {hub_id}: Batch {batch_num} complete, pausing for {rate_params['batch_pause']}s")
                        await asyncio.sleep(rate_params['batch_pause'])
                        messages_in_batch = 0
                    else:
                        delay = random.uniform(rate_params['delay_min'], rate_params['delay_max'])
                        await asyncio.sleep(delay)

            # Update last run
            hub.auto_send_last_run = datetime.utcnow()
            db.commit()
            logger.info(f"Hub {hub_id}: auto-send complete — {sent_count} sent, {failed_count} failed")

        except Exception as e:
            logger.error(f"Error processing hub {hub_id} for auto-send: {e}")
        finally:
            db.close()
            self._processing_hubs.discard(hub_id)


# Global scheduler instance
followup_send_scheduler = FollowupSendScheduler()
