# CHATHUB.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run development server (http://localhost:8000)
python run.py

# Generate encryption key for .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Run tests
pytest tests/
pytest tests/test_auth.py -v          # Single file
pytest tests/ -v --cov=app            # With coverage

# Build Windows executable
python build_windows.py
python build_windows.py --clean --skip-playwright
```

## Architecture Overview

ChatHub is a multi-tenant platform for AI-powered WhatsApp bots with coordinated multi-bot orchestration.

### Project File Structure

```
chathub/
├── app/
│   ├── main.py                    # FastAPI app entry, lifespan, router registration
│   ├── database.py                # All SQLAlchemy models + migrations (23 models)
│   ├── config.py                  # Settings from .env
│   ├── logging_config.py          # Logging setup
│   ├── metrics.py                 # Prometheus metrics endpoint
│   ├── ai/                        # AI provider abstraction
│   │   ├── factory.py             # get_ai_provider(provider_name, api_key, model)
│   │   ├── base.py                # AIProvider base class
│   │   └── providers/             # OpenAI, Anthropic, Google, DeepSeek, Qwen, Grok
│   ├── auth/
│   │   ├── routes.py              # Login, register, logout, profile (9 routes)
│   │   ├── utils.py               # JWT, password hash, encrypt/decrypt, WS auth
│   │   └── ownership.py           # get_user_hub_ids() helper
│   ├── bots/
│   │   ├── manager.py             # BotManager singleton + BotInstance class
│   │   ├── whatsapp_bot.py        # Playwright browser automation (11K+ lines)
│   │   └── routes.py              # Bot CRUD, start/stop, contacts, groups, WS QR (19 routes)
│   ├── conversations/
│   │   └── routes.py              # Conversation list, messages, send, export, WS (11 routes)
│   ├── hubs/
│   │   ├── routes.py              # Hub CRUD, contacts, agents, content, groups (48 routes)
│   │   ├── coordinator.py         # RoutingCache, multi-bot message routing
│   │   ├── scheduler.py           # Content delivery scheduler
│   │   ├── analysis_scheduler.py  # Contact auto-analysis
│   │   └── followup_scheduler.py  # Follow-up auto-send
│   ├── scripts/
│   │   ├── routes.py              # Script CRUD, execution (17 routes)
│   │   └── scheduler.py           # Script execution scheduler
│   ├── tools/
│   │   ├── __init__.py            # Exports tools_router
│   │   ├── routes.py              # All tool pages + APIs (32 routes)
│   │   └── monitoring.py          # ToolMonitor class for execution logging
│   ├── agents/
│   │   └── routes.py              # Agent CRUD, templates, testing (13 routes)
│   ├── analytics/
│   │   └── routes.py              # Overview, daily stats, activity feed (5 routes)
│   ├── ai_workspace/
│   │   ├── __init__.py            # Exports ai_workspace_manager
│   │   ├── manager.py             # AiWorkspaceManager — CLI subprocess management
│   │   └── routes.py              # HTTP + WebSocket routes (8 routes)
│   ├── middleware/
│   │   └── rate_limit.py          # SlowAPI rate limiter
│   └── templates/
│       ├── base.html              # Base layout with navbar
│       ├── auth/                   # login.html, register.html
│       ├── errors/                 # 404.html
│       └── dashboard/
│           ├── index.html          # Main dashboard
│           ├── bots.html, hubs.html, conversations.html, analytics.html, settings.html, agents.html
│           └── tools/              # group_management, scheduled_content, scripted_conversations,
│                                   # contact_analyzer, contact_followup, content_generator,
│                                   # message_routing, ai_workspace
├── static/
│   ├── css/style.css, tools.css
│   ├── js/app.js
│   └── images/ai-agent.svg
├── data/                          # Runtime data (sessions, DB, backups)
├── migrations/                    # Migration scripts (run with python migrations/xxx.py)
├── tests/                         # pytest tests
├── .env                           # Environment config
├── run.py                         # Dev server entry
├── build_windows.py               # PyInstaller build script
└── tray_app.py                    # Windows system tray app
```

### Core Components

**Bot Lifecycle**: `BotProfile` (DB) → `BotInstance` (memory) → WhatsApp automation → `Conversations` (DB)

- `app/bots/manager.py`: `BotManager` singleton manages `BotInstance` objects with callback system for QR codes and status updates
- `app/bots/whatsapp_bot.py`: Playwright-based browser automation (11K+ lines — the core bot logic)
- Sessions persist in `data/sessions/bot_{id}/`

**AI Provider Abstraction** (`app/ai/`):
- Factory pattern via `get_ai_provider(provider_name, api_key, model)`
- Providers: OpenAI, Anthropic, Google, DeepSeek, Qwen, xAI Grok
- Each implements `AIProvider` base class with `chat_completion()` and `analyze_image()`
- Handle provider differences: GPT-5.x uses `max_completion_tokens`; some don't support temperature/penalties

**Hub System** (`app/hubs/`):
- Groups multiple bots with AI agents for intelligent message routing
- `coordinator.py`: `RoutingCache` prevents duplicate AI calls when multiple bots receive same message
- Agents: classifier (categorizes messages), router (determines which bot responds)

**AI Workspace Integration** (`app/ai_workspace/`):
- Embeds AI CLI tools as subprocesses with real-time WebSocket streaming
- `manager.py`: `AiWorkspaceManager` singleton — spawns CLI, reads stream-json output, broadcasts to WebSockets
- Safety: auto git commit + DB backup before each session, rollback support
- One active session per user enforced

**WebSocket Real-time**:
- QR code display: `WS /api/bots/{id}/qr`
- Conversation updates via `conversation_ws_manager`
- AI Workspace streaming: `WS /tools/api/ai-workspace/stream/{session_id}`
- Cross-thread calls: `conversation_ws_manager.set_main_loop()` stores asyncio loop

### Key Patterns

**Authentication & Multi-tenancy**:
```python
from app.auth.utils import get_current_user
@router.get("/path")
async def endpoint(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # All resources filtered by user.id
```

**WebSocket Authentication**:
```python
@router.websocket("/path/{id}")
async def ws_endpoint(websocket: WebSocket, id: int):
    await websocket.accept()
    db = next(get_db())
    user = await get_websocket_user(websocket, db)  # Token from cookie or ?token= param
    if not user:
        await websocket.close(code=4001)
        return
```

**API Key Encryption**: Use `encrypt_string()` / `decrypt_string()` from `app/auth/utils.py` for storing API keys

**Async/Threading**: FastAPI async routes, but Playwright runs in thread. `BotInstance` uses `threading.Lock` for `_queue_lock`

**Message Deduplication**: WhatsApp message ID + 5-minute content hash fallback

### Database Models (23 tables)

All models in `app/database.py`:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `users` | User accounts | id, email, name, password_hash, is_active |
| `bot_profiles` | Bot config | id, user_id, name, ai_provider, api_key_encrypted, model, system_prompt, is_running |
| `conversations` | Chat threads | id, bot_profile_id, chat_name, chat_type, is_group |
| `messages` | Individual messages | id, conversation_id, content, sender, direction, wa_message_id |
| `scheduled_messages` | Legacy scheduled | id, bot_profile_id, content, scheduled_for |
| `activity_logs` | Activity tracking | id, user_id, action, details |
| `hubs` | Multi-bot groups | id, user_id, name, task_type, ai_provider |
| `hub_bot_memberships` | Bot-Hub links | id, hub_id, bot_profile_id, role |
| `hub_message_topics` | Message categories | id, hub_id, name, keywords |
| `agent_templates` | Template library | id, name, agent_type, system_prompt |
| `ai_agents` | AI agents | id, hub_id, name, agent_type, model, api_key_encrypted |
| `contacts` | WhatsApp contacts | id, hub_id, phone, display_name, sentiment, urgency, follow_up_needed |
| `contact_tags` | Contact labels | id, contact_id, tag, value, confidence, source |
| `scheduled_contents` | Content queue | id, hub_id, content, scheduled_for, status, recipient_type |
| `agent_executions` | Agent run logs | id, agent_id, input_data, output_data, tokens_used |
| `message_routings` | Routing decisions | id, hub_id, message_id, classification, assigned_bots |
| `conversation_scripts` | Script templates | id, hub_id, name, schedule_type, status |
| `script_messages` | Script steps | id, script_id, bot_profile_id, content, delay_seconds |
| `script_executions` | Script run logs | id, script_id, status, messages_sent |
| `tool_executions` | Tool monitor | id, hub_id, tool_type, operation, status, execution_time_ms |
| `claude_code_sessions` | CLI sessions | id, user_id, prompt, status, git_commit_hash, db_backup_path, pid |
| `claude_code_messages` | Stream events | id, session_id, role, content, message_type, metadata |
| `claude_code_settings` | Per-user config | id, user_id, anthropic_api_key_encrypted, default_model |

### API Endpoints Catalog (179 routes)

**Auth** (`/auth`): login, register, logout, profile CRUD, password change
**Bots** (`/api/bots`): CRUD, start/stop, toggle AI, sync history, contacts/groups, analytics, WS QR
**Conversations** (`/api/conversations`): list by bot, messages, send, send-file, export, WS updates
**Hubs** (`/api/hubs`): CRUD, bot membership, agents, contacts (CRUD + analyze + export), groups, topics, scheduled content, generation
**Scripts** (`/scripts`): script CRUD, messages, execution, scheduling
**Tools** (`/tools`): tool pages (7 pages), monitoring API, tool-specific stats/simulation/operations
**AI Workspace** (`/tools`): page, settings, session CRUD, stop, rollback, WS stream
**Agents** (`/agents`): CRUD, templates, test, history
**Analytics** (`/api/analytics`): overview, daily, per-bot, top conversations, activity feed
**Dashboard** (root): 6 page routes (dashboard, bots, conversations, analytics, settings, hubs)

### Frontend Tech Stack

- **Bootstrap 5.3.2** — CSS framework + JS components
- **Bootstrap Icons 1.11.1** — Icon library
- **HTMX 1.9.9** — Progressive enhancement
- **marked.js** — Markdown rendering (AI Workspace page)
- **Custom CSS** — `static/css/style.css` (main), `static/css/tools.css` (tool pages)
- **Custom JS** — `static/js/app.js` (auth, toast, utilities)

### Deployment Modes

- **Development**: `python run.py`
- **Linux**: systemd services (xvfb.service + chathub.service) with DISPLAY=:99
- **Windows**: PyInstaller executable with system tray (`tray_app.py`), data in `%APPDATA%/ChatHub/`

### Frozen Executable Path Handling

When running as PyInstaller executable vs script:
```python
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).resolve().parent.parent.parent  # <project>/dist/ChatHub/ChatHub.exe
else:
    BASE_DIR = Path(__file__).resolve().parent.parent.parent
