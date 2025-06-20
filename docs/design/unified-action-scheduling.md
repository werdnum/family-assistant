# Unified Action Scheduling Design

## Overview

This document describes the implementation plan for unifying the action model across scheduled tasks and event listeners in Family Assistant. Currently, event listeners support multiple action types (wake_llm, script) while scheduled tasks only support LLM callbacks. This design extends scheduled tasks to use the same action abstraction.

## Goals

1. **Consistency**: Same action model for both event-based and time-based triggers
2. **Code Reuse**: Share action execution logic between systems
3. **Type Safety**: Use enums for action types
4. **Incremental Implementation**: Each step leaves the system in a working state

## Implementation Plan

### Step 1: Create Shared Action Executor

Create a new module `src/family_assistant/actions.py` to house shared action logic:

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

### Step 2: Refactor Event Processor

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

### Step 3: Create Database Migration

Create a new Alembic migration to add action columns to the tasks table:

```python
# alembic/versions/xxxx_add_action_columns_to_tasks.py
"""Add action columns to tasks table

Revision ID: xxxx
Revises: yyyy
Create Date: 2024-12-20 xx:xx:xx

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = 'xxxx'
down_revision = 'yyyy'
branch_labels = None
depends_on = None


def upgrade():
    # Add action columns with defaults
    op.add_column('tasks', 
        sa.Column('action_type', sa.String(), nullable=True))
    op.add_column('tasks',
        sa.Column('action_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    
    # Set defaults for existing rows
    op.execute(
        sa.text("UPDATE tasks SET action_type = 'wake_llm' WHERE action_type IS NULL")
    )
    op.execute(
        sa.text("UPDATE tasks SET action_config = '{}' WHERE action_config IS NULL")
    )
    
    # Migrate existing llm_callback tasks
    tasks_table = sa.table('tasks',
        sa.column('task_type', sa.String),
        sa.column('action_type', sa.String),
        sa.column('action_config', postgresql.JSONB),
        sa.column('payload', postgresql.JSONB)
    )
    
    # Build the update using SQLAlchemy constructs
    op.execute(
        tasks_table.update()
        .where(tasks_table.c.task_type == 'llm_callback')
        .where(tasks_table.c.action_type == 'wake_llm')
        .values(
            action_config=sa.func.jsonb_build_object(
                'context', tasks_table.c.payload['callback_context']
            )
        )
    )
    
    # Add constraints
    op.create_check_constraint(
        'tasks_action_type_check',
        'tasks',
        sa.text("action_type IN ('wake_llm', 'script')")
    )
    
    # Set NOT NULL after migration
    op.alter_column('tasks', 'action_type',
        existing_type=sa.String(),
        nullable=False,
        server_default='wake_llm')
    op.alter_column('tasks', 'action_config',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"))


def downgrade():
    # Drop constraints
    op.drop_constraint('tasks_action_type_check', 'tasks', type_='check')
    
    # Remove columns
    op.drop_column('tasks', 'action_config')
    op.drop_column('tasks', 'action_type')
```

### Step 4: Update Tasks Repository

Update `src/family_assistant/storage/repositories/tasks.py` to handle the new columns:

```python
# In TasksRepository.enqueue method, add parameters:
async def enqueue(
    self,
    task_id: str,
    task_type: str,
    payload: dict[str, Any],
    scheduled_at: datetime | None = None,
    max_retries_override: int | None = None,
    recurrence_rule: str | None = None,
    action_type: str = "wake_llm",  # New parameter
    action_config: dict[str, Any] | None = None,  # New parameter
) -> None:
    """Enqueue a new task with optional action configuration."""
    
    if action_config is None:
        action_config = {}
    
    # Add to insert statement
    stmt = tasks_table.insert().values(
        task_id=task_id,
        task_type=task_type,
        payload=payload,
        scheduled_at=scheduled_at or datetime.now(timezone.utc),
        max_retries=max_retries_override or 3,
        recurrence_rule=recurrence_rule,
        action_type=action_type,  # New column
        action_config=action_config,  # New column
        # ... other columns
    )
```

### Step 5: Update Task Worker

Update `src/family_assistant/task_worker.py` to handle scheduled actions:

