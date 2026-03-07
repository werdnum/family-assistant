# Processing Module Cleanup

Comprehensive analysis of issues in `src/family_assistant/processing/`.

## 1. Excessive Fallbacks / Silent Failures

### 1.1 History fetch fails → silently continues with empty history

**Files**: `service.py:399-414`, `service.py:828-845`

```python
try:
    raw_history_messages = await db_context.message_history.get_recent(...)
except Exception as hist_err:
    logger.error(...)
    raw_history_messages = []
```

If history retrieval fails, the assistant silently responds with no context, producing a confusing
response. The user has no idea the assistant forgot the entire conversation. This should propagate —
it's not correct to continue without history, and masking it violates "errors should look like
errors."

### 1.2 Context provider errors silently swallowed

**File**: `context.py:103-112`

```python
for provider in self.context_providers:
    try:
        fragments_output = await provider.get_context_fragments()
        all_fragments.extend(fragments_output)
    except Exception as e:
        logger.error(...)
```

Each context provider failure is swallowed. If a critical provider (e.g., the calendar context)
fails, the assistant just answers without that context. This produces subtly wrong answers — worse
than an error. At minimum, a failed provider should inject a visible note into the context telling
the LLM "calendar context is unavailable due to an error."

### 1.3 Thread history fetch fails → silently falls back to recent history

**Files**: `service.py:428-462`, `service.py:853-868`

```python
try:
    full_thread_messages = await db_context.message_history.get_by_thread_id(...)
    initial_messages_for_llm = await self.context_preparer.format_history(full_thread_messages)
except Exception as thread_fetch_err:
    logger.error(...)
```

If thread history fetch fails, the code silently uses the recent-messages fallback. The user is
replying to a specific thread, but the assistant loses that thread context and responds generically.

### 1.4 Attachment processing: errors → `continue` (silent data loss)

**File**: `attachments.py:128-133`

```python
except Exception as e:
    logger.error(f"Error processing attachment content part {attachment_id}: {e}", ...)
    continue
```

If an attachment the user sent can't be processed, it's silently dropped. The user thinks the
assistant saw their image/file but it didn't. This is the exact "cascading silent failure" pattern
described in the error handling guidelines.

### 1.5 Attachment context extraction → returns empty string on error

**File**: `attachments.py:367-371`

```python
except Exception as e:
    logger.error(...)
    return ""
```

Broad `except Exception` returning empty string. The LLM just doesn't know about attachments.

### 1.6 `select_for_response` → falls back to last N on any error

**File**: `attachments.py:483-485`

```python
except Exception as e:
    logger.error(f"Error selecting attachments: {e}", exc_info=True)
    return pending_attachment_ids[-self.app_config.max_response_attachments:]
```

Any error during LLM-based attachment selection silently falls back to a "last N" heuristic. This
masks bugs in the selection logic.

### 1.7 `handle_large_result` → returns original content on failure

**File**: `attachments.py:593-598`

```python
except Exception as e:
    logger.error(...)
    return content, None
```

If storing an attachment fails, the large content is returned raw to the LLM context, potentially
blowing up the context window (the very problem this function is trying to prevent).

### 1.8 System prompt formatting failure → uses raw template

**File**: `service.py:547-551`

```python
except ValueError as e:
    logger.error(...)
    final_system_prompt = system_prompt_template.strip()
```

If prompt formatting fails, the raw template with `{placeholders}` is sent to the LLM. The LLM gets
gibberish like `"Current time is {current_time}"`. This should be a fatal error.

### 1.9 Streaming: prompt formatting → bare `except Exception` with no log

**File**: `service.py:910-915`

```python
try:
    final_system_prompt = system_prompt_template.format(**format_args).strip()
except Exception:
    final_system_prompt = system_prompt_template.strip()
```

Even worse than 1.8 — no logging at all. The exception type is also broader (`Exception` vs
`ValueError`).

## 2. Overly Broad Exception Handling