```

## Environment Variables

Required in `.env`:
```
SECRET_KEY=           # JWT signing
ENCRYPTION_KEY=       # Fernet key for API keys
DATABASE_URL=sqlite:///./data/app.db
HOST=0.0.0.0
PORT=8000
```

## UI/Styling Conventions

### Modal Close Buttons
Use `modal-close-btn` class (not Bootstrap's `btn-close`):
```html
<button type="button" class="modal-close-btn" data-bs-dismiss="modal" aria-label="Close"></button>
```
This creates a circular button with X icon using CSS pseudo-elements. Defined in `static/css/style.css`.

### Button Alignment in Tab Headers
When buttons need to match the height of a search input in the same row:
- Use `align-items-stretch` on the parent flex container
- Add CSS to center button content: `display: flex; align-items: center; justify-content: center;`
- Example (Contacts tab in hubs.html):
```css
#contactsTab .d-flex.align-items-stretch > .btn,
#contactsTab .d-flex.align-items-stretch > .dropdown > .btn {
    display: flex;
    align-items: center;
    justify-content: center;
}
```

### Dropdown Buttons Without Bootstrap Caret
To avoid height issues with `dropdown-toggle` class, use a custom chevron icon:
```html
<button class="btn btn-sm btn-outline-secondary rounded-pill" type="button" data-bs-toggle="dropdown">
    <i class="bi bi-download me-1"></i>Export<i class="bi bi-chevron-down ms-1" style="font-size: 0.65rem;"></i>
