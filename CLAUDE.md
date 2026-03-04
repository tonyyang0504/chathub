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

### Core Components

**Bot Lifecycle**: `BotProfile` (DB) → `BotInstance` (memory) → WhatsApp automation → `Conversations` (DB)

- `app/bots/manager.py`: `BotManager` singleton manages `BotInstance` objects with callback system for QR codes and status updates
- `app/bots/whatsapp_bot.py`: Playwright-based browser automation (707KB - the core bot logic)
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

**WebSocket Real-time**:
- QR code display: `WS /api/bots/{id}/qr`
- Conversation updates via `conversation_ws_manager`
- Cross-thread calls: `conversation_ws_manager.set_main_loop()` stores asyncio loop

### Key Patterns

**Authentication & Multi-tenancy**:
```python
from app.auth.utils import get_current_user
@router.get("/path")
async def endpoint(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # All resources filtered by user.id
```

**API Key Encryption**: Use `encrypt_string()` / `decrypt_string()` from `app/auth/utils.py` for storing API keys

**Async/Threading**: FastAPI async routes, but Playwright runs in thread. `BotInstance` uses `threading.Lock` for `_queue_lock`

**Message Deduplication**: WhatsApp message ID + 5-minute content hash fallback

### Database

SQLAlchemy models in `app/database.py` (50+ tables). Key tables:
- `users`, `bot_profiles`, `conversations`, `messages`
- `hubs`, `hub_bot_memberships`, `ai_agents`
- `contacts`, `contact_tags`, `scheduled_contents`

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

## Testing

Test fixtures in `tests/conftest.py`:
- `client`: FastAPI TestClient with in-memory SQLite
- `registered_user`: Pre-created test user
- `auth_token` / `auth_headers`: For authenticated requests
