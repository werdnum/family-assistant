# Refactor: `add_message` to Accept `LLMMessage` Directly

## Problem

`MessageHistoryRepository.add_message()` has a ~20 keyword argument signature that decomposes
messages into loose fields (`role: str`, `content: str | None`,
`tool_calls: list[ToolCallItem] | None`, etc.). This is:

- **Fragile**: Easy to pass wrong combinations of fields for a role
- **Redundant**: We already have typed `LLMMessage` Pydantic models that enforce these constraints
- **Duplicative**: The `_validate_message` method reconstructs `LLMMessage` objects just to
  validate, then throws them away

## Design

### New Signature

Split metadata from message content. The new primary method accepts an `LLMMessage` directly:

```python
async def add_message(
    self,
    message: LLMMessage,
    *,
    # Metadata (not part of the message itself)
    interface_type: str,
    conversation_id: str,
    timestamp: datetime,
    interface_message_id: str | None = None,
    turn_id: str | None = None,
    thread_root_id: int | None = None,
    processing_profile_id: str | None = None,
    subconversation_id: str | None = None,
    user_id: str | None = None,
    reasoning_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
```

The message content fields (`role`, `content`, `tool_calls`, `tool_call_id`, `tool_name`,
`error_traceback`, `provider_metadata`, `attachments`) are extracted from the `LLMMessage` object.

### Extraction Logic

Extract DB columns from the typed message:

```python
role = message.role
content = message.content if hasattr(message, 'content') else None
tool_calls = message.tool_calls if isinstance(message, AssistantMessage) else None
tool_call_id = message.tool_call_id if isinstance(message, ToolMessage) else None
tool_name = message.name if isinstance(message, ToolMessage) else None
error_traceback = message.error_traceback if hasattr(message, 'error_traceback') else None
provider_metadata = message.provider_metadata if hasattr(message, 'provider_metadata') else None
attachments = message.attachments if isinstance(message, ToolMessage) else None
```

### `_validate_message` Removal

No longer needed — passing an `LLMMessage` is self-validating by construction.

### Backward Compatibility: `add()` alias

The existing `add(**kwargs)` alias currently used by all callers will be kept temporarily as a
compatibility shim that constructs the `LLMMessage` from kwargs and delegates to `add_message()`.
This allows incremental migration of callers.

### Migration of Callers

There are two categories of callers:

1. **Explicit kwargs callers** (user/system messages in processing.py, web_chat_interface.py,
   telegram/handler.py, communication.py, task_worker.py, tests): These construct simple messages
   and can be migrated to pass `UserMessage(...)`, `SystemMessage(...)`, or `AssistantMessage(...)`
   directly.

2. **Dict-unpacking callers** (`**msg_to_save` in processing.py): These get dicts from
   `process_message()` / `process_message_stream()`. The ideal fix is for those methods to return
   `LLMMessage` objects instead of dicts, but that's a larger scope. For now, these callers can use
   the `add()` compat shim or construct messages from the dicts using `dict_to_message()`.

### Milestones

1. **Add the new `add_message(message: LLMMessage, ...)` signature** and extract DB columns from the
   message. Rename existing `add_message` to a private `_add_message_from_kwargs`. Have `add()` shim
   delegate to the kwargs version.

2. **Migrate explicit-kwargs callers** to construct `LLMMessage` objects and call `add_message()`
   directly. This covers: `web_chat_interface.py`, `telegram/handler.py`, `communication.py`,
   `task_worker.py`, and the 3 explicit calls in `processing.py` (lines 2118, 2570, 2590).

3. **Migrate dict-unpacking callers** in `processing.py` (lines 2383, 2805, 2807) by using
   `dict_to_message()` to convert the dicts to `LLMMessage` before calling `add_message()`.

4. **Migrate test callers** to use the new signature.

5. **Remove the `add()` compat shim** and `_add_message_from_kwargs` once all callers are migrated.

## What's Out of Scope

- Changing `process_message()` / `process_message_stream()` to return `LLMMessage` instead of dicts
  (separate refactor)
- Changing the return type of `add_message` from `dict[str, Any] | None` (separate concern)