</button>
```

### Form Switch Styling (Matching Bot Card)
For toggle switches that match the bot card style:
```css
.form-switch .form-check-input {
    width: 40px;
    height: 22px;
}
.form-switch .form-check-label {
    margin-left: 8px;
    line-height: 22px;
}
```

### Color Themes by Tool/Hub Type
- **Contact Analyzer**: Pink gradient (`#f093fb` to `#f5576c`)
- **Contact Follow Up**: Emerald gradient (`#10b981` to `#059669`)
- **Group Management**: Teal/WhatsApp green (`--wa-teal`, `#25D366`)
- **Scheduled Content**: Purple-blue gradient (`#667eea` to `#764ba2`)
- **Message Routing**: Purple gradient (`#a855f7` to `#7c3aed`)
- **Scripted Conversations**: Indigo gradient (`#6366f1` to `#4f46e5`)
- **AI Workspace**: Orange/coral gradient (`#da6a46` to `#d4562a`)

### Card Grid Layout (Equal Height)
When using a 2-column card grid (`col-lg-6`), do NOT use `h-100` on cards to force equal height — it absorbs `margin-bottom` and removes spacing between rows. Instead:
- Clamp variable-length text to single lines with `white-space: nowrap; overflow: hidden; text-overflow: ellipsis;` and add `title` attribute for hover tooltip
- Use `g-3` on the `.row` for consistent gap spacing (not `margin-bottom` on cards)
- Apply the same truncation to badge containers (remove `flex-wrap`, add `overflow: hidden`)

