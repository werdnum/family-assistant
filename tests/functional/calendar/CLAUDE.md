# Calendar Testing Guide

This file provides guidance for working with calendar tests in this project.

## Overview

Calendar tests verify the complete calendar feature workflow, including:

- Creating, updating, and deleting events
- Calendar synchronization with CalDAV servers
- Event reminders and notifications
- Recurring event handling and timezone support
- User confirmation for calendar operations

Tests are located in `tests/functional/calendar/` and cover end-to-end scenarios using real database
and CalDAV services.

## Test Files

**`test_event_management.py`** - Core event operations

- Creating events via chat and API
- Modifying event properties (title, time, description)
- Deleting events
- Handling multiple calendar sources

**`test_reminders.py`** - Event reminders and notifications

- Setting reminder times
- Dismissing reminders
- Integrating with task worker for reminder execution
- Testing notification delivery

**`test_integration.py`** - CalDAV server integration

- Synchronizing events with CalDAV servers
- Verifying events appear on external calendars
- Handling authentication and connection management

**`test_calendar_confirmation.py`** - User confirmation workflows

- Requiring confirmation for certain calendar operations
- Canceling operations via confirmation UI
- Testing confirmation UI interactions

**`test_recurring_task_timezone.py`** - Recurring events and timezones

- Creating recurring events with proper recurrence rules
- Handling timezone conversions for international users
- Verifying recurring event instances

## Key Fixtures

### CalDAV Server Fixtures

**`radicale_server_session`** (session scope)

- Starts a Radicale CalDAV server for the entire test session
- Creates test user with credentials: `testuser`/`testpass`
- Returns tuple: `(base_url, username, password)`
- Reused across multiple tests for efficiency

**`radicale_server`** (function scope)

- Creates a unique calendar for each test function
- Depends on `radicale_server_session` and `pg_vector_db_engine`
- Returns tuple: `(base_url, username, password, unique_calendar_url)`
- Automatically cleans up the calendar after test

Example usage:

```python
async def test_create_event_on_caldav(radicale_server):
    base_url, username, password, calendar_url = radicale_server

    # Test creates an event that should appear on CalDAV
    # Verify event is accessible via calendar_url
```

### Calendar-Specific Fixtures

**`assistant`** - Configured Assistant instance with all dependencies **`db_context`** - Database
context for querying events and calendars **`task_worker_manager`** - Task worker for reminder
execution

## Testing Patterns

### Pattern 1: Creating and Verifying Events

```python
async def test_create_event(db_context, assistant):
    # Create event via assistant
    response = await assistant.process_message("Create event tomorrow at 2pm")
    assert "event" in response.lower()

    # Query database to verify event was created
    async with db_context() as db:
        events = await db.calendar_events.get_upcoming_events()
        assert len(events) > 0
        assert events[0].title == expected_title
```

### Pattern 2: Testing CalDAV Synchronization

```python
async def test_caldav_sync(radicale_server, db_context):
    base_url, username, password, calendar_url = radicale_server

    # Create local event
    async with db_context() as db:
        event = await db.calendar_events.create_event(
            title="Test Event",
            start_time=tomorrow_at_2pm,
            calendar_id=calendar_id
        )

    # Verify event appears on CalDAV server
    response = await fetch_calendar(calendar_url, username, password)
    assert "Test Event" in response
```

### Pattern 3: Testing Reminders

```python
async def test_event_reminder(task_worker_manager, db_context):
    worker, new_task_event, shutdown_event = task_worker_manager

    # Create event with reminder
    async with db_context() as db:
        event = await db.calendar_events.create_event(
            title="Important Meeting",
            start_time=tomorrow_at_2pm,
            reminder_minutes=15
        )

    # Register reminder handler
    async def handle_reminder(task):
        # Send notification
        pass

    worker.register_handler("reminder", handle_reminder)

    # Verify reminder is triggered at correct time
    await new_task_event.wait()
```

