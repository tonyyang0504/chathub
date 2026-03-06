This is a worktree (feature/wechat) of the ChatHub project for implementing
the WeChat adapter.

CONTEXT:
- Study app/platforms/base.py for the PlatformAdapter interface, PlatformType enum,
  AuthMethod, and PlatformCapabilities
- Study app/platforms/whatsapp/adapter.py as the reference implementation
- Study app/platforms/message_handler.py for shared utilities
- Study app/bots/manager.py to understand how adapters are called
- Study app/database.py for Message/Conversation/BotProfile models

YOUR TASK:
1. Create app/platforms/wechat/__init__.py and app/platforms/wechat/adapter.py
2. Implement WeChatAdapter(PlatformAdapter) using WeChat Official Account API
3. Integration type: Webhook-based (receive XML messages via HTTP POST)
4. Auth method: CREDENTIALS (AppID + AppSecret)
5. Capabilities: groups=True, media=True, file_send=True, reactions=False,
   typing_indicator=False, max_message_length=2048
6. The run() method should:
   - Set up webhook endpoint for receiving XML-formatted messages
   - Handle WeChat server verification (signature check with token, timestamp, nonce)
   - Parse XML message format (text, image, voice, video, location, link)
   - Use message_handler utilities for DB/AI processing
   - Reply in XML format within 5 seconds (WeChat requirement) or use async customer service API
7. Handle access_token management (2-hour expiry, refresh cycle)
8. Register WeChatAdapter in app/main.py
9. Add required dependencies (xmltodict or lxml for XML parsing)
10. Use shared message_handler.py functions — do NOT duplicate DB/AI/WebSocket logic

NOTE: WeChat uses XML not JSON. Messages must be replied within 5 seconds or use
the async customer service message API. Handle message encryption (AES) if enabled.
Do NOT modify existing adapters.
