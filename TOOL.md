---
name: conversation-summarizer
display_name: Conversation Summarizer
description: Analyze saved conversations to generate AI summaries, intent, sentiment, key points, and a suggested reply.
icon: bi-chat-square-text
trigger: summarize conversation
widgets:
  - page: dashboard
    endpoint: /api/widget/dashboard
---

# Conversation Summarizer

Conversation Summarizer is a self-contained ChatHub custom tool that lets users review recent conversations and generate structured AI analysis on demand. It uses existing `Conversation`, `Message`, and `BotProfile` records, respects user ownership by filtering through the bot owner, and logs executions through the shared `tool_executions` table.

## Features

- Browse recent conversations owned by the current user
- Search conversations by name or phone number
- Generate a concise conversation summary
- Detect likely customer intent and sentiment
- Extract key points from the recent exchange
- Draft a suggested reply for the operator
- Show a compact dashboard widget with recent conversations

## Notes

- No new database tables are required
- If AI generation fails, the tool falls back to a rule-based summary
- Execution events are recorded with `ToolMonitor`