```python
# Add import
from family_assistant.actions import ActionType, execute_action

# Add method to ToolExecutionContext to get current task
class ToolExecutionContext:
    async def get_current_task(self) -> dict[str, Any]:
        """Get the current task being executed."""
        if hasattr(self, 'task_id') and self.task_id:
            async with DatabaseContext() as db_ctx:
                result = await db_ctx.fetch_one(
                    text("SELECT * FROM tasks WHERE task_id = :task_id"),
                    {"task_id": self.task_id}
                )
                return dict(result) if result else None
        return None

# Create new handler for scheduled actions
async def handle_scheduled_action(
    exec_context: ToolExecutionContext,
    payload: dict[str, Any]
) -> None:
    """Execute scheduled actions using shared action executor."""
    
    # Get action details from task
    task = await exec_context.get_current_task()
    if not task:
        raise ValueError("Could not find current task")
    
    action_type = ActionType(task["action_type"])
    action_config = task["action_config"]
    
    # Build context
    context = {
        "trigger": "Scheduled action",
        "task_id": task["task_id"],
        "scheduled_at": task["scheduled_at"].isoformat(),
    }
    
    # Add reminder config if present (for backward compatibility)
    if "reminder_config" in payload:
        context["reminder_config"] = payload["reminder_config"]
    
    # Execute using shared executor
    async with DatabaseContext() as db_ctx:
        await execute_action(
            db_ctx=db_ctx,
            action_type=action_type,
            action_config=action_config,
            conversation_id=payload["conversation_id"],
            interface_type=payload.get("interface_type", "telegram"),
            context=context,
        )

# Update handle_llm_callback to delegate to handle_scheduled_action
async def handle_llm_callback(
    exec_context: ToolExecutionContext,
    payload: dict[str, Any]
) -> None:
    """Legacy handler - delegates to handle_scheduled_action."""
    await handle_scheduled_action(exec_context, payload)
```

### Step 6: Update Scheduling Tools

Update `src/family_assistant/tools/tasks.py` to support action-based scheduling:

```python
from family_assistant.actions import ActionType

# Update tool definitions
TASK_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "schedule_action",
            "description": (
                "Schedule any action (wake_llm or script) to execute at a specific time. "
                "This is the general-purpose scheduling tool that replaces schedule_future_callback."
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
    # Similar updates for schedule_recurring_action and schedule_reminder
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
    
    # Create task with action details
    task_id = f"scheduled_action_{uuid.uuid4()}"
    
    # Minimal payload - just identifiers
    payload = {
        "interface_type": exec_context.interface_type,
        "conversation_id": exec_context.conversation_id,
    }
    
    await exec_context.db_context.tasks.enqueue(
        task_id=task_id,
        task_type="scheduled_action",  # New generic task type
        payload=payload,
        scheduled_at=scheduled_dt,
        action_type=action_type,
        action_config=action_config,
    )
    
    return f"OK. {action_type} action scheduled for {schedule_time}"

# Keep backward compatibility aliases
async def schedule_future_callback_tool(
    exec_context: ToolExecutionContext,
    callback_time: str,
    context: str,
) -> str:
    """Legacy tool - delegates to schedule_action."""
    return await schedule_action_tool(
        exec_context=exec_context,
        schedule_time=callback_time,
        action_type="wake_llm",
        action_config={"context": context},
    )
```

## Testing Strategy

### Step 1 & 2: Refactor Event System

- Run existing event listener tests after refactoring
- No new tests needed, all should pass

### Step 3 & 4: Database Migration

- Test migration on development database
- Verify existing tasks are migrated correctly
- Test repository with new columns

### Step 5: Task Worker

- Update existing callback tests to verify they still work
- Add new tests for script scheduling:

```python
async def test_schedule_and_execute_script_action(test_db_engine):
    """Test scheduling and executing a script action."""
    # Schedule a script action
    # Verify it executes correctly
    # Check that script was run with correct context
```

### Step 6: Tools Update

- Update tool tests to use new action-based tools
- Add tests for both wake_llm and script actions
- Verify backward compatibility tools still work

## Benefits

1. **Unified Model**: Single action abstraction used everywhere
2. **Type Safety**: Enum prevents invalid action types
3. **Code Reuse**: Shared executor reduces duplication
4. **Extensibility**: Easy to add new action types
5. **Clean Migration**: Existing data is properly migrated

## Future Extensions

Once this foundation is in place, it becomes easy to:

- Add new action types (e.g., "webhook", "email")
- Add action composition (multiple actions in sequence)
- Add conditional actions (though scripts can handle this)
- Track action execution history uniformly

## Migration Checklist

- [ ] Create actions.py with shared executor
- [ ] Refactor EventProcessor and verify tests pass
- [ ] Create and run database migration
- [ ] Update TasksRepository
- [ ] Update task worker with new handler
- [ ] Update scheduling tools
- [ ] Add new tests for script scheduling
- [ ] Update documentation
- [ ] Deploy to staging and test
- [ ] Deploy to production
