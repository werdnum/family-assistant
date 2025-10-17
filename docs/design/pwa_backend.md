# Backend Push Notification Implementation Plan

## Overview

Complete backend push notification service with proper user tracking. Add `user_id` to
message_history table, initialize PushNotificationService in Assistant, and integrate with
WebChatInterface.

## Important Notes for Subagents

### Alembic Migrations

- **DO NOT** use `DATABASE_URL="sqlite+aiosqlite:///family_assistant.db" alembic ...`
- The correct `DATABASE_URL` is already set in the environment
- Simply run: `alembic revision --autogenerate -m "Add user_id to message_history"`
- **Note**: Should add `DATABASE_URL=` to `.claude/banned_commands.json` to prevent this mistake

### Testing

- Migration testing: Handled automatically by `tests/test_alembic.py` (pytest-alembic)
- All other testing via pytest - no manual app startup required

## Sequential Implementation Tasks

### Task 1: Read VAPID Keys from Environment

**File**: `src/family_assistant/__main__.py` **Delegate to**: focused-coder agent

Add VAPID key reading in `load_config()` after other secrets (around line 290):

```python
# PWA Configuration from Env Vars
config_data.setdefault("pwa_config", {})
config_data["pwa_config"]["vapid_public_key"] = os.getenv("VAPID_PUBLIC_KEY")
config_data["pwa_config"]["vapid_private_key"] = os.getenv("VAPID_PRIVATE_KEY")
```

**Testing**: Unit test that verifies config loads keys from environment

**Commit**: `feat(pwa): Read VAPID keys from environment variables`

______________________________________________________________________

### Task 2: Add user_id Column to message_history Table

**File**: `alembic/versions/YYYYMMDD_*.py` (new migration) **Delegate to**: focused-coder agent

**IMPORTANT**: DATABASE_URL is already in environment - just run:

```bash
alembic revision --autogenerate -m "Add user_id to message_history"
```

Create migration:

```python
# Add user_id column (nullable for backward compatibility)
op.add_column('message_history',
    sa.Column('user_id', sa.String(255), nullable=True))
# Add index for efficient user lookups
op.create_index('ix_message_history_user_id', 'message_history', ['user_id'])
```

**Testing**: Automated by `tests/test_alembic.py` (pytest-alembic)

**Commit**: `feat(pwa): Add user_id column to message_history table`

______________________________________________________________________

### Task 3: Update Message History Repository to Store user_id

**Files**:

- `src/family_assistant/storage/repositories/message_history.py`
- `src/family_assistant/storage/message_history.py` (table definition)

**Delegate to**: focused-coder agent

Update table definition and repository:

- Add `user_id` column to message_history table definition
- Update `add()` method to accept optional `user_id` parameter
- Store `user_id` when provided
- Return `user_id` in message dict

**Testing**: Unit test that user_id is stored and retrieved

**Commit**: `feat(pwa): Update message history repository to store user_id`

______________________________________________________________________

### Task 4: Update Chat API to Pass user_id When Saving Messages

**File**: `src/family_assistant/web/routers/chat_api.py` **Delegate to**: focused-coder agent

Update chat endpoints to pass user_id:

- `send_message` endpoint: Pass `current_user["user_identifier"]` as `user_id` when saving user
  message
- `send_message_stream` endpoint: Same update
- Assistant responses get same user_id from conversation context

**Testing**: Functional test that messages include user_id

**Commit**: `feat(pwa): Pass user_id when saving web chat messages`

______________________________________________________________________

### Task 5: Complete PushNotificationService Implementation

**File**: `src/family_assistant/services/push_notification.py` **Delegate to**: focused-coder agent

Implement py-vapid integration:

- Import py-vapid and base64
- Decode URL-safe base64 VAPID keys (strip padding with `.rstrip('=')`)
- Implement `send_notification()` using py-vapid's `webpush()`
- Fetch subscriptions from database via repository for user_identifier
- Send to all user subscriptions
- Handle 410 Gone responses → delete stale subscriptions via repository
- Error handling: log failures, don't raise exceptions
- Type hints for all methods

**Testing**: Unit test with mocked httpx.post responses

**Commit**: `feat(pwa): Implement py-vapid push notification sending`

______________________________________________________________________

### Task 6: Add PushNotificationService Tests

**File**: `tests/unit/test_push_notification_service.py` (new) **Delegate to**: focused-coder agent

Test in isolation:

- Test sending to single subscription
- Test sending to multiple subscriptions
- Test handling 410 Gone (verify cleanup via repository)
- Test handling other HTTP errors (4xx, 5xx)
- Test disabled service (no VAPID keys - should not send)
- Mock httpx.post and repository methods

**Testing**: `pytest tests/unit/test_push_notification_service.py -xq`

**Commit**: `test(pwa): Add unit tests for PushNotificationService`

______________________________________________________________________

### Task 7: Initialize PushNotificationService in Assistant

**File**: `src/family_assistant/assistant.py` **Delegate to**: focused-coder agent

In `setup_dependencies()` after AttachmentRegistry (around line 423):

```python
# Initialize PushNotificationService
vapid_public_key = self.config.get("pwa_config", {}).get("vapid_public_key")
vapid_private_key = self.config.get("pwa_config", {}).get("vapid_private_key")

from family_assistant.services.push_notification import PushNotificationService
self.push_notification_service = PushNotificationService(
    vapid_private_key=vapid_private_key,
    vapid_public_key=vapid_public_key
)
# Store in app.state for lifespan to retrieve
self.fastapi_app.state.push_notification_service = self.push_notification_service
logger.info(f"PushNotificationService initialized (enabled={self.push_notification_service.enabled})")
```

