# ChatHub

A multi-tenant platform for AI-powered messaging bots across 13 platforms with real-time monitoring, conversation analytics, and coordinated multi-bot orchestration.

## Supported Platforms

| Platform | Auth Method | Status |
|----------|------------|--------|
| WhatsApp | QR Code (Playwright) | Production |
| Telegram | Phone + Code (Telethon) | Production |
| Discord | Bot Token | Production |
| Facebook Page | Facebook OAuth | Production |
| Instagram | Facebook OAuth | Production |
| Slack | Bot + App Token (Socket Mode) | Production |
| Signal | signal-cli REST API | Ready |
| LINE | Channel Token | Ready |
| LinkedIn | OAuth | Ready |
| iMessage | BlueBubbles | Ready |
| Tinder | Auth Token (unofficial) | Beta |
| Bumble | Phone SMS (unofficial) | Beta |
| WeChat | QR Code | Planned |

## Features

### Core
- **Multi-Platform Bots** - Create and run bots across 13 messaging platforms
- **AI-Powered Responses** - 7 providers: OpenAI, Anthropic, Google, DeepSeek, Qwen, xAI Grok, Ollama
- **AI Provider Failover** - Auto-switch to fallback providers on failure
- **Per-Conversation Model** - Override AI model per conversation without editing bot settings
- **Voice Support** - STT (Whisper) for incoming voice messages + TTS for voice responses
- **DM Pairing** - Require admin approval for unknown senders before AI responds
- **Conversation Threading** - Reply-to on Discord, Slack, Telegram

### Dashboard
- **Real-Time Dashboard** - Bot status, stats, activity feed with hero gradient banners
- **Conversation Manager** - Full message history with media, model override, approve/reject senders
- **Analytics** - Bot-filterable stats, charts, daily table with pagination, sentiment breakdown
- **Health Check** - System/DB/bot/AI diagnostics with auto-repair

### Orchestration
- **Hub System** - Coordinate multiple bots with AI agents for routing and classification
- **Contact Analysis** - Sentiment, urgency, intent detection with auto-tagging
- **Contact Segmentation** - Filter contacts by criteria for targeted campaigns
- **Scheduled Content** - Queue messages for automated delivery with recipient targeting
- **Follow-Up Automation** - Auto-send follow-ups based on AI analysis
- **Scripted Conversations** - Multi-step message sequences across bots

### AI Tools
- **AI Workspace** - Embedded CLI (Claude/Codex/Gemini) with real-time streaming
- **AI Coder** - Code modification agent with sandbox preview and git worktrees
- **Tool Builder** - Build custom tools via AI with Docker sandbox and live preview
- **Custom Tool Event Bus** - Pub/sub system connecting 55+ events to custom tool hooks

### Integrations
- **External Webhooks** - Trigger actions via webhook URL (Zapier, Shopify, CRM)
- **Scheduled Reports** - Auto-generate daily/weekly hub summaries
- **Public API v1** - REST API with X-API-Key auth for third-party integration
- **Mobile API** - Lightweight endpoints for companion app
- **Chrome Extension** - Sidebar chat on any web page (skeleton)

## Tech Stack

- **Backend**: Python, FastAPI, SQLAlchemy, Uvicorn
- **Browser Automation**: Playwright (Chromium) for WhatsApp
- **AI**: OpenAI, Anthropic, Google, DeepSeek, Qwen, xAI Grok, Ollama
- **Platform SDKs**: Telethon (Telegram), discord.py, slack-bolt, httpx
- **Database**: SQLite (23 tables, auto-migration)
- **Frontend**: Jinja2, Bootstrap 5, HTMX, Chart.js
- **Sandbox**: Docker containers for Tool Builder preview

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/tonyyang0504/chathub.git
cd chathub
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Create `.env`:

```env
SECRET_KEY=your-secure-random-string
ENCRYPTION_KEY=your-fernet-key
DATABASE_URL=sqlite:///./data/app.db
HOST=0.0.0.0
PORT=8000
```

Generate encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Run

```bash
python run.py
```

Open `http://localhost:8000`, register, create a bot, and start messaging.

## Usage

1. **Register** at `/auth/register`
2. **Create a bot** - Pick a platform, enter AI provider + API key
3. **Start the bot** - Platform-specific auth (QR code, phone code, OAuth, token)
4. **Monitor** - Conversations, analytics, health check from the dashboard
5. **Scale** - Create hubs to coordinate multiple bots with AI routing

## Project Structure

```
chathub/
├── app/
│   ├── main.py                    # FastAPI app + router registration
│   ├── database.py                # 23 SQLAlchemy models + auto-migration
│   ├── config.py                  # Settings from .env
│   ├── ai/                        # AI provider abstraction (7 providers)
│   ├── auth/                      # JWT auth, encryption, ownership
│   ├── bots/                      # Bot CRUD, manager, WhatsApp automation
│   ├── platforms/                 # Platform adapters (13 platforms)
│   │   ├── base.py                # PlatformAdapter base class
│   │   ├── message_handler.py     # Shared message/AI/WebSocket logic
│   │   ├── send.py                # Platform-agnostic message sending
│   │   ├── telegram/              # Telethon adapter
│   │   ├── discord/               # discord.py adapter
│   │   ├── messenger/             # Facebook Graph API
│   │   ├── instagram/             # Instagram Graph API
│   │   ├── slack/                 # Slack Bolt (Socket Mode)
│   │   ├── signal/                # signal-cli REST API
│   │   ├── imessage/              # BlueBubbles API
│   │   └── facebook/              # OAuth flow
│   ├── conversations/             # Message list, send, export, WebSocket
│   ├── hubs/                      # Multi-bot coordination, scheduling
│   ├── analytics/                 # Stats, charts, conversation analytics
│   ├── agents/                    # AI agent CRUD + templates
│   ├── ai_workspace/              # Embedded CLI (Claude/Codex/Gemini)
│   ├── tools/                     # Custom tools, event bus, sandbox
│   ├── health.py                  # System diagnostics
│   ├── webhooks.py                # External webhook triggers
│   ├── api_v1.py                  # Public REST API
│   └── templates/dashboard/       # 20+ page templates
├── extensions/chrome/             # Browser extension (skeleton)
├── static/                        # CSS, JS, images
├── data/                          # Runtime data (DB, sessions, backups)
├── requirements.txt
└── run.py
```

## Deployment

### Development
```bash
python run.py
```

### Linux (systemd)
```bash
# Xvfb for WhatsApp headless browser
systemctl start xvfb  # DISPLAY=:99

# ChatHub service
systemctl start chathub
```

### Windows
```bash
python run.py
```

## API Overview

| Category | Endpoints | Description |
|----------|-----------|-------------|
| Auth | 9 routes | Login, register, profile, password |
| Bots | 19 routes | CRUD, start/stop, contacts, groups, QR WebSocket |
| Conversations | 11 routes | List, messages, send, export, model override |
| Hubs | 48 routes | CRUD, agents, contacts, content, groups, topics |
| Analytics | 5 routes | Overview, daily, per-bot, conversation analytics |
| Tools | 32 routes | Tool pages, monitoring, custom tool APIs |
| AI Workspace | 8 routes | Sessions, streaming WebSocket |
| Agents | 13 routes | CRUD, templates, test, history |
| Webhooks | 4 routes | Key management, trigger |
| Health | 2 routes | Diagnostics, quick fixes |
| Public API | 3 routes | Bots, send, conversations (X-API-Key) |

**336 routes total**

## License

MIT License

## Author

Tony Yang - [@tonyyang0504](https://github.com/tonyyang0504)
