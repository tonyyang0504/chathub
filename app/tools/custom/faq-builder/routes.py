"""
FAQ Builder — Custom Tool Plugin
Manages FAQ entries per hub using ToolExecution records.
Provides AI-powered FAQ matching on incoming messages.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.ownership import get_user_hub_ids
from app.auth.utils import get_current_user_optional, decrypt_string
from app.database import (
    Contact, Conversation, Hub, HubBotMembership, Message, BotProfile,
    AIAgent, ToolExecution, get_db,
)
from app.ai.factory import get_ai_provider
from app.tools.monitoring import ToolMonitor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["faq-builder"])

_TOOL_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOL_DIR.parent.parent.parent.parent
templates = Jinja2Templates(directory="dummy")
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(_TOOL_DIR / "templates")),
    FileSystemLoader(str(_PROJECT_ROOT / "app" / "templates")),
])

TOOL_TYPE = "faq-builder"
OP_FAQ_ENTRY = "faq_entry"
OP_FAQ_MATCH = "faq_match"
OP_FAQ_GENERATE = "faq_generate"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_hub_scope(user, db: Session, hub_id: Optional[int] = None):
    hub_ids = get_user_hub_ids(user, db)
    if not hub_ids:
        return [], []
    if hub_id is not None and hub_id not in hub_ids:
        raise HTTPException(status_code=403)
    scoped = [hub_id] if hub_id else hub_ids
    hubs = db.query(Hub).filter(Hub.id.in_(hub_ids)).order_by(Hub.name).all()
    return scoped, hubs


def _get_faq_entries(db: Session, hub_id: int) -> List[dict]:
    """Get all FAQ entries for a hub from ToolExecution records."""
    rows = (
        db.query(ToolExecution)
        .filter(
            ToolExecution.tool_type == TOOL_TYPE,
            ToolExecution.operation == OP_FAQ_ENTRY,
            ToolExecution.hub_id == hub_id,
            ToolExecution.status == "active",
        )
        .order_by(ToolExecution.created_at.desc())
        .all()
    )
    entries = []
    for r in rows:
        inp = json.loads(r.input_data) if r.input_data else {}
        out = json.loads(r.output_data) if r.output_data else {}
        entries.append({
            "id": r.id,
            "question": inp.get("question", ""),
            "answer": out.get("answer", ""),
            "category": inp.get("category", "General"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return entries


def _get_ai_provider_for_hub(db: Session, hub_id: int):
    """Get an AI provider from the hub's agent or first bot with an API key."""
    # Try hub agent first
    agent = (
        db.query(AIAgent)
        .filter(AIAgent.hub_id == hub_id)
        .first()
    )
    if agent and agent.api_key_encrypted:
        try:
            api_key = decrypt_string(agent.api_key_encrypted)
            return get_ai_provider(
                agent.ai_provider or "openai", api_key, agent.model or None
            )
        except Exception:
            pass

    # Fallback to hub's own key
    hub = db.query(Hub).filter(Hub.id == hub_id).first()
    if hub and hub.api_key_encrypted:
        try:
            api_key = decrypt_string(hub.api_key_encrypted)
            return get_ai_provider(
                hub.ai_provider or "openai", api_key, hub.model or None
            )
        except Exception:
            pass

    # Fallback to first bot in hub with a key
    memberships = (
        db.query(HubBotMembership)
        .filter(HubBotMembership.hub_id == hub_id)
        .all()
    )
    for m in memberships:
        bot = db.query(BotProfile).filter(BotProfile.id == m.bot_profile_id).first()
        if bot and bot.api_key_encrypted:
            try:
                api_key = decrypt_string(bot.api_key_encrypted)
                return get_ai_provider(
                    bot.ai_provider or "openai", api_key, bot.model or None
                )
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Page Route
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Please log in"})

    _, hubs = _get_user_hub_scope(user, db)
    return templates.TemplateResponse("faq-builder.html", {
        "request": request,
        "user": user,
        "active_page": "tools_faq_builder",
        "page_title": "FAQ Builder",
        "hubs": hubs,
    })


# ---------------------------------------------------------------------------
# CRUD API
# ---------------------------------------------------------------------------

