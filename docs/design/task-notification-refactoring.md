# Task Notification System Refactoring

## Date: 2025-01-14
## Author: Development Team
## Status: Approved for Implementation

## Executive Summary

This document outlines the refactoring of the task notification system in Family Assistant. The current design requires passing a `new_task_event` through multiple layers of the application, leading to missed notifications and performance issues. The new design encapsulates task notifications within the storage layer, providing automatic notifications for all enqueued tasks.

## Problem Statement

### Current Issues

1. **Event Propagation Complexity**: The `new_task_event` is created in `Assistant.__init__` and must be passed through ~15 different components
2. **Missing Notifications**: Many code paths enqueue tasks without passing the event, causing up to 5-second delays
3. **Leaky Abstraction**: Task notification is an implementation detail that shouldn't be exposed throughout the codebase
4. **Test Performance**: Tests wait up to 5 seconds for tasks due to missing notifications

### Affected Components

- System tasks (cleanup, maintenance) - no notifications
- Web API endpoints (document upload, email ingestion) - no notifications  
- Event-triggered callbacks - no notifications
- Note/email indexing from storage modules - explicitly passes `None`

## Design Goals

1. **Encapsulation**: Task notifications should be internal to the storage layer
2. **Automatic**: Every immediate task should notify automatically
3. **Simplicity**: Minimal code changes and no new abstractions
4. **Performance**: Eliminate polling delays in tests and production
5. **Backwards Compatibility**: Don't break existing code

## Proposed Solution

### Core Design

Add a module-level event in `storage/tasks.py` that is automatically triggered when immediate tasks are enqueued:

```python
# storage/tasks.py
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio import Event

# Module state
_task_event: Event | None = None

def get_task_event() -> Event:
    """Get the event that's set when new tasks are available.
    
    This event is automatically set when immediate tasks are enqueued.
    Task workers can wait on this event to be notified of new work.
    
    Returns:
        The global task notification event
    """
    global _task_event
    if _task_event is None:
        _task_event = asyncio.Event()
    return _task_event

# Modified enqueue_task function
async def enqueue_task(
    db_context: DatabaseContext,
    task_id: str,
    task_type: str,
    payload: dict[str, Any] | None = None,
    scheduled_at: datetime | None = None,
    max_retries_override: int | None = None,
    recurrence_rule: str | None = None,
    original_task_id: str | None = None,
    notify_event: asyncio.Event | None = None,  # DEPRECATED - will be removed
) -> None:
    """Enqueue a task for processing.
    
    Args:
        ... (existing args) ...
        notify_event: DEPRECATED - Notification is now automatic. This parameter
                     will be removed in v2.0.
    """
    if notify_event is not None:
        import warnings
        warnings.warn(
            "The notify_event parameter is deprecated and will be removed in v2.0. "
            "Task notification is now automatic.",
            DeprecationWarning,
            stacklevel=2
        )
    
    # ... existing enqueue logic ...
    
    # Automatically notify for immediate tasks
    is_immediate = scheduled_at is None or scheduled_at <= datetime.now(timezone.utc)
    if is_immediate:
        # Use deprecated event if provided, otherwise use global
        if notify_event:
            db_context.on_commit(lambda: notify_event.set())
        else:
            event = get_task_event()
            db_context.on_commit(lambda: event.set())
            logger.info(f"Scheduled notification for immediate task {task_id}")
```

### TaskWorker Integration

Update `TaskWorker` to use the global event:

```python
# task_worker.py
from family_assistant.storage.tasks import get_task_event

class TaskWorker:
    def __init__(self, ...):
        # Remove new_task_event parameter
        # ... other init code ...
        
    async def run(self, wake_up_event: asyncio.Event | None = None) -> None:
        """Run the task worker loop.
        
        Args:
            wake_up_event: Optional override event for testing. If not provided,
                          uses the global task event.
        """
        if wake_up_event is None:
            wake_up_event = get_task_event()
            
        # ... rest of run method uses wake_up_event as before ...
```

