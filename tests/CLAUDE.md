# Testing Guide

This file provides guidance for working with tests in this project.

## Test Structure Overview

The project uses a three-tier testing structure organized by test type and feature area:

### Three-Tier Test Organization

```
tests/
├── unit/                  # Unit tests - individual function/class behavior
│   ├── attachments/
│   ├── calendar/
│   ├── events/
│   ├── indexing/          # Document/email indexing logic
│   ├── llm/
│   ├── processing/
│   ├── services/
│   ├── storage/           # Database/storage layer
│   ├── tools/
│   └── web/
├── integration/           # Integration tests - component interactions & external services
│   ├── home_assistant/    # Home Assistant API integration
│   ├── llm/               # LLM provider integrations (OpenAI, Gemini, etc)
│   └── fixtures/          # Shared fixture configurations
├── functional/            # Functional tests - end-to-end feature flows
│   ├── attachments/
│   ├── automations/       # Automation execution and event system
│   ├── calendar/          # Calendar operations and reminders
│   ├── events/
│   ├── home_assistant/
│   ├── indexing/          # Email/document indexing pipeline
│   │   └── processors/    # Content processor tests
│   ├── notes/
│   ├── scripting/
│   ├── storage/
│   ├── tasks/
│   ├── telegram/          # Telegram bot functionality
│   ├── tools/
│   ├── vector_search/
│   └── web/               # Web API and UI tests
│       ├── api/           # REST API endpoint tests
│       ├── ui/            # Playwright end-to-end UI tests
│       └── pages/         # Playwright Page Object Models
└── mocks/                 # Mock utilities and fixtures

```

### Understanding Each Tier

**Unit Tests** (`tests/unit/`)

- Test individual functions, classes, or methods in isolation
- Use mocks for external dependencies
- Run quickly and detect regressions early
- Located in directories matching `src/` structure
- Example: Testing CalendarValidator logic without database access

**Integration Tests** (`tests/integration/`)

- Test interactions with external services (LLM APIs, Home Assistant, etc.)
- Use VCR.py to record/replay HTTP interactions for reproducibility
- Verify correct API usage and response handling
- Located in `tests/integration/` subdirectories by service
- Example: Testing that Home Assistant API calls work correctly

**Functional Tests** (`tests/functional/`)

- Test complete feature workflows end-to-end
- Use real or fake dependencies to maximize coverage
- Verify features work as users experience them
- Organized by feature area, not implementation details
- Example: Creating a calendar event, confirming it, verifying it appears in UI

### Finding Tests for a Feature

To find tests for a specific feature:

1. **By Feature Name**: Look in `tests/functional/{feature}/` (e.g., calendar tests in
   `tests/functional/calendar/`)
2. **By Component**: Look in corresponding `tests/unit/{component}/` (e.g., storage tests in
   `tests/unit/storage/`)
3. **By API**: Look in `tests/functional/web/api/` for endpoint tests
4. **By UI Page**: Look in `tests/functional/web/ui/test_{feature}*.py` for Playwright tests

### Benefits of This Organization

- **Discoverability**: Find tests quickly by feature or component
- **Maintainability**: Group related tests together, making changes easier
- **Performance**: Run unit tests quickly for fast feedback, save slower tests for CI
- **Clarity**: Clear separation between test types helps understand test purpose

## Testing Principles

- **Testing Philosophy: Prefer Real/Fake Dependencies Over Mocks**

  - **"Bring the system into contact with reality."** The most important goal is to prove the system
    actually works. The most valuable tests are those that resemble how the application is used in
    production.
  - **Use real dependencies** like the test database (`db_engine` fixture) or a real protocol client
    whenever you can. This provides real assurance of compatibility with actual clients of the
    protocols we implement.
  - **Use fake dependencies** for services that are complex or slow. Prefer high-fidelity "fakes" or
    simple in-memory implementations using official SDKs that mimic real behavior.
  - **Avoid fragile "change-detector" unit tests.** Pure unit tests with extensive mocking often
    just repeat implementation mistakes in the test. If you can't spot an error in the
    implementation, you likely won't spot it in a mocked test either.
  - **Use mocks as a very last resort.** Mocks should be reserved only for external third-party
    services that are truly impossible to control or fake.
  - **Why?** We want to guard against hallucinating or misunderstanding interfaces. Integrating an
    external reference (SDK, real client, etc.) makes tests more robust and realistic.

- **Each test tests one independent behaviour** of the system under test. Arrange, Act, Assert.
  NEVER Arrange, Act, Assert, Act, Assert.

- **ALWAYS run tests with `-xq`** so there is less output to process. NEVER use `-s` or `-v` unless
  you have already tried with `-q` and you are sure there is information in the output of `-s` or
  `-v` that you need for debugging.

