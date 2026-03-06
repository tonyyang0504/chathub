This is a worktree (feature/instagram) of the ChatHub project for implementing
the Instagram adapter.

CONTEXT:
- Study app/platforms/base.py for the PlatformAdapter interface, PlatformType enum,
  AuthMethod, and PlatformCapabilities
- Study app/platforms/whatsapp/adapter.py as the reference implementation
- Study app/platforms/message_handler.py for shared utilities
- Study app/bots/manager.py to understand how adapters are called
- Study app/database.py for Message/Conversation/BotProfile models

YOUR TASK:
1. Create app/platforms/instagram/__init__.py and app/platforms/instagram/adapter.py
2. Implement InstagramAdapter(PlatformAdapter) using Meta Instagram Messaging API
3. Integration type: Webhook-based (receive messages via HTTP POST callback)
4. Auth method: OAUTH (Instagram Business/Creator account + Facebook Page token)
5. Capabilities: groups=False, media=True, file_send=False, reactions=True,
   typing_indicator=True, supports_read_receipts=True, max_message_length=1000
6. The run() method should set up webhook endpoint for receiving DMs,
   process incoming messages using message_handler utilities, send replies via Graph API
7. Add webhook verification endpoint (GET) for Meta's challenge verification
8. Register InstagramAdapter in app/main.py
9. Add httpx or requests to requirements.txt if needed
10. Use shared message_handler.py functions — do NOT duplicate DB/AI/WebSocket logic

NOTE: Instagram DM API requires a Facebook Page linked to an Instagram Professional
account. Handle OAuth token refresh. Do NOT modify existing adapters.
