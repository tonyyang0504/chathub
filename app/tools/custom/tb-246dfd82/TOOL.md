---
name: recent-conversations-dashboard
display_name: Recent Conversations Dashboard
description: Adds a Recent Conversations section to the dashboard showing latest chat threads across all bots with avatars, message previews, and bot badges
icon: bi-chat-dots
trigger: dashboard recent conversations
---

# System Prompt

You are a dashboard enhancement that displays recent conversations across all bots.

## Instructions
- Shows the 8 most recent conversations across all user's bots
- Displays contact avatar (with gradient fallback), name, bot badge, last message preview, and timestamp
- Group chats show a group icon
- Outgoing messages show a checkmark indicator
- Clicking a conversation navigates to the conversations page with that chat selected
- Auto-refreshes every 30 seconds along with other dashboard data

## API Endpoint
- GET /api/conversations/recent?limit=8 — returns recent conversations across all bots for the authenticated user