### Avatar Colors (Consistent Across Pages)
Use CSS class-based avatar colors (`avatar-color-1` through `avatar-color-8`) with gradient backgrounds, NOT inline `style="background: #hex;"`. The hash function should return a class name:
```javascript
function getAvatarColorClass(name) {
    if (!name) return 'avatar-color-1';
    let hash = 0;
    for (let i = 0; i < name.length; i++) {
        hash = name.charCodeAt(i) + ((hash << 5) - hash);
    }
    return `avatar-color-${(Math.abs(hash) % 8) + 1}`;
}
```
Reference `hubs.html` for the canonical gradient definitions. Each tool page must define matching `.contact-avatar.avatar-color-N` and `.activity-avatar-placeholder.avatar-color-N` CSS rules.

### Inline Expansion Panels (Not Static Top Panels)
When a "Generate" or action button on a card needs to show an expansion panel:
- Do NOT use a static panel at the top of the page — it's disorienting
- Dynamically inject the panel as a `col-12` div directly above the target card using `insertAdjacentHTML('beforebegin')`
- Use fixed element IDs (only one panel open at a time) so existing functions (`regenerateMessage`, `sendFollowup`, `loadBots`) work without changes
- `closePanel()` should `remove()` the DOM element, not hide it

### API Parameters — Match Hub Page Behavior
When a tool page calls the same API endpoint as a hub page, always compare parameters. Common miss: the tool page omits `hub_id` which the hub page sends. Without it, the server can't find the AI agent's API key, causing 400 errors. Always check the working hub page implementation first.

### Search Input Focus Styling
Search inputs should match the page theme on focus — set `border-color` to theme color and `box-shadow: none` (no glow):
```css
#searchInput:focus {
    border-color: #10b981; /* or page theme color */
    box-shadow: none;
}
```

