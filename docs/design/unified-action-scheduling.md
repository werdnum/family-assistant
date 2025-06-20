# Unified Action Scheduling Design (Revised)

## Overview

This document describes the implementation plan for unifying the action model across scheduled tasks and event listeners in Family Assistant. Currently, event listeners support multiple action types (wake_llm, script) while scheduled tasks only support LLM callbacks. This design extends scheduled tasks to use the same action abstraction.

**Key Insight**: The existing task system already provides everything we need for scheduled actions. Tasks ARE actions - we don't need database changes, just a thin abstraction layer that maps action types to task types.

## Goals

1. **Consistency**: Same action model for both event-based and time-based triggers
2. **Code Reuse**: Share action execution logic between systems
3. **Type Safety**: Use enums for action types
4. **Simplicity**: No database migrations or schema changes
5. **Incremental Implementation**: Each step leaves the system in a working state

## Implementation Plan

### Step 1: Create Shared Action Executor ✓ COMPLETED

Create a new module `src/family_assistant/actions.py` to house shared action logic that maps actions to existing task types:

```python
# src/family_assistant/actions.py
from enum import Enum
from typing import Any
import logging

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import enqueue_task

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    """Action types supported by the system."""
    WAKE_LLM = "wake_llm"
    SCRIPT = "script"


async def execute_action(
    db_ctx: DatabaseContext,
    action_type: ActionType,
    action_config: dict[str, Any],
    conversation_id: str,
    interface_type: str = "telegram",
    context: dict[str, Any] = None,
) -> None:
    """
    Execute an action. Used by both event listeners and scheduled tasks.
    
    Args:
        db_ctx: Database context
        action_type: Type of action to execute
        action_config: Configuration for the action
        conversation_id: Conversation to execute in
        interface_type: Interface type (telegram, web, etc)
        context: Additional context (e.g., event data, trigger info)
    """
    import time
    from datetime import datetime, timezone
    
    if context is None:
        context = {}
    
    if action_type == ActionType.WAKE_LLM:
        # Prepare callback context
        callback_context = {
            "trigger": context.get("trigger", "Scheduled action"),
            **context
        }
        
        # Include any wake context from config
        if "context" in action_config:
            callback_context["message"] = action_config["context"]
        
        task_id = f"action_{int(time.time() * 1000)}"
        
        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="llm_callback",
            payload={
                "interface_type": interface_type,
                "conversation_id": conversation_id,
                "callback_context": callback_context,
                "scheduling_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        
    elif action_type == ActionType.SCRIPT:
        task_id = f"script_{int(time.time() * 1000)}"
        
        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="script_execution",
            payload={
                "script_code": action_config.get("script_code", ""),
                "config": action_config,
                "conversation_id": conversation_id,
                **context
            },
        )
    else:
        raise ValueError(f"Unknown action type: {action_type}")
```

### Step 2: Refactor Event Processor ✓ COMPLETED

Update `src/family_assistant/events/processor.py` to use the shared executor:

```python
# In events/processor.py
from family_assistant.actions import ActionType, execute_action

# Replace the _execute_action_in_context method:
async def _execute_action_in_context(
    self,
    db_ctx: DatabaseContext,
    listener: dict[str, Any],
    event_data: dict[str, Any],
) -> None:
    """Execute the action defined in the listener within existing DB context."""
    action_type = ActionType(listener["action_type"])
    action_config = listener.get("action_config", {})
    
    # Build context for action
    context = {
        "trigger": f"Event listener '{listener['name']}' matched",
        "listener_id": listener["id"],
        "source": listener["source_id"],
    }
    
    if action_type == ActionType.WAKE_LLM and action_config.get("include_event_data", True):
        context["event_data"] = event_data
    elif action_type == ActionType.SCRIPT:
        context["event_data"] = event_data
    
    await execute_action(
        db_ctx=db_ctx,
        action_type=action_type,
        action_config=action_config,
        conversation_id=listener["conversation_id"],
        interface_type=listener.get("interface_type", "telegram"),
        context=context,
    )
    
    logger.info(f"Executed {action_type.value} action for listener {listener['id']}")
```

**Verification**: Run all event listener tests after this change. They should pass without modification.

### Step 3: Extend Action Executor for Scheduling

Update `src/family_assistant/actions.py` to support scheduled execution:

