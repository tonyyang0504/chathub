# Release Report: ChatHub v2.0.0

**Release Date:** 2026-03-06
**Tag:** `v2.0.0`
**Branch:** `main` (merged from `integration/merge-all`)
**Status:** Prepared locally, awaiting human approval to push

---

## Overview

ChatHub v2.0.0 introduces multi-platform support with 9 new platform adapters, a platform abstraction layer, shared message handling utilities, and comprehensive security hardening. This release brings the total supported platforms to 10 (including the existing WhatsApp adapter).

---

## New Platforms Added

| # | Platform | Auth Method | Connection Type |
|---|----------|------------|-----------------|
| 1 | Telegram | API Token | Long polling |
| 2 | Instagram | API Token | Webhook |
| 3 | Messenger | API Token | Webhook |
| 4 | WeChat | API Token | Webhook |
| 5 | LINE | API Token | Webhook |
| 6 | LinkedIn | OAuth 2.0 | Polling |
| 7 | Tinder | Credentials | Polling |
| 8 | Bumble | Credentials | Polling |
| 9 | Discord | API Token | WebSocket Gateway |

All adapters implement the `PlatformAdapter` abstract interface defined in `app/platforms/base.py`, supporting: `run`, `send_message`, `send_file`, `cleanup`, `get_contacts`, `get_groups`.

---

## Architecture Changes

- **Platform abstraction layer** (`app/platforms/base.py`): Abstract base class with `PlatformType` enum, `AuthMethod` enum, `PlatformCapabilities` dataclass, and `PlatformRegistry` singleton.
- **Shared message handler** (`app/platforms/message_handler.py`): Common DB operations, message deduplication, AI response generation, and media handling shared across all adapters.
- **Platform registry** (`app/platforms/registry.py`): Auto-discovery and registration of adapters at startup.
- **Unified send routing** (`app/platforms/send.py`): Route `send_message`/`send_file` calls through the correct adapter based on platform type.
- **Database migration** (`migrations/add_platform_type_field.py`): Adds `platform_type` and `platform_config` columns to support multi-platform conversations.

### New Dependencies

- `discord.py>=2.3.0`
- `python-telegram-bot>=21.0`
- `line-bot-sdk>=3.0.0`
- `defusedxml>=0.7.1`

---

## Security Fixes Applied

The following security issues were identified during code review and resolved before integration:

| Fix | Platforms Affected | Description |
|-----|--------------------|-------------|
| XXE prevention | WeChat | Replaced `xml.etree` with `defusedxml.ElementTree` to prevent XML External Entity attacks |
| Webhook hardening | Instagram, Messenger | Webhook signature verification made explicitly fail-closed (reject if no secret configured) |
| Token encryption | LinkedIn, Tinder, Bumble | OAuth tokens and session tokens encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256) |
| CSRF protection | LinkedIn | OAuth state parameter using `secrets.token_urlsafe(32)` with validation on callback |
| Async fixes | Tinder, Bumble, LinkedIn, Messenger | Replaced blocking I/O calls with async equivalents to prevent event loop stalls |
| Constant-time comparison | All webhook adapters | All signature checks use `hmac.compare_digest()` to prevent timing attacks |

---

## Test Results

| Metric | Value |
|--------|-------|
| Total tests | 173 |
| Passed | 173 |
| Failed | 0 |
| Errors | 0 |
| Warnings | 8 (deprecation, non-blocking) |
| Execution time | 0.08s |

### Test Coverage

- 28 platform abstraction tests (enums, capabilities, registry)
- 115 per-adapter tests (interface compliance, message splitting, credential parsing, signature verification, XML parsing)
- 30 shared utility tests (message building, conversation management, deduplication, media handling, send routing)

---

## Security Audit Results

**Verdict: PASSED (no CRITICAL issues)**

All 9 adapters passed security audit. Summary:

- Credentials encrypted at rest (Fernet AES)
- Webhook signatures verified with fail-closed behavior
- XML parsing uses defusedxml (XXE-safe)
- OAuth flows include CSRF protection
- All API calls use HTTPS (no TLS overrides)
- Database queries use ORM-parameterized filters (no SQL injection risk)

### Known Warnings (Non-Blocking)

1. **No file upload size limits** -- adapters rely on platform-side limits; recommend adding server-side `MAX_MEDIA_SIZE` check
2. **PII in logs** -- some adapters log user names and message previews at INFO level; recommend masking in production
3. **Open dependency version ranges** -- `>=` without upper bounds in `requirements.txt`; recommend pinning for production
4. **Unofficial API usage** -- Tinder and Bumble use unofficial APIs (ToS risk, API stability risk)

---

## Migration Notes

1. **Database migration required:** Run `python migrations/add_platform_type_field.py` to add `platform_type` and `platform_config` columns to the conversations and bot_profiles tables.
2. **New dependencies:** Run `pip install -r requirements.txt` to install the 4 new packages.
3. **Environment variables:** Each platform adapter requires its own configuration (API keys, tokens, secrets). Refer to individual adapter documentation for required config keys.
4. **Existing WhatsApp functionality:** No breaking changes to the existing WhatsApp adapter. It is now registered in the platform registry alongside the new adapters.
5. **Deprecation warnings:** Address SQLAlchemy `declarative_base()`, Pydantic class-based config, and FastAPI `regex` parameter deprecations before upgrading to Python 3.13.

---

## Merge Details

- **Merge commit:** `87edf64` on `main`
- **Tag:** `v2.0.0`
- **Files changed:** 40 files, +15,051 lines, -8 lines
- **Conflicts resolved:** 6 branches had conflicts in `app/main.py` (adapter import/registration ordering); all resolved by combining imports
- **Post-merge fixups:** Added missing `__init__.py` for LinkedIn, Tinder, Bumble; added Tinder/Bumble registrations to `app/main.py`

---

## Next Steps

1. Human review and approval of this release
2. Push `main` branch and `v2.0.0` tag to remote
3. Deploy to staging environment
4. Run integration tests against live platform sandboxes
5. Address WARNING-level security findings (file size limits, PII masking, dependency pinning)
6. Production deployment
