# Error Handling Improvements

## Problem Statement

When errors occur in the system, especially in background tasks, we often see cascading failures where the error handling code itself fails. This makes it difficult to diagnose the root cause and leaves the system in an inconsistent state.

### Example: Task Worker Cascading Errors

A concrete example from the codebase:

1. **Initial Error**: A Starlark script fails with a type error

   ```
   error: Type of parameter `x` doesn't match, expected `int`, actual `float`
   ```

2. **Secondary Error**: The task worker tries to reschedule the failed task, but the database update fails

   ```
   Reschedule Failed: Unconsumed column names: last_error
   ```

This happens because:

- The tasks table has a column named `error`
- The `reschedule_for_retry` method tries to update a column named `last_error`
- The mismatch causes the SQL update to fail
- The task is left in an inconsistent state

## Root Causes

1. **Insufficient Testing of Error Paths**
   - Error handling code is often not as thoroughly tested as the happy path
   - No tests exist for `reschedule_for_retry` method

2. **Schema/Code Mismatches**
   - Column naming inconsistencies between code and database schema
   - No compile-time checking of SQL column names

3. **Error Context Loss**
   - When error handling fails, the original error context can be lost
   - Makes debugging difficult

## Proposed Solutions

### 1. Immediate Fixes

- Fix the column name mismatch: Change `last_error` to `error` in `tasks.py`
- Add comprehensive tests for all error handling paths

### 2. Testing Strategy

Create a test pattern for error handling:

```python
@pytest.mark.asyncio
async def test_task_retry_after_handler_error(db_context):
    """Test that tasks can be properly rescheduled after handler errors."""
    # 1. Enqueue a task
    # 2. Make the handler fail
    # 3. Verify the task is rescheduled with error recorded
    # 4. Verify retry count increments
    # 5. Verify error message is stored
```

### 3. Error Handling Patterns

Establish clear patterns for error handling in background tasks:

```python
try:
    # Main operation
    result = await operation()
except SpecificError as e:
    # Handle specific, expected errors
    logger.warning(f"Expected error: {e}")
    # Take specific recovery action
except Exception as e:
    # Catch-all for unexpected errors
    logger.error(f"Unexpected error: {e}", exc_info=True)
    # Ensure error is recorded even if subsequent operations fail
    try:
        await record_error(e)
    except Exception as record_error:
        # Last resort - log both errors
        logger.critical(
            f"Failed to record error. Original: {e}, Recording error: {record_error}"
        )
```

### 4. Schema Validation

- Use SQLAlchemy's type-safe query builders consistently
- Consider using a schema validation tool to ensure code matches database
- Add integration tests that verify all database operations

### 5. Error Observability

Improve error tracking and debugging:

1. **Structured Error Logging**
   - Include context (task_id, retry_count, etc.) in all error logs
   - Use consistent error formats

2. **Error Correlation**
   - Link related errors (original error + handling error)
   - Maintain error chains for debugging

3. **Health Checks**
   - Add health check endpoints that verify error handling paths work
   - Regular automated testing of error scenarios

## Implementation Plan

1. **Phase 1: Fix Immediate Issues** (1 day)
   - Fix column name mismatches
   - Add basic tests for error paths

2. **Phase 2: Comprehensive Testing** (3 days)
   - Add tests for all error handling methods
   - Create test utilities for simulating various error conditions
   - Test cascading error scenarios

3. **Phase 3: Error Handling Framework** (1 week)
   - Establish error handling patterns
   - Create base classes with proper error handling
   - Add error correlation and tracking

4. **Phase 4: Monitoring and Alerting** (1 week)
   - Add metrics for error rates
   - Create alerts for cascading errors
   - Build debugging tools

## Success Metrics

- Zero cascading errors in production
- All error handling paths have test coverage
- Mean time to diagnose errors reduced by 50%
- No tasks left in inconsistent states

## Related Issues

- Task worker reliability
- Database transaction handling
- Script execution error handling
