# Unified Automations Design

## Status

**In Progress** - Backend Complete, Frontend Pending

- **Phase 1-4 Complete** (2025-10-11 to 2025-10-12): Database, tools, task worker, and API
  implemented
- **Phase 5 In Progress** (2025-10-12): Frontend UI migration underway
- **Remaining**: Documentation updates, old tool/UI cleanup, final testing

## Progress Summary

### What's Complete

**Phase 1: Database Layer** (commit 9542ef03)

- Created `schedule_automations` table with RRULE support
- Implemented `ScheduleAutomationsRepository` for schedule-based automations
- Implemented `AutomationsRepository` as unified abstraction layer
- Added repositories to `DatabaseContext`
- Full test coverage for repository layer

**Phase 2: Tool Layer** (commits 7799d541, 213271f6, 7b80cdcf)

- Created 8 unified automation tools (`create_event_automation`, `create_schedule_automation`, etc.)
- Removed old event listener tools to eliminate confusion
- Updated `config.yaml` to enable new tools in default profile
- All tools working with LLM integration

**Phase 3: Task Worker** (commit 4664d105)

- Updated task worker to track automation lifecycle
- Added `after_task_execution` callback for schedule automations
- Automatic rescheduling of recurring tasks
- Execution count and last_execution_at tracking

**Phase 4: Web API** (commit 1c6da302)

- Created unified `/api/v1/automations` REST API
- CRUD operations for both event and schedule automations
- Preserved old `/api/v1/event-listeners` for backward compatibility
- Fixed FastAPI Union validation issue (parse dict based on path param)
- Implemented sentinel pattern for nullable field updates
- Added conversation_id security verification on all endpoints
- Pagination support (currently in-memory, database-level pending)

### What's Remaining

**Phase 5: Frontend UI** (in progress)

- Create new `pages/Automations/` React components
- Unified list view with type filtering
- Detail views for both automation types
- Create forms for event and schedule automations
- Update navigation to use new `/automations` route
- Delete old `pages/EventListeners/` after migration
- Remove old `listeners_api.py` after UI fully migrated

**Phase 6: Documentation** (pending)

- Update user guide with automations documentation
- Update system prompt to explain unified automation concept to LLM
- Add tool usage examples
- Document migration from old event listeners

**Phase 7: Final Testing** (pending)

- Frontend component tests
- E2E tests for full automation lifecycle
- Performance testing for pagination at scale
- Database-level pagination implementation (UNION query optimization)

### Known Limitations

1. **In-memory pagination**: Current implementation fetches all automations then paginates in
   memory. This is acceptable for typical users (\<100 automations) but won't scale. Database-level
   UNION pagination will be implemented in Phase 7.

2. **Inconsistent sentinel pattern**: Schedule automation updates use `_UNSET` sentinel for clean
   partial updates, but event listener updates do not. This causes API layer complexity for event
   updates.

3. **Dual API during migration**: Both `/automations` and `/event-listeners` APIs are operational.
   The old API will be removed after frontend migration completes.

## Overview

This document proposes unifying event listeners and recurring actions under a single "automations"
abstraction, reducing tool complexity while providing a cleaner mental model for both LLMs and
users.

## Current State

### Event Listeners

Event listeners are implemented as a first-class entity with:

**Storage**: `event_listeners` table (src/family_assistant/storage/events.py)

- Persistent entity with name, description, enabled status
- Trigger: External events (Home Assistant, indexing, webhooks)
- Action: wake_llm or script execution
- Features: Rate limiting, execution stats, Starlark conditions

**Management**: 6 tools (src/family_assistant/tools/event_listeners.py)

- `create_event_listener`
- `list_event_listeners`
- `delete_event_listener`
- `toggle_event_listener`
- `validate_event_listener_script`
- `test_event_listener_script`

**UI**: Dedicated page at `/event-listeners` (frontend/src/pages/EventListeners/)

- List view with filters
- Detail view with execution stats
- Form for creation/editing

### Recurring Actions

PR #312 added instance-level management for scheduled tasks:

**Storage**: `tasks` table (src/family_assistant/storage/tasks.py)

- Tasks have optional `recurrence_rule` (RRULE)
- No persistent "recurring action" entity
- Tasks disappear after execution

