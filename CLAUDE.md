# CLAUDE.md

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

### Custom Tool Card Controls (Toggle + Delete)
Toggle switch and delete button are positioned independently, NOT side by side:
- **Toggle switch**: `position: absolute; top: 10px; right: 10px;` — uses CSS variable `--tool-theme-color` set from the tool's gradient end color so the checked state matches the tool's theme
- **Delete button**: `position: absolute; bottom: 10px; right: 12px;` — vertically aligned with the toggle on the right edge
- Delete icon: borderless `.btn-trash` class, muted by default (`opacity: 0.5; color: var(--text-muted)`), red on hover
- Toggle CSS uses `var(--tool-theme-color, #6366f1)` for `background-color` and `border-color` on `:checked` and `:focus` states
- Set the variable inline: `style="--tool-theme-color: {{ tool_gradients[tool.id][1] }};"`

### Custom Tool Hero Layout — Match Built-in Tools
Custom tool pages AND detail pages must use the same two-column hero layout as built-in tool pages:
```html
<div class="tool-hero"> <!-- or .attention-hero, .snapshot-hero, etc. -->
    <div class="row align-items-center">
        <div class="col-lg-8">
            <h1><i class="bi bi-icon me-3"></i>Tool Title</h1>
            <p>Description text...</p>
        </div>
        <div class="col-lg-4 text-center d-none d-lg-block">
            <i class="bi bi-decorative-icon" style="font-size: 8rem; opacity: 0.3;"></i>
        </div>
    </div>
</div>
```
- The decorative icon should differ from the heading icon (e.g., heading `bi-calendar-event`, decorative `bi-calendar-check`)
- Hidden on small screens via `d-none d-lg-block`
- The detail page (`custom_tool_detail.html`) uses the tool's own icon for both heading and decorative icon
- Do NOT use a separate flex layout with the icon in a box next to the title — keep the icon inline in the `<h1>`

### Custom Tool Theme Colors — Extract from Template
Custom tools built by the AI Tool Builder hardcode their gradient colors in their HTML template hero section, which may differ from the DB `gradient_start`/`gradient_end` defaults. To keep card colors in sync with tool page colors:
- Parse the actual gradient from the tool's template file: `app/tools/custom/{name}/templates/{name}.html`
- Use regex on the first 500 chars: `linear-gradient(135deg, #hex 0%, #hex 100%)`
- Pass resolved colors to the template via a separate dict (e.g., `tool_gradients[tool.id]`), NOT by setting attributes on SQLAlchemy model objects (ORM objects silently ignore arbitrary attribute assignment)
- Fallback to DB values only if template parsing fails

### SQLAlchemy ORM — No Dynamic Attributes
Never set arbitrary attributes on SQLAlchemy model instances (e.g., `tool._my_attr = value`) — they will silently fail in templates. Instead, pass supplementary data as a separate dict to the Jinja2 template context.

### AI Tools Unified Styling (AI Workspace, AI Coder, Tool Builder)

All three AI tools share the same 3-panel layout and must stay visually aligned. AI Workspace is the canonical reference.

**Layout**: Left sidebar | Center chat | Right panel (Tools). All use `.claude-app` flex container with `.claude-sidebar`, `.claude-main`, `.claude-artifacts`.

**Provider Theming**: All three use the same CSS custom properties driven by `data-provider` on `.claude-app`:
- Claude: `#da6a46` (orange/coral) — all three tools use this same color for Claude
- Codex: `#10a37f`, Gemini: `#4285f4`, ChatHub: `#8b5cf6`

**Session Type Separation**: Each tool filters sessions by `session_type` in the DB:
- AI Workspace: `claude_code`, Tool Builder: `tool_builder`, AI Coder: `ai_coder`
- Never query without this filter or sessions will mix across tools

**Multi-Session Support**: AI Workspace supports multiple simultaneous sessions per user (`_sessions` keyed by `session_id`). AI Coder and Tool Builder enforce single-session (`_sessions` keyed by `user_id`).

**Provider Auto-Selection**: On page load, `loadSettings()` auto-selects the best provider by priority: Claude > Codex > Gemini > ChatHub (based on which API keys/membership are configured). On save, use `loadSettings(false)` to skip auto-selection and preserve the current provider.

**Provider Locking**: Once a session is created with a provider, lock the provider pills (`.provider-pill.disabled`) so the user can't switch mid-session. Unlock on `newSession()`.

**Per-Provider Auth Methods**: Each provider has its own auth method field in `AiWorkspaceSettings`:
- Claude: `auth_method` ("api_key" or "membership")
- Codex: `codex_auth_method` ("api_key" or "membership") — device auth via `codex login --device-auth`
- Gemini: `gemini_auth_method` ("api_key" or "membership") — Google OAuth via `~/.gemini/oauth_creds.json`
- When membership: don't pass API key env var → CLI uses stored OAuth credentials
- Note: `codex login status` outputs to stderr, not stdout

