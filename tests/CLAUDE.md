# Testing Guide

This file provides guidance for working with tests in this project.

## Testing Principles

- **IMPORTANT**: Write your tests as "end-to-end" as you can.

  - Use mock objects as little as possible. Use real databases (fixtures available in
    tests/conftest.py and tests/functional/telegram/conftest.py) and only mock external dependencies
    with no good fake implementations.

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
- The unified `test_db_engine` fixture automatically provides the appropriate database based on the
  flag

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

**`test_db_engine`** (function scope, autouse)

- Automatically provides either SQLite or PostgreSQL database based on `--postgres` flag
- Default: Creates an in-memory SQLite database for each test
- With `--postgres` flag: Creates a unique PostgreSQL database for each test
  - Database name format: `test_{test_name}_{random_id}`
  - Complete isolation - no data sharing between tests
  - Database is dropped after test completion
- Patches the global storage engine to use the test database
- Initializes the database schema
- Cleans up after the test completes
- Usage: Automatically available in all tests, no need to explicitly request

**`postgres_container`** (session scope)

- Starts a PostgreSQL container with pgvector extension for the test session
- Uses testcontainers library with `pgvector/pgvector:0.8.0-pg17` image
- Respects DOCKER_HOST environment variable
- Usage: `def test_something(postgres_container):`

**`pg_vector_db_engine`** (function scope)

- PostgreSQL database engine with vector support
- Always creates a unique PostgreSQL database regardless of `--postgres` flag
- Database name format: `test_pgvec_{test_name}_{random_id}`
- Complete isolation - database is created before and dropped after each test
- Usage: `async def test_something(pg_vector_db_engine):`

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