- **CRITICAL: Avoid Fixed Waits in Tests**: Tests must NEVER use arbitrary time-based waits like
  `setTimeout`, `sleep`, or fixed delays. These cause flaky tests that fail under load or in CI.

  **Always wait for the specific condition you care about:**

  - ✅ GOOD: `await waitFor(() => expect(element).toBeEnabled())`
  - ✅ GOOD: `await screen.findByText('Success message')`
  - ✅ GOOD: `await waitForMessageSent(input)` (waits for input.value === '')
  - ❌ BAD: `await new Promise(resolve => setTimeout(resolve, 2000))`
  - ❌ BAD: `await sleep(500)`

  **For frontend tests (Vitest/React Testing Library):**

  - Use `waitFor()` to wait for conditions
  - Use `findBy*` queries which wait automatically
  - Create reusable condition-based wait helpers
  - Only use fixed waits if modeling actual user behavior timing (e.g., 500ms between rapid actions)

  **For backend tests (pytest):**

  - Use `pytest-asyncio` with condition-based waits
  - Poll with timeouts: `while not condition and time.time() < deadline: await asyncio.sleep(0.1)`
  - Never use bare `time.sleep()` or `asyncio.sleep()` without a condition check

  **Why this matters:**

  - Fixed waits are always too short (fail under load) or too long (waste time)
  - Condition-based waits complete as soon as possible
  - Tests become reliable across different hardware and CI environments

## Finding Flaky Tests

When debugging test flakiness, use pytest's `--flake-finder` plugin:

```bash
pytest tests/path/to/test.py --flake-finder --flake-runs=100 -x
```

This is much more efficient than running tests in a loop. The `-x` flag stops on first failure.
Never use manual loops or shell scripts to repeatedly run tests.

## Running Tests

```bash
# Run all tests
poe test  # Note: You will need a long timeout - something like 15 minutes

# Run tests with PostgreSQL (production database)
poe test-postgres  # Quick mode with -xq
poe test-postgres-verbose  # Verbose mode with -xvs

# Run tests with PostgreSQL using pytest directly
pytest --postgres -xq  # All tests with PostgreSQL
pytest --postgres tests/functional/test_specific.py -xq  # Specific tests

# Run specific test files
pytest tests/functional/test_specific.py -xq
```

## Database Backend Selection

By default, tests run with an in-memory SQLite database for speed. However, production uses
PostgreSQL, so it's important to test with PostgreSQL to catch database-specific issues:

- Use `--postgres` flag to run tests with PostgreSQL instead of SQLite
- PostgreSQL container starts automatically when the flag is used (requires Docker/Podman)
- Tests that specifically need PostgreSQL features can use `pg_vector_db_engine` fixture, but will
  get a warning if run without `--postgres` flag
- The unified `db_engine` fixture automatically provides the appropriate database based on the flag

**PostgreSQL Test Isolation**: When using `--postgres`, each test gets its own unique database:

- A new database is created before each test (e.g., `test_my_function_12345678`)
- The database is completely dropped after the test completes
- This ensures complete isolation - tests cannot interfere with each other
- No data persists between tests, eliminating order-dependent failures

**Important**: Running tests with `--postgres` has already revealed PostgreSQL-specific issues like:

- Event loop conflicts in error logging when using PostgreSQL
- Different transaction handling between SQLite and PostgreSQL
- Schema differences that only manifest with PostgreSQL

It's recommended to run tests with `--postgres` before pushing changes that touch database
operations.

## Core Test Fixtures

The project provides a comprehensive set of pytest fixtures for testing different components. These
fixtures are defined in `tests/conftest.py` and `tests/functional/telegram/conftest.py`.

### Database Fixtures

**`db_engine`** (function scope)

- The primary fixture for database tests, providing either SQLite or PostgreSQL.
- Controlled by `--db` or `--postgres` flags (defaults to SQLite).
- **SQLite**: Creates a temporary on-disk database for each test.
- **PostgreSQL**: Creates a unique database for each test using `postgres_container`.
- Handles schema initialization and automatic cleanup after each test.
- **Note**: This is not an `autouse` fixture; it must be requested by the test.

**`postgres_container`** (session scope)

- Manages a PostgreSQL server instance for the entire test session.
- Uses `pgserver` (embedded PostgreSQL) or an external instance via `TEST_DATABASE_URL`.
- Provides the shared infrastructure for PostgreSQL-based tests.
- Includes `pgvector` support.

**`pg_vector_db_engine`** (function scope)

- PostgreSQL database engine with `pgvector` support guaranteed.
- Always provides a PostgreSQL backend, even if the suite is running with SQLite.
- Creates a unique database per test for complete isolation.
- Usage: `async def test_something(pg_vector_db_engine):`

**`session_db_engine`** (session scope)

