---
name: system-health
display_name: System Health Dashboard
description: Real-time system health monitoring section on the main dashboard showing database status, uptime, memory usage, bot fleet health, hub status, and scheduled content pipeline
icon: bi-heart-pulse
trigger: system health
---

# System Health Dashboard

Adds a system health monitoring section to the main dashboard page. Displays real-time health metrics including:

- **System Status** — overall healthy/degraded indicator
- **Database** — connection status
- **Uptime** — server process uptime
- **Memory** — process memory usage
- **Bot Fleet** — running bots vs total, recovery alerts
- **Hubs** — active hubs count
- **Scheduled Content** — pending and failed content items

Data refreshes automatically every 60 seconds with a manual refresh button.
