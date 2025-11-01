# Attachment Issues Investigation - 2025-11-01

## Summary

Investigation into two attachment-related bugs and discovery of disabled linter checks for private
method access.

## Issues Identified

### Issue #1: JSON Attachments Treated as "Binary Content Not Accessible"

**Symptoms:**

- JSON attachments from tools like `download_state_history` show as "Binary content not accessible"
  when using GoogleGenAI or OpenAI providers
- The attachment data (223KB JSON with 410 states) is not accessible to the LLM for processing

**Root Cause:**

- Base class `BaseLLMClient._create_attachment_injection()` has intelligent JSON handling logic
  (added in commit cb6cc85a)
- This logic provides two modes:
  - Small files (≤10KiB): Inject full JSON content inline
  - Large files (>10KiB): Inject JSON schema + metadata for symbolic querying via `jq` tool
- `GoogleGenAIClient` and `OpenAIClient` override `_create_attachment_injection()` for multimodal
  support (images/PDFs)
- These overrides don't call the base class method, so JSON handling is lost
- They treat all non-image/PDF content as "Binary content not accessible"

**Fix:**

- Modify provider implementations to delegate JSON/text handling to base class before applying
  provider-specific logic
- Proper layering: base class handles structured data, providers handle multimodal content

______________________________________________________________________

### Issue #2: Telegram Photos Not Accessible to Tools Cross-Turn

**Symptoms:**

- Telegram photo `b8f27c8e-64ab-4ce1-a01e-1e99f6dd81d6` uploaded in turn 1
- Tools trying to access it in turn 2 fail with "Attachment not found or access denied"
- File exists on disk, but database query returns None

**Root Cause:**

- `src/family_assistant/telegram/handler.py:338` calls `AttachmentRegistry._store_file_only()`
- This is a **private method** that:
  - ✅ Saves file to disk
  - ❌ **Does NOT create database record** in `attachment_metadata` table
  - Returns metadata with `source_type="file_only"`
- When tools call `fetch_attachment_object()`:
  - It queries `attachment_metadata` table
  - Query returns None (no record exists)
  - Tool cannot access the file

**Investigation Results:**

**Transaction Scope Analysis:**

- Initially suspected transaction isolation issues
- Investigation proved this wrong:
  - Each HTTP request creates a fresh `DatabaseContext` with new transaction
  - Each conversation turn gets completely independent transaction
  - Messages are saved in separate mini-transactions (lines 2226-2228 in `processing.py`)
- Transaction isolation cannot explain cross-turn access failures

**Actual Problem:**

- File storage and database registration are separate operations
- Telegram handler uses file-only storage (no DB record)
- Database query correctly returns None because no record was created

**Fix:**

- Replace `_store_file_only()` with `register_user_attachment()`
- This method:
  - Saves file to disk
  - Creates database record with proper metadata
  - Associates with conversation_id and user_id
  - Makes attachment accessible in future turns

______________________________________________________________________

## Linter Investigation: Why Wasn't Private Method Access Caught?

### Question

How did code calling `_store_file_only()` (a private method) pass linting?

### Findings

**Pylint Configuration (.pylintrc line 72):**

```python
W0212,  # protected-access (needed in tests for mocking/inspection)
```

- The `protected-access` check (W0212) is **globally disabled**
- Justification: "needed in tests for mocking/inspection"
- Problem: Disabled everywhere, not just in tests

**Ruff Configuration (pyproject.toml lines 318-349):**

```toml
select = [
    "E", "F", "UP", "B", "SIM", "I", "ANN", "ERA",
    "ASYNC", "FAST", "TC", "PLC", "PLR", "PLE", "PLW"
]
```

- SLF (flake8-self) rules are **not enabled**
- SLF001 would catch private member access

**Manual Check Results:**

```bash
ruff check --select SLF001
```

Found **11 violations** across the codebase:

