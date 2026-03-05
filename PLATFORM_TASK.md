This is a worktree (feature/line) of the ChatHub project for implementing
the Line adapter.

CONTEXT:
- Study app/platforms/base.py for the PlatformAdapter interface, PlatformType enum,
  AuthMethod, and PlatformCapabilities
- Study app/platforms/whatsapp/adapter.py as the reference implementation
- Study app/platforms/message_handler.py for shared utilities
- Study app/bots/manager.py to understand how adapters are called
- Study app/database.py for Message/Conversation/BotProfile models

YOUR TASK:
1. Create app/platforms/line/__init__.py and app/platforms/line/adapter.py
2. Implement LineAdapter(PlatformAdapter) using LINE Messaging API
3. Integration type: Webhook-based (receive events via HTTP POST)
4. Auth method: API_TOKEN (Channel Access Token + Channel Secret)
5. Capabilities: groups=True, media=True, file_send=True, reactions=True,
   typing_indicator=True, supports_read_receipts=True, max_message_length=5000
6. The run() method should:
   - Set up webhook endpoint for receiving LINE events
   - Validate webhook signature (X-Line-Signature using HMAC-SHA256)
   - Handle event types: message, follow, unfollow, join, leave
   - Support message types: text, image, video, audio, file, location, sticker
   - Use message_handler utilities for DB/AI processing
   - Reply using reply token (within 1 minute) or push message API
7. Register LineAdapter in app/main.py
8. Add line-bot-sdk to requirements.txt
9. Use shared message_handler.py functions — do NOT duplicate DB/AI/WebSocket logic

NOTE: LINE reply tokens expire in 1 minute. For delayed responses, use the push
message API instead. Do NOT modify existing adapters.