- Session-scoped SQLite engine for performance-critical, read-only tests.
- Shared across multiple tests to reduce setup overhead.
- Supports parallel execution via `pytest-xdist` (one database per worker).
- Located in `tests/functional/web/conftest.py`.

**`api_db_context`** (function scope)

- Provides an already-entered `DatabaseContext` for API-level tests.
- Simplifies access to database repository methods.
- Located in `tests/functional/web/conftest.py`.

### Task Worker Fixtures

**`task_worker_manager`** (function scope)

- Manages lifecycle of a TaskWorker instance for background task testing

- Returns tuple: `(TaskWorker, new_task_event, shutdown_event)`

- Worker has mock ChatInterface and embedding generator

- Tests must register their own task handlers

- Usage:

  ```python
  async def test_task(task_worker_manager):
      worker, new_task_event, shutdown_event = task_worker_manager
      worker.register_handler("my_task", my_handler)
      # ... test task execution
  ```

### CalDAV Server Fixtures

**`radicale_server_session`** (session scope)

- Starts a Radicale CalDAV server for the test session
- Creates test user with credentials: `testuser`/`testpass`
- Returns tuple: `(base_url, username, password)`
- Server persists for entire test session

**`radicale_server`** (function scope)

- Creates a unique calendar for each test function

- Depends on `radicale_server_session` and `pg_vector_db_engine`

- Returns tuple: `(base_url, username, password, unique_calendar_url)`

- Automatically cleans up the calendar after test

- Usage:

  ```python
  async def test_calendar(radicale_server):
      base_url, username, password, calendar_url = radicale_server
      # Use calendar_url for test operations
  ```

### Indexing Pipeline Fixtures

**`mock_pipeline_embedding_generator`** (function scope)

- HashingWordEmbeddingGenerator for deterministic embeddings in tests

**`indexing_task_worker`** (function scope)

- TaskWorker configured for indexing tasks
- Returns tuple: `(TaskWorker, new_task_event, shutdown_event)`

### Mock Utilities

The project includes `tests/mocks/mock_llm.py` with:

**`RuleBasedMockLLMClient`**

- Mock LLM that responds based on predefined rules

- Rules are (matcher_function, LLMOutput) tuples

- Matcher functions receive keyword arguments and return bool

- Usage:

  ```python
  mock_llm = RuleBasedMockLLMClient(
      rules=[
          (lambda args: "weather" in args["messages"][0]["content"],
           LLMOutput(content="It's sunny today!")),
      ],
      default_response=LLMOutput(content="Default response")
  )
  ```

## Testing Chat API Endpoints

### Waiting for Server Startup

When the server is starting up (e.g., in CI, Docker containers, or local development), use the
wait-for-server script to ensure the server is ready before making requests:

```bash
# Wait for server with default 120 second timeout
scripts/wait-for-server.sh

# Wait with custom timeout (e.g., 60 seconds)
scripts/wait-for-server.sh 60
```

The script polls `http://devcontainer-backend-1:8000/api/health` every 2 seconds until the server
responds or the timeout is reached. Exit codes:

- `0` - Server is ready
- `1` - Timeout waiting for server

To test the chat API functionality manually, you can use curl to make requests to both streaming and
non-streaming endpoints:

**Non-streaming chat:**

```bash
curl -X POST http://devcontainer-backend-1:8000/api/v1/chat/send_message \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello, can you tell me what 2+2 equals?"}'
```

**Streaming chat:**

```bash
curl -X POST http://devcontainer-backend-1:8000/api/v1/chat/send_message_stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is 5+7? Please calculate it for me."}'
```

**Debug Logging:** To see detailed LLM request/response debugging, set the `DEBUG_LLM_MESSAGES=true`
environment variable when running the application. This will log the exact messages being sent to
the LLM, including system prompts, user messages, tool calls, and tool responses.

**Diagnostics Export:** For comprehensive debugging, use the diagnostics export API to get a
combined export of error logs, LLM requests, and message history. This is designed for use with
`curl` and `jq`, and outputs data suitable for passing to Claude Code or other LLMs for debugging.

```bash
# Get JSON export (default) - requires authentication
curl -s -H "Authorization: Bearer YOUR_API_TOKEN" \
  http://localhost:8000/api/diagnostics/export | jq .

# Get just LLM requests from last 30 minutes
curl -s -H "Authorization: Bearer YOUR_API_TOKEN" \
  http://localhost:8000/api/diagnostics/export | jq '.llm_requests'

# Get errors from last 5 minutes
curl -s -H "Authorization: Bearer YOUR_API_TOKEN" \
  'http://localhost:8000/api/diagnostics/export?minutes=5' | jq '.error_logs'

# Get markdown format for pasting to LLM
curl -s -H "Authorization: Bearer YOUR_API_TOKEN" \
  'http://localhost:8000/api/diagnostics/export?format=markdown'

# Filter by conversation
curl -s -H "Authorization: Bearer YOUR_API_TOKEN" \
  'http://localhost:8000/api/diagnostics/export?conversation_id=abc123' | jq .
```

