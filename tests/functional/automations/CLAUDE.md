# Automations Testing Guide

This file provides guidance for working with automation and event system tests in this project.

## Overview

Automation tests verify the complete automation feature workflow, including:

- Creating, updating, and managing automations
- Event system and event triggering
- Automation execution and task delegation
- Scheduled script execution
- Complex event workflows and condition matching

Tests are located in `tests/functional/automations/` and cover end-to-end scenarios using the real
database and event system.

## Test Files

### Automations CRUD Operations

**`test_automations_crud.py`** - Create, Read, Update, Delete automations

- Creating automations via chat and API
- Retrieving automation details
- Updating automation conditions and actions
- Deleting automations
- Listing automations with filtering
- Enabling/disabling automations

### Automations Execution

**`test_automations_execution.py`** - Running automations when conditions are met

- Triggering automations based on events
- Executing automation actions
- Verifying action results
- Handling automation failures and retries
- Logging automation execution

### Event System - Basic

**`test_event_system_basic.py`** - Core event system functionality

- Creating and publishing events
- Registering event listeners/handlers
- Event matching and filtering
- Event payload and metadata
- Simple event workflows

### Event System - Advanced

**`test_event_system_advanced.py`** - Complex event scenarios

- Event chaining and cascading
- Conditional event handling
- Event aggregation and batching
- Event prioritization and ordering
- Race conditions and event ordering guarantees

### Task Delegation

**`test_delegation_basic.py`** - Basic task delegation workflows

- Delegating tasks to other agents or systems
- Task status tracking
- Task results and feedback
- Simple delegation scenarios

**`test_delegation_workflows.py`** - Complex delegation workflows

- Multi-step task chains
- Conditional task routing
- Rollback and error handling
- Complex workflow orchestration

### Scheduled Script Execution

**`test_scheduled_script_execution.py`** - Scheduled automation execution

- Creating scheduled automations
- Cron expressions and timing
- Timezone handling in schedules
- One-time vs recurring execution
- Execution logs and debugging

## Key Fixtures

### Task Worker Fixtures

**`task_worker_manager`** (function scope)

- Manages lifecycle of a TaskWorker instance
- Returns tuple: `(TaskWorker, new_task_event, shutdown_event)`
- Worker has mock ChatInterface and embedding generator
- Tests must register their own task handlers

```python
async def test_automation_execution(task_worker_manager):
    worker, new_task_event, shutdown_event = task_worker_manager

    # Register handler for automation tasks
    async def handle_automation(task):
        # Execute automation action
        return result

    worker.register_handler("execute_automation", handle_automation)

    # Submit task and wait for execution
    await new_task_event.wait()
```

### Assistant Fixtures

**`assistant`** - Configured Assistant instance with all dependencies

### Database Fixtures

**`db_engine`** (function scope)

- Provides SQLite or PostgreSQL database
- Initializes schema with automations table

**`db_context`** - DatabaseContext for querying automations and events

## Testing Patterns

### Pattern 1: Creating Automations via Chat

```python
async def test_create_automation_via_chat(assistant, db_context):
    # Create automation via natural language
    prompt = "Create an automation: when I get an email with subject 'urgent', send me a notification"
    response = await assistant.process_message(prompt)

    # Verify automation was created
    async with db_context() as db:
        automations = await db.automations.get_all()
        assert len(automations) > 0
        auto = automations[0]
        assert "email" in auto.trigger.lower()
        assert "notification" in auto.action.lower()
```

### Pattern 2: Testing Event Triggering

```python
async def test_automation_triggered_by_event(task_worker_manager, db_context):
    worker, new_task_event, shutdown_event = task_worker_manager

    # Create automation with event trigger
    async with db_context() as db:
        automation = await db.automations.create(
            name="Email Notifier",
            trigger_type="event",
            trigger_event="email_received",
            action="send_notification"
        )

    # Register handler
    async def handle_notification(task):
        # Send notification
        return {"notified": True}

    worker.register_handler("send_notification", handle_notification)

    # Publish event that triggers automation
    await publish_event(
        event_type="email_received",
        payload={"subject": "Important", "from": "boss@company.com"}
    )

    # Wait for automation to execute
    await new_task_event.wait()

    # Verify action was taken
    # Check notification was sent
```

### Pattern 3: Testing Event Filtering

```python
async def test_event_filtering(db_context):
    # Create automation with conditional trigger
    async with db_context() as db:
        automation = await db.automations.create(
            name="Important Emails Only",
            trigger_type="event",
            trigger_event="email_received",
            trigger_condition='event.subject.contains("urgent")',
            action="create_task"
        )

    # Publish matching event
    event1 = {"subject": "URGENT: Server Down", "from": "ops@company.com"}
    results1 = await evaluate_trigger_condition(automation, event1)
    assert results1.matches == True

    # Publish non-matching event
    event2 = {"subject": "Weekly Status Report", "from": "team@company.com"}
    results2 = await evaluate_trigger_condition(automation, event2)
    assert results2.matches == False
```

### Pattern 4: Testing Task Delegation

```python
async def test_task_delegation_workflow(task_worker_manager, db_context):
    worker, new_task_event, shutdown_event = task_worker_manager

    # Create delegation automation
    async with db_context() as db:
        automation = await db.automations.create(
            name="Delegate to Team",
            trigger_type="event",
            trigger_event="large_task_created",
            action_type="delegate_to_team",
            action_params={"team": "engineering"}
        )

    # Register delegation handler
    async def delegate_task(task):
        # Delegate to team
        return {"delegated_to": "engineering"}

    worker.register_handler("delegate_to_team", delegate_task)

    # Create large task
    await publish_event(
        event_type="large_task_created",
        payload={"task": "Build new feature"}
    )

    # Wait for delegation
    await new_task_event.wait()

    # Verify task was delegated
```