### Activity Log / Execution Logging
When logging tool executions via `ToolMonitor.log_execution()`, include rich data for meaningful activity display:
- `input_data`: contact_name, bot_name, tone, message_preview (first 80 chars)
- `output_data`: contact_phone, predicted_intent, follow_up_reason
- Frontend should display contact name (fallback to phone), bot name, message preview (italic), intent/reason, exact datetime (not relative), and tone badge
- Reference `scheduled_content.html` activity log for the canonical styling pattern
- Avoid rendering empty wrapper divs — only render rows when content exists to prevent blank lines

### CSS Variables — Always Use These
Never hardcode colors/backgrounds. Use the CSS variables defined in `static/css/style.css :root`:
- **Backgrounds**: `var(--card-bg)` (white), `var(--hover-bg)` (light gray), `var(--wa-panel-bg)` (panel gray)
- **Text**: `var(--text-primary)`, `var(--text-secondary)`, `var(--text-muted)`
- **Borders**: `var(--border-color)`
- **Brand**: `var(--wa-green)`, `var(--wa-teal)` (both `#25D366`)
- **Shadow**: `var(--shadow)`

### Modal Styling
Every modal must use `var()` backgrounds and borders — not Bootstrap defaults. This ensures theme consistency:
```html
<div class="modal-content" style="background: var(--card-bg); border: 1px solid var(--border-color);">
    <div class="modal-header border-bottom" style="border-color: var(--border-color) !important;">
        <h5 class="modal-title"><i class="bi bi-icon me-2"></i>Title</h5>
        <button type="button" class="modal-close-btn" data-bs-dismiss="modal" aria-label="Close"></button>
    </div>
    <div class="modal-body">...</div>
    <div class="modal-footer border-top" style="border-color: var(--border-color) !important;">
        <button class="btn btn-sm btn-outline-secondary rounded-pill px-3" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-sm rounded-pill px-3" style="background: linear-gradient(135deg, #start, #end); color: white;">
            <i class="bi bi-check-lg me-1"></i>Confirm
        </button>
    </div>
</div>
```

### Border Radius Hierarchy
Consistent border-radius values across all components:
- **20px**: pill buttons (`rounded-pill`), inputs, form controls
- **12px**: cards, panels, list-group first/last items
- **10px**: dropdowns, alerts, toasts
- **6px**: dropdown items, inline code blocks

### Focus State — No Blue Glow
All focusable elements must use `box-shadow: none` on focus. Use `border-color` change only (theme color or `var(--wa-teal)`):
```css
.my-input:focus {
    border-color: var(--wa-teal); /* or page theme color */
    box-shadow: none;
    outline: none;
}
```
Never allow Bootstrap's default blue `box-shadow` glow to appear.

### Button Conventions
- Always use `rounded-pill` class on buttons
- **Primary/action**: gradient via inline style — `style="background: linear-gradient(135deg, #start, #end); color: white;"`
- **Secondary/cancel**: `btn-outline-secondary rounded-pill`
- **Size**: `.btn-sm` for most action buttons, `.btn` for main CTAs
- **Icons**: `<i class="bi bi-icon-name me-1"></i>` before button text

### Scrollbar Styling
Any scrollable container (`overflow-y: auto`, `max-height`, textarea, etc.) must have custom scrollbars:
```css
.my-scrollable::-webkit-scrollbar { width: 6px; }
.my-scrollable::-webkit-scrollbar-track { background: transparent; }
.my-scrollable::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.2); border-radius: 3px; }
.my-scrollable::-webkit-scrollbar-thumb:hover { background: rgba(0,0,0,0.3); }
.my-scrollable { scrollbar-width: thin; scrollbar-color: rgba(0,0,0,0.2) transparent; } /* Firefox */
```

## Testing

Test fixtures in `tests/conftest.py`:
- `client`: FastAPI TestClient with in-memory SQLite
- `registered_user`: Pre-created test user
- `auth_token` / `auth_headers`: For authenticated requests