### Testing Support

Add fixture to reset the global event between tests:

```python
# tests/conftest.py
@pytest.fixture(autouse=True)
def reset_task_event():
    """Reset the global task event for each test to ensure isolation."""
    import family_assistant.storage.tasks as tasks_module
    
    # Reset before test
    tasks_module._task_event = None
    
    yield
    
    # Reset after test
    if tasks_module._task_event is not None:
        # Clear any pending notifications
        try:
            tasks_module._task_event.clear()
        except:
            pass
    tasks_module._task_event = None
```

## Implementation Plan

### Phase 1: Core Changes (Day 1)
1. Add `get_task_event()` function to `storage/tasks.py`
2. Modify `enqueue_task` to auto-notify with deprecation warning
3. Update `TaskWorker` to use global event by default
4. Add test fixture to `tests/conftest.py`

### Phase 2: Update Callers (Day 1-2)
1. Remove `new_task_event` from `Assistant.__init__`
2. Remove from `ProcessingService` constructor  
3. Remove from `ToolExecutionContext`
4. Remove from web API endpoints
5. Update all `enqueue_task` calls to stop passing `notify_event`

### Phase 3: Testing and Validation (Day 2-3)
1. Run full test suite to ensure no regressions
2. Add specific tests for notification behavior
3. Measure performance improvements
4. Update any failing tests

### Phase 4: Cleanup (Day 3-4)
1. Remove deprecated `notify_event` parameter
2. Remove `new_task_event` from all remaining contexts
3. Update documentation
4. Final test run

## Migration Guide

### For Internal Code
```python
# Before
await enqueue_task(
    db_context=db,
    task_id="task-123",
    task_type="process_email",
    notify_event=self.new_task_event  # Remove this
)

# After  
await enqueue_task(
    db_context=db,
    task_id="task-123",
    task_type="process_email"
    # Notification is automatic!
)
```

### For Tests
```python
# Before
new_task_event = asyncio.Event()
worker = TaskWorker(
    new_task_event=new_task_event,
    # ... other params ...
)
# ... enqueue task ...
new_task_event.set()  # Manual notification

# After
worker = TaskWorker(
    # ... other params ...
)
# ... enqueue task ...
# Notification is automatic!
```

## Alternatives Considered

1. **TaskEventManager Class**: Rejected as unnecessary abstraction
2. **Singleton Pattern**: Rejected due to testing complexity
3. **Event Bus**: Rejected as over-engineering
4. **Database LISTEN/NOTIFY**: Rejected as PostgreSQL-specific

## Risks and Mitigations

### Event Loop Binding
- **Risk**: Events are bound to specific event loops
- **Mitigation**: Lazy initialization ensures event is created in correct loop

### Test Isolation  
- **Risk**: Global state could leak between tests
- **Mitigation**: Auto-reset fixture ensures clean state

### Multi-Process Deployment
- **Risk**: Events don't work across processes
- **Mitigation**: Document as single-process limitation; future enhancement possible

## Success Metrics

1. **No Manual Notifications**: All `notify_event` parameters removed
2. **Performance**: Task notification latency <100ms (was 0-5s)
3. **Test Speed**: Indexing tests run in <3s (was 5-8s)
4. **Code Simplicity**: ~200 lines removed from propagation code

## Future Enhancements

1. **Multi-Process Support**: Add Redis pub/sub or PostgreSQL LISTEN/NOTIFY
2. **Metrics**: Add notification latency tracking
3. **Circuit Breaker**: Handle notification failures gracefully

## Appendix: Performance Analysis

Current test durations showing impact:
```
7.45s test_vector_ranking
7.19s test_document_indexing_and_query_e2e  
6.83s test_notes_indexing_graceful_degradation
```

Expected improvements:
- Remove 0-5s polling delays
- Reduce test times by 50-70%
- Improve user experience with instant task processing