### Pattern 5: Testing Scheduled Execution

```python
async def test_scheduled_automation(db_context):
    # Create scheduled automation
    async with db_context() as db:
        automation = await db.automations.create(
            name="Daily Report",
            trigger_type="schedule",
            trigger_schedule="0 9 * * MON-FRI",  # 9am weekdays
            action="send_daily_report"
        )

    # Verify schedule is parsed correctly
    assert automation.trigger_schedule == "0 9 * * MON-FRI"

    # Calculate next run time
    next_run = calculate_next_run(automation.trigger_schedule)
    assert next_run is not None
    assert next_run.weekday() in [0, 1, 2, 3, 4]  # Mon-Fri
```

## Common Issues and Debugging

### Issue: Automation Not Triggering

**Error**: Event is published but automation doesn't execute

**Debug Steps**:

1. Verify automation is enabled:

```python
async with db_context() as db:
    auto = await db.automations.get(auto_id)
    assert auto.enabled == True
```

2. Check event matching:

```python
# Verify event type matches trigger
event_type = "email_received"
trigger_event = automation.trigger_event
assert event_type == trigger_event
```

3. Check trigger condition:

```python
# Test condition evaluation
condition = automation.trigger_condition
event = {"subject": "Test", "from": "test@example.com"}
matches = evaluate_condition(condition, event)
print(f"Condition matches: {matches}")
```

4. Verify handler is registered:

```python
worker, _, _ = task_worker_manager
assert "your_action" in worker.handlers
```

5. Enable debug logging:

```bash
pytest tests/functional/automations/test_automations_execution.py -xvs --log-cli-level=DEBUG
```

### Issue: Race Conditions in Event Ordering

**Error**: Events fire out of order or automation executes before event is published

**Debug Steps**:

1. Use event timestamps for ordering:

```python
# Ensure events have strict ordering
event1 = await publish_event("event1", timestamp=time1)
event2 = await publish_event("event2", timestamp=time2)
assert event1.timestamp < event2.timestamp
```

2. Verify event listener is registered before publishing:

```python
# Register listener first
listener = await register_event_listener(event_type, handler)
# Then publish event
event = await publish_event(event_type, payload)
```

3. Use explicit wait events:

```python
# Don't rely on timing, use synchronization events
await new_event.wait()  # Wait for event to be processed
```

### Issue: Scheduled Automation Doesn't Execute

**Error**: Scheduled automation never runs or runs at wrong time

**Debug Steps**:

1. Verify cron schedule is valid:

```python
from croniter import croniter

schedule = "0 9 * * MON-FRI"
assert croniter.is_valid(schedule)
```

2. Check next run calculation:

```python
from croniter import croniter
from datetime import datetime

cron = croniter("0 9 * * MON-FRI", datetime.now())
next_run = cron.get_next(datetime)
print(f"Next run: {next_run}")
```

3. Verify scheduler is running:

```bash
# Check scheduler logs
pytest tests/functional/automations/test_scheduled_script_execution.py -xvs --log-cli-level=DEBUG
```

## Running Automation Tests

```bash
# Run all automation tests
pytest tests/functional/automations/ -xq

# Run specific test file
pytest tests/functional/automations/test_automations_crud.py -xq

# Run CRUD tests only
pytest tests/functional/automations/test_automations_crud*.py -xq

# Run execution tests only
pytest tests/functional/automations/test_*execution*.py -xq

# Run event system tests
pytest tests/functional/automations/test_event_system*.py -xq

# Run with verbose output for debugging
pytest tests/functional/automations/test_automations_execution.py -xvs

# Run with PostgreSQL backend
pytest tests/functional/automations/ --postgres -xq

# Run single test
pytest tests/functional/automations/test_automations_crud.py::test_create_automation -xvs
```

## Integration with Other Features

Automations are used throughout the system:

- **Chat API**: `tests/functional/web/api/test_automations_*_api.py` - REST API for automations
- **Web UI**: `tests/functional/web/ui/test_automations_ui.py` - Automation UI management
- **Calendar**: Automations can trigger on calendar events
- **Email Processing**: Automations can trigger on incoming emails
- **Tasks**: Automations can create and delegate tasks

## Advanced Topics

### Custom Event Types

```python
# Create custom event type
event_type = "custom_business_event"
event = {
    "type": event_type,
    "data": {
        "key1": "value1",
        "key2": "value2"
    },
    "timestamp": datetime.now()
}
await publish_event(event_type, event)
```

### Automation Templating

```python
# Create automation with template variables
automation = await db.automations.create(
    name="Dynamic Notification",
    trigger_event="event_occurred",
    action="send_notification",
    action_template="Received: {{event.data.message}}"
)

# Publish event that fills template
event = {"data": {"message": "System Alert"}}
# Template becomes: "Received: System Alert"
```

### Conditional Action Chains

```python
# Create automation with multiple conditional actions
automation = await db.automations.create(
    name="Complex Workflow",
    trigger_event="large_task_created",
    actions=[
        {"type": "log", "message": "Task received"},
        {"type": "delegate_if", "condition": "task.priority == 'high'", "target": "cto"},
        {"type": "notify", "channels": ["email", "slack"]}
    ]
)
```

## See Also

- **[tests/CLAUDE.md](../CLAUDE.md)** - General testing patterns and three-tier test organization
- **[tests/functional/web/ui/test_automations_ui.py](../web/ui/test_automations_ui.py)** -
  Automation UI tests
- **[tests/functional/web/api/test_automations\_\*\_api.py](../web/api/)** - Automation API tests
- **[src/family_assistant/tools/CLAUDE.md](../../../src/family_assistant/tools/CLAUDE.md)** -
  Automation tool development