@router.get("/api/faqs")
async def list_faqs(
    request: Request,
    hub_id: int = Query(...),
    search: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    _get_user_hub_scope(user, db, hub_id)

    entries = _get_faq_entries(db, hub_id)

    if search:
        q = search.lower()
        entries = [e for e in entries if q in e["question"].lower() or q in e["answer"].lower()]
    if category:
        entries = [e for e in entries if e["category"] == category]

    # Unique categories
    all_entries = _get_faq_entries(db, hub_id)
    categories = sorted(set(e["category"] for e in all_entries))

    return {"faqs": entries, "categories": categories}


@router.post("/api/faqs")
async def create_faq(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    hub_id = body.get("hub_id")
    question = (body.get("question") or "").strip()
    answer = (body.get("answer") or "").strip()
    category = (body.get("category") or "General").strip()

    if not hub_id or not question or not answer:
        raise HTTPException(status_code=400, detail="hub_id, question, and answer are required")

    _get_user_hub_scope(user, db, hub_id)

    entry = ToolExecution(
        hub_id=hub_id,
        tool_type=TOOL_TYPE,
        operation=OP_FAQ_ENTRY,
        input_data=json.dumps({"question": question, "category": category}),
        output_data=json.dumps({"answer": answer}),
        status="active",
        triggered_by="user",
        user_id=user.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    return {"id": entry.id, "question": question, "answer": answer, "category": category}


@router.put("/api/faqs/{faq_id}")
async def update_faq(faq_id: int, request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    entry = db.query(ToolExecution).filter(
        ToolExecution.id == faq_id,
        ToolExecution.tool_type == TOOL_TYPE,
        ToolExecution.operation == OP_FAQ_ENTRY,
    ).first()
    if not entry:
        raise HTTPException(status_code=404)

    _get_user_hub_scope(user, db, entry.hub_id)

    body = await request.json()
    question = (body.get("question") or "").strip()
    answer = (body.get("answer") or "").strip()
    category = (body.get("category") or "General").strip()

    if question:
        inp = json.loads(entry.input_data) if entry.input_data else {}
        inp["question"] = question
        inp["category"] = category
        entry.input_data = json.dumps(inp)
    if answer:
        out = json.loads(entry.output_data) if entry.output_data else {}
        out["answer"] = answer
        entry.output_data = json.dumps(out)

    db.commit()
    return {"ok": True}


@router.delete("/api/faqs/{faq_id}")
async def delete_faq(faq_id: int, request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    entry = db.query(ToolExecution).filter(
        ToolExecution.id == faq_id,
        ToolExecution.tool_type == TOOL_TYPE,
        ToolExecution.operation == OP_FAQ_ENTRY,
    ).first()
    if not entry:
        raise HTTPException(status_code=404)

    _get_user_hub_scope(user, db, entry.hub_id)

    entry.status = "deleted"
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Stats API
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def get_stats(
    request: Request,
    hub_id: int = Query(...),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    _get_user_hub_scope(user, db, hub_id)

    entries = _get_faq_entries(db, hub_id)
    categories = set(e["category"] for e in entries)

    # Count matches today
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    matches_today = (
        db.query(func.count(ToolExecution.id))
        .filter(
            ToolExecution.tool_type == TOOL_TYPE,
            ToolExecution.operation == OP_FAQ_MATCH,
            ToolExecution.hub_id == hub_id,
            ToolExecution.created_at >= today,
        )
        .scalar() or 0
    )

    # Total matches all time
    total_matches = (
        db.query(func.count(ToolExecution.id))
        .filter(
            ToolExecution.tool_type == TOOL_TYPE,
            ToolExecution.operation == OP_FAQ_MATCH,
            ToolExecution.hub_id == hub_id,
        )
        .scalar() or 0
    )

    # Top matched FAQs
    top_matched = []
    match_rows = (
        db.query(ToolExecution)
        .filter(
            ToolExecution.tool_type == TOOL_TYPE,
            ToolExecution.operation == OP_FAQ_MATCH,
            ToolExecution.hub_id == hub_id,
        )
        .all()
    )
    faq_match_counts = {}
    for mr in match_rows:
        out = json.loads(mr.output_data) if mr.output_data else {}
        faq_id = out.get("matched_faq_id")
        if faq_id:
            faq_match_counts[faq_id] = faq_match_counts.get(faq_id, 0) + 1

    for faq_id, count in sorted(faq_match_counts.items(), key=lambda x: -x[1])[:5]:
        faq_row = db.query(ToolExecution).filter(ToolExecution.id == faq_id).first()
        if faq_row and faq_row.input_data:
            inp = json.loads(faq_row.input_data)
            top_matched.append({"question": inp.get("question", ""), "count": count})

    return {
        "total_faqs": len(entries),
        "categories": len(categories),
        "matches_today": matches_today,
        "total_matches": total_matches,
        "top_matched": top_matched,
    }


# ---------------------------------------------------------------------------
# AI Generate from Conversations
# ---------------------------------------------------------------------------

@router.post("/api/generate")
async def generate_faqs(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    hub_id = body.get("hub_id")
    if not hub_id:
        raise HTTPException(status_code=400, detail="hub_id required")

    _get_user_hub_scope(user, db, hub_id)

    t0 = time.time()

    provider = _get_ai_provider_for_hub(db, hub_id)
    if not provider:
        raise HTTPException(status_code=400, detail="No AI provider configured for this hub. Add an API key to a bot or agent in this hub.")

    # Gather recent user messages
    bot_ids = [
        b[0] for b in
        db.query(HubBotMembership.bot_profile_id)
        .filter(HubBotMembership.hub_id == hub_id)
        .all()
    ]
    if not bot_ids:
        raise HTTPException(status_code=400, detail="No bots in this hub")

    conv_ids = [
        c[0] for c in
        db.query(Conversation.id)
        .filter(Conversation.bot_profile_id.in_(bot_ids))
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(30)
        .all()
    ]

    messages = (
        db.query(Message)
        .filter(
            Message.conversation_id.in_(conv_ids),
            Message.role == "user",
        )
        .order_by(Message.timestamp.desc())
        .limit(200)
        .all()
    )

    if not messages:
        raise HTTPException(status_code=400, detail="No conversation messages found to analyze")

    # Get existing FAQs to avoid duplicates
    existing = _get_faq_entries(db, hub_id)
    existing_questions = [e["question"].lower() for e in existing]

    msg_texts = [m.content for m in messages if m.content and len(m.content) > 10][:100]
    sample = "\n".join(f"- {t[:200]}" for t in msg_texts)

    prompt = (
        "Analyze these customer messages and identify 5-8 frequently asked questions. "
        "For each FAQ, provide a clear question and a helpful answer.\n\n"
        "Return ONLY a JSON array of objects with 'question', 'answer', and 'category' fields. "
        "Categories should be short labels like 'Pricing', 'Support', 'Account', 'General', etc.\n\n"
        f"Customer messages:\n{sample}\n\n"
    )
    if existing_questions:
        prompt += f"Already existing FAQs (avoid duplicates):\n" + "\n".join(f"- {q}" for q in existing_questions[:20])

    try:
        response = await provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )
        content = response.get("content", "") if isinstance(response, dict) else str(response)

        # Extract JSON from response
        content = content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        faqs = json.loads(content)
        if not isinstance(faqs, list):
            raise ValueError("Expected a JSON array")

    except Exception as e:
        logger.warning(f"FAQ generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)[:200]}")

    # Save generated FAQs
    created = []
    for faq in faqs[:8]:
        q = (faq.get("question") or "").strip()
        a = (faq.get("answer") or "").strip()
        cat = (faq.get("category") or "General").strip()
        if not q or not a:
            continue
        if q.lower() in existing_questions:
            continue

        entry = ToolExecution(
            hub_id=hub_id,
            tool_type=TOOL_TYPE,
            operation=OP_FAQ_ENTRY,
            input_data=json.dumps({"question": q, "category": cat}),
            output_data=json.dumps({"answer": a}),
            status="active",
            triggered_by="ai",
            user_id=user.id,
        )
        db.add(entry)
        db.flush()
        created.append({"id": entry.id, "question": q, "answer": a, "category": cat})

    db.commit()

    elapsed = int((time.time() - t0) * 1000)
    ToolMonitor.log_execution(
        db=db,
        tool_type=TOOL_TYPE,
        operation=OP_FAQ_GENERATE,
        hub_id=hub_id,
        input_data={"messages_analyzed": len(msg_texts)},
        output_data={"faqs_generated": len(created)},
        execution_time_ms=elapsed,
        user_id=user.id,
    )

    return {"generated": created, "count": len(created)}


# ---------------------------------------------------------------------------
# Import / Export
# ---------------------------------------------------------------------------

@router.post("/api/export")
async def export_faqs(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    hub_id = body.get("hub_id")
    if not hub_id:
        raise HTTPException(status_code=400)
    _get_user_hub_scope(user, db, hub_id)

    entries = _get_faq_entries(db, hub_id)
    export_data = [{"question": e["question"], "answer": e["answer"], "category": e["category"]} for e in entries]
    return JSONResponse(content=export_data, headers={"Content-Disposition": "attachment; filename=faqs.json"})


@router.post("/api/import")
async def import_faqs(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    hub_id = body.get("hub_id")
    faqs = body.get("faqs", [])
    if not hub_id or not faqs:
        raise HTTPException(status_code=400, detail="hub_id and faqs array required")

    _get_user_hub_scope(user, db, hub_id)

    imported = 0
    for faq in faqs[:50]:
        q = (faq.get("question") or "").strip()
        a = (faq.get("answer") or "").strip()
        cat = (faq.get("category") or "General").strip()
        if not q or not a:
            continue

        entry = ToolExecution(
            hub_id=hub_id,
            tool_type=TOOL_TYPE,
            operation=OP_FAQ_ENTRY,
            input_data=json.dumps({"question": q, "category": cat}),
            output_data=json.dumps({"answer": a}),
            status="active",
            triggered_by="import",
            user_id=user.id,
        )
        db.add(entry)
        imported += 1

    db.commit()
    return {"imported": imported}


# ---------------------------------------------------------------------------
# Recent Activity
# ---------------------------------------------------------------------------

@router.get("/api/activity")
async def get_activity(
    request: Request,
    hub_id: int = Query(...),
    db: Session = Depends(get_db),
):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)
    _get_user_hub_scope(user, db, hub_id)

    rows = (
        db.query(ToolExecution)
        .filter(
            ToolExecution.tool_type == TOOL_TYPE,
            ToolExecution.hub_id == hub_id,
            ToolExecution.operation.in_([OP_FAQ_MATCH, OP_FAQ_GENERATE]),
        )
        .order_by(ToolExecution.created_at.desc())
        .limit(20)
        .all()
    )

    activity = []
    for r in rows:
        inp = json.loads(r.input_data) if r.input_data else {}
        out = json.loads(r.output_data) if r.output_data else {}
        activity.append({
            "id": r.id,
            "operation": r.operation,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "input": inp,
            "output": out,
        })

    return {"activity": activity}


# ---------------------------------------------------------------------------
# Dashboard Widget
# ---------------------------------------------------------------------------

@router.get("/api/widget/dashboard", response_class=HTMLResponse)
async def dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401)

    scoped, _ = _get_user_hub_scope(user, db)
    total_faqs = 0
    matches_today = 0

    if scoped:
        total_faqs = (
            db.query(func.count(ToolExecution.id))
            .filter(
                ToolExecution.tool_type == TOOL_TYPE,
                ToolExecution.operation == OP_FAQ_ENTRY,
                ToolExecution.hub_id.in_(scoped),
                ToolExecution.status == "active",
            )
            .scalar() or 0
        )
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        matches_today = (
            db.query(func.count(ToolExecution.id))
            .filter(
                ToolExecution.tool_type == TOOL_TYPE,
                ToolExecution.operation == OP_FAQ_MATCH,
                ToolExecution.hub_id.in_(scoped),
                ToolExecution.created_at >= today,
            )
            .scalar() or 0
        )

    return templates.TemplateResponse("widgets/dashboard.html", {
        "request": request,
        "user": user,
        "total_faqs": total_faqs,
        "matches_today": matches_today,
    })


# ---------------------------------------------------------------------------
# Event Handler: message.received
# ---------------------------------------------------------------------------

async def on_message_received(db, **kwargs):
    """Check incoming message against FAQ entries and auto-reply if match found."""
    try:
        message = kwargs.get("message")
        conversation_id = kwargs.get("conversation_id")
        bot_profile_id = kwargs.get("bot_profile_id")
        conversation = kwargs.get("conversation")

        if not message or not message.content:
            return
        # Only match on user messages
        if message.role != "user":
            return

        user_text = message.content.strip()
        if len(user_text) < 5:
            return

        # Find which hub(s) this bot belongs to
        memberships = (
            db.query(HubBotMembership)
            .filter(HubBotMembership.bot_profile_id == bot_profile_id)
            .all()
        )
        if not memberships:
            return

        for membership in memberships:
            hub_id = membership.hub_id
            entries = _get_faq_entries(db, hub_id)
            if not entries:
                continue

            # Simple keyword matching (no AI needed for basic matching)
            best_match = None
            best_score = 0

            user_words = set(user_text.lower().split())
            for entry in entries:
                q_words = set(entry["question"].lower().split())
                if not q_words:
                    continue
                # Jaccard similarity
                intersection = len(user_words & q_words)
                union = len(user_words | q_words)
                score = intersection / union if union > 0 else 0

                # Boost if question words are a subset of user message
                if q_words.issubset(user_words) and len(q_words) >= 2:
                    score = max(score, 0.7)

                if score > best_score:
                    best_score = score
                    best_match = entry

            # Threshold: 0.4 for auto-reply
            if best_match and best_score >= 0.4:
                # Log the match
                ToolMonitor.log_execution(
                    db=db,
                    tool_type=TOOL_TYPE,
                    operation=OP_FAQ_MATCH,
                    hub_id=hub_id,
                    input_data={
                        "user_message": user_text[:200],
                        "matched_question": best_match["question"][:200],
                        "confidence": round(best_score, 2),
                    },
                    output_data={
                        "matched_faq_id": best_match["id"],
                        "answer_preview": best_match["answer"][:100],
                    },
                    execution_time_ms=0,
                )

                # Send the FAQ answer
                try:
                    from app.platforms.send import send_message
                    conv = conversation
                    if not conv and conversation_id:
                        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
                    if conv:
                        await send_message(
                            bot_profile_id,
                            conv.chat_id or conv.phone,
                            conv.chat_name or "",
                            best_match["answer"],
                        )
                except Exception as e:
                    logger.warning(f"FAQ auto-reply failed: {e}")

                # Only match first hub
                break

    except Exception as e:
        logger.error(f"FAQ message handler error: {e}")