```python
# Add scheduled_at and recurrence_rule parameters to execute_action:
async def execute_action(
    db_ctx: DatabaseContext,
    action_type: ActionType,
    action_config: dict[str, Any],
    conversation_id: str,
    interface_type: str = "telegram",
    context: dict[str, Any] | None = None,
    scheduled_at: datetime | None = None,  # New parameter
    recurrence_rule: str | None = None,  # New parameter
) -> None:
    """
    Execute an action. Used by both event listeners and scheduled tasks.
    
    Args:
        db_ctx: Database context
        action_type: Type of action to execute
        action_config: Configuration for the action
        conversation_id: Conversation to execute in
        interface_type: Interface type (telegram, web, etc)
        context: Additional context (e.g., event data, trigger info)
        scheduled_at: When to execute (None for immediate)
        recurrence_rule: RRULE for recurring tasks (None for one-time)
    """
    # ... existing code ...
    
    if action_type == ActionType.WAKE_LLM:
        # ... existing payload building ...
        
        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_at,  # Pass through
            recurrence_rule=recurrence_rule,  # Pass through
        )
        
    elif action_type == ActionType.SCRIPT:
        # ... existing payload building ...
        
        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="script_execution",
            payload=payload,
            scheduled_at=scheduled_at,  # Pass through
            recurrence_rule=recurrence_rule,  # Pass through
        )
```

**Note**: No database changes needed! The existing `tasks` table already has `scheduled_at` and `recurrence_rule` columns.

### Step 4: Update Scheduling Tools

Update `src/family_assistant/tools/tasks.py` to support action-based scheduling. The key insight is that we're just adding a convenience layer over the existing task system:

```python
from family_assistant.actions import ActionType, execute_action

# Add new tool definition
TASK_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "schedule_action",
            "description": (
                "Schedule any action (wake_llm or script) to execute at a specific time. "
                "This is the general-purpose scheduling tool that complements schedule_future_callback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "When to execute the action (ISO 8601 format with timezone)",
                    },
                    "action_type": {
                        "type": "string",
                        "enum": ["wake_llm", "script"],
                        "description": "Type of action to execute",
                        "default": "wake_llm",
                    },
                    "action_config": {
                        "type": "object",
                        "description": (
                            "Configuration for the action. "
                            "For wake_llm: {'context': 'message for LLM'}. "
                            "For script: {'script_code': 'Starlark code', 'timeout': 600}"
                        ),
                    },
                },
                "required": ["schedule_time", "action_config"],
            },
        },
    },
    # Keep existing tools for backward compatibility
]

# New implementation
async def schedule_action_tool(
    exec_context: ToolExecutionContext,
    schedule_time: str,
    action_type: str = "wake_llm",
    action_config: dict[str, Any] = None,
) -> str:
    """Schedule any action for future execution."""
    
    if action_config is None:
        action_config = {}
    
    # Validate action type using enum
    try:
        action_type_enum = ActionType(action_type)
    except ValueError:
        return f"Error: Invalid action_type. Must be one of: {[e.value for e in ActionType]}"
    
    # Validate action config based on type
    if action_type_enum == ActionType.WAKE_LLM:
        if "context" not in action_config:
            return "Error: wake_llm action requires 'context' in action_config"
    elif action_type_enum == ActionType.SCRIPT:
        if "script_code" not in action_config:
            return "Error: script action requires 'script_code' in action_config"
    
    # Parse and validate time
    clock = exec_context.clock or SystemClock()
    try:
        scheduled_dt = isoparse(schedule_time)
        if scheduled_dt.tzinfo is None:
            logger.warning(f"Schedule time lacks timezone, assuming {exec_context.timezone_str}")
            scheduled_dt = scheduled_dt.replace(tzinfo=ZoneInfo(exec_context.timezone_str))
        
        if scheduled_dt <= clock.now():
            return "Error: Schedule time must be in the future"
    except ValueError as e:
        return f"Error: Invalid schedule_time format: {e}"
    
    # Use the shared action executor with scheduling
    await execute_action(
        db_ctx=exec_context.db_context,
        action_type=action_type_enum,
        action_config=action_config,
        conversation_id=exec_context.conversation_id,
        interface_type=exec_context.interface_type,
        context={"scheduled_via": "schedule_action tool"},
        scheduled_at=scheduled_dt,
    )
    
    return f"OK. {action_type} action scheduled for {schedule_time}"
```

