# Calendar Testing Guide

End-to-end tests for calendar events, reminders, and CalDAV sync, in `tests/functional/calendar/`.

## Test Files

- `test_event_management.py` — creating, modifying, and deleting events via tools/chat.
- `test_reminders.py` — reminder scheduling, dismissal, and delivery through the task worker.
- `test_integration.py` — CalDAV sync against the Radicale test server.
- `test_calendar_confirmation.py` — operations that require user confirmation.

Recurring schedule and timezone coverage is **not** here: it lives in
`tests/functional/automations/` and `tests/functional/storage/test_automations_crud.py`, because
recurring schedules are created via `create_automation`, not via calendar tools.

## Fixtures

**`radicale_server_session`** (session scope, `tests/conftest.py`) — starts one Radicale CalDAV
server for the session with user `testuser`/`testpass`; returns `(base_url, username, password)`.

**`radicale_server`** (function scope) — depends on `radicale_server_session` *and*
`pg_vector_db_engine`, creates a unique calendar per test, and cleans it up afterwards. Returns
`(base_url, username, password, calendar_url)`.

Tests use `db_engine` or `pg_vector_db_engine` and open their own context via
`DatabaseContext(engine=...)` / `get_db_context(engine=...)`; there is no `db_context` or
`assistant` fixture. `mock_clock` (`tests/conftest.py`) is used heavily for reminder timing, and
`task_worker_manager` for reminder execution.

Tests talk to the CalDAV server with the `caldav` library directly (`caldav.DAVClient`) to assert
what actually landed on the server.

## Running

```bash
pytest tests/functional/calendar/ -xq
pytest tests/functional/calendar/ --postgres -xq
```

## Related Coverage

- `tests/functional/web/ui/test_events_list.py` and `tests/functional/web/ui/test_events_detail.py`
  — calendar UI, see [tests/functional/web/CLAUDE.md](../web/CLAUDE.md).

## See Also

- **[tests/CLAUDE.md](../../CLAUDE.md)** — general testing patterns and fixtures
- **[tests/integration/CLAUDE.md](../../integration/CLAUDE.md)** — integration testing with VCR.py
- **[src/family_assistant/tools/CLAUDE.md](../../../src/family_assistant/tools/CLAUDE.md)** —
  calendar tool development
