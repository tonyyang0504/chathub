"""
Voice Call Support via Twilio
Handles incoming phone calls: transcribe → AI response → TTS → play back.

Requires:
- TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
- Twilio phone number configured with webhook URL
- Webhook: POST /api/voice/incoming → TwiML response
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request, Response, Depends
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voice Calls"])


@router.post("/api/voice/incoming")
async def handle_incoming_call(request: Request):
    """Handle incoming Twilio voice call — return TwiML to gather speech."""
    # TwiML response: greet caller, gather speech input
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Hello, this is an AI assistant. Please speak after the beep.</Say>
    <Gather input="speech" action="/api/voice/process" method="POST"
            speechTimeout="auto" language="en-US">
        <Say>I'm listening.</Say>
    </Gather>
    <Say>I didn't hear anything. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


@router.post("/api/voice/process")
async def process_speech(request: Request, db: Session = Depends(get_db)):
    """Process transcribed speech from Twilio → AI response → TTS."""
    form = await request.form()
    speech_result = form.get("SpeechResult", "")
    caller = form.get("From", "Unknown")
    confidence = form.get("Confidence", "0")

    logger.info(f"Voice call from {caller}: '{speech_result}' (confidence: {confidence})")

    if not speech_result:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>I didn't understand that. Please try again.</Say>
    <Redirect>/api/voice/incoming</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    # Generate AI response
    try:
        from app.ai.factory import get_ai_provider
        from app.config import settings

        # Use default provider (could be configured per Twilio number)
        # For now, use a simple response
        ai_text = f"You said: {speech_result}. I'm an AI assistant. This feature is in development."

        # TODO: Connect to actual bot's AI provider based on Twilio number → bot mapping

    except Exception as e:
        logger.error(f"Voice AI error: {e}")
        ai_text = "Sorry, I encountered an error. Please try again."

    # Return TwiML with AI response + continue gathering
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{ai_text}</Say>
    <Gather input="speech" action="/api/voice/process" method="POST"
            speechTimeout="auto" language="en-US">
        <Say>Is there anything else?</Say>
    </Gather>
    <Say>Thank you for calling. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="text/xml")