**Left Sidebar**:
- Headers: AI Workspace = "Tasks" (`bi-list-task`), AI Coder = "Coding" (`bi-code-slash`), Tool Builder = "Builds" (`bi-hammer`)
- Session items show provider badge (`.provider-badge.pb-{provider}`) before status badge
- Delete button is inline in `.session-meta` flex row (not absolutely positioned), uses `bi-trash3`, `border-radius: 6px`
- `.session-item:hover` uses `var(--border-color)` background (not provider-tinted)
- `.session-meta`: `gap: 6px; flex-wrap: nowrap; overflow: hidden;` with child span truncation
- `.prompt-preview` must have `title` attribute for hover tooltip, `font-size: 0.8rem`
- `.btn-new-session`: 32x32, `border-radius: 12px`, no border
- Empty state text: AI Workspace = "No tasks yet", AI Coder = "No coding sessions yet", Tool Builder = "No builds yet"

**Center Section**:
- Sidebar toggle button (`.btn-sidebar-toggle`): 34x34, `border-radius: 12px`, no border, `bi-layout-sidebar-inset` icon
- Tools toggle button: `bi-layers` icon, label "Tools", uses standard `btn btn-sm btn-outline-secondary rounded-pill` (no `#artifactsToggleBtn` CSS override)
- Provider/model label: `<span id="providerModelLabel">` near voice button in input toolbar, updated via `updateProviderModelLabel()`
- Empty state: Bootstrap icon (no background) at 3.5rem/0.5 opacity (AI Workspace: `bi-stars`, AI Coder: `bi-terminal`, Tool Builder: `bi-hammer`)

**Right Panel (Tools)**:
- Header title: "Tools" with `bi-layers` icon, only close button (no refresh button)
- Shows artifact cards for tool calls (same system across all three)
- AI Coder/Tool Builder: collapsible Build Status section below artifacts, action buttons (Apply/Publish + Discard) in footer
- Footer buttons: `border-radius: 20px`, `padding: 0.4rem 0.75rem`, no `flex-wrap`
- `.artifacts-header` padding: `0.65rem 0.85rem`

**Tool Use Rendering**: Tool calls render as clickable pills in chat (`.msg-tool-pill`) that open artifact cards in the right panel. Must handle three event types:
1. `content_block_start` with `tool_use` type — pass `toolUseId`
2. `assistant` with `message.content` containing `tool_use` blocks — update existing artifact or create new
3. `content_block_delta` with `input_json_delta` — stream partial input into artifact card

**Settings Modal**:
- Use `modal-close-btn` class (not Bootstrap's `btn-close`)
- On save: hide modal FIRST via `hidden.bs.modal` event, THEN call `loadSettings(false)` — prevents visible flash during close animation

**Toast Notifications**: Use the same `showToast()` implementation as `app.js` — with icons (`bi-check-circle-fill`, etc.), 5000ms duration, and `error`→`danger` type mapping

**AI Coder Modifications Section**: Collapsed by default, toggle arrow integrated in header, "View all" link with `stopPropagation()`

**AI Coder/Tool Builder Post-Publish**: After apply/publish, session auto-resumes (new worktree created from updated main). User can continue modifying. Tool Builder updates existing `BuiltTool` by name (no duplicates).

**Session Sorting (Frontend)**: Sessions sorted by `ended_at || created_at` with `null ended_at` (active sessions) always first. ChatHub Agent datetime must include `"Z"` UTC suffix (matching CLI sessions) to prevent timezone-based mis-sorting.

**ChatHub Agent WebSocket**:
- In `startSession()`, set `currentSessionEngine` BEFORE `currentSessionId` to avoid wrong WS endpoint
- WS while loop: `active.is_running or active.is_waiting` (same as CLI — keeps WS open for follow-ups)
- Always replay `output_buffer` regardless of `no_replay` param (events can race ahead of WS connection)

**ChatHub Agent Session Ordering**: Don't set `ended_at` on first turn completion (keeps session at top with `null ended_at`). Only set `ended_at` on follow-up turns via `_run_followup()`.

**Artifact Timestamps**: When replaying ChatHub Agent tool calls from DB, pass `msg.created_at` as third param to `appendToolUse(content, toolName, timestamp)`. Without this, artifact cards show current time instead of original time. CLI sessions already pass timestamps via `renderStreamEvent(data, msg.created_at)`.

**Tools Index Cards** (`tools/index.html`):
- AI Workspace: `bi-stars`, teal gradient (`#0ea5e9/#06b6d4`)
- Tool Builder: `bi-hammer`, purple gradient (`#6366f1/#8b5cf6`)
- AI Coder: `bi-terminal`, dark navy gradient (`#1a1a2e/#16213e`)

**Intent Routing**: The AI agent CLI detects code-change or tool-building intent and includes `[SUGGEST:AI_CODER]` or `[SUGGEST:TOOL_BUILDER]` markers in its response (instruction added to system context in `manager.py` and `agent_loop.py`). The frontend strips these markers and renders an inline suggestion card with a link to the appropriate tool. No regex pre-filtering — the AI itself determines intent. AI Coder and Tool Builder pick up pending prompts via `sessionStorage`.

**Google AI Provider**: Uses `google-genai` SDK (not deprecated `google-generativeai`). Default model: `gemini-2.0-flash`.

## Testing

Test fixtures in `tests/conftest.py`:
- `client`: FastAPI TestClient with in-memory SQLite
- `registered_user`: Pre-created test user
- `auth_token` / `auth_headers`: For authenticated requests
