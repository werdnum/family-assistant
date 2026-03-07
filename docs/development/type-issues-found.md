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

### 3. Repository methods returned `{}` instead of `None` for not-found cases

**Status:** Fixed in this PR **Description:** `get_listener_execution_stats` and
`get_execution_stats` returned empty dicts `{}` when the entity was not found, violating their
TypedDict return types. Changed to return `None` with `| None` return types, consistent with other
repository methods.

### 4. `ToolCallResponseItem` TypedDict had wrong structure

**Status:** Fixed in this PR **Description:** The TypedDict had `name` and `arguments` at the top
level, but `chat_api.py` constructs the OpenAI-format nested structure with a `function` dict. Fixed
to match the actual data shape.

### 5. `thought_summaries` type mismatch in Google GenAI client

**Status:** Fixed in this PR **Description:** In the non-streaming path, `thought_summaries` was
`list[str]` but `MessageReasoningInfo.thought_summaries` expects `list[dict[str, str | int]]`. The
streaming path already used the correct dict format. Fixed the variable type annotation to match.
