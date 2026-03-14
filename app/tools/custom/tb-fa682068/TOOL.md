---
name: hubs-overview-dashboard
display_name: Hubs Overview Dashboard
description: Dashboard section showing all hubs with bot counts, agent counts, contact counts, status, and AI provider info
icon: bi-diagram-3
trigger: hubs overview
---

# Hubs Overview Dashboard Section

Adds a Hubs Overview card to the main dashboard page, displayed between the Bots table and Recent Conversations sections.

## Features
- Displays all user hubs in a responsive 2-column card grid
- Each hub card shows: name, active/inactive status dot, task type badge (color-coded), bot/agent/contact counts, and AI provider/model
- Task type badges are color-coded to match the platform's existing tool color themes
- Cards link directly to the hub detail page
- Empty state with a "Create your first hub" CTA
- Hub count badge in the section header
- Auto-refreshes every 30 seconds with the rest of the dashboard