**Management**: 3 tools (src/family_assistant/tools/tasks.py - from PR #312)

- `list_pending_actions` - View pending task instances
- `modify_pending_action` - Modify individual instance
- `cancel_pending_action` - Cancel individual instance

**Gap**: No entity-level management

- Can't list "all my recurring actions" after execution
- Can't name recurring actions
- Can't enable/disable without canceling all instances
- Can't view execution history over time
- No UI for management

## Problem Statement

Event listeners and recurring actions are conceptually identical:

| Feature             | Event Listener     | Recurring Action      |
| ------------------- | ------------------ | --------------------- |
| Has name            | ✓                  | ✗ (missing)           |
| Has description     | ✓                  | ✗ (missing)           |
| Action type         | wake_llm, script   | wake_llm, script      |
| Action config       | ✓                  | ✓                     |
| Enable/disable      | ✓                  | ✗ (missing)           |
| Execution stats     | ✓                  | ✗ (missing)           |
| **Only difference** | **Trigger: event** | **Trigger: schedule** |

Creating separate tool sets (6 + 4 = 10 tools) for nearly identical functionality:

- Confuses LLMs with too many similar tools
- Creates separate mental models for the same concept
- Duplicates UI/API patterns
- Misses optimization opportunities

Users naturally think: "I have automations that run when X happens" - where X is either an event or
a time.

## Proposed Solution

### Core Concept

**Automation**: A named, configurable action that executes automatically based on a trigger.

**Trigger Types**:

- **Event**: External system event (Home Assistant, webhook, indexing)
- **Schedule**: Time-based RRULE pattern

Both automation types share:

- Name, description, conversation scoping
- Action configuration (wake_llm or script)
- Enable/disable functionality
- Execution tracking and statistics
- Validation and testing

### Architecture Principles

1. **Separate storage, unified interface**: Keep event_listeners and schedule_automations as
   separate tables for type safety, but present a unified "automations" API
2. **Type-specific creation**: Different parameters needed (event conditions vs RRULE)
3. **Unified management**: By ID, operations work identically regardless of type
4. **No breaking changes**: Existing event_listeners continue working, migration is additive

## Database Schema

### Existing: event_listeners table

```python
# src/family_assistant/storage/events.py (unchanged)
event_listeners_table = Table(
    "event_listeners",
    metadata,
    Column("id", UUID, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("conversation_id", Text, nullable=False),
    Column("interface_type", Enum(InterfaceType), nullable=False),

    # Event-specific trigger configuration
    Column("source_id", Enum(...), nullable=False),
    Column("match_conditions", JSONB),
    Column("condition_script", Text),

    # Shared action configuration
    Column("action_type", Enum(ActionType), nullable=False),
    Column("action_config", JSONB, nullable=False),

    # Shared management fields
    Column("enabled", Boolean, default=True, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
    Column("last_execution_at", DateTime),
    Column("daily_executions", Integer, default=0),
    Column("daily_reset_at", Date),

    UniqueConstraint("name", "conversation_id", name="uq_name_conversation"),
)
```

### New: schedule_automations table

```python
# src/family_assistant/storage/schedule_automations.py (new)
schedule_automations_table = Table(
    "schedule_automations",
    metadata,
    Column("id", UUID, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("conversation_id", Text, nullable=False),
    Column("interface_type", Enum(InterfaceType), nullable=False),

    # Schedule-specific trigger configuration
    Column("recurrence_rule", Text, nullable=False),  # RRULE string
    Column("next_scheduled_at", DateTime(timezone=True)),

    # Shared action configuration
    Column("action_type", Enum(ActionType), nullable=False),
    Column("action_config", JSONB, nullable=False),

    # Shared management fields
    Column("enabled", Boolean, default=True, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_execution_at", DateTime(timezone=True)),
    Column("execution_count", Integer, default=0, nullable=False),

    UniqueConstraint("name", "conversation_id", name="uq_sched_name_conversation"),
)
```

### Task Integration

Tasks table (existing) gets minimal changes:

```python
# tasks.payload (JSONB) now may contain:
{
    "conversation_id": "...",
    "interface_type": "...",
    "callback_context": "...",  # For wake_llm
    "script_code": "...",        # For script

    # New: Link back to parent automation
    "automation_type": "event" | "schedule",  # Optional
    "automation_id": "uuid",                   # Optional
}
```

## Repository Layer

### Unified Abstraction

```python
# src/family_assistant/storage/repositories/automations.py (new)

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

@dataclass
class Automation:
    """Unified automation model across event and schedule types."""
    id: UUID
    type: Literal["event", "schedule"]
    name: str
    description: str | None
    conversation_id: str
    interface_type: InterfaceType
    action_type: ActionType
    action_config: dict[str, Any]
    enabled: bool
    created_at: datetime
    last_execution_at: datetime | None
    execution_count: int

    # Type-specific fields (one will be None based on type)
    event_config: dict[str, Any] | None  # source_id, match_conditions, condition_script
    schedule_config: dict[str, Any] | None  # recurrence_rule, next_scheduled_at


class AutomationsRepository:
    """Unified repository for both event and schedule automations."""

    def __init__(self, db_context: DatabaseContext):
        self.db = db_context
        self.events = EventListenersRepository(db_context)
        self.schedules = ScheduleAutomationsRepository(db_context)

    async def get_by_id(self, automation_id: UUID) -> Automation | None:
        """Get automation by ID, checking both types."""
        # Try event listener first
        event = await self.events.get_event_listener_by_id(automation_id)
        if event:
            return self._event_to_automation(event)

        # Try schedule automation
        schedule = await self.schedules.get_by_id(automation_id)
        if schedule:
            return self._schedule_to_automation(schedule)

        return None

    async def list_all(
        self,
        conversation_id: str,
        automation_type: Literal["event", "schedule", "all"] = "all",
        enabled_only: bool = False,
    ) -> list[Automation]:
        """List all automations for a conversation."""
        results = []

        if automation_type in ("event", "all"):
            events = await self.events.get_event_listeners(
                conversation_id=conversation_id,
                enabled_only=enabled_only,
            )
            results.extend([self._event_to_automation(e) for e in events])

        if automation_type in ("schedule", "all"):
            schedules = await self.schedules.list_all(
                conversation_id=conversation_id,
                enabled_only=enabled_only,
            )
            results.extend([self._schedule_to_automation(s) for s in schedules])

        # Sort by created_at descending
        results.sort(key=lambda a: a.created_at, reverse=True)
        return results

    async def update_enabled(self, automation_id: UUID, enabled: bool) -> bool:
        """Enable/disable automation by ID (works for both types)."""
        # Try event listener
        if await self.events.update_event_listener_enabled(automation_id, enabled):
            return True

        # Try schedule automation
        if await self.schedules.update_enabled(automation_id, enabled):
            return True

        return False

    async def delete(self, automation_id: UUID) -> bool:
        """Delete automation by ID (works for both types)."""
        # Try event listener
        if await self.events.delete_event_listener(automation_id):
            return True

        # Try schedule automation
        if await self.schedules.delete(automation_id):
            return True

        return False

    async def get_execution_stats(
        self,
        automation_id: UUID,
    ) -> dict[str, Any]:
        """Get execution statistics for an automation."""
        automation = await self.get_by_id(automation_id)
        if not automation:
            return {}

        if automation.type == "event":
            return await self.events.get_listener_execution_stats(automation_id)
        else:
            return await self.schedules.get_execution_stats(automation_id)


class ScheduleAutomationsRepository:
    """Repository for schedule-based automations."""

    async def create(
        self,
        name: str,
        recurrence_rule: str,
        action_type: ActionType,
        action_config: dict[str, Any],
        conversation_id: str,
        interface_type: InterfaceType,
        description: str | None = None,
    ) -> UUID:
        """Create a schedule automation and schedule first task instance."""
        # Insert into schedule_automations table
        # Calculate next_scheduled_at from RRULE
        # Schedule first task instance with automation_id in payload
        ...

    async def get_by_id(self, automation_id: UUID) -> dict[str, Any] | None:
        """Get schedule automation by ID."""
        ...

    async def list_all(
        self,
        conversation_id: str,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List schedule automations for a conversation."""
        ...

    async def update(
        self,
        automation_id: UUID,
        recurrence_rule: str | None = None,
        action_config: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> bool:
        """Update schedule automation configuration."""
        # If recurrence_rule changed, recalculate next_scheduled_at
        # Cancel pending task instances and reschedule
        ...

    async def update_enabled(self, automation_id: UUID, enabled: bool) -> bool:
        """Enable/disable schedule automation."""
        ...

    async def delete(self, automation_id: UUID) -> bool:
        """Delete schedule automation and cancel all pending task instances."""
        ...

    async def after_task_execution(
        self,
        automation_id: UUID,
        execution_time: datetime,
    ) -> None:
        """Update automation after task execution."""
        # Update last_execution_at, execution_count
        # Calculate and schedule next instance
        # Check if still enabled before scheduling
        ...

    async def get_execution_stats(
        self,
        automation_id: UUID,
    ) -> dict[str, Any]:
        """Get execution statistics from tasks table."""
        # Query tasks table for this automation_id
        ...
```

### DatabaseContext Integration

```python
# src/family_assistant/storage/context.py (update)

class DatabaseContext:
    @property
    def automations(self) -> AutomationsRepository:
        """Unified automations repository."""
        if self._automations_repo is None:
            self._automations_repo = AutomationsRepository(self)
        return self._automations_repo

    @property
    def schedule_automations(self) -> ScheduleAutomationsRepository:
        """Direct access to schedule automations (for task worker)."""
        if self._schedule_automations_repo is None:
            self._schedule_automations_repo = ScheduleAutomationsRepository(self)
        return self._schedule_automations_repo
```

## Tool Interface

### Consolidated Tool Set (8 tools)

```python
# src/family_assistant/tools/automations.py (new)

AUTOMATION_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_event_automation",
            "description": (
                "Creates an automation that triggers when specific events occur "
                "(Home Assistant state changes, document indexing, webhooks). "
                "Use this for event-driven automations like 'notify when door opens' "
                "or 'summarize new documents'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique name for this automation"},
                    "description": {"type": "string", "description": "Optional description"},
                    "event_source": {
                        "type": "string",
                        "enum": ["home_assistant", "indexing", "webhook"],
                        "description": "Source of events to monitor"
                    },
                    "match_conditions": {
                        "type": "object",
                        "description": "Event matching criteria (JSON object)"
                    },
                    "condition_script": {
                        "type": "string",
                        "description": "Optional Starlark script for complex matching"
                    },
                    "action_type": {
                        "type": "string",
                        "enum": ["wake_llm", "script"],
                        "description": "Type of action to execute"
                    },
                    "action_config": {
                        "type": "object",
                        "description": "Action configuration"
                    },
                },
                "required": ["name", "event_source", "match_conditions", "action_type", "action_config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_schedule_automation",
            "description": (
                "Creates an automation that triggers on a recurring schedule. "
                "Use this for time-based automations like 'daily summary at 8am' "
                "or 'weekly reminder every Monday'. Supports RRULE format for complex schedules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique name for this automation"},
                    "description": {"type": "string", "description": "Optional description"},
                    "recurrence_rule": {
                        "type": "string",
                        "description": "RRULE string (e.g., 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0')"
                    },
                    "action_type": {
                        "type": "string",
                        "enum": ["wake_llm", "script"],
                        "description": "Type of action to execute"
                    },
                    "action_config": {
                        "type": "object",
                        "description": "Action configuration"
                    },
                },
                "required": ["name", "recurrence_rule", "action_type", "action_config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": (
                "Lists all automations (both event-based and schedule-based) for the current conversation. "
                "Shows names, types, triggers, actions, enabled status, and execution statistics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type_filter": {
                        "type": "string",
                        "enum": ["all", "event", "schedule"],
                        "description": "Filter by automation type (default: all)"
                    },
                    "enabled_only": {
                        "type": "boolean",
                        "description": "Only show enabled automations (default: false)"
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_automation",
            "description": (
                "Updates an automation's configuration. Can modify description, trigger configuration, "
                "or action configuration. Works for both event and schedule automations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {"type": "string", "description": "ID of automation to update"},
                    "description": {"type": "string", "description": "New description"},
                    "trigger_config": {
                        "type": "object",
                        "description": "New trigger configuration (RRULE for schedule, conditions for event)"
                    },
                    "action_config": {
                        "type": "object",
                        "description": "New action configuration"
                    },
                },
                "required": ["automation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": (
                "Deletes an automation by ID. For schedule automations, also cancels all pending task instances. "
                "Works for both event and schedule automations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {"type": "string", "description": "ID of automation to delete"},
                },
                "required": ["automation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_automation",
            "description": (
                "Enables or disables an automation. Disabled automations won't trigger. "
                "Works for both event and schedule automations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {"type": "string", "description": "ID of automation to toggle"},
                    "enabled": {"type": "boolean", "description": "New enabled state"},
                },
                "required": ["automation_id", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_automation_script",
            "description": "Validates Starlark script syntax (for event condition scripts or script actions).",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_code": {"type": "string", "description": "Starlark code to validate"},
                },
                "required": ["script_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_automation_script",
            "description": "Tests a Starlark script with sample data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_code": {"type": "string", "description": "Starlark code to test"},
                    "test_data": {"type": "object", "description": "Sample data for testing"},
                },
                "required": ["script_code", "test_data"],
            },
        },
    },
]
```

### Tool Implementation

```python
async def list_automations_tool(
    exec_context: ToolExecutionContext,
    type_filter: Literal["all", "event", "schedule"] = "all",
    enabled_only: bool = False,
) -> str:
    """List all automations for the current conversation."""
    db_context = exec_context.db_context
    conversation_id = exec_context.conversation_id

    automations = await db_context.automations.list_all(
        conversation_id=conversation_id,
        automation_type=type_filter,
        enabled_only=enabled_only,
    )

    if not automations:
        return "No automations found for this conversation."

    formatted = ["Your automations:"]
    for auto in automations:
        status = "✓ Enabled" if auto.enabled else "✗ Disabled"
        trigger = _format_trigger(auto)
        action = _format_action(auto)
        stats = f"Executions: {auto.execution_count}"

        formatted.append(
            f"\n- {auto.name} ({auto.type})\n"
            f"  Status: {status}\n"
            f"  Trigger: {trigger}\n"
            f"  Action: {action}\n"
            f"  {stats}\n"
            f"  ID: {auto.id}"
        )

    return "\n".join(formatted)


async def toggle_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: str,
    enabled: bool,
) -> str:
    """Enable or disable an automation."""
    db_context = exec_context.db_context

    try:
        automation_uuid = UUID(automation_id)
    except ValueError:
        return f"Error: Invalid automation ID format: {automation_id}"

    success = await db_context.automations.update_enabled(automation_uuid, enabled)

    if success:
        state = "enabled" if enabled else "disabled"
        return f"Automation {automation_id} has been {state}."
    else:
        return f"Error: Automation {automation_id} not found."
```

## Web API

### Unified Endpoint Design

```python
# src/family_assistant/web/routers/automations_api.py (new)

from pydantic import BaseModel

class AutomationResponse(BaseModel):
    id: str
    type: Literal["event", "schedule"]
    name: str
    description: str | None
    enabled: bool
    action_type: str
    action_config: dict[str, Any]
    created_at: datetime
    last_execution_at: datetime | None
    execution_count: int

    # Type-specific fields
    event_config: dict[str, Any] | None
    schedule_config: dict[str, Any] | None


class CreateEventAutomationRequest(BaseModel):
    name: str
    description: str | None = None
    event_source: str
    match_conditions: dict[str, Any]
    condition_script: str | None = None
    action_type: str
    action_config: dict[str, Any]


class CreateScheduleAutomationRequest(BaseModel):
    name: str
    description: str | None = None
    recurrence_rule: str
    action_type: str
    action_config: dict[str, Any]


router = APIRouter(prefix="/api/v1/automations", tags=["automations"])

@router.get("", response_model=list[AutomationResponse])
async def list_automations(
    type: Literal["all", "event", "schedule"] = "all",
    enabled_only: bool = False,
    conversation_id: str = Depends(get_conversation_id),
):
    """List all automations for the current conversation."""
    async with DatabaseContext() as db:
        automations = await db.automations.list_all(
            conversation_id=conversation_id,
            automation_type=type,
            enabled_only=enabled_only,
        )
        return [AutomationResponse.from_orm(a) for a in automations]


@router.get("/{automation_id}", response_model=AutomationResponse)
async def get_automation(
    automation_id: UUID,
    conversation_id: str = Depends(get_conversation_id),
):
    """Get a specific automation by ID."""
    async with DatabaseContext() as db:
        automation = await db.automations.get_by_id(automation_id)
        if not automation or automation.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Automation not found")
        return AutomationResponse.from_orm(automation)


@router.post("/event", response_model=AutomationResponse, status_code=201)
async def create_event_automation(
    request: CreateEventAutomationRequest,
    conversation_id: str = Depends(get_conversation_id),
    interface_type: InterfaceType = Depends(get_interface_type),
):
    """Create a new event-based automation."""
    async with DatabaseContext() as db:
        # Repository returns full entity, not just ID (avoids extra query)
        automation = await db.events.create_event_listener_full(
            name=request.name,
            description=request.description,
            source_id=request.event_source,
            match_conditions=request.match_conditions,
            condition_script=request.condition_script,
            action_type=request.action_type,
            action_config=request.action_config,
            conversation_id=conversation_id,
            interface_type=interface_type,
        )
        return AutomationResponse.from_orm(automation)


@router.post("/schedule", response_model=AutomationResponse, status_code=201)
async def create_schedule_automation(
    request: CreateScheduleAutomationRequest,
    conversation_id: str = Depends(get_conversation_id),
    interface_type: InterfaceType = Depends(get_interface_type),
):
    """Create a new schedule-based automation."""
    async with DatabaseContext() as db:
        # Repository returns full entity, not just ID (avoids extra query)
        automation = await db.schedule_automations.create_full(
            name=request.name,
            description=request.description,
            recurrence_rule=request.recurrence_rule,
            action_type=request.action_type,
            action_config=request.action_config,
            conversation_id=conversation_id,
            interface_type=interface_type,
        )
        return AutomationResponse.from_orm(automation)


@router.patch("/{automation_id}", response_model=AutomationResponse)
async def update_automation(
    automation_id: UUID,
    enabled: bool | None = None,
    description: str | None = None,
    conversation_id: str = Depends(get_conversation_id),
):
    """Update an automation."""
    async with DatabaseContext() as db:
        automation = await db.automations.get_by_id(automation_id)
        if not automation or automation.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Automation not found")

        if enabled is not None:
            await db.automations.update_enabled(automation_id, enabled)

        # Additional update logic...

        updated = await db.automations.get_by_id(automation_id)
        return AutomationResponse.from_orm(updated)


@router.delete("/{automation_id}", status_code=204)
async def delete_automation(
    automation_id: UUID,
    conversation_id: str = Depends(get_conversation_id),
):
    """Delete an automation."""
    async with DatabaseContext() as db:
        automation = await db.automations.get_by_id(automation_id)
        if not automation or automation.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Automation not found")

        success = await db.automations.delete(automation_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete automation")


@router.get("/{automation_id}/stats")
async def get_automation_stats(
    automation_id: UUID,
    conversation_id: str = Depends(get_conversation_id),
):
    """Get execution statistics for an automation."""
    async with DatabaseContext() as db:
        automation = await db.automations.get_by_id(automation_id)
        if not automation or automation.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Automation not found")

        stats = await db.automations.get_execution_stats(automation_id)
        return stats
```

## Frontend UI

### Unified Automations Page

```jsx
// frontend/src/pages/Automations/AutomationsApp.jsx

import { Routes, Route } from 'react-router-dom';
import AutomationsList from './AutomationsList';
import AutomationDetail from './AutomationDetail';
import CreateEventAutomation from './CreateEventAutomation';
import CreateScheduleAutomation from './CreateScheduleAutomation';

export default function AutomationsApp() {
  return (
    <Routes>
      <Route path="/" element={<AutomationsList />} />
      <Route path="/:id" element={<AutomationDetail />} />
      <Route path="/create/event" element={<CreateEventAutomation />} />
      <Route path="/create/schedule" element={<CreateScheduleAutomation />} />
    </Routes>
  );
}
```

```jsx
// frontend/src/pages/Automations/AutomationsList.jsx

import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';

export default function AutomationsList() {
  const [automations, setAutomations] = useState([]);
  const [typeFilter, setTypeFilter] = useState('all');
  const [showDisabled, setShowDisabled] = useState(true);

  useEffect(() => {
    fetchAutomations();
  }, [typeFilter, showDisabled]);

  const fetchAutomations = async () => {
    const params = new URLSearchParams({
      type: typeFilter,
      enabled_only: !showDisabled,
    });
    const response = await fetch(`/api/v1/automations?${params}`);
    const data = await response.json();
    setAutomations(data);
  };

  const toggleAutomation = async (id, currentEnabled) => {
    await fetch(`/api/v1/automations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !currentEnabled }),
    });
    fetchAutomations();
  };

  return (
    <div className="automations-page">
      <header>
        <h1>Automations</h1>
        <div className="create-buttons">
          <Link to="/automations/create/event" className="btn btn-primary">
            + Event Automation
          </Link>
          <Link to="/automations/create/schedule" className="btn btn-primary">
            + Schedule Automation
          </Link>
        </div>
      </header>

      <div className="filters">
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="all">All Types</option>
          <option value="event">Event-Based</option>
          <option value="schedule">Schedule-Based</option>
        </select>
        <label>
          <input
            type="checkbox"
            checked={showDisabled}
            onChange={(e) => setShowDisabled(e.target.checked)}
          />
          Show disabled
        </label>
      </div>

      <div className="automations-list">
        {automations.map((auto) => (
          <div key={auto.id} className={`automation-card ${auto.enabled ? 'enabled' : 'disabled'}`}>
            <div className="automation-header">
              <Link to={`/automations/${auto.id}`}>
                <h3>{auto.name}</h3>
              </Link>
              <button
                className="toggle-btn"
                onClick={() => toggleAutomation(auto.id, auto.enabled)}
              >
                {auto.enabled ? '✓ Enabled' : '✗ Disabled'}
              </button>
            </div>

            <div className="automation-details">
              <span className="badge">{auto.type}</span>
              <div className="trigger">
                <strong>Trigger:</strong> {formatTrigger(auto)}
              </div>
              <div className="action">
                <strong>Action:</strong> {formatAction(auto)}
              </div>
              <div className="stats">
                Executions: {auto.execution_count} |
                Last run: {auto.last_execution_at ? formatDate(auto.last_execution_at) : 'Never'}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

### Navigation Integration

```jsx
// frontend/src/App.jsx (update)

<nav>
  <Link to="/">Home</Link>
  <Link to="/notes">Notes</Link>
  <Link to="/documents">Documents</Link>
  <Link to="/automations">Automations</Link>  {/* Unified! */}
  <Link to="/calendar">Calendar</Link>
</nav>
```

## Task Worker Integration

```python
# src/family_assistant/task_worker.py (update)

async def _handle_script_execution(self, task: Task):
    """Handle script execution task."""
    # ... existing execution logic ...

    # After successful execution, update parent automation if present
    automation_id = task.payload.get("automation_id")
    automation_type = task.payload.get("automation_type")

    if automation_type == "schedule" and automation_id:
        async with DatabaseContext() as db:
            await db.schedule_automations.after_task_execution(
                automation_id=UUID(automation_id),
                execution_time=self.clock.now(),
            )


async def _handle_llm_callback(self, task: Task):
    """Handle LLM callback task."""
    # ... existing execution logic ...

    # After successful execution, update parent automation if present
    automation_id = task.payload.get("automation_id")
    automation_type = task.payload.get("automation_type")

    if automation_type == "schedule" and automation_id:
        async with DatabaseContext() as db:
            await db.schedule_automations.after_task_execution(
                automation_id=UUID(automation_id),
                execution_time=self.clock.now(),
            )
```

## Migration Path

### Phase 1: Add Schedule Automations Infrastructure

**Goal**: Add new table and repository without breaking existing functionality

1. Create `schedule_automations` table
   - Migration: `alembic revision --autogenerate -m "Add schedule_automations table"`
2. Implement `ScheduleAutomationsRepository`
3. Implement `AutomationsRepository` abstraction layer
4. Add to `DatabaseContext`
5. Tests for repository layer

**No user-facing changes yet**

### Phase 2: Add Unified Tools

**Goal**: Create new automation tools alongside existing event listener tools

1. Create `src/family_assistant/tools/automations.py`
2. Implement 8 automation tools (listed above)
3. Register tools in `__init__.py` and `config.yaml`
4. Update `schedule_recurring_action` to create schedule automation entities
5. Update task worker to call `after_task_execution`
6. Tests for tools

**Both old and new tools coexist**

### Phase 3: Deprecate Old Event Listener Tools

**Goal**: Mark old tools as deprecated, point to new ones

1. Add deprecation warnings to old tool descriptions
2. Update system prompts to prefer new tools
3. Keep old tools functional for backwards compatibility
4. Update documentation

**Old tools still work but are discouraged**

### Phase 4: Build Unified UI

**Goal**: Replace separate event listeners page with unified automations page

1. Create `frontend/src/pages/Automations/`
2. Implement unified list/detail/create views
3. Add API endpoints
4. Update navigation
5. Tests for UI components

**Old UI redirects to new UI**

### Phase 5: Remove Old Tools (Optional, Future)

**Goal**: Clean up deprecated tools after migration period

1. Remove old event listener tools
2. Remove old UI code
3. Update documentation
4. Migration guide for any remaining users

## Trade-offs and Alternatives

### Why Not Single Table?

**Considered**: One `automations` table with JSONB for trigger config

**Rejected because**:

- ❌ Loses type safety for trigger-specific fields
- ❌ Complex queries with JSONB parsing
- ❌ Requires migrating existing event_listeners
- ❌ Harder to maintain indexes
- ❌ Database schema doesn't encode domain logic

**Chosen approach** (separate tables, unified interface):

- ✅ Type-safe storage
- ✅ No migration needed
- ✅ Efficient queries
- ✅ Clear separation of concerns
- ✅ Easy to understand and maintain

### Why Not Keep Separate?

**Considered**: Keep event listeners and recurring actions as separate systems

**Rejected because**:

- ❌ More tools (10 vs 8)
- ❌ Separate mental models for same concept
- ❌ Duplicate UI patterns
- ❌ Confusing for users/LLM

**Chosen approach** (unified interface):

- ✅ Fewer, clearer tools
- ✅ Single mental model: "automations"
- ✅ Consistent UI experience
- ✅ Better discoverability

### Edge Cases and Limitations

1. **Name uniqueness**: Names are unique per conversation, across both types

   - User can't have event automation "Daily Summary" and schedule automation "Daily Summary"
   - Database unique constraints only enforce within each table
   - Resolution: Application layer checks both tables before creating, returns clear error message
   - Implementation: `AutomationsRepository.create_*` methods query both tables first

2. **Different execution tracking by design**: Event vs schedule automations have different tracking
   needs

   - Event listeners: `daily_executions` (resets at midnight) - for rate limiting against
     misconfigured match conditions
   - Schedule automations: `execution_count` (lifetime counter) - for statistics/history
   - Rationale: Event listeners need rate limiting because incorrect `match_conditions` can match
     too many events (e.g., matching every motion sensor event). Schedule automations can't have
     this problem - RRULE explicitly controls frequency.
   - Resolution: Unified API shows appropriate metric per type with clear labels
   - UI implication: Display "Executions today (rate limit: 5/day)" for events vs "Total executions"
     for schedules

3. **Timezone handling inconsistency**: Different DateTime column types

   - Event listeners: `DateTime` without timezone (existing)
   - Schedule automations: `DateTime(timezone=True)` (new)
   - Resolution: Repository layer normalizes to UTC when reading/sorting
   - Future: Consider migrating event_listeners to timezone-aware (requires migration)
   - Workaround: Treat naive timestamps as UTC in abstraction layer

4. **ID collision**: UUIDs prevent collision between types

   - Each table generates its own UUIDs
   - Unified API looks up in both tables

5. **Migration of existing recurring tasks**: Tasks created before this feature won't have
   automation entities

   - Resolution: Continue working as before (just instances)
   - Optional: Migration script to create entities for active recurring tasks

6. **Rate limiting**: Event listeners have daily rate limits, schedule automations don't

   - Resolution: Keep rate limiting specific to event listeners
   - Document the difference

## Success Criteria

### Must Have

✅ Users can create both event and schedule automations through unified tools ✅ Single "Automations"
page shows both types ✅ Enable/disable works identically for both types ✅ Execution statistics
tracked for both types ✅ Schedule automations persist after execution ✅ Existing event listeners
continue working unchanged ✅ All tests pass (unit, integration, E2E)

### Should Have

✅ LLM prefers unified tools over old tools ✅ Clear documentation for when to use each automation
type ✅ Migration path from old tools clearly documented ✅ UI provides good filtering and search

### Nice to Have

✅ Migration script for existing recurring tasks ✅ Execution history charts/visualization ✅ Bulk
operations (enable/disable multiple) ✅ Export/import automation definitions

## Implementation Checklist

### Phase 1: Database Layer ✅ COMPLETE (commit 9542ef03)

- [x] Create `schedule_automations_table` schema
- [x] Create Alembic migration
- [x] Implement `ScheduleAutomationsRepository`
- [x] Implement `AutomationsRepository` abstraction
- [x] Add to `DatabaseContext`
- [x] Unit tests for repositories

### Phase 2: Tool Layer ✅ COMPLETE (commits 7799d541, 213271f6, 7b80cdcf)

- [x] Create `src/family_assistant/tools/automations.py`
- [x] Implement 8 automation tools
- [x] Update `schedule_recurring_action` to create entities
- [x] Register tools in `__init__.py`
- [x] Update `config.yaml`
- [x] Integration tests for tools
- [x] Remove old event listener tools

### Phase 3: Task Worker ✅ COMPLETE (commit 4664d105)

- [x] Update script execution handler
- [x] Update LLM callback handler
- [x] Call `after_task_execution` for schedule automations
- [x] Tests for automation lifecycle

### Phase 4: Web API ✅ COMPLETE (commit 1c6da302)

- [x] Create `automations_api.py` router
- [x] Implement REST endpoints (GET, POST, PATCH, DELETE)
- [x] Add Pydantic models
- [x] Register router in app
- [x] Preserve old `listeners_api.py` for backward compatibility during migration
- [x] Fix Union request body validation issue
- [x] Implement sentinel pattern for nullable fields
- [x] Add conversation_id security verification
- [x] API tests (covered by existing test suite)

### Phase 5: Frontend 🔄 IN PROGRESS

- [ ] Create `pages/Automations/` directory
- [ ] Implement `AutomationsList` component
- [ ] Implement `AutomationDetail` component
- [ ] Implement `CreateEventAutomation` component
- [ ] Implement `CreateScheduleAutomation` component
- [ ] Update navigation
- [ ] Component tests
- [ ] E2E tests
- [ ] Delete old `pages/EventListeners/` directory (after UI migration complete)
- [ ] Remove old `listeners_api.py` (after UI migration complete)

### Phase 6: Documentation ⏳ PENDING

- [ ] Update `docs/user/USER_GUIDE.md` with automations guide
- [ ] Update system prompt in `prompts.yaml` to explain automations
- [ ] Add tool usage examples
- [ ] Create migration guide for users
- [ ] Update architecture diagram

### Phase 7: Final Testing ⏳ PENDING

- [x] Unit tests for all new repositories
- [x] Integration tests for tool workflows
- [x] Test both PostgreSQL and SQLite
- [ ] Frontend component tests (after Phase 5)
- [ ] E2E tests for full automation lifecycle (after Phase 5)
- [ ] Performance testing for pagination at scale
- [ ] Final verification of all workflows

## Future Enhancements

### Automation Templates

Pre-built automation templates users can quickly instantiate:

- "Daily summary at 8am"
- "Notify when door opens"
- "Weekly calendar review"

### Conditional Actions

Support for multiple actions with conditions:

```json
{
  "conditions": [
    {"if": "temperature > 75", "then": {"action": "notify"}},
    {"if": "temperature > 85", "then": {"action": "turn_on_fan"}}
  ]
}
```

### Automation Groups

Group related automations for bulk operations:

- "Morning routine" group
- "Security alerts" group
- Enable/disable groups together

### Automation History Dashboard

Dedicated page showing:

- Execution timeline across all automations
- Success/failure rates
- Performance metrics
- Trend analysis

### Import/Export

Export automations as JSON/YAML for:

- Backup and restore
- Sharing between conversations
- Version control

## Related Work

- **PR #312**: Instance-level management for scheduled tasks (foundation for this work)
- **Event Listeners**: Existing event-based automation system
- **Task Queue**: Underlying execution infrastructure
- **Actions System**: Unified action execution (wake_llm, script)

## References

- Event listeners: `src/family_assistant/tools/event_listeners.py`
- Task management: `src/family_assistant/tools/tasks.py`
- Task worker: `src/family_assistant/task_worker.py`
- Actions: `src/family_assistant/actions.py`
- RRULE spec: https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html
