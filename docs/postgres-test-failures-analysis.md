# PostgreSQL Test Failures Analysis

This document analyzes test failures that occur when running the test suite with PostgreSQL (`--postgres` flag) but pass with SQLite.

## Current Status (2025-06-21)

**UPDATE**: After implementing NullPool for PostgreSQL connections, we've reduced failures from 29 to just 1. The NullPool approach eliminates event loop affinity issues by creating fresh connections for each request rather than reusing them across event loops.

### Remaining Issue

- 1 test failure in `test_indexing_event_listener_integration` - appears to be a document ID mismatch issue

### Resolved Issues

- ✅ Event loop attachment errors - Fixed with NullPool
- ✅ SQL function incompatibility - Tests updated to use SQLAlchemy JSON operators
- ✅ Starlark script syntax errors - Fixed
- ✅ JSON serialization issues - Fixed

## Original Summary of Issues

Running `pytest --postgres -q` revealed 29 test failures across 6 main categories of issues:

1. **SQL Function Incompatibility** - `json_extract()` doesn't exist in PostgreSQL [FIXED]
2. **Event Loop Attachment Issues** - asyncpg's strict event loop affinity causes conflicts [FIXED with NullPool]
3. **Starlark Script Syntax Error** - Reserved keyword usage [FIXED]
4. **JSON Serialization Issues** - Incorrect parameter types [FIXED]
5. **Transaction/Connection Errors** - Cascading failures from above issues [FIXED]

## Detailed Analysis

### 1. SQL Function Incompatibility (json_extract)

**Root Cause:** Tests use SQLite's `json_extract()` function which doesn't exist in PostgreSQL.

**Error Message:**

```
function json_extract(jsonb, unknown) does not exist
```

**Affected Tests:**

- `tests/functional/indexing/test_indexing_events.py::test_document_ready_event_emitted`
- `tests/functional/indexing/test_indexing_events.py::test_indexing_event_listener_integration`
- `tests/functional/test_event_system.py::test_cleanup_old_events`
- `tests/functional/test_event_system.py::test_event_type_matching`

**Code Locations:**

- `tests/functional/indexing/test_indexing_events.py`: Lines using `json_extract(event_data, '$.event_type')` and `json_extract(event_data, '$.document_id')`
- `tests/functional/test_event_system.py`: Lines using `json_extract(event_data, '$.entity_id')`

**PostgreSQL Equivalent:**

- SQLite: `json_extract(event_data, '$.event_type')`
- PostgreSQL: `event_data->>'event_type'`

**Note:** The codebase already handles this correctly in `src/family_assistant/indexing/tasks.py` with conditional logic based on dialect.

### 2. Event Loop Attachment Issues

These errors occur because PostgreSQL's asyncpg driver maintains strict event loop affinity, while SQLite's aiosqlite uses thread pools.

#### 2.1 Starlark Tools API Event Loop Issues (8 failures)

**Root Cause:** The Starlark scripting engine's ToolsAPI (`src/family_assistant/scripting/apis/tools.py`) creates complex event loop handling to bridge synchronous Starlark code and asynchronous tool execution.

**Error Pattern:** `Task ... got Future ... attached to a different loop` in `CompositeToolsProvider.execute_tool`

**Affected Tests:**

- `tests/functional/test_scheduled_script_execution.py::test_schedule_script_execution`
- `tests/functional/test_scheduled_script_execution.py::test_schedule_recurring_script`
- `tests/functional/test_script_execution_handler.py::test_script_execution_creates_note`
- `tests/functional/test_script_execution_handler.py::test_script_creates_multiple_notes`
- `tests/functional/test_script_wake_llm.py::test_script_wake_llm_single_call`
- `tests/functional/test_script_wake_llm.py::test_script_wake_llm_multiple_contexts`
- `tests/functional/test_script_wake_llm.py::test_script_conditional_wake_llm`
- `tests/functional/test_event_script_integration.py::test_create_script_listener_via_tool_and_execute`

**Code Location:** `src/family_assistant/scripting/apis/tools.py` - The `_run_async` method (lines 101-132) attempts to handle multiple event loop scenarios.

#### 2.2 SQLAlchemy Error Handler Event Loop Issue (1 failure)

**Root Cause:** The SQLAlchemyErrorHandler creates its own event loop in a separate worker thread.

**Error Pattern:** Task in `SQLAlchemyErrorHandler._worker_async`

**Affected Test:**

- `tests/functional/test_error_logging.py::test_error_logging_integration`

**Code Location:** `src/family_assistant/utils/logging_handler.py` - Already has a fix for cross-event-loop execution (lines 87-93).

#### 2.3 Background Task Event Loop Issue (1 failure)

**Root Cause:** Event processing in background tasks creates event loop conflicts.

