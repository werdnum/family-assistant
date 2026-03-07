# Real issues found during dict[str, Any] type replacement

## Issues found and fixed in this PR

### 1. Dead code in chat_api.py accessing non-existent `metadata` column

**File:** `src/family_assistant/web/routers/chat_api.py` **Status:** Fixed in this PR
**Description:** ~40 lines of code were trying to read a `metadata` column from the
`message_history` table, but no such column exists. This code was silently doing nothing because the
dict access would return `None`/missing. The code was removed and `metadata=None` is set directly.

### 2. Circular import from storage/types.py through llm layer

**File:** `src/family_assistant/storage/types.py` **Status:** Fixed in this PR **Description:**
Adding typed fields to `MessageHistoryRow` (referencing `ToolCallItem`, `MessageReasoningInfo`,
etc.) created a circular import: `storage/types.py` -> `llm/google_types` -> `llm/__init__.py` ->
`tools` -> `actions` -> `storage/tasks` -> `storage/types.py`. Fixed with `TYPE_CHECKING` imports +
string annotations for the specific fields. **Lesson:** TypedDicts used as Pydantic/FastAPI response
types must have runtime-evaluable annotations, so `from __future__ import annotations` cannot be
used as a blanket fix.

## Issues found but not yet fixed (tracked for follow-up)

### 3. Repository methods return `{}` instead of `None` for not-found cases

**Files:**

- `src/family_assistant/storage/repositories/events.py:800` - `get_listener_execution_stats` returns
  `{}` when listener not found
- `src/family_assistant/storage/repositories/schedule_automations.py:931` - `get_execution_stats`
  returns `{}` when automation not found

**Description:** These methods have typed return values (`ListenerExecutionStatsDict` /
`ScheduleExecutionStatsDict`) but return empty dicts `{}` when the entity is not found. This
violates the return type and is inconsistent with other repository methods (like
`get_event_listener_by_id`) which return `None`. Both have `# type: ignore[return-value]` comments
now.

**Impact:** Callers must check for both `None` and empty dict, or may get KeyError when accessing
expected fields on the empty dict.

**Recommended fix:** These methods should return `None` (with `| None` return type), consistent with
other repository patterns.