**Testing**: Existing test suite should pass

**Commit**: `feat(pwa): Initialize PushNotificationService in Assistant`

______________________________________________________________________

### Task 8: Integrate Push Notifications in WebChatInterface

**Files**:

- `src/family_assistant/web/web_chat_interface.py`
- `src/family_assistant/web/app_creator.py`

**Delegate to**: focused-coder agent

**WebChatInterface changes**:

- Add `push_notification_service: PushNotificationService | None = None` to `__init__`
- Store as instance variable
- In `send_message()` after successful save:
  ```python
  # Send push notification if enabled
  if self.push_notification_service and self.push_notification_service.enabled:
      try:
          # Get user_id from saved message
          user_id = saved_message.get("user_id")
          if not user_id:
              # Fallback: query recent messages in conversation
              recent = await db_context.message_history.get_recent(
                  interface_type="web",
                  conversation_id=conversation_id,
                  limit=1,
                  max_age=timedelta(days=365)
              )
              user_id = recent[0]["user_id"] if recent else None

          if user_id:
              await self.push_notification_service.send_notification(
                  user_identifier=user_id,
                  title="New message from Family Assistant",
                  body=text[:100],  # Truncate long messages
                  db_context=db_context
              )
      except Exception as e:
          logger.warning(f"Failed to send push notification: {e}", exc_info=True)
  ```

**app_creator.py lifespan** (line 164):

```python
# Retrieve push service from app.state (injected by Assistant)
push_notification_service = getattr(app.state, 'push_notification_service', None)

app.state.web_chat_interface = WebChatInterface(
    app.state.database_engine,
    push_notification_service=push_notification_service
)
```

**Testing**: Functional test

**Commit**: `feat(pwa): Integrate push notifications in WebChatInterface`

______________________________________________________________________

### Task 9: Integration Tests

**File**: `tests/functional/web/test_web_chat_push_integration.py` (new) **Delegate to**:
focused-coder agent

End-to-end functional test:

- Create test database with message containing user_id
- Create fake PushNotificationService that records calls
- Create WebChatInterface with test engine and fake service
- Send assistant message
- Verify message saved with user_id
- Verify push notification sent to correct user_id with correct content
- Test graceful error handling when push service fails

**Testing**: `pytest tests/functional/web/test_web_chat_push_integration.py -xq`

**Commit**: `test(pwa): Add WebChatInterface push notification integration tests`

______________________________________________________________________

### Task 10: Update Documentation and Add banned_commands Entry

**Files**:

- `docs/design/pwa.md`
- `AGENTS.md`
- `.claude/banned_commands.json`

**Delegate to**: focused-coder agent

**pwa.md**: Mark Part 3 backend as ✅ complete, update notification trigger status

**AGENTS.md**: Add VAPID environment variables section:

```markdown
- `VAPID_PUBLIC_KEY` - VAPID public key for push notifications (URL-safe base64, no padding)
- `VAPID_PRIVATE_KEY` - VAPID private key for push notifications (URL-safe base64, no padding)
  - Generate using: `python scripts/generate_vapid_keys.py`
  - Format: Raw key bytes encoded with `base64.urlsafe_b64encode().rstrip(b'=').decode()`
```

**.claude/banned_commands.json**: Add entry to prevent DATABASE_URL override:

```json
{
  "regexp": "DATABASE_URL=.*alembic",
  "explanation": "DATABASE_URL is already correctly set in the environment. Do not override it when running alembic commands."
}
```

**Testing**: Review documentation for clarity

**Commit**: `docs(pwa): Update progress, document VAPID vars, and add banned command`

______________________________________________________________________

## Testing Strategy

All testing via pytest:

1. **Task 2**: Alembic migration tested automatically by `tests/test_alembic.py`
2. **Task 3**: Unit tests for repository user_id storage
3. **Task 4**: Functional test that API passes user_id
4. **Task 6**: Unit tests for PushNotificationService
5. **Task 9**: Integration tests for full flow
6. **Final**: `poe test` for full suite

## Architecture & Design Decisions

### User Identification

- **Problem**: WebChatInterface.send_message() receives only conversation_id (UUID)
- **Solution**: Add user_id column to message_history table
- **Alternative considered**: Parse user_id from conversation_id string - rejected as brittle
- **Implementation**: Query message_history to get user_id from conversation

### Dependency Injection Pattern

- PushNotificationService initialized in Assistant.setup_dependencies()
- Stored in app.state.push_notification_service for lifespan to access
- WebChatInterface receives service via constructor injection from lifespan
- Follows established pattern: no retrieval from app.state in business logic

### Error Handling

- Push notification failures are logged but don't fail message delivery
- Stale subscriptions (410 Gone) are automatically cleaned up
- Service gracefully handles missing VAPID keys (disabled state)

## Success Criteria

✅ user_id column added to message_history (proper DB schema) ✅ Messages saved with user_id from
authentication context ✅ VAPID keys read from environment in config ✅ PushNotificationService sends
notifications via py-vapid ✅ Stale subscriptions cleaned up on 410 Gone ✅ Service initialized in
Assistant, stored in app.state ✅ WebChatInterface receives service via constructor (DI from
lifespan) ✅ Push notifications sent on assistant message delivery ✅ User identified from database,
not string parsing ✅ All pytest tests pass ✅ `poe test` passes ✅ Documentation complete ✅
banned_commands.json updated to prevent DATABASE_URL override

## Progress Tracking

This document will be updated as tasks are completed. See `docs/design/pwa.md` for overall PWA
implementation progress.