### 2.1 Tool execution: catch-all `except Exception`

**File**: `tool_execution.py:470-486`

```python
except Exception as e:
    logger.error(f"Error executing tool '{function_name}': {e}", exc_info=True)
    error_content = f"Error executing {function_name}: {str(e)}"
```

Every tool execution error is caught and converted to a tool result string. This is defensible for
user-facing resilience (the LLM gets the error and can explain it), but it means bugs in tool code
never propagate — they just produce a confusing "Error executing X" message. Consider distinguishing
between expected tool failures (e.g., "note not found") and unexpected exceptions (bugs).

### 2.2 LLM loop: parallel tool execution double-catches

**File**: `llm_loop.py:584-604`

```python
except Exception as e:
    # This should not happen since we handle exceptions inside tool_executor.execute
    # But adding as extra safety
    logger.error(f"Unexpected error in parallel tool execution: {e}", ...)
```

The comment admits this should never happen. If it does, the generated `error_tool_message` has
`tool_call_id=f"error_{uuid.uuid4()}"` — a made-up ID that doesn't match any tool call the LLM
issued, which will likely confuse the LLM or be rejected by providers that validate tool call IDs.
This "safety net" would actually make things worse if triggered.

### 2.3 Attachment metadata fetch → `except Exception` with `continue`

**File**: `llm_loop.py:472-475`

```python
except Exception as e:
    logger.warning(f"Failed to fetch metadata for attachment {att_id}: {e}")
```

Individual attachment metadata failures silently skip that attachment from the response.

### 2.4 Attachment storage failure → `except Exception` with "continue without URL"

**File**: `tool_execution.py:305-308`

```python
except Exception as e:
    logger.error(f"Failed to store tool result attachment: {e}")
    # Continue without URL if storage fails
```

The attachment data is lost but execution continues as if nothing happened.

## 3. Duplicated / Mixed Concerns

### 3.1 `handle_chat_interaction` and `handle_chat_interaction_stream` are ~80% duplicated

**File**: `service.py:254-673` (non-streaming) vs `service.py:675-1027` (streaming)

These two methods duplicate:

- Thread root ID determination
- User message content extraction from `trigger_content_parts`
- Temp interface message ID generation
- User message saving
- History fetching with fallback
- Thread history fetching
- Leading message pruning
- System prompt construction and formatting
- Context aggregation
- Attachment processing and URL conversion

The streaming version has minor differences (PostgreSQL-specific transaction handling, attachment
metadata injection) but the overall structure is copy-pasted. This is a maintenance hazard — any bug
fix or feature must be applied in two places.

### 3.2 Thought signature detection is duplicated

**Files**: `context.py:134-151`, `llm_loop.py:228-244`

The same logic to detect Google thought signatures on tool calls is duplicated in two places with
slightly different code structure. This should be a shared utility function.

### 3.3 User content extraction from `trigger_content_parts` is duplicated

**File**: `service.py:354-367` and `service.py:757-769`

Identical logic to extract text from content parts appears in both `handle_chat_interaction` and
`handle_chat_interaction_stream`.

### 3.4 Leading message pruning is duplicated

**File**: `service.py:467-482` and `service.py:873-883`

Same pruning loop appears in both methods.

### 3.5 `ToolExecutor` and `AttachmentProcessor` share attachment storage concerns

`ToolExecutor.execute()` has extensive logic for storing attachments, determining file extensions,
and registering metadata. `AttachmentProcessor` also handles attachment storage (in
`handle_large_result`). The attachment storage pipeline is split across both classes.

### 3.6 `attach_to_response` special-casing in two places

**Files**: `llm_loop.py:558-576`, `tool_execution.py:378-425`

The `attach_to_response` tool gets special handling in both the LLM loop (to update
`pending_attachment_ids`) and the tool executor (to enrich stream metadata). This tool-specific
logic is scattered across the processing pipeline.

## 4. Mushy / Excessively Permissive Interfaces

