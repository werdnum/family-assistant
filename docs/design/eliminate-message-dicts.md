# Plan: Eliminate Message Dicts from Processing Pipeline

## Goal

Replace `dict[str, Any]` message representations in `processing.py` with typed `LLMMessage` objects,
and migrate all `add()` callers to `add_message()`.

## Current State

### `process_message_stream()` yields `tuple[LLMStreamEvent, dict[str, Any]]`

The second element (`message_dict`) is built in two ways:

1. **Assistant messages** (line ~816): A hand-built dict with keys `role`, `content`, `tool_calls`,
   `reasoning_info`, `provider_metadata`, `tool_call_id`, `error_traceback`.
2. **Tool messages** (line ~1101+): `message_to_json_dict(llm_message)` plus `tool_name` and
   sometimes `attachments`. The `llm_message` (a `ToolMessage`) already exists at each site.

### `process_message()` wraps `process_message_stream()` and returns

`tuple[list[dict[str, Any]], dict[str, Any] | None, list[str] | None]`

### Callers of the yielded/returned message dicts

1. **`handle_chat_interaction` non-streaming path** (line ~2358): Iterates `generated_turn_messages`
   (dicts), copies each, enriches with metadata, splats into
   `db_context.message_history.add(**msg_to_save)`. Reads `role`, `content`, `internal_id` from
   return.

2. **`handle_streaming_chat_interaction`** (line ~2763): Same pattern — copies dict, enriches,
   splats into `add(**msg_to_save)`.

3. **Inside `process_message_stream` itself** (line ~964): Reads `history_message.get("tool_name")`
   to detect `attach_to_response`.

### `add()` shim

3 call sites, all in `processing.py`. They splat a dict of kwargs. Return value used only in the
non-streaming path to extract `internal_id`.

### `add_message()` return type

Currently `dict[str, Any] | None`. The only field ever read from it is `internal_id`.

## Plan

### Milestone 1: Change `process_message_stream` to yield `LLMMessage` instead of dicts

**What changes:**

- The `assistant_message_for_turn` dict (line ~816) becomes an `AssistantMessage` object. The
  `reasoning_info` and `provider_metadata` are already being serialized — we'll keep
  `reasoning_info` as metadata passed separately in the event, not stuffed into the message.
- The `history_message` dict at tool execution sites already has a corresponding
  `llm_message: ToolMessage`. Yield that instead. The extra `tool_name` is already in
  `llm_message.name`. The `attachments` field needs to be passed separately (via the event or a new
  field on `ToolExecutionResult`).
- **Signature**: `AsyncIterator[tuple[LLMStreamEvent, LLMMessage | None]]`
- **`ToolExecutionResult.history_message`**: Change from `dict[str, Any]` to `ToolMessage`. The
  `tool_name` check at line 964 becomes `history_message.name == "attach_to_response"`.

**What stays the same:**

- `LLMStreamEvent` metadata still carries `reasoning_info`, `attachment_ids`, etc. — these are event
  metadata, not message fields.

### Milestone 2: Migrate `add()` callers to `add_message()`

The 3 call sites in `processing.py` currently:

1. Copy the message dict
2. Enrich it with `interface_type`, `conversation_id`, `turn_id`, etc.
3. Splat it into `add(**msg_to_save)`

After Milestone 1, the message is already an `LLMMessage`. The enrichment metadata maps directly to
`add_message()` keyword arguments:

```python
saved = await db_context.message_history.add_message(
    message=llm_message,
    interface_type=interface_type,
    conversation_id=conversation_id,
    turn_id=turn_id,
    thread_root_id=thread_root_id_for_turn,
    timestamp=self.clock.now(),
    processing_profile_id=self.service_config.id,
    subconversation_id=subconversation_id,
    user_id=user_id,
    reasoning_info=reasoning_info,  # for assistant messages
    attachments=attachments,  # for tool messages
)
```

For the assistant message in the non-streaming path, we also need `reasoning_info` — currently
stuffed into the dict. After Milestone 1 it'll be in the event metadata, which `process_message()`
already extracts.

### Milestone 3: Delete `add()` and `_add_from_kwargs()`

Once all callers are migrated, delete both methods. Also delete `message_to_json_dict` if it becomes
unused (check).

### Milestone 4: Change `add_message()` return type

Currently returns `dict[str, Any] | None`. The only field ever read is `internal_id`. Change to
return `int | None` (just the internal_id). This simplifies the interface and removes the last
`dict[str, Any]` from the message history API.

### Milestone 5: Update `process_message()` return type

Change from `tuple[list[dict[str, Any]], ...]` to `tuple[list[LLMMessage], ...]`. The caller
(`handle_chat_interaction`) reads `role` and `content` from these — both are attributes on
`LLMMessage`.

## Files to Modify

- `src/family_assistant/processing.py` — Main changes (all milestones)
- `src/family_assistant/storage/repositories/message_history.py` — Delete `add()`,
  `_add_from_kwargs()`, change `add_message()` return type
- `src/family_assistant/llm/messages.py` — Possibly remove `message_to_json_dict` if unused
- Tests that use `add()` or depend on dict returns — migrate

## Risk Assessment

- **Medium risk**: `process_message_stream` is the core processing loop. Type checker + tests should
  catch issues.
- **Low risk**: `add()` has only 3 call sites, all in one file.
- **Low risk**: Return type change — only `internal_id` is read.