Query parameters:

- `minutes`: Time window (1-120, default: 30)
- `max_errors`: Max error logs to return (1-100, default: 50)
- `max_llm_requests`: Max LLM requests to return (1-100, default: 20)
- `max_messages`: Max message history entries (1-500, default: 100)
- `conversation_id`: Optional filter for specific conversation
- `format`: Output format - `json` (default) or `markdown`

The export includes:

- System info (Python version, platform, database type)
- Error logs with timestamps, levels, and tracebacks
- LLM requests with messages, tools, responses, and timing
- Message history with roles, content, and conversation IDs

**API Discovery:** To discover available API endpoints, use jq to parse the OpenAPI spec:

```bash
# List all available endpoints
curl -s 'http://devcontainer-backend-1:8000/openapi.json' | jq '.paths | keys'

# Find specific endpoints (e.g., tools-related)
curl -s 'http://devcontainer-backend-1:8000/openapi.json' | jq '.paths | keys | map(select(contains("tool")))'
```

**Testing Tools API directly:**

```bash
# Execute a tool directly (bypasses LLM, saves tokens for testing)
curl -X POST "http://devcontainer-backend-1:8000/api/tools/execute/get_camera_frame" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"camera_id": "chickens", "timestamp": "2025-12-27T09:30:00"}}'

# Get tool definitions
curl -s 'http://devcontainer-backend-1:8000/api/tools/definitions' | jq
```

## Playwright Tests

End-to-end tests for the web UI are written using Playwright and can be found in
`tests/functional/web/`. These tests are marked with `@pytest.mark.playwright`.

See [tests/functional/web/CLAUDE.md](functional/web/CLAUDE.md) for detailed Playwright testing
guidance and debugging techniques.

## Telegram Bot Tests

Tests for the Telegram bot functionality can be found in `tests/functional/telegram/`.

See [tests/functional/telegram/CLAUDE.md](functional/telegram/CLAUDE.md) for Telegram-specific
testing patterns.

## CI Debugging and Troubleshooting

When CI tests fail, use these tools and techniques to debug issues efficiently.

**Note**: Do not use `CI=true` when debugging locally - it is used in CI as a shortcut to skip
rebuilding the frontend.

### CI Status Monitoring

```bash
# View recent CI runs
gh run list --limit 5

# Check specific run status
gh run view <run-id>

# Monitor CI run in real-time (tip: pipe to tail if output is too long)
gh run watch <run-id>
gh run watch <run-id> | tail -50

# View failed job logs once run completes
gh run view <run-id> --log-failed
```

### Downloading and Analyzing Artifacts

**First step when debugging: Download the JSON test report from artifacts**

```bash
# Download all artifacts from a CI run
gh run download <run-id>

# Download specific artifact (faster for large runs)
gh run download <run-id> --name playwright-artifacts-sqlite-<run-id>
gh run download <run-id> --name playwright-artifacts-postgres-<run-id>

# List available artifacts
gh run view <run-id>  # Shows artifacts section at bottom
```

### Artifact Contents

**JSON Test Reports:**

- `test-results/sqlite-report.json` - Detailed SQLite test results with timing and metadata
- `test-results/postgres-report.json` - Detailed PostgreSQL test results
- Include test names, outcomes, durations, and error details
- Useful for analyzing patterns across test runs and understanding failures

**Playwright Debugging Artifacts:**

- `test-results/**/test-failed-*.png` - Screenshots at point of failure
- `test-results/**/test-*.webm` - Videos of entire test execution
- `test-results/**/trace.zip` - Detailed browser traces for step-by-step debugging
- Located in subdirectories named after the failing test

### Debugging Workflow

1. **Check CI status:** `gh run list --limit 5`
2. **Download JSON report:** `gh run download <run-id> --name <artifact-name>`
3. **Analyze test failures:** Open `*-report.json` to see detailed error messages
4. **Review visual artifacts:** Check screenshots and videos for UI test failures
5. **Use traces for deep debugging:** Open `trace.zip` in Playwright Trace Viewer

### Common CI Issues

**Frontend Build Failures:**

- Check container build logs for frontend asset copying
- Verify `router.html` and `.vite/manifest.json` are present in artifacts

**Mobile Responsive Test Failures:**

- Screenshots show actual vs. expected layout on mobile viewport
- Check for horizontal scroll issues in 375px viewport

**Navigation Test Failures:**

- Videos show actual user interaction flow
- Verify navigation elements are visible and clickable
