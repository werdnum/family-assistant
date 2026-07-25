# Testing Guide

This file provides guidance for working with tests in this project.

## Test Structure

`unit/` mirrors the `src/` layout; `integration/` is organized by external service; `functional/` is
organized by feature area, not by implementation.

## Testing Principles

- **Prefer real/fake dependencies over mocks.** Use real dependencies like the test database
  (`db_engine`) or a real protocol client wherever possible — this proves compatibility with actual
  clients of the protocols we implement. Use high-fidelity fakes (in-memory implementations built on
  official SDKs) for services that are complex or slow. Mocks are a last resort, reserved for
  external third-party services that cannot be controlled or faked. Heavily-mocked unit tests tend
  to repeat the implementation's own mistakes back at you.

- **Each test tests one independent behaviour** of the system under test. Arrange, Act, Assert.
  Never Arrange, Act, Assert, Act, Assert.

- **Run tests with `-xq`** so there is less output to process. Only reach for `-s` or `-v` once
  you've tried `-q` and know you need the extra output.

- **No fixed waits.** Wait on the condition you care about — `waitFor()` / `findBy*` in frontend
  tests, condition polling with a deadline in pytest. This is enforced by the ast-grep rules
  `no-asyncio-sleep-in-tests`, `no-time-sleep-in-tests`, and `no-playwright-wait-for-timeout`; use
  `# ast-grep-ignore: <rule>` with a reason for the rare genuine exception.

## Finding Flaky Tests

Use pytest-flakefinder rather than a shell loop:

```bash
pytest tests/path/to/test.py --flake-finder --flake-runs=100 -x
```

## Database Backend Selection

`db_engine` is parameterized by the `--db` flag (`sqlite`, `postgres`, or `all` — the default). The
older `--postgres` boolean flag is equivalent to `--db postgres`. `poe test-postgres`,
`poe test-postgres-verbose`, and `poe test-sqlite` wrap the common combinations.

Each PostgreSQL test gets its own uniquely-named database, created before the test and dropped
after, so there is no cross-test state. The container starts automatically when postgres is selected
(requires Docker/Podman, or `pgserver`). Tests needing pgvector should request
`pg_vector_db_engine`.

Running against PostgreSQL has caught real issues SQLite hides — event loop conflicts in error
logging, different transaction handling, and schema differences — so exercise it before pushing
changes that touch database operations.

## Core Test Fixtures

Defined in `tests/conftest.py` unless noted otherwise.

### Database Fixtures

**`db_engine`** (function scope) — the primary fixture for database tests. Parameterized to SQLite
or PostgreSQL by `pytest_generate_tests` from the `--db`/`--postgres` flags. SQLite uses a temporary
on-disk file (`fa_test_*.sqlite`) deleted after the test; PostgreSQL creates and drops a unique
database per test. Handles schema initialization. Not `autouse` — tests must request it.

**`postgres_container`** (session scope) — manages the PostgreSQL server for the whole session, via
`pgserver` (embedded) or an external instance from `TEST_DATABASE_URL`. Includes `pgvector`.

**`pg_vector_db_engine`** (function scope) — PostgreSQL with `pgvector` guaranteed, unique database
per test. Requesting it forces the test to the postgres backend only.

**`session_db_engine`** (session scope, `tests/functional/web/conftest.py`) — shared file-based
SQLite engine for performance-critical **read-only** tests; state persists across tests in the
session. One database file per `pytest-xdist` worker.

**`api_db_context`** (function scope, `tests/functional/web/conftest.py`) — an already-entered
`DatabaseContext` over `db_engine`, so API tests can call repository methods directly.

### Task Worker Fixtures

**`task_worker_manager`** (function scope) — yields a **factory**, not a worker. Call it with a
`ProcessingService` and a chat interface to construct and start a `TaskWorker`; the fixture shuts it
down at teardown. The factory returns `(TaskWorker, new_task_event, shutdown_event)` and registers
the delegation and schedule-automation handlers by default (opt out with
`register_delegation_handler=False`). Tests register any additional handlers themselves.

```python
async def test_task(task_worker_manager):
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=service, chat_interface=MagicMock()
    )
    worker.register_task_handler("my_task", my_handler)
```

**`mock_clock`** (function scope) — a `MockClock` for controlling time; `task_worker_manager` wires
it into the worker.

### CalDAV Server Fixtures

**`radicale_server_session`** (session scope) — starts a Radicale CalDAV server for the session with
user `testuser`/`testpass`. Yields `(base_url, username, password)`.

**`radicale_server`** (function scope) — depends on `radicale_server_session` and `db_engine`;
creates a uniquely-named calendar per test and cleans it up afterwards. Yields
`(base_url, username, password, unique_calendar_url)`.

### Indexing Pipeline Fixtures

