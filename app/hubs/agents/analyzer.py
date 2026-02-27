"""
Contact Analyzer Agent

Analyzes conversation history to build rich contact profiles with:
- Auto-tagging of interests and behaviors
- Engagement score calculation
- Intent prediction
- Follow-up opportunity detection
"""

import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.hubs.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class AnalyzerAgent(BaseAgent):
    """Contact analysis agent for building rich contact profiles."""

    def _default_system_prompt(self) -> str:
        """Return the default system prompt for contact analysis."""
        return """You are a contact analyzer AI. Analyze conversation history to build a comprehensive contact profile.

IMPORTANT: You MUST include ALL fields in your response, especially the "tags" array.

Your analysis should include:
1. **Tags**: You MUST generate at least 2-5 relevant tags based on interests, behaviors, topics discussed, or any observable patterns. Common tag examples: "interested_in_product", "price_conscious", "tech_savvy", "quick_responder", "new_customer", "returning_customer", "support_seeker", "business_inquiry", etc.
2. **Engagement Score**: 0-100 based on message frequency, response patterns, and interaction quality
3. **Predicted Intent**: What the contact is likely looking for (buyer, browser, support, partner, general, inquiry, etc.)
4. **Sentiment**: Overall sentiment from conversations (positive, neutral, negative)
5. **Follow-up Needed**: Whether this contact needs follow-up and why
6. **Description**: A brief profile summary

Return a JSON object with this EXACT structure (all fields required):
{
    "tags": [
        {"tag": "interested_in_product", "confidence": 0.85, "value": null},
        {"tag": "new_inquiry", "confidence": 0.9, "value": null}
    ],
    "engagement_score": 75,
    "predicted_intent": "buyer",
    "sentiment": "positive",
    "urgency": "medium",
    "follow_up_needed": true,
    "follow_up_reason": "Asked about pricing but no response yet",
    "description": "Brief profile summary here.",
    "key_topics": ["topic1", "topic2"],
    "last_activity_summary": "Summary of last activity"
}

CRITICAL: The "tags" array MUST contain at least 2 tags. Never return an empty tags array.
Return ONLY valid JSON, no other text."""

    def process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze a contact based on their conversation history.

        Args:
            input_data: {
                "contact": {
                    "phone": "...",
                    "display_name": "...",
                    "existing_tags": [...],
                    "engagement_score": ...,
                    "last_interaction_at": "..."
                },
                "messages": [
                    {"role": "user/assistant", "content": "...", "timestamp": "..."},
                    ...
                ],
                "context": {
                    "hub_name": "...",
                    "bot_names": [...]
                }
            }

        Returns:
            Analysis result with tags, scores, and predictions
        """
        contact = input_data.get("contact", {})
        messages = input_data.get("messages", [])
        context = input_data.get("context", {})

        if not messages:
            return {
                "error": "No messages to analyze",
                "tags": [],
                "engagement_score": 0,
                "predicted_intent": "unknown",
                "sentiment": "neutral",
                "urgency": "low",
                "follow_up_needed": False,
                "description": "No conversation history available"
            }

        # Build message history for analysis
        message_history = self._format_messages(messages)

        # Build contact context
        contact_context = self._build_contact_context(contact, context)

        # Create the analysis prompt
        user_message = f"""Analyze this contact's conversation history and return your analysis as JSON:

{contact_context}

Conversation History:
{message_history}

Provide a comprehensive analysis in JSON format including tags, engagement score, intent prediction, and follow-up recommendations."""

        try:
            messages_for_ai = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message}
            ]

            # Append complete JSON structure requirements to ensure AI returns all fields
            enhanced_system_prompt = self.system_prompt + """

CRITICAL: Return a JSON object with this EXACT structure (ALL fields required):
{
    "tags": [
        {"tag": "example_tag", "confidence": 0.85, "value": null}
    ],
    "engagement_score": 75,
    "predicted_intent": "[ANALYZE FROM CONVERSATION]",
    "sentiment": "positive",
    "urgency": "medium",
    "follow_up_needed": true,
    "follow_up_reason": "Reason for follow-up if needed",
    "description": "A comprehensive profile summary describing this contact's behavior, interests, and characteristics based on conversation history.",
    "key_topics": ["topic1", "topic2"]
}