- 3 calls to `_store_file_only()` in production code
- 3 calls to `_store_file_only()` in tests
- 2 calls to `_send_attachments()`
- 1 call to `_chunk_text_natively()`
- 1 call to `_last_error` (setter)
- 1 call to `_refresh_listener_cache()` in tests

### Recommendations (Deferred)

**Option 1: Enable SLF001 with per-file test ignores**

```toml
[tool.ruff.lint]
select = [..., "SLF"]

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["SLF001"]
```

- Catch violations in production code
- Allow flexibility in tests
- Enforce public API usage

**Option 2: Re-enable pylint W0212 with per-file ignores**

- Similar approach using pylint instead of ruff

**Decision:** Defer linter configuration changes to future session

______________________________________________________________________

## Implementation Status

- [x] Investigation completed
- [x] Root causes identified
- [x] Fixes planned
- [x] Telegram handler fix implemented (`telegram/handler.py:338`)
- [x] GoogleGenAI provider fix implemented
- [x] OpenAI provider fix implemented
- [x] All lint checks pass
- [x] Existing tests pass (verified with `test_telegram_photo_persistence_and_llm_context`)
- [ ] Linter changes (deferred to future session)

## Summary of Changes

### 1. Telegram Photo Registration Fix

**File**: `src/family_assistant/telegram/handler.py` (line 338)

**Before**:

```python
attachment_metadata = await self.telegram_service.attachment_registry._store_file_only(
    file_content=first_photo_bytes,
    filename=f"telegram_photo_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.jpg",
    content_type="image/jpeg",
)
```

**After**:

```python
async with self.get_db_context() as db_context:
    user_id_str = str(user.id) if user else "unknown"
    attachment_metadata = await self.telegram_service.attachment_registry.register_user_attachment(
        db_context=db_context,
        content=first_photo_bytes,
        filename=f"telegram_photo_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.jpg",
        mime_type="image/jpeg",
        conversation_id=str(chat_id),
        message_id=reply_target_message_id,
        user_id=user_id_str,
        description=f"Telegram photo from {user_name}",
    )
```

**Impact**: Telegram photos are now properly registered in the `attachment_metadata` table, making
them accessible to tools in future conversation turns.

### 2. GoogleGenAI JSON Attachment Fix

**File**: `src/family_assistant/llm/providers/google_genai_client.py` (line 135-151)

Added delegation to base class for JSON/text attachments before applying Gemini-specific multimodal
handling:

```python
# Handle JSON/text attachments using base class logic first
if (
    attachment.content
    and attachment.mime_type
    and (
        attachment.mime_type in {"application/json", "text/csv"}
        or attachment.mime_type.startswith("text/")
    )
):
    # Delegate to base class for intelligent JSON/text handling
    base_message = super()._create_attachment_injection(attachment)
    # Convert to Gemini format
    return {
        "role": "user",
        "parts": [{"text": base_message["content"]}]
    }
```

**Impact**: JSON attachments now get proper schema injection for large files or inline content for
small files, making structured data accessible to the model.

### 3. OpenAI JSON Attachment Fix

**File**: `src/family_assistant/llm/providers/openai_client.py` (line 77-93)

Applied same pattern as GoogleGenAI client, delegating to base class for JSON/text handling.

**Impact**: Consistent JSON attachment handling across all LLM providers.

______________________________________________________________________

## Related Files

- `src/family_assistant/telegram/handler.py` - Telegram photo handling
- `src/family_assistant/services/attachment_registry.py` - Attachment storage
- `src/family_assistant/llm/providers/google_genai_client.py` - GoogleGenAI provider
- `src/family_assistant/llm/providers/openai_client.py` - OpenAI provider
- `src/family_assistant/llm/__init__.py` - Base LLM client with JSON handling
- `src/family_assistant/tools/attachment_utils.py` - Tool attachment access

______________________________________________________________________

## Logs

Investigation based on production logs from 2025-11-01 05:30-05:36 UTC showing:

- User requesting pool temperature graph (delegate to data_visualization service)
- User uploading photo and requesting transformation
- Both operations failing due to attachment access issues
