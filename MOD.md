---
title: Save Agent as Template feature for Agents page
description: Added a "Save as Template" button to the agent detail modal that lets users save any configured agent as a reusable template. Includes a new backend endpoint (POST /agents/api/{agent_id}/save-as-template) that extracts the agent's system prompt, type, and config into an AgentTemplate record, and a frontend modal for naming and describing the template before saving.
---