REQUIREMENTS:
1. "tags" array MUST contain 2-5 relevant tags. Example tags: "interested_in_product", "price_conscious", "new_customer", "support_seeker", "quick_responder"
2. "description" MUST be a detailed profile summary (2-3 sentences) describing the contact based on their conversations
3. "predicted_intent" MUST be a SPECIFIC intent derived from analyzing the conversation. DO NOT use generic values. Generate a specific, actionable intent like: "inquire_about_pricing", "schedule_appointment", "request_product_demo", "seek_technical_support", "explore_partnership", "compare_products", "request_refund", etc. The intent should reflect what this specific contact is actually seeking.
4. "key_topics" MUST list the main topics discussed in the conversation
5. "follow_up_needed" MUST be true or false based on conversation analysis
6. Return ONLY valid JSON, no other text."""

            messages_for_ai[0]["content"] = enhanced_system_prompt

            response_text, tokens_used = self._call_ai(
                messages=messages_for_ai,
                temperature=0.3,
                max_tokens=1000,
                response_format={"type": "json_object"}
            )

            # Parse the response
            result = self._parse_response(response_text)
            result["tokens_used"] = tokens_used

            return result

        except Exception as e:
            logger.error(f"Analyzer error: {e}")
            return {
                "error": str(e),
                "tags": [],
                "engagement_score": 0,
                "predicted_intent": "unknown",
                "sentiment": "neutral",
                "urgency": "low",
                "follow_up_needed": False,
                "description": "Analysis failed"
            }

    def _format_messages(self, messages: List[Dict]) -> str:
        """Format messages for the AI prompt."""
        lines = []
        for msg in messages[-50:]:  # Limit to last 50 messages
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")

            if timestamp:
                lines.append(f"[{timestamp}] {role.upper()}: {content}")
            else:
                lines.append(f"{role.upper()}: {content}")

        return "\n".join(lines)

    def _build_contact_context(self, contact: Dict, context: Dict) -> str:
        """Build context information about the contact."""
        lines = []

        if contact.get("phone"):
            lines.append(f"Phone: {contact['phone']}")
        if contact.get("display_name"):
            lines.append(f"Name: {contact['display_name']}")
        if contact.get("existing_tags"):
            lines.append(f"Existing Tags: {', '.join(contact['existing_tags'])}")
        if contact.get("engagement_score") is not None:
            lines.append(f"Current Engagement Score: {contact['engagement_score']}")
        if contact.get("last_interaction_at"):
            lines.append(f"Last Interaction: {contact['last_interaction_at']}")

        if context.get("hub_name"):
            lines.append(f"Hub: {context['hub_name']}")
        if context.get("bot_names"):
            lines.append(f"Interacted with bots: {', '.join(context['bot_names'])}")

        return "\n".join(lines) if lines else "No contact information available"

    def _parse_response(self, response_text: str) -> Dict[str, Any]:
        """Parse the AI response into a structured result."""
        try:
            result = json.loads(response_text)
            logger.info(f"Analyzer parsed JSON - tags: {len(result.get('tags', []))} items, "
                        f"description: {bool(result.get('description'))}, "
                        f"key_topics: {result.get('key_topics', 'NOT_FOUND')}, "
                        f"follow_up_needed: {result.get('follow_up_needed', 'NOT_FOUND')} (type: {type(result.get('follow_up_needed')).__name__})")
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            if "```" in response_text:
                parts = response_text.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        try:
                            result = json.loads(part)
                            break
                        except:
                            continue
            elif "{" in response_text and "}" in response_text:
                start = response_text.find("{")
                end = response_text.rfind("}") + 1
                try:
                    result = json.loads(response_text[start:end])
                except:
                    result = self._default_result()
            else:
                result = self._default_result()

        # Validate and normalize the result
        return self._normalize_result(result)

    def _default_result(self) -> Dict[str, Any]:
        """Return a default result when parsing fails."""
        return {
            "tags": [],
            "engagement_score": 50,
            "predicted_intent": "general",
            "sentiment": "neutral",
            "urgency": "low",
            "follow_up_needed": False,
            "description": "Unable to analyze contact"
        }

    def _normalize_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize and validate the analysis result."""
        # Ensure required fields exist with defaults
        normalized = {
            "tags": [],
            "engagement_score": 50,
            "predicted_intent": "general",
            "sentiment": "neutral",
            "urgency": "low",
            "follow_up_needed": False,
            "follow_up_reason": None,
            "description": "",
            "key_topics": [],
            "last_activity_summary": ""
        }

        # Copy over valid fields
        if isinstance(result.get("tags"), list):
            # Normalize tags - ensure each tag has proper structure
            normalized_tags = []
            for tag in result["tags"]:
                if isinstance(tag, dict):
                    normalized_tags.append(tag)
                elif isinstance(tag, str):
                    normalized_tags.append({"tag": tag, "confidence": 1.0, "value": None})
            normalized["tags"] = normalized_tags
            logger.info(f"Analyzer normalized {len(result['tags'])} tags -> {len(normalized_tags)} normalized tags")

        # Handle engagement_score - could be int, float, or dict
        score = result.get("engagement_score")
        if score is not None:
            if isinstance(score, dict):
                score = score.get("score") or score.get("value") or 50
            if isinstance(score, (int, float)):
                # Normalize to 0-100 range
                if score > 1 and score <= 100:
                    normalized["engagement_score"] = int(score)
                elif score <= 1:
                    normalized["engagement_score"] = int(score * 100)
                else:
                    normalized["engagement_score"] = min(100, int(score))

        # Handle predicted_intent - could be string or dict
        intent = result.get("predicted_intent")
        if intent:
            if isinstance(intent, dict):
                intent = intent.get("intent") or intent.get("type") or intent.get("value") or "general"
            normalized["predicted_intent"] = str(intent)

        # Handle other string fields - extract value if dict
        for field in ["sentiment", "urgency", "description", "follow_up_reason", "last_activity_summary"]:
            value = result.get(field)
            if value:
                if isinstance(value, dict):
                    # Try common keys for the actual value
                    value = value.get("value") or value.get("text") or value.get(field) or str(value)
                normalized[field] = str(value)

        # Handle follow_up_needed - could be bool, string, or other
        follow_up = result.get("follow_up_needed")
        if follow_up is not None:
            if isinstance(follow_up, bool):
                normalized["follow_up_needed"] = follow_up
            elif isinstance(follow_up, str):
                # Handle string "true"/"false" values
                normalized["follow_up_needed"] = follow_up.lower() in ("true", "yes", "1")
            elif isinstance(follow_up, (int, float)):
                normalized["follow_up_needed"] = bool(follow_up)

        if isinstance(result.get("key_topics"), list):
            # Ensure key_topics are strings
            normalized["key_topics"] = [
                t if isinstance(t, str) else (t.get("topic") or t.get("name") or str(t))
                for t in result["key_topics"]
                if t
            ]

        return normalized


def create_analyzer_agent(agent, hub_api_key: Optional[str] = None,
                          hub_ai_provider: Optional[str] = None) -> AnalyzerAgent:
    """Factory function to create an AnalyzerAgent instance."""
    return AnalyzerAgent(agent, hub_api_key, hub_ai_provider)