Defined locally in `tests/functional/indexing/test_indexing_pipeline.py` (see
[tests/functional/indexing/CLAUDE.md](functional/indexing/CLAUDE.md)):
`mock_pipeline_embedding_generator` (a `HashingWordEmbeddingGenerator` for deterministic embeddings)
and `indexing_task_worker` (a `TaskWorker` configured for indexing, returning
`(TaskWorker, new_task_event, shutdown_event)`).

### Mock Utilities

`tests/mocks/mock_llm.py` provides **`RuleBasedMockLLMClient`**: rules are
`(matcher_function, LLMOutput | callable)` tuples evaluated in order, where the matcher receives a
dict of the call's keyword arguments. It also accepts `structured_rules` for `generate_structured`
and a `response_gate` `asyncio.Event` the test can use to hold a reply in flight.

```python
mock_llm = RuleBasedMockLLMClient(
    rules=[
        (
            lambda args: "weather" in args["messages"][0]["content"],
            LLMOutput(content="It's sunny today!"),
        ),
    ],
    default_response=LLMOutput(content="Default response"),
)
```

## Manual API Testing

`scripts/wait-for-server.sh [timeout]` polls `http://devcontainer-backend-1:8000/api/health` every 2
seconds (default timeout 120s; exit 0 ready, 1 timeout) — use it before driving the API in CI or
containers.

Chat endpoints (base `http://devcontainer-backend-1:8000`):

- `POST /api/v1/chat/send_message` — non-streaming, body `{"prompt": ...}`.
- `POST /api/v1/chat/turns` — kicks off a streaming turn; the client supplies a UUID `turn_id`, so
  retries are idempotent.
- `GET /api/v1/chat/conversations/{id}/stream?from_seq=0` — replays from `from_seq` then tails live;
  `follow=false` closes once the turn ends.

Tools endpoints: `POST /api/tools/execute/{tool_name}` with body `{"arguments": {...}}` runs a tool
directly, bypassing the LLM; `GET /api/tools/definitions` lists them. Endpoint discovery:
`curl -s .../openapi.json | jq '.paths | keys'`.

Set `DEBUG_LLM_MESSAGES=true` to log the exact messages sent to the LLM (system prompts, user
messages, tool calls and responses).

**Diagnostics export:** `GET /api/diagnostics/export` returns a combined export of system info,
error logs, LLM requests (messages, tools, responses, timing), and message history — designed to be
piped through `jq` or pasted to an LLM. Requires authentication (or `DIAGNOSTICS_READONLY_TOKEN`).
Query parameters:

- `minutes`: time window (1-120, default 30)
- `max_errors`: (1-100, default 50)
- `max_llm_requests`: (1-100, default 20)
- `max_messages`: (1-500, default 100)
- `conversation_id`: filter to one conversation
- `format`: `json` (default) or `markdown`

## Per-Area Test Guides

- [tests/integration/CLAUDE.md](integration/CLAUDE.md) — VCR.py cassettes, record modes, and the
  Home Assistant fixture.
- [tests/functional/web/CLAUDE.md](functional/web/CLAUDE.md) — Playwright web UI tests (marked
  `@pytest.mark.playwright`), page objects, and trace debugging.
- [tests/functional/telegram/CLAUDE.md](functional/telegram/CLAUDE.md) — Telegram bot tests.
- [tests/functional/automations/CLAUDE.md](functional/automations/CLAUDE.md) — automations, the
  event system, and delegation.
- [tests/functional/calendar/CLAUDE.md](functional/calendar/CLAUDE.md) — calendar operations and
  reminders.
- [tests/functional/indexing/CLAUDE.md](functional/indexing/CLAUDE.md) — document and email indexing
  pipeline.

## CI Debugging

Do not set `CI=true` locally — CI uses it as a shortcut to skip rebuilding the frontend, and the
web-test fixtures will then expect pre-built assets that aren't there.

Locally, `poe test` writes a pass/fail summary to `.poe-test-summary.txt`, which is useful when the
full output is truncated.

In CI, start from the JSON test report in the run's artifacts
(`gh run download <run-id> --name <artifact-name>`). Artifact names are suffixed with the run id:

- `backend-test-artifacts-sqlite-<run-id>` → `test-results/backend-sqlite-report.json`
- `backend-test-artifacts-postgres-<run-id>` → `test-results/backend-postgres-report.json`
- `playwright-artifacts-sqlite-<run-id>` → `test-results/playwright-sqlite-report.json`
- `playwright-artifacts-postgres-<run-id>` → `test-results/playwright-postgres-report.json`
- `frontend-test-results-<run-id>` → `test-results/frontend-report.json`
- `gemini-live-test-results-<run-id>`, `llm-integration-test-results-<run-id>`

The Playwright artifacts also contain, in subdirectories named after the failing test:
`test-results/**/test-failed-*.png` (screenshots at failure), `test-results/**/test-*.webm` (videos
of the run), and `test-results/**/trace.zip` (open in the Playwright Trace Viewer).

For frontend build failures, check that `router.html` and `.vite/manifest.json` made it into the
artifacts — a missing manifest means the asset copy step failed rather than the test.
