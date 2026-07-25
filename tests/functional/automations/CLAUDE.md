# Automations Testing Guide

End-to-end tests for automations, the event system, and delegation, in
`tests/functional/automations/`.

## Fixtures

These tests take `db_engine` (from `tests/conftest.py`) and open their own `DatabaseContext` /
`get_db_context(engine=...)`; there is no shared `db_context` or `assistant` fixture.

`task_worker_manager` (function scope, `tests/conftest.py`) yields a **factory**, not a worker. Call
it with a `ProcessingService` and a chat interface to get
`(TaskWorker, new_task_event, shutdown_event)`; it registers the delegation and schedule-automation
handlers by default (opt out with `register_delegation_handler=False`), so a delegated run is not
silently stranded as "queued". Register any additional handlers with
`worker.register_task_handler(...)`.

Delegation tests build their own primary/specialized/delegated `ProcessingService` fixtures locally,
and the supporting config and LLM-mock fixtures are per-file rather than a symmetric naming scheme —
some are plain fixtures (`primary_service_config`, `delegated_service_config`,
`specialized_llm_mock`) and some are factories (`specialized_service_config_factory`,
`primary_llm_mock_factory`), and not every combination exists. Read the conftest-level fixtures in
the test file you are editing rather than assuming a shared or matching one exists.

## Running

```bash
pytest tests/functional/automations/ -xq
pytest tests/functional/automations/ --postgres -xq
```

## Related Coverage

- `tests/functional/web/api/test_automations_crud_api.py` and
  `tests/functional/web/api/test_automations_execution_api.py` — REST API.
- `tests/functional/web/ui/test_automations_ui.py` — automation management UI.
- `tests/functional/storage/test_automations_crud.py` — storage-layer CRUD, including recurring
  schedules.

## See Also

- **[tests/CLAUDE.md](../../CLAUDE.md)** — general testing patterns and fixtures
- **[src/family_assistant/tools/CLAUDE.md](../../../src/family_assistant/tools/CLAUDE.md)** —
  automation tool development
