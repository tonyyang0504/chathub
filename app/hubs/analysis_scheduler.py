"""
Contact Analysis Scheduler

Background task that processes queued contact analyses with rate limiting.
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional, Dict, Set, List

from sqlalchemy.orm import Session

from app.database import SessionLocal, Contact, Hub, AIAgent, Conversation, Message, ContactTag
from app.tools.monitoring import ToolMonitor

logger = logging.getLogger(__name__)


class ContactAnalysisScheduler:
    """Background scheduler for processing contact analysis queue."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 5  # Check every 5 seconds for pending contacts
        self._active_analyses: Dict[int, asyncio.Task] = {}  # contact_id -> task
        self._cancelled_contacts: Set[int] = set()  # Set of cancelled contact IDs
        self._cancelled_hubs: Set[int] = set()  # Set of hub IDs with cancelled bulk analysis
        self._analysis_delay = 2  # Seconds between contact analyses (rate limiting)

    async def start(self):
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Contact analysis scheduler started.")

    async def stop(self):
        """Stop the scheduler."""
        self._running = False

        # Cancel all active analyses
        for contact_id, task in self._active_analyses.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._active_analyses.clear()
        self._cancelled_contacts.clear()
        self._cancelled_hubs.clear()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Contact analysis scheduler stopped.")

    def queue_contact(self, db: Session, contact_id: int) -> bool:
        """Queue a single contact for analysis."""
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        if not contact:
            return False

        # Don't queue if already pending or analyzing
        if contact.analysis_status in ("pending", "analyzing"):
            return False

        # Clear cancellation state for this contact and hub
        # This allows re-queuing after a previous cancellation
        self._cancelled_contacts.discard(contact_id)
        self._cancelled_hubs.discard(contact.hub_id)

        contact.analysis_status = "pending"
        contact.analysis_queued_at = datetime.utcnow()
        db.commit()
        logger.info(f"Queued contact {contact_id} for analysis")
        return True

    def queue_contacts(self, db: Session, hub_id: int, contact_ids: List[int] = None) -> int:
        """Queue multiple contacts for analysis. Returns count of queued contacts."""
        # Remove hub from cancelled set if re-queuing
        self._cancelled_hubs.discard(hub_id)

        if contact_ids:
            # Queue specific contacts
            contacts = db.query(Contact).filter(
                Contact.id.in_(contact_ids),
                Contact.hub_id == hub_id
            ).all()
        else:
            # Queue all contacts in hub
            contacts = db.query(Contact).filter(Contact.hub_id == hub_id).all()

        queued_count = 0
        for contact in contacts:
            # Skip if already pending or analyzing
            if contact.analysis_status in ("pending", "analyzing"):
                continue

            # Remove from cancelled set if re-queuing
            self._cancelled_contacts.discard(contact.id)

            contact.analysis_status = "pending"
            contact.analysis_queued_at = datetime.utcnow()
            queued_count += 1

        db.commit()
        logger.info(f"Queued {queued_count} contacts for analysis in hub {hub_id}")
        return queued_count

    def cancel_contact(self, contact_id: int):
        """Cancel analysis for a single contact."""
        self._cancelled_contacts.add(contact_id)
        if contact_id in self._active_analyses:
            self._active_analyses[contact_id].cancel()
            logger.info(f"Cancelled active analysis for contact {contact_id}")
        else:
            logger.info(f"Marked contact {contact_id} for cancellation")

    def cancel_all_pending(self, db: Session, hub_id: int) -> int:
        """Cancel all pending analyses for a hub. Returns count of cancelled."""
        self._cancelled_hubs.add(hub_id)

        # Cancel any active analyses for contacts in this hub
        contacts = db.query(Contact).filter(
            Contact.hub_id == hub_id,
            Contact.analysis_status.in_(["pending", "analyzing"])
        ).all()

        cancelled_count = 0
        for contact in contacts:
            self._cancelled_contacts.add(contact.id)
            if contact.id in self._active_analyses:
                self._active_analyses[contact.id].cancel()

            if contact.analysis_status == "pending":
                contact.analysis_status = "cancelled"
                cancelled_count += 1
            elif contact.analysis_status == "analyzing":
                # Will be cancelled when the task checks
                cancelled_count += 1

        db.commit()
        logger.info(f"Cancelled {cancelled_count} pending analyses for hub {hub_id}")
        return cancelled_count

    def is_cancelled(self, contact_id: int, hub_id: int = None) -> bool:
        """Check if a contact's analysis has been cancelled."""
        if contact_id in self._cancelled_contacts:
            return True
        if hub_id and hub_id in self._cancelled_hubs:
            return True
        return False

    def get_queue_status(self, db: Session, hub_id: int) -> dict:
        """Get the current queue status for a hub."""
        pending = db.query(Contact).filter(
            Contact.hub_id == hub_id,
            Contact.analysis_status == "pending"
        ).count()

        analyzing = db.query(Contact).filter(
            Contact.hub_id == hub_id,
            Contact.analysis_status == "analyzing"
        ).count()

        return {
            "pending": pending,
            "analyzing": analyzing,
            "total_queued": pending + analyzing
        }

    async def _run_loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                await self._process_pending_contacts()
            except Exception as e:
                logger.error(f"Error in contact analysis scheduler: {e}")

            await asyncio.sleep(self._check_interval)

    async def _process_pending_contacts(self):
        """Process contacts that are pending analysis."""
        db = SessionLocal()
        try:
            # Find the next pending contact (oldest first)
            contact = db.query(Contact).filter(
                Contact.analysis_status == "pending"
            ).order_by(Contact.analysis_queued_at.asc()).first()

            if not contact:
                return

            # Skip if cancelled
            if self.is_cancelled(contact.id, contact.hub_id):
                contact.analysis_status = "cancelled"
                db.commit()
                self._cancelled_contacts.discard(contact.id)
                return

            # Skip if already being processed
            if contact.id in self._active_analyses:
                return

            # Mark as analyzing
            contact.analysis_status = "analyzing"
            db.commit()

            logger.info(f"Starting analysis for contact {contact.id} ({contact.display_name or contact.phone})")

            # Start analysis task
            task = asyncio.create_task(
                self._analyze_contact(contact.id, contact.hub_id)
            )
            self._active_analyses[contact.id] = task

        except Exception as e:
            logger.error(f"Error processing pending contacts: {e}")
        finally:
            db.close()

    async def _analyze_contact(self, contact_id: int, hub_id: int):
        """Perform analysis for a single contact."""
        from app.hubs.agents.analyzer import AnalyzerAgent
        from app.auth.utils import decrypt_string
        from concurrent.futures import ThreadPoolExecutor

        db = SessionLocal()
        start_time = time.time()

        try:
            # Check cancellation
            if self.is_cancelled(contact_id, hub_id):
                contact = db.query(Contact).filter(Contact.id == contact_id).first()
                if contact:
                    contact.analysis_status = "cancelled"
                    db.commit()
                self._cancelled_contacts.discard(contact_id)
                return

            contact = db.query(Contact).filter(Contact.id == contact_id).first()
            if not contact:
                logger.error(f"Contact {contact_id} not found")
                return

            hub = db.query(Hub).filter(Hub.id == hub_id).first()
            if not hub:
                contact.analysis_status = "failed"
                db.commit()
                return

            # Check if hub has an analyzer agent
            analyzer_agent = db.query(AIAgent).filter(
                AIAgent.hub_id == hub_id,
                AIAgent.agent_type == "analyzer",
                AIAgent.is_active == True
            ).first()

            # Get API key
            api_key = None
            if analyzer_agent and analyzer_agent.api_key_encrypted:
                api_key = decrypt_string(analyzer_agent.api_key_encrypted)
            elif hub.api_key_encrypted:
                api_key = decrypt_string(hub.api_key_encrypted)

            if not api_key:
                contact.analysis_status = "failed"
                db.commit()
                logger.error(f"No API key for contact {contact_id}")
                return

            # Get conversation history
            conversations = db.query(Conversation).filter(
                Conversation.phone == contact.phone
            ).all()

            messages = []
            bot_names = set()
            found_profile_pic = None

            for conv in conversations:
                if conv.bot_profile:
                    bot_names.add(conv.bot_profile.name)

                if not contact.profile_pic and not found_profile_pic and conv.profile_pic:
                    found_profile_pic = conv.profile_pic

                conv_messages = db.query(Message).filter(
                    Message.conversation_id == conv.id
                ).order_by(Message.timestamp.asc()).limit(100).all()

                for msg in conv_messages:
                    messages.append({
                        "role": msg.role or "user",
                        "content": msg.content or "",
                        "timestamp": msg.timestamp.isoformat() if msg.timestamp else None
                    })
                    if not contact.profile_pic and not found_profile_pic and msg.role == "user" and msg.sender_profile_pic:
                        found_profile_pic = msg.sender_profile_pic

            # Update profile pic if found
            if found_profile_pic and not contact.profile_pic:
                contact.profile_pic = found_profile_pic

            # Check cancellation again before AI call
            if self.is_cancelled(contact_id, hub_id):
                contact.analysis_status = "cancelled"
                db.commit()
                self._cancelled_contacts.discard(contact_id)
                return

            # Get existing tags
            existing_tags = db.query(ContactTag).filter(
                ContactTag.contact_id == contact_id
            ).all()
            existing_tag_names = [t.tag for t in existing_tags]

            # Prepare input data for analysis
            input_data = {
                "contact": {
                    "phone": contact.phone,
                    "display_name": contact.display_name,
                    "existing_tags": existing_tag_names,
                    "engagement_score": contact.engagement_score,
                    "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None
                },
                "messages": messages,
                "context": {
                    "hub_name": hub.name,
                    "bot_names": list(bot_names)
                }
            }

            # Run AI analysis - use AnalyzerAgent if configured, otherwise use direct AI call
            if analyzer_agent:
                # Use the AnalyzerAgent class with its better prompt
                from app.hubs.agents.analyzer import AnalyzerAgent

                agent = AnalyzerAgent(
                    agent=analyzer_agent,
                    hub_api_key=hub.api_key_encrypted,
                    hub_ai_provider=hub.ai_provider
                )

                # Run in executor to not block
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = await loop.run_in_executor(
                        executor,
                        lambda: agent.run(input_data, trigger_type="queued")
                    )
            else:
                # Fallback: Direct AI call with improved prompt
                from app.ai import get_ai_provider

                provider = get_ai_provider(
                    provider_name=hub.ai_provider or "openai",
                    api_key=api_key,
                    model=hub.model or "gpt-4o-mini"
                )

                system_prompt = """You are a contact analyzer AI. Analyze conversation history to build a comprehensive contact profile.

IMPORTANT: You MUST include ALL fields in your response.

Your analysis should include:
1. **Tags**: Generate 2-5 relevant tags based on interests, behaviors, topics discussed. Examples: "interested_in_product", "price_conscious", "tech_savvy", "quick_responder", "new_customer"
2. **Engagement Score**: 0-100 based on message frequency, response patterns, and interaction quality
3. **Predicted Intent**: A SPECIFIC intent derived from analyzing the conversation (e.g., "inquire_about_pricing", "schedule_appointment", "request_product_demo", "seek_technical_support", "explore_partnership"). DO NOT use generic values - the intent should reflect what this specific contact is actually seeking.
4. **Sentiment**: Overall sentiment (positive, neutral, negative)
5. **Urgency**: How urgent is follow-up needed (low, medium, high)
6. **Follow-up Needed**: Whether this contact needs follow-up and why
7. **Description**: A detailed profile summary (2-3 sentences) describing the contact's behavior, interests, and characteristics
8. **Key Topics**: Main topics discussed in the conversation

Return a JSON object with this EXACT structure:
{
    "tags": [
        {"tag": "example_tag", "confidence": 0.85, "value": null}
    ],
    "engagement_score": 75,
    "predicted_intent": "[ANALYZE FROM CONVERSATION]",
    "sentiment": "positive",
    "urgency": "medium",
    "follow_up_needed": true,
    "follow_up_reason": "Reason for follow-up",
    "description": "Detailed profile summary describing the contact",
    "key_topics": ["topic1", "topic2"]
}

CRITICAL: Generate a SPECIFIC predicted_intent based on conversation analysis. The "tags" array MUST contain at least 2 relevant tags. Description MUST be 2-3 sentences.
Return ONLY valid JSON."""

                message_history = "\n".join([
                    f"[{m.get('timestamp', '')}] {m['role'].upper()}: {m['content']}"
                    for m in messages[-50:]
                ])

                user_message = f"""Analyze this contact's conversation history and return your analysis as JSON:

Phone: {contact.phone}
Name: {contact.display_name or 'Unknown'}
Existing Tags: {', '.join(existing_tag_names) if existing_tag_names else 'None'}

Conversation History:
{message_history if message_history else 'No messages available'}

Provide a comprehensive analysis in JSON format."""

                # Run in executor to not block
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    ai_response = await loop.run_in_executor(
                        executor,
                        lambda: provider.chat_completion(
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_message}
                            ],
                            max_tokens=1000,
                            temperature=0.3,
                            json_mode=True
                        )
                    )

                result = json.loads(ai_response.content)
                result["tokens_used"] = ai_response.usage.get("total_tokens", 0)

            # Check cancellation before updating
            if self.is_cancelled(contact_id, hub_id):
                contact.analysis_status = "cancelled"
                db.commit()
                self._cancelled_contacts.discard(contact_id)
                return

            # Update contact with results
            if result.get("description"):
                desc = result["description"]
                if isinstance(desc, dict):
                    desc = desc.get("text") or desc.get("summary") or str(desc)
                contact.description = str(desc) if desc else None

            if result.get("predicted_intent"):
                intent = result["predicted_intent"]
                if isinstance(intent, dict):
                    intent = intent.get("intent") or intent.get("type") or intent.get("value") or list(intent.values())[0]
                contact.predicted_intent = str(intent) if intent else None

            if result.get("engagement_score") is not None:
                score = result["engagement_score"]
                if isinstance(score, dict):
                    score = score.get("score") or score.get("value") or 50
                if score > 1:
                    score = score / 100.0
                contact.engagement_score = min(1.0, max(0.0, float(score)))

            if result.get("sentiment"):
                sentiment = result["sentiment"]
                if isinstance(sentiment, dict):
                    sentiment = sentiment.get("value") or sentiment.get("sentiment") or str(sentiment)
                contact.sentiment = str(sentiment) if sentiment else None

            if result.get("urgency"):
                urgency = result["urgency"]
                if isinstance(urgency, dict):
                    urgency = urgency.get("value") or urgency.get("level") or str(urgency)
                contact.urgency = str(urgency) if urgency else None

            follow_up = result.get("follow_up_needed", False)
            if isinstance(follow_up, bool):
                contact.follow_up_needed = follow_up
            elif isinstance(follow_up, str):
                contact.follow_up_needed = follow_up.lower() in ("true", "yes", "1")
            else:
                contact.follow_up_needed = bool(follow_up)

            if result.get("follow_up_reason"):
                reason = result["follow_up_reason"]
                if isinstance(reason, dict):
                    reason = reason.get("reason") or reason.get("text") or str(reason)
                contact.follow_up_reason = str(reason) if reason else None

            if result.get("key_topics"):
                topics = result["key_topics"]
                if isinstance(topics, list):
                    topics = [str(t) if not isinstance(t, str) else t for t in topics]
                    contact.key_topics = json.dumps(topics)
                elif isinstance(topics, str):
                    contact.key_topics = topics

            contact.updated_at = datetime.utcnow()
            contact.last_interaction_at = datetime.utcnow()

            # Replace AI-generated tags
            db.query(ContactTag).filter(
                ContactTag.contact_id == contact_id,
                ContactTag.source == "ai_analyzer"
            ).delete()

            new_tags_added = 0
            if result.get("tags"):
                for tag_data in result["tags"]:
                    tag_name = tag_data.get("tag") if isinstance(tag_data, dict) else str(tag_data)
                    if not tag_name:
                        continue

                    existing = db.query(ContactTag).filter(
                        ContactTag.contact_id == contact_id,
                        ContactTag.tag == tag_name
                    ).first()

                    if not existing:
                        new_tag = ContactTag(
                            contact_id=contact_id,
                            tag=tag_name,
                            value=tag_data.get("value") if isinstance(tag_data, dict) else None,
                            confidence=tag_data.get("confidence", 1.0) if isinstance(tag_data, dict) else 1.0,
                            source="ai_analyzer"
                        )
                        db.add(new_tag)
                        new_tags_added += 1

            contact.analysis_status = "completed"
            db.commit()

            # Log execution
            execution_time_ms = int((time.time() - start_time) * 1000)
            result_tags = result.get("tags", [])
            tag_names = []
            for tag_data in result_tags[:10]:
                if isinstance(tag_data, dict):
                    tag_name = tag_data.get("tag") or tag_data.get("name") or ""
                    if tag_name:
                        tag_names.append(str(tag_name))
                elif isinstance(tag_data, str) and tag_data:
                    tag_names.append(tag_data)

            ToolMonitor.log_execution(
                db=db,
                tool_type="contact_analyzer",
                operation="analyze_contact",
                hub_id=hub_id,
                input_data={
                    "contact_id": contact_id,
                    "contact_phone": contact.phone,
                    "contact_name": contact.display_name,
                    "contact_profile_pic": contact.profile_pic,
                    "message_count": len(messages),
                    "bot_names": list(bot_names)
                },
                output_data={
                    "predicted_intent": result.get("predicted_intent"),
                    "sentiment": result.get("sentiment"),
                    "urgency": result.get("urgency"),
                    "engagement_score": result.get("engagement_score"),
                    "tags": tag_names,
                    "tags_count": len(result_tags),
                    "new_tags_added": new_tags_added,
                    "follow_up_needed": result.get("follow_up_needed"),
                    "follow_up_reason": result.get("follow_up_reason"),
                    "key_topics": result.get("key_topics", []),
                    "description": result.get("description")
                },
                status="success",
                execution_time_ms=execution_time_ms,
                tokens_used=result.get("tokens_used", 0),
                triggered_by="queued",
                related_entity_type="contact",
                related_entity_id=contact_id
            )

            logger.info(f"Completed analysis for contact {contact_id}")

            # Rate limiting delay before next analysis
            await asyncio.sleep(self._analysis_delay)

        except asyncio.CancelledError:
            logger.info(f"Analysis cancelled for contact {contact_id}")
            contact = db.query(Contact).filter(Contact.id == contact_id).first()
            if contact:
                contact.analysis_status = "cancelled"
                db.commit()
            raise

        except Exception as e:
            logger.error(f"Error analyzing contact {contact_id}: {e}")
            contact = db.query(Contact).filter(Contact.id == contact_id).first()
            if contact:
                contact.analysis_status = "failed"
                db.commit()

            # Log error
            execution_time_ms = int((time.time() - start_time) * 1000)
            ToolMonitor.log_execution(
                db=db,
                tool_type="contact_analyzer",
                operation="analyze_contact",
                hub_id=hub_id,
                input_data={"contact_id": contact_id},
                status="error",
                error_message=str(e),
                execution_time_ms=execution_time_ms,
                triggered_by="queued",
                related_entity_type="contact",
                related_entity_id=contact_id
            )

        finally:
            db.close()
            # Remove from active analyses
            if contact_id in self._active_analyses:
                del self._active_analyses[contact_id]
            # Clean up cancelled set
            self._cancelled_contacts.discard(contact_id)


# Global scheduler instance
contact_analysis_scheduler = ContactAnalysisScheduler()
