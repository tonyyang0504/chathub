This is a worktree (feature/messenger) of the ChatHub project for implementing
the Facebook Messenger adapter.

CONTEXT:
- Study app/platforms/base.py for the PlatformAdapter interface, PlatformType enum,
  AuthMethod, and PlatformCapabilities
- Study app/platforms/whatsapp/adapter.py as the reference implementation
- Study app/platforms/message_handler.py for shared utilities
- Study app/bots/manager.py to understand how adapters are called
- Study app/database.py for Message/Conversation/BotProfile models

YOUR TASK:
1. Create app/platforms/messenger/__init__.py and app/platforms/messenger/adapter.py
2. Implement MessengerAdapter(PlatformAdapter) using Meta Send API / Messenger Platform
3. Integration type: Webhook-based (receive via HTTP POST, send via Send API)
4. Auth method: API_TOKEN (Page Access Token)
5. Capabilities: groups=False, media=True, file_send=True, reactions=True,
   typing_indicator=True, supports_read_receipts=True, max_message_length=2000
6. The run() method should set up webhook endpoint, handle verification challenge
   (hub.verify_token, hub.challenge), process messages using message_handler utilities
7. Support message types: text, image, video, file attachments
8. Handle delivery/read receipts from Meta webhook events
9. Register MessengerAdapter in app/main.py
10. Use shared message_handler.py functions — do NOT duplicate DB/AI/WebSocket logic

NOTE: Messenger requires a Facebook Page + App with messaging permissions.
Handle webhook signature verification (X-Hub-Signature-256). Do NOT modify existing adapters.