### Pattern 4: Testing Timezone Handling

```python
async def test_recurring_event_timezone(db_context):
    # Create recurring event in specific timezone
    async with db_context() as db:
        event = await db.calendar_events.create_recurring_event(
            title="Daily Standup",
            start_time="14:00",  # 2pm in Pacific Time
            timezone="America/Los_Angeles",
            recurrence_rule="FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"
        )

    # Verify instances are generated correctly
    # Verify timezone conversions are correct
```

## Common Issues and Debugging

### Issue: CalDAV Connection Failed

**Error**: `Connection refused` when connecting to Radicale server

**Debug Steps**:

1. Verify Radicale fixture started successfully:

```python
# In test, check that radicale_server fixture was provided
async def test_something(radicale_server):
    base_url, username, password, calendar_url = radicale_server
    assert base_url  # Should not be None
```

2. Check Radicale logs for errors:

```bash
# Radicale typically runs on http://localhost:5232
curl -u testuser:testpass http://localhost:5232/.well-known/caldav-home
```

3. Verify authentication credentials match fixture:

```python
username = "testuser"
password = "testpass"
```

### Issue: Timezone Conversion Errors

**Error**: Events appear at wrong time in different timezone

**Debug Steps**:

1. Verify timezone is set correctly:

```python
event = await db.calendar_events.get_event(event_id)
assert event.timezone == "America/Los_Angeles"
```

2. Check datetime objects have timezone info:

```python
assert event.start_time.tzinfo is not None
```

3. Test specific timezone conversions:

```python
from datetime import datetime
import pytz

pst = pytz.timezone("America/Los_Angeles")
utc = pytz.UTC
local_time = datetime(2025, 10, 15, 14, 0, tzinfo=pst)
utc_time = local_time.astimezone(utc)
```

### Issue: Reminders Not Triggering

**Error**: Reminder tasks are not being executed

**Debug Steps**:

1. Verify reminder was created in database:

```python
async with db_context() as db:
    reminders = await db.reminders.get_pending_reminders()
    print(f"Pending reminders: {len(reminders)}")
```

2. Check task worker is registered for reminder tasks:

```python
worker, new_task_event, shutdown_event = task_worker_manager
# Ensure handler is registered before test runs
assert "reminder" in worker.handlers
```

3. Enable debug logging:

```bash
pytest tests/functional/calendar/test_reminders.py -xvs --log-cli-level=DEBUG
```

## Running Calendar Tests

```bash
# Run all calendar tests
pytest tests/functional/calendar/ -xq

# Run specific test file
pytest tests/functional/calendar/test_event_management.py -xq

# Run with verbose output for debugging
pytest tests/functional/calendar/test_reminders.py -xvs

# Run with PostgreSQL backend
pytest tests/functional/calendar/ --postgres -xq

# Run single test
pytest tests/functional/calendar/test_event_management.py::test_create_event -xvs
```

## Integration with Web UI

Calendar functionality is also tested at the UI level in:

- `tests/functional/web/ui/test_events_list.py` - Event display
- `tests/functional/web/ui/test_events_detail.py` - Event detail view
- `tests/functional/web/api/test_automations_crud_api.py` - Calendar API endpoints

For end-to-end testing of calendar features through the web UI, see
[tests/functional/web/CLAUDE.md](../web/CLAUDE.md).

## See Also

- **[tests/CLAUDE.md](../CLAUDE.md)** - General testing patterns and three-tier test organization
- **[tests/unit/calendar/CLAUDE.md](../../unit/calendar/CLAUDE.md)** - Unit tests for calendar logic
  (if available)
- **[tests/integration/CLAUDE.md](../integration/CLAUDE.md)** - Integration testing with VCR.py
- **[src/family_assistant/tools/CLAUDE.md](../../../src/family_assistant/tools/CLAUDE.md)** -
  Calendar tool development