**Error Pattern:** Single occurrence for "Motion Events" processing

**Affected Test:**

- `tests/functional/test_event_system.py::test_end_to_end_event_listener_wakes_llm`

### 3. Starlark Script Syntax Error

**Root Cause:** Test uses `is` as a variable name, which is a reserved keyword in Starlark.

**Error Message:**

```
Parse error: cannot use reserved keyword `is`
```

**Affected Test:**

- `tests/functional/test_event_script_integration.py::test_script_listener_with_complex_conditions`

**Code Location:** Look for Starlark script content in the test that uses `is` as a variable name.

### 4. JSON Serialization Issues

**Root Cause:** Event testing tools passing dict directly to JSON functions instead of strings.

**Error Message:**

```
the JSON object must be str, bytes or bytearray, not dict
```

**Affected Tests:**

- `tests/functional/test_event_system.py::test_test_event_listener_tool_matches_person_coming_home`
- `tests/functional/test_event_system.py::test_test_event_listener_tool_no_match_wrong_state`

**Code Location:** Event testing tools that need to serialize dicts to JSON strings before passing to functions.

### 5. Other Affected Tests

These tests fail due to cascading effects from the above issues:

- `tests/functional/indexing/test_indexing_pipeline.py::test_indexing_pipeline_e2e`
- `tests/functional/indexing/test_indexing_pipeline.py::test_indexing_pipeline_pdf_processing`
- `tests/functional/test_event_listener_crud.py::test_create_event_listener_duplicate_name_error`
- `tests/functional/test_event_system.py::test_event_listener_matching`
- `tests/functional/test_event_system.py::test_event_storage_sampling`
- `tests/functional/test_recurring_task_timezone.py::test_recurring_task_respects_user_timezone`
- `tests/functional/test_script_execution_handler.py::test_script_with_syntax_error_creates_no_note`
- `tests/functional/test_smoke_callback.py::test_schedule_reminder_with_follow_up`
- `tests/functional/test_smoke_callback.py::test_schedule_recurring_callback`
- `tests/functional/test_smoke_callback.py::test_list_pending_callbacks`
- `tests/functional/test_task_error_column.py::test_manually_retry_clears_error_column`
- `tests/functional/test_task_error_column.py::test_reschedule_for_retry_uses_correct_error_column`

## Fix Priority

1. **High Priority:**
   - Fix `json_extract` SQL incompatibility (4 direct test failures)
   - Fix Starlark tools API event loop handling (8 test failures)

2. **Medium Priority:**
   - Fix SQLAlchemy error handler (1 failure, partial fix exists)
   - Fix background task event loop issue (1 failure)

3. **Low Priority:**
   - Fix Starlark syntax error (`is` keyword)
   - Fix JSON serialization issue

## Key Differences: PostgreSQL vs SQLite

1. **JSON Functions:**
   - SQLite: `json_extract(column, '$.path')`
   - PostgreSQL: `column->>'path'` or `column->'path'`

2. **Event Loop Handling:**
   - SQLite (aiosqlite): Uses thread pool executor, forgiving of event loop switches
   - PostgreSQL (asyncpg): Native asyncio with strict event loop affinity

3. **Transaction Management:**
   - SQLite: Lenient, errors don't abort entire transaction
   - PostgreSQL: Strict, requires explicit rollback after errors

## Next Steps

1. Update tests to use database-agnostic JSON queries (SQLAlchemy operators or conditional SQL) [DONE]
2. Fix Starlark tools API event loop handling to properly manage asyncio contexts [FIXED via NullPool]
3. Review and fix remaining event loop issues in error handler and background tasks [FIXED via NullPool]
4. Update tests to avoid reserved keywords in Starlark scripts [DONE]
5. Fix JSON serialization in event testing tools [DONE]

## Solution Implemented

### NullPool for PostgreSQL

We resolved the event loop affinity issues by implementing NullPool for PostgreSQL connections in `storage/base.py`:

```python
def create_engine_with_sqlite_optimizations(database_url: str) -> AsyncEngine:
    # Determine pool class based on database type
    if database_url.startswith("sqlite"):
        # Use StaticPool for SQLite to reuse connections
        pool_class = StaticPool
    else:
        # Use NullPool for PostgreSQL to avoid event loop affinity issues
        # This creates a new connection for each request
        pool_class = NullPool
    
    engine = create_async_engine(
        database_url,
        poolclass=pool_class,
        # ... other settings
    )
```

**Trade-offs:**

- **Pros**: Completely eliminates event loop affinity errors, simple implementation
- **Cons**: Slightly higher latency due to connection overhead (1-5ms locally, 10-50ms remote)

**Future Optimization:**
Consider implementing an event-loop-aware engine registry that maintains separate connection pools per event loop for better performance while maintaining correctness.
