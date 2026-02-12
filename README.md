# ChatHub

A multi-tenant platform for managing AI-powered WhatsApp bots with real-time monitoring, conversation analytics, and coordinated bot orchestration.

## Features

- **Multi-Bot Management** - Create and run multiple WhatsApp bots simultaneously
- **AI-Powered Responses** - Multi-provider support (OpenAI, Anthropic, Google, DeepSeek, Qwen, xAI Grok)
- **Real-Time Dashboard** - Monitor bot status, conversations, and analytics
- **QR Code Authentication** - WebSocket-based real-time QR code display
- **Conversation History** - Full message storage with media support (images, documents, audio)
- **Hub System** - Coordinate multiple bots with AI agents for routing and classification
- **Scheduled Messages** - Queue messages for automated delivery
- **Group Chat Support** - Smart response rules for group conversations
- **Proxy Support** - Run bots through proxies with authentication
- **Session Persistence** - Auto-recover bots after server restart

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy, Uvicorn
- **Browser Automation**: Playwright (Chromium)
- **AI**: OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Qwen, xAI Grok
- **Database**: SQLite (dev) / PostgreSQL (prod)
- **Frontend**: Jinja2 templates, Bootstrap, JavaScript

## Requirements

- Python 3.10+
- Node.js (for Playwright browsers)
- AI provider API key (OpenAI, Anthropic, Google, DeepSeek, Qwen, or xAI)
- Linux server with Xvfb (for headless deployment)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/tonyyang0504/chathub.git
cd chathub
```

### 2. Create virtual environment

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browser

```bash
playwright install chromium
```

### 5. Configure environment

Create a `.env` file:

```env
# Security (required)
SECRET_KEY=your-secure-random-string-here
ENCRYPTION_KEY=your-fernet-key-here

# Database
DATABASE_URL=sqlite:///./data/app.db

# Server
HOST=0.0.0.0
PORT=8000
DEBUG=false

# JWT
ACCESS_TOKEN_EXPIRE_MINUTES=1440
```

Generate encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 6. Run the server

```bash
python run.py
```

Access the dashboard at `http://localhost:8000`

## Server Deployment (Linux)

### Install Xvfb for virtual display

```bash
apt update
apt install -y xvfb
```

### Create Xvfb service

```bash
cat > /etc/systemd/system/xvfb.service << 'EOF'
[Unit]
Description=X Virtual Frame Buffer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1280x800x24
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl enable xvfb
systemctl start xvfb
```

### Create chathub service

```bash
cat > /etc/systemd/system/chathub.service << 'EOF'
[Unit]
Description=ChatHub
After=network.target xvfb.service
Requires=xvfb.service

[Service]
Type=simple
User=root
WorkingDirectory=/var/www/chathub
Environment="PATH=/var/www/chathub/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="DISPLAY=:99"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/var/www/chathub/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl enable chathub
systemctl start chathub
```

### Nginx reverse proxy (optional)

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Usage

### 1. Register an account

Navigate to `/auth/register` and create your account.

### 2. Create a bot

- Go to **Bots** page
- Click **Add Bot**
- Enter bot name and select AI provider with API key
- Configure system prompt and response parameters

### 3. Start the bot

- Click **Start** on your bot
- Scan the QR code with WhatsApp
- Bot will begin responding to messages

### 4. Monitor conversations

- View real-time messages in **Conversations**
- Check analytics in **Analytics** dashboard

## Project Structure

```
chathub/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Configuration
│   ├── database.py          # SQLAlchemy models
│   ├── auth/                # Authentication
│   ├── bots/                # Bot management
│   │   ├── routes.py        # API endpoints
│   │   ├── manager.py       # Bot instance manager
│   │   └── whatsapp_bot.py  # WhatsApp automation
│   ├── conversations/       # Message handling
│   ├── hubs/                # Multi-bot coordination
│   ├── analytics/           # Statistics
│   └── templates/           # HTML templates
├── static/                  # CSS, JS, images
├── data/                    # Database & sessions
├── requirements.txt
└── run.py
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /auth/login` | User authentication |
| `GET /api/bots/` | List user's bots |
| `POST /api/bots/` | Create new bot |
| `POST /api/bots/{id}/start` | Start bot |
| `POST /api/bots/{id}/stop` | Stop bot |
| `WS /api/bots/{id}/qr` | QR code WebSocket |
| `GET /api/conversations/{bot_id}` | Get conversations |
| `GET /api/analytics/dashboard` | Dashboard stats |

## Configuration Options

### Bot Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `system_prompt` | AI personality/instructions | - |
| `temperature` | Response creativity (0-2) | 0.7 |
| `max_tokens` | Max response length | 1000 |
| `response_delay_min` | Min delay before response (sec) | 3 |
| `response_delay_max` | Max delay before response (sec) | 8 |
| `max_history` | Messages to include in context | 10 |
| `group_chat_enabled` | Respond in group chats | false |
| `headless` | Run browser without GUI | false |

## Troubleshooting

### QR code not appearing
- Ensure Xvfb is running: `systemctl status xvfb`
- Check bot logs: `journalctl -u chathub -f`
- Verify DISPLAY environment variable is set

### Bot not responding
- Check AI provider API key is valid
- Verify WhatsApp session is active
- Review conversation logs in dashboard

### Session expired
- Stop and restart the bot
- Scan new QR code to re-authenticate

## License

MIT License

## Author

Tony Yang - [@tonyyang0504](https://github.com/tonyyang0504)
