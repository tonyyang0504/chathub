This is a worktree (feature/telegram) of the ChatHub project for implementing
the Telegram adapter.

CONTEXT:
- Study app/platforms/base.py for the PlatformAdapter interface, PlatformType enum,
  AuthMethod, and PlatformCapabilities
- Study app/platforms/whatsapp/adapter.py as the reference implementation
- Study app/platforms/message_handler.py for shared utilities (DB ops, AI response
  building, WebSocket broadcasting, media handling)
- Study app/bots/manager.py to understand how adapters are called
- Study app/database.py for Message/Conversation/BotProfile models

YOUR TASK:
1. Create app/platforms/telegram/__init__.py and app/platforms/telegram/adapter.py
2. Implement TelegramAdapter(PlatformAdapter) using python-telegram-bot library
3. Integration type: Bot API with webhook OR long polling
4. Auth method: API_TOKEN (bot token from @BotFather)
5. Capabilities: groups=True, media=True, file_send=True, reactions=True,
   typing_indicator=True, max_message_length=4096
6. The run() method should start polling/webhook loop, receive messages,
   use message_handler utilities for DB storage and AI response, then send replies
7. Register TelegramAdapter in app/main.py
8. Add python-telegram-bot to requirements.txt
9. Use shared message_handler.py functions — do NOT duplicate DB/AI/WebSocket logic

IMPORTANT: Do NOT modify whatsapp_bot.py or any existing platform adapters.
Study the code thoroughly before writing anything.