**Note**: The existing `schedule_future_callback` tool continues to work as before. We're adding new capabilities without breaking existing ones.

### Step 5: Add Recurring Action Support

Extend the scheduling tools to support recurring actions:

```python
# Add tool for recurring actions
{
    "type": "function",
    "function": {
        "name": "schedule_recurring_action",
        "description": "Schedule a recurring action (wake_llm or script) using RRULE format",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "format": "date-time",
                    "description": "When to start the recurring schedule",
                },
                "recurrence_rule": {
                    "type": "string",
                    "description": "RRULE format string (e.g., 'FREQ=DAILY;INTERVAL=1')",
                },
                "action_type": {
                    "type": "string",
                    "enum": ["wake_llm", "script"],
                    "description": "Type of action to execute",
                    "default": "wake_llm",
                },
                "action_config": {
                    "type": "object",
                    "description": "Configuration for the action",
                },
            },
            "required": ["start_time", "recurrence_rule", "action_config"],
        },
    },
}

# Implementation
async def schedule_recurring_action_tool(
    exec_context: ToolExecutionContext,
    start_time: str,
    recurrence_rule: str,
    action_type: str = "wake_llm",
    action_config: dict[str, Any] = None,
) -> str:
    """Schedule a recurring action."""
    # Similar validation as schedule_action_tool...
    
    # Use the shared action executor with recurrence
    await execute_action(
        db_ctx=exec_context.db_context,
        action_type=action_type_enum,
        action_config=action_config,
        conversation_id=exec_context.conversation_id,
        interface_type=exec_context.interface_type,
        context={"scheduled_via": "schedule_recurring_action tool"},
        scheduled_at=start_dt,
        recurrence_rule=recurrence_rule,
    )
    
    return f"OK. Recurring {action_type} action scheduled starting {start_time}"
```

## Testing Strategy

### Step 1 & 2: Refactor Event System ✓ COMPLETED

- Existing event listener tests pass after refactoring
- No new tests needed

### Step 3: Extended Action Executor

- Test that `execute_action` correctly passes `scheduled_at` and `recurrence_rule` to `enqueue_task`
- Verify immediate execution still works when these parameters are None

### Step 4: Scheduling Tools

- Add tests for `schedule_action_tool`:
  - Test scheduling wake_llm actions
  - Test scheduling script actions
  - Test validation of action types and configs
  - Test timezone handling

### Step 5: Recurring Actions

- Test `schedule_recurring_action_tool`:
  - Test RRULE parsing and validation
  - Test that recurring tasks are created correctly
  - Verify task worker handles recurring execution

### Integration Tests

- Test end-to-end flow:
  1. Schedule a script action for future execution
  2. Wait for scheduled time
  3. Verify script executes with correct context
  4. Test recurring script execution

## Benefits

1. **Unified Model**: Single action abstraction used everywhere
2. **Type Safety**: Enum prevents invalid action types
3. **Code Reuse**: Shared executor reduces duplication
4. **Simplicity**: No database changes required
5. **Backward Compatibility**: Existing scheduled tasks continue to work
6. **Extensibility**: Easy to add new action types

## Key Design Decisions

1. **No Database Migration**: The existing task system already has all the fields we need (`task_type`, `payload`, `scheduled_at`, `recurrence_rule`)
2. **Actions as Task Types**: Actions map directly to existing task types (`wake_llm` → `llm_callback`, `script` → `script_execution`)
3. **Thin Abstraction Layer**: The action system is just a convenience layer over the task system
4. **Preserve Existing APIs**: All existing scheduling tools continue to work

## Future Extensions

Once this foundation is in place, it becomes easy to:

- Add new action types (e.g., "webhook", "email") by:
  1. Adding to the ActionType enum
  2. Adding a new task type and handler
  3. Updating `execute_action` to map the new action
- Add action composition (multiple actions in sequence)
- Track action execution history uniformly

## Implementation Checklist

- [x] Create actions.py with shared executor
- [x] Refactor EventProcessor and verify tests pass
- [ ] Extend execute_action to support scheduling
- [ ] Update scheduling tools to use actions
- [ ] Add tests for scheduled script execution
- [ ] Add support for recurring actions
- [ ] Update documentation
- [ ] Deploy and test