### 4.1 `LLMStreamingLoop.run` / `run_stream` accept too many parameters

**File**: `llm_loop.py:56-92`, `llm_loop.py:134-170`

These methods accept **16+ parameters**, most of which are just passed through to
`ToolExecutor.execute()`. Many are `Any`-typed with `noqa` comments:

```python
processing_service: Any = None,  # noqa: ANN401
home_assistant_client: Any = None,  # noqa: ANN401
camera_backend: Any = None,  # noqa: ANN401
event_sources: dict[str, Any] | None = None,
```

These `Any` types exist to avoid circular imports, but they defeat the purpose of type checking. The
parameter list is a code smell indicating the LLM loop knows too much about the system.

### 4.2 `ToolExecutor.execute()` has the same parameter explosion

**File**: `tool_execution.py:58-94`

Same 16+ parameter signature. The `request_confirmation_callback` type annotation is particularly
unwieldy — a `Callable` with 8 positional parameters, none named. This is duplicated in 4+ places.

### 4.3 `ProcessingService` methods duplicate the same callback type annotation

**Files**: `service.py:148-165`, `service.py:206-223`, `service.py:266-283`, `service.py:687-704`

The `request_confirmation_callback` type is copy-pasted four times. It should be a `TypeAlias` or a
`Protocol`.

### 4.4 `ProcessingServiceConfig` is a grab-bag

**File**: `types.py:41-67`

This dataclass mixes:

- History settings (`max_history_messages`, `history_max_age_hours`)
- Web-specific overrides (`web_max_history_messages`, `web_history_max_age_hours`)
- LLM configuration (`model_parameters`, `fallback_model_id`, `fallback_model_parameters`)
- Security settings (`delegation_security_level`)
- Access control (`visibility_grants`, `default_note_visibility_labels`)
- Note registry reference (`note_registry`)
- Audio settings (`greeting_wav_path`)
- Identity (`id`, `description`)

This config class has too many unrelated responsibilities.

### 4.5 `ChatInteractionResult.text_reply` is `str | None` even on success

**File**: `types.py:16-28`

It's possible for a successful interaction to return `text_reply=None` — the LLM responded with only
tool calls and no text. But callers have to handle this ambiguity: does `None` mean "success with no
text" or "something went wrong"? The `has_error` property helps, but a result type that uses a union
(success vs error) would be clearer.

## 5. Structural / Design Issues

### 5.1 `ProcessingService` uses `hasattr` checks in property setters

**File**: `service.py:114-129`

```python
if hasattr(self, "llm_loop"):
    self.llm_loop.llm_client = value
```

This is a sign of fragile initialization ordering. The property setters check `hasattr` because they
might be called before `__init__` finishes constructing sub-objects. This could be solved by
initializing all sub-objects first or using a builder pattern.

### 5.2 `ProcessingService` has mutable state set after construction

**File**: `service.py:80-83`

```python
self.processing_services_registry: dict[str, ProcessingService] | None = None
self.home_assistant_client: HomeAssistantClientWrapper | None = None
self.camera_backend: CameraBackend | None = None
```

These are set after construction via external calls, creating a temporal coupling where
`ProcessingService` might be used before these are wired up.

### 5.3 PostgreSQL-specific branching in `handle_chat_interaction_stream`

**File**: `service.py:780-815`, `service.py:982-1011`

```python
if db_context.engine.dialect.name == "postgresql":
    async with get_db_context(...) as user_msg_db:
        ...
else:
    ...
```

The processing layer shouldn't know about database dialect differences. This is a storage concern
that should be encapsulated in the database context layer.

### 5.4 `SafePromptFormatter` is an inner class

**File**: `service.py:515-522`

This helper class is defined inside `handle_chat_interaction`. It's fine as a utility but being
nested makes it invisible for testing and reuse. More importantly, the entire "safe template
formatting" block (lines 524-551) is a non-trivial string processing algorithm that's hard to
understand and test in isolation.

### 5.5 The streaming path doesn't use `SafePromptFormatter`

**File**: `service.py:910-915`

The non-streaming path uses the elaborate `SafePromptFormatter` with placeholder-preserving logic.
The streaming path uses a bare `format(**format_args)`. These will behave differently when prompts
contain literal braces (e.g., JSON examples).

### 5.6 `__init__.py` exports private symbols

**File**: `__init__.py:6-8`

```python
from .utils import (
    _map_stream_error_to_exception,
    _user_friendly_error_message,
    prune_messages_for_context,
)
```

Functions prefixed with `_` are exported in `__all__`. Either they're public (remove the underscore)
or they're internal (don't export them).

## 6. Error Handling Pattern Issues

### 6.1 Errors saved as `AssistantMessage`, not `ErrorMessage`

**File**: `service.py:648-649`

```python
error_message_record = await db_context.message_history.add_message(
    AssistantMessage(content=error_message),
    ...
)
```

Errors are saved as assistant messages rather than using the `ErrorMessage` type that exists in the
codebase. This means the error and a real response are indistinguishable in history.

### 6.2 Error timestamp uses `datetime.now(UTC)` instead of `self.clock.now()`

**File**: `service.py:656`

```python
timestamp=datetime.now(UTC),
```

Inconsistent with the rest of the file which uses `self.clock.now()`. This would break in tests that
use a fake clock.

### 6.3 `error_message_internal_id` redundant None-check

**File**: `service.py:658-660`

```python
error_message_internal_id = (
    error_message_record if error_message_record is not None else None
)
```

This is `x if x is not None else None`, which is just `x`.

## 7. Minor Issues

### 7.1 Unnecessary `role="tool"` and `role="assistant"` in message constructors

**Files**: `tool_execution.py:134`, `llm_loop.py:491`

```python
llm_message = ToolMessage(role="tool", ...)
llm_context_assistant_message = AssistantMessage(role="assistant", ...)
```

These message types presumably have `role` as a default/literal field. Passing it explicitly adds
noise.

### 7.2 Unused variable `processed_trigger_parts`

**File**: `service.py:561-566`

```python
(
    processed_trigger_parts,
    attachment_injection_messages,
) = await self.attachment_processor.process_content_parts(...)
```

`processed_trigger_parts` is computed but never used in `handle_chat_interaction`. Same in the
streaming version at `service.py:929-934`.

### 7.3 MIME type detection by trial-parsing JSON

**File**: `attachments.py:531-535`

```python
try:
    json.loads(content)
    mime_type = "application/json"
except json.JSONDecodeError:
    pass
```

Parsing potentially hundreds of KB of content just to detect MIME type. Could check the first
character or use a lighter heuristic.

### 7.4 Magic numbers

- `attachments.py:509`: `threshold_kb = 100` default
- `utils.py:111`: `min_turns = 3`
- `llm_loop.py` / `attachments.py`: various threshold checks

These should be configuration values or named constants.

## Summary: Recommended Priorities

1. **Eliminate the `handle_chat_interaction` / `handle_chat_interaction_stream` duplication** — this
   is the biggest maintenance risk and the source of multiple inconsistencies (prompt formatting,
   thread attachment context, message pruning logging).

2. **Create a `TypeAlias` or `Protocol` for the confirmation callback** — used in 4+ places, each
   with an 8-parameter `Callable` annotation.

3. **Introduce a context object for tool execution parameters** — collapse the 16+ parameters passed
   through `run` → `run_stream` → `execute` into a single `TurnContext` dataclass.

4. **Fix the silent history/thread fetch failures** — either propagate the error or inject a visible
   "context unavailable" note into the LLM context.

5. **Fix silent attachment processing failures** — at minimum, tell the LLM "an attachment could not
   be processed" rather than silently dropping it.

6. **Extract thought-signature detection** into a shared utility.

7. **Move PostgreSQL-specific transaction logic** into the database context layer.

8. **Unify prompt formatting** between streaming and non-streaming paths.
