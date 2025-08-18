# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

> **Note**: CLAUDE.md is a symlink to this file (AGENTS.md). If you need to edit CLAUDE.md, you
> should edit AGENTS.md instead.
>
> **Architecture Documentation**: For a comprehensive visual overview of the system architecture,
> component interactions, and data flows, see
> [docs/architecture-diagram.md](docs/architecture-diagram.md).

## Style

- Comments are used to explain implementation when it's unclear. Do NOT add comments that are
  self-evident from the code, or that explain the code's history (that's what commit history is
  for). No comments like `# Removed db_context`.

## Development Setup

### Installation

```bash
# Install the project in development mode with all dependencies
uv pip install -e '.[dev]'

# Optional: Install local embedding model support (adds ~450MB of dependencies)
# Only needed if you want to use local sentence transformer models instead of cloud APIs
uv pip install -e '.[dev,local-embeddings]'
```

## Frontend Development

The frontend is a modern React application built with Vite.

### Setup

All frontend code is located in the `frontend/` directory. To get started, install the dependencies:

```bash
npm install --prefix frontend
```

### Development Server

To run the frontend development server with hot module replacement (HMR):

```bash
poe dev
```

This command starts both the FastAPI backend and the Vite frontend development server. The frontend
is served on `http://localhost:5173` (or `http://devcontainer-backend-1:5173` in the dev container),
and all API requests are proxied to the backend running on port 8000.

### Building for Production

To build the frontend for production:

```bash
npm run build --prefix frontend
```

This will create an optimized production build in `src/family_assistant/static/dist`.

### Linting and Formatting

We use ESLint for linting and Biome for formatting.

**Frontend linting commands:**

- **Lint:** `poe frontend-lint` or `npm run lint --prefix frontend`
- **Format:** `poe frontend-format` or `npm run format --prefix frontend`
- **Check both:** `poe frontend-check` or `npm run check --prefix frontend`

**Note**: The `--prefix frontend` pattern avoids directory changes and is preferred for scripts and
subagents.

These are also integrated into the pre-commit hooks and the main `scripts/format-and-lint.sh`
script.

### Pages

The application is a modern React single-page application (SPA) served by Vite. All UI pages are now
React-based components within the SPA architecture. The entry point is `frontend/chat.html`, and the
application uses client-side routing to handle navigation between different views and features.

## Development Commands

### Linting and Type Checking

```bash
# Lint entire codebase (src/ and tests/)
scripts/format-and-lint.sh

# Lint specific Python files only
scripts/format-and-lint.sh path/to/file.py path/to/another.py

# Lint only changed Python files (useful before committing)
scripts/format-and-lint.sh $(git diff --name-only --cached | grep '\.py$')

# Note: This script is for Python files only. It will error if given non-Python files.
```

This script runs:

- `ruff check --fix` (linting with auto-fixes)
- `ruff format` (code formatting)
- `basedpyright` (type checking)
- `pylint` (additional linting in errors-only mode)

**IMPORTANT**: `scripts/format-and-lint.sh` MUST pass before committing. NEVER use
`git commit --no-verify` -- all lint failures must be fixed or properly disabled.

### Using the `llm` CLI

- `cat myscript.py | llm 'explain this code'` - Analyze a script
- `git diff | llm -s 'Describe these changes'` - Understand code changes
- `llm -f error.log 'debug this error'` - Debug from log files
- `cat file1.py file2.py | llm 'how do these interact?'` - Analyze multiple files
- Use `llm chat` for multi-line inputs (paste errors/tracebacks with `!multi` and `!end`)

### Testing

- IMPORTANT: Write your tests as "end-to-end" as you can.

  - Use mock objects as little as possible. Use real databases (fixtures available in
    tests/conftest.py and tests/functional/telegram/conftest.py) and only mock external dependencies
    with no good fake implementations.

- Each test tests one independent behaviour of the system under test. Arrange, Act, Assert. NEVER
  Arrange, Act, Assert, Act, Assert.

- ALWAYS run tests with `-xq` so there is less output to process. NEVER use `-s` or `-v` unless you
  have already tried with `-q` and you are sure there is information in the output of `-s` or `-v`
  that you need for debugging.

- **Finding Flaky Tests**: When debugging test flakiness, use pytest's `--flake-finder` plugin:

  ```bash
  pytest tests/path/to/test.py --flake-finder --flake-runs=100 -x
  ```

  This is much more efficient than running tests in a loop. The `-x` flag stops on first failure.
  Never use manual loops or shell scripts to repeatedly run tests.

```bash
# Run all tests with verbose output
poe test # Note: You will need a long timeout for this - something like 15 minutes

# Run tests with PostgreSQL (production database)
poe test-postgres  # Quick mode with -xq
poe test-postgres-verbose  # Verbose mode with -xvs

# Run tests with PostgreSQL using pytest directly
pytest --postgres -xq  # All tests with PostgreSQL
pytest --postgres tests/functional/test_specific.py -xq  # Specific tests with PostgreSQL

# Run specific test files
pytest tests/functional/test_specific.py -xq

```

#### Playwright Tests

End-to-end tests for the web UI are written using Playwright and can be found in
`tests/functional/web/`. These tests are marked with `@pytest.mark.playwright`.

**Debugging Playwright Tests:**

When a Playwright test fails, `pytest-playwright` automatically captures screenshots and records a
video of the test execution. These artifacts are invaluable for debugging.

- **Screenshots:** A screenshot is taken at the point of failure.
- **Videos:** A video of the entire test run is saved.
- **Traces:** Comprehensive debugging data including network requests, console logs, DOM snapshots,
  and action timeline.

By default, these are saved to the `test-results` directory. You can also use the `--screenshot on`
and `--video on` flags to capture these artifacts for passing tests as well.

**Advanced Debugging Techniques:**

1. **Analyzing Network Traffic:**

   ```bash
   # Extract and examine network requests from trace files
   unzip -p test-results/*/trace.zip trace.network | strings | grep -A 5 -B 5 "send_message_stream"

   # Look for specific API endpoints or error responses
   unzip -p test-results/*/trace.zip trace.network | strings | grep "status.*[45][0-9][0-9]"
   ```

2. **Examining Server-Sent Events (SSE) Streams:**

   ```bash
   # Extract actual streaming response data to debug partial content issues
   unzip -p test-results/*/trace.zip resources/*.dat | head -50

   # This shows the actual SSE events and data sent by the server
   # Useful for debugging streaming chat responses or real-time updates
   ```

3. **Console Log Analysis:**

   ```bash
   # Check for JavaScript errors or warnings
   unzip -p test-results/*/trace.zip trace.trace | strings | grep -i "error\|warning\|exception"
   ```

4. **Interactive Trace Viewing:**

   ```bash
   # Open the full interactive trace viewer (requires Playwright CLI)
   npx playwright show-trace test-results/*/trace.zip

   # This provides a timeline view with:
   # - Network requests and responses with full headers/body
   # - Console messages with timestamps
   # - DOM snapshots at each action
   # - Screenshots at each step
   ```

5. **Debugging Common Issues:**

   - **Partial content/streaming issues:** Check SSE data extraction (method 2) to verify server
     sends complete data
   - **Timing/race conditions:** Use trace timeline to see exact timing of actions vs. UI updates
   - **Network failures:** Examine network requests for failed API calls or timeouts
   - **DOM state issues:** Use DOM snapshots in trace viewer to see element state at failure point

#### Database Backend Selection

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

### Test Fixtures

The project provides a comprehensive set of pytest fixtures for testing different components. These
fixtures are defined in `tests/conftest.py` and `tests/functional/telegram/conftest.py`.

#### Core Database Fixtures

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

#### Task Worker Fixtures

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

#### CalDAV Server Fixtures

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

#### Telegram Bot Testing Fixtures

**`telegram_handler_fixture`** (function scope)

- Comprehensive fixture for testing Telegram bot functionality

- Located in `tests/functional/telegram/conftest.py`

- Returns `TelegramHandlerTestFixture` named tuple with:

  - `assistant`: Configured Assistant instance
  - `handler`: TelegramUpdateHandler
  - `mock_bot`: Mocked Telegram bot (AsyncMock)
  - `mock_llm`: RuleBasedMockLLMClient for controlled LLM responses
  - `mock_confirmation_manager`: Mock for tool confirmation requests
  - `mock_application`: Mock Telegram Application
  - `processing_service`: Configured ProcessingService
  - `tools_provider`: Configured ToolsProvider
  - `get_db_context_func`: Function to get database context

- Usage:

  ```python
  async def test_telegram_command(telegram_handler_fixture):
      fixture = telegram_handler_fixture
      # Add LLM rules
      fixture.mock_llm.rules.append((matcher_func, LLMOutput(...)))
      # Test handler methods
      await fixture.handler.handle_message(update, context)
      # Assert bot interactions
      fixture.mock_bot.send_message.assert_called_once()
  ```

#### Web API Testing Fixtures

These fixtures are available in various web API test files:

**`db_context`** (function scope)

- Provides a DatabaseContext for web API tests
- Usage: `async def test_api(db_context):`

**`mock_processing_service_config`** (function scope)

- Provides a ProcessingServiceConfig with test prompts

**`mock_llm_client`** (function scope)

- Provides a RuleBasedMockLLMClient for API tests

**`test_tools_provider`** (function scope)

- Configured ToolsProvider with local tools enabled

**`test_processing_service`** (function scope)

- ProcessingService instance with mock components

**`app_fixture`** (function scope)

- FastAPI application instance configured for testing

**`test_.client`** (function scope)

- HTTPX AsyncClient for the test FastAPI app

- Usage:

  ```python
  async def test_endpoint(test_client):
      response = await test_client.post("/api/endpoint", json={...})
      assert response.status_code == 200
  ```

#### Indexing Pipeline Fixtures

**`mock_pipeline_embedding_generator`** (function scope)

- HashingWordEmbeddingGenerator for deterministic embeddings in tests

**`indexing_task_worker`** (function scope)

- TaskWorker configured for indexing tasks
- Returns tuple: `(TaskWorker, new_task_event, shutdown_event)`

#### Mock Utilities

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

### CI Debugging and Troubleshooting

When CI tests fail, use these tools and techniques to debug issues efficiently.

Do not use `CI=true` when debugging locally - it is used in CI as a shortcut to skip rebuilding the frontend.

#### CI Status Monitoring

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

#### Downloading and Analyzing Artifacts

**First step when debugging: Download the JSON test report from artifacts**

```bash
# Download all artifacts from a CI run
gh run download <run-id>

# Download specific artifact (faster for large runs)
gh run download <run-id> --name playwright-artifacts-sqlite-<run-id>
gh run download <run-id> --name playwright-artifacts-postgres-<run-id>

# List available artifacts
gh run view <run-id> # Shows artifacts section at bottom
```

#### Artifact Contents

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

#### Debugging Workflow

1. **Check CI status:** `gh run list --limit 5`
2. **Download JSON report:** `gh run download <run-id> --name <artifact-name>`
3. **Analyze test failures:** Open `*-report.json` to see detailed error messages
4. **Review visual artifacts:** Check screenshots and videos for UI test failures
5. **Use traces for deep debugging:** Open `trace.zip` in Playwright Trace Viewer

#### Common Issues and Solutions

**Frontend Build Failures:**

- Check container build logs for frontend asset copying
- Verify `router.html` and `.vite/manifest.json` are present in artifacts

**Mobile Responsive Test Failures:**

- Screenshots show actual vs. expected layout on mobile viewport
- Check for horizontal scroll issues in 375px viewport

**Navigation Test Failures:**

- Videos show actual user interaction flow
- Verify navigation elements are visible and clickable

#### Playwright Artifacts in Detail

When Playwright tests fail, these artifacts are automatically generated:

- **Screenshots:** Capture the exact state when test failed
- **Videos:** Show the complete test execution, helpful for understanding test flow
- **Traces:** Comprehensive debugging data including:
  - Network requests and responses
  - Console logs and errors
  - DOM snapshots at each step
  - Action timeline with screenshots

Open traces with: `npx playwright show-trace trace.zip`

### Running the Application

```bash
# Development mode with hot-reloading (recommended)
poe dev
# Access the app at http://localhost:5173 (or http://devcontainer-backend-1:5173 in dev container)

# Main application entry point (production mode)
python -m family_assistant

# Via setuptools script
family-assistant

# Backend API server only (for testing)
poe serve
# Or directly: uvicorn family_assistant.web_server:app --reload --host 0.0.0.0 --port 8000
```

### Database Migrations

```bash
# Create new migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Use DATABASE_URL="sqlite+aiosqlite:///family_assistant.db" with alembic to make a new revision
# alembic migrations run on startup
```

### Code Generation

```bash
# Generate SYMBOLS.md file
poe symbols
```

### Finding Symbol Definitions and Signatures

```bash
# Use symbex to find symbol definitions and signatures
# Docs: https://github.com/simonw/symbex

# Find a specific function or class
symbex my_function
symbex MyClass

# Show signatures for all symbols
symbex -s

# Show signatures with docstrings
symbex --docstrings

# Search with wildcards
symbex 'test_*'
symbex '*Tool.*'

# Find async functions
symbex --async -s

# Find undocumented public functions
symbex --function --public --undocumented

# Search in specific files
symbex MyClass -f src/family_assistant/assistant.py
symbex 'handle_*' -f src/family_assistant/telegram_bot.py

# Search in specific directories
symbex -d src/family_assistant --function -s
```

### Making large-scale changes: Prefer `ast-grep`

`ast-grep` is available for making mechanical syntactic changes and is the tool of choice in most
cases.

**Note**: Use `ast-grep scan` for applying complex rule-based transformations (not `ast-grep run`).
The `scan` command supports YAML rule files and inline rules with `--inline-rules`.

### Removing a Keyword Argument

**Task:** Reliably remove the `cache=...` keyword argument from all calls to `my_function`,
regardless of its position. *(This requires `--inline-rules` because a single pattern cannot handle
all comma variations.)*

**Before:**

```python
my_function(arg1, cache=True, other_arg=123)
my_function(cache=True, other_arg=123)
my_function(cache=True)
```

**Command:**

```bash
ast-grep -U --inline-rules '
id: remove-cache-kwarg-robust
language: python
rule:
  any:
    - pattern: my_function($$$START, cache=$_, $$$END)
      fix: my_function($$$START, $$$END)
    - pattern: my_function(cache=$_, $$$END)
      fix: my_function($$$END)
    - pattern: my_function(cache=$_)
      fix: my_function()
' .
```

**After:**

```python
my_function(arg1, other_arg=123)
my_function(other_arg=123)
my_function()
```

### Changing Module Method to Instance Method

**Task:** Change calls from `mymodule.mymethod(object, ...)` to `object.mymethod(...)`. *(This is a
direct transformation suitable for the simpler `-p`/`-r` flags.)*

**Before:**

```python
my_instance = MyClass()
mymodule.mymethod(my_instance, 'arg1', kwarg='value')
```

**Command:**

```bash
ast-grep -U -p 'mymodule.mymethod($OBJECT, $$$ARGS)' -r '$OBJECT.mymethod($$$ARGS)' .
```

**After:**

```python
my_instance = MyClass()
my_instance.mymethod('arg1', kwarg='value')
```

### Adding a Keyword Argument Conditionally

**Task:** Add `timeout=10` to `requests.get()` calls, but only if they don't already have one.
*(This requires `--inline-rules` to use the relational `not` and `has` operators.)*

**Before:**

```python
requests.get("https://api.example.com/status")
requests.get("https://api.example.com/data", timeout=5)
```

**Command:**

```bash
ast-grep -U --inline-rules '
id: add-timeout-to-requests-get
language: python
rule:
  pattern: requests.get($$$ARGS)
  not:
    has:
      pattern: timeout = $_
  fix: requests.get($$$ARGS, timeout=10)
' .
```

**After:**

```python
requests.get("https://api.example.com/status", timeout=10)
requests.get("https://api.example.com/data", timeout=5)
```

### Unifying Renamed Functions (Order-Independent)

**Task:** Unify `send_json_payload(...)` and `post_data_as_json(...)` to `api_client.post(...)`,
regardless of keyword argument order. *(This requires `--inline-rules` to handle multiple conditions
(`any`, `all`) and order-insensitivity (`has`).)*

**Before:**

```python
send_json_payload(endpoint="/users", data={"name": "Alice"})
post_data_as_json(json_body={"name": "Bob"}, url="/products")
```

**Command:**

```bash
ast-grep -U --inline-rules '
id: unify-json-posting-functions-robust
language: python
rule:
  any:
    - all:
        - pattern: send_json_payload($$$_)
        - has: {pattern: endpoint = $URL}
        - has: {pattern: data = $PAYLOAD}
    - all:
        - pattern: post_data_as_json($$$_)
        - has: {pattern: url = $URL}
        - has: {pattern: json_body = $PAYLOAD}
  fix: api_client.post(url=$URL, json=$PAYLOAD)
' .
```

**After:**

```python
api_client.post(url="/users", json={"name": "Alice"})
api_client.post(url="/products", json={"name": "Bob"})
```

### Modernizing `unittest` Assertions to `pytest`

**Task:** Convert `unittest` style assertions to modern `pytest` `assert` statements. *(Using
`--inline-rules` is best here to bundle multiple, related transformations into a single command.)*

**Before:**

```python
self.assertEqual(result, 4)
self.assertTrue(is_active)
self.assertIsNone(value)
```

**Command:**

```bash
ast-grep -U --inline-rules '
- id: refactor-assertEqual
  language: python
  rule: {pattern: self.assertEqual($A, $B), fix: "assert $A == $B"}
- id: refactor-assertTrue
  language: python
  rule: {pattern: self.assertTrue($A), fix: "assert $A"}
- id: refactor-assertIsNone
  language: python
  rule: {pattern: self.assertIsNone($A), fix: "assert $A is None"}
' .
```

**After:**

```python
assert result == 4
assert is_active
assert value is None
```

## Embedding Models

Family Assistant supports multiple embedding model backends:

### Cloud-based Embeddings (Default)

The default configuration uses cloud-based embedding models via LiteLLM (e.g.,
`gemini/gemini-embedding-exp-03-07`). These require no additional dependencies and provide
high-quality embeddings with minimal setup.

### Local Embeddings (Optional)

For privacy or offline use cases, you can use local sentence transformer models. These require the
`local-embeddings` optional dependency group:

```bash
# Install with local embedding support
uv pip install -e '.[dev,local-embeddings]'
```

Local models are identified by:

- Paths starting with `/` (e.g., `/path/to/model`)
- Known model names like `all-MiniLM-L6-v2`

Note: The `local-embeddings` extra adds ~450MB of dependencies (torch, transformers,
sentence-transformers).

### Hashing-based Embeddings

For testing or lightweight deployments, a deterministic hashing-based embedding generator is
available (`hashing-word-v1`) that requires no external dependencies.

## Architecture Overview

Family Assistant is an LLM-powered application designed to centralize family information management
and automate tasks. It provides multiple interfaces (Telegram, Web UI, Email webhooks) and uses a
modular architecture built with Python, FastAPI, and SQLAlchemy.

### Core Components

01. **Entry Point (`__main__.py`)**:

    - Handles configuration loading from multiple sources (defaults → config.yaml → environment
      variables → CLI args)
    - Manages application lifecycle through the `Assistant` class
    - Sets up signal handlers for graceful shutdown

02. **Assistant (`assistant.py`)**:

    - Orchestrates application lifecycle and dependency injection
    - Wires up all core components (LLM clients, tools, processing services, storage, etc.)
    - Manages service startup/shutdown coordination

03. **Processing Layer (`processing.py`)**:

    - Core business logic for handling chat interactions
    - Manages conversation history and context aggregation
    - Supports multiple service profiles with different LLM models, tools, and prompts
    - Executes tool calls and manages delegation between profiles

04. **User Interfaces**:

    - **Telegram Bot (`telegram_bot.py`)**: Primary interface with slash command support
    - **Web UI (`web/`)**: FastAPI-based web interface with routers for various features
    - **Email Webhook**: Receives and processes emails via `/webhook/mail`

05. **Storage Layer (`storage/`)**:

    - Repository pattern architecture with SQLAlchemy (supports SQLite and PostgreSQL)
    - **DatabaseContext**: Central hub providing access to all repositories
    - **Repository Classes** (`storage/repositories/`):
      - `NotesRepository`: Note management and search
      - `TasksRepository`: Background task queue operations
      - `MessageHistoryRepository`: Conversation history storage
      - `EmailRepository`: Email storage and retrieval
      - `VectorRepository`: Vector embeddings for semantic search
      - `EventsRepository`: Event storage and matching
      - `ErrorLogsRepository`: Error tracking and logging
    - Each repository extends `BaseRepository` for consistent error handling and logging
    - Includes retry logic, connection pooling, and transaction management
    - Database schema managed by Alembic migrations

06. **Tools System (`tools/`)**:

    - Modular tool architecture with local Python functions and MCP (Model Context Protocol)
      integration
    - Tools organized by category: notes, calendar, documents, communication, tasks, etc.
    - Supports tool confirmation requirements and delegation security levels
    - Composite tool provider system for flexible tool management

07. **Task Queue (`task_worker.py`)**:

    - Database-backed async task queue for background processing
    - Supports scheduled tasks, retries with exponential backoff, and recurring tasks
    - Handles LLM callbacks, email indexing, embedding generation, and system maintenance

08. **Document Indexing (`indexing/`)**:

    - Pipeline-based document processing system
    - Supports multiple document types (PDFs, emails, web pages, notes)
    - Includes text extraction, chunking, embedding generation, and vector storage
    - Configurable processing pipeline with various processors

09. **Event System (`events/`)**:

    - Event-driven architecture for system notifications
    - Supports multiple event sources (Home Assistant, indexing pipeline)
    - Event listeners with flexible matching conditions and rate limiting
    - Event storage and processing with action execution

10. **Context Providers (`context_providers.py`)**:

    - Pluggable system for injecting dynamic context into LLM prompts
    - Includes providers for calendar events, notes, weather, known users, and Home Assistant

### Data Flow

1. **User Request Flow**:

   - User sends message via interface (Telegram/Web/Email)
   - Interface layer forwards to Processing Service
   - Processing Service aggregates context from providers
   - LLM generates response with potential tool calls
   - Tools execute actions (database updates, external API calls)
   - Response sent back through interface

2. **Background Task Flow**:

   - Tasks enqueued to database queue
   - Task worker polls/receives notifications for new tasks
   - Worker executes task handlers (callbacks, indexing, maintenance)
   - Results stored and failures retried with backoff

3. **Document Indexing Flow**:

   - Document uploaded/ingested via API or tools
   - Indexing pipeline processes document through configured processors
   - Text extracted, chunked, and embedded
   - Embeddings stored in vector database for semantic search

### Configuration

- **Hierarchical configuration**: Code defaults → config.yaml → environment variables → CLI
  arguments
- **Service Profiles**: Multiple profiles with different LLMs, tools, and prompts
- **Tool Configuration**: Fine-grained control over available tools per profile
- **MCP Servers**: External tool integration via Model Context Protocol

### Key Design Patterns

- **Repository Pattern**: Data access logic encapsulated in repository classes, accessed via
  DatabaseContext
- **Dependency Injection**: Core services accept dependencies as constructor arguments
- **Protocol-based Interfaces**: Uses Python protocols for loose coupling (ChatInterface,
  LLMInterface, EmbeddingGenerator)
- **Async/Await**: Fully asynchronous architecture using asyncio -- **Context Managers**: Database
  operations use context managers for proper resource cleanup
- **Retry Logic**: Built-in retry mechanisms for transient failures
- **Event-Driven**: Loosely coupled components communicate via events

## Development Guidelines

- ALWAYS make a plan before you make any nontrivial changes.
- ALWAYS ask the user to approve the plan before you start work. In particular, you MUST stop and
  ask for approval before doing major rearchitecture or reimplementations, or making technical
  decisions that may require judgement calls.
- Significant changes should have the plan written to docs/design for approval and future
  documentation.
- When completing a user-visible feature, always update docs/user/USER_GUIDE.md and tell the
  assistant how it works in the system prompt in prompts.yaml or in tool descriptions. This is NOT
  optional or low priority.
- When solving a problem, always consider whether there's a better long term fix and ask the user
  whether they prefer the tactical pragmatic fix or the "proper" long term fix. Look out for design
  or code smells. Refactoring is relatively cheap in this project - cheaper than leaving something
  broken.
- IMPORTANT: You NEVER leave tests broken. We do not commit changes that cause tests to break. You
  NEVER make excuses like saying that test failures are 'unrelated' or 'separate issues'. You ALWAYS
  fix ALL test failures, even if you don't think you caused them.

#### Debugging and change verification

Once you've implemented a change, you ALWAYS go through the following algorithm:

1. Run scripts/format-and-lint.sh to check for linter errors.
2. Make sure that you have tests covering the new functionality, and that they pass.
3. Run a broad subset of tests related to your fixes.
4. Run `poe test` for final verification - this is what runs in CI and it runs all tests and linters.

You NEVER push new changes or make a PR if `poe test` does not pass. We do not merge PRs with failing
tests or linter errors.

### Planning guidelines

- Always break plans down into meaningful milestones that deliver incremental value, or at least
  which can be tested independently. This is key to maintaining momentum.
- Do NOT give timelines in weeks or other units of time. Development on this project does not
  proceed in this manner as a hobby project predominantly developed using LLM assistance tools like
  Claude Code.

### Adding New Tools

See the detailed guide in `src/family_assistant/tools/README.md` for complete instructions on
implementing new tools.

**IMPORTANT**: Tools must be registered in TWO places:

1. **In the code** (`src/family_assistant/tools/__init__.py`):

   - Add the tool function to `AVAILABLE_FUNCTIONS` dictionary
   - Add the tool definition to the appropriate `TOOLS_DEFINITION` list (e.g.,
     `NOTE_TOOLS_DEFINITION`)

2. **In the configuration** (`config.yaml`):

   - Add the tool name to `enable_local_tools` list for each profile that should have access
   - If `enable_local_tools` is not specified for a profile, ALL tools are enabled by default

This dual registration system provides:

- **Security**: Different profiles can have different tool access (e.g., browser profile has only
  browser tools)
- **Flexibility**: Each profile can be tailored with specific tools without code changes
- **Safety**: Destructive tools can be excluded from certain profiles

Example:

```yaml
# config.yaml
service_profiles:
  - id: "default_assistant"
    tools_config:
      enable_local_tools:
        - "add_or_update_note"
        - "search_documents"
        # ... other tools this profile should have
```

### Adding New UI Endpoints

When adding new web API endpoints:

1. Create your router in `src/family_assistant/web/routers/`
2. **Important**: Add your new endpoint to the appropriate test files in `tests/functional/web/` to
   ensure it's tested for basic functionality

Note: UI pages are now handled entirely by the React frontend. If you need to add new UI views,
create React components in the `frontend/` directory rather than server-side endpoints.

## Important Notes

- Always make sure you start with a clean working directory. Commit any uncommitted changes.

- NEVER revert existing changes without the user's explicit permission.

- Always check that linters and tests are happy when you're finished.

- Always commit changes after each major step. Prefer many small self contained commits as long as
  each commit passes lint checks.

- **Important**: When adding new imports, add the code that uses the import first, then add the
  import. Otherwise, a linter running in another tab might remove the import as unused before you
  add the code that uses it.

- Always use symbolic SQLAlchemy queries, avoid literal SQL text as much as possible. Literal SQL
  text may break across engines.

- **Database Access Pattern**: Use the repository pattern via DatabaseContext:

  ```python
  from family_assistant.storage.context import DatabaseContext

  async with DatabaseContext() as db:
      # Access repositories as properties
      await db.notes.add_or_update(title, content)
      tasks = await db.tasks.get_pending_tasks()
      await db.email.store_email(email_data)
  ```

- **SQLAlchemy Count Queries**: When using `func.count()` in SQLAlchemy queries, always use
  `.label("count")` to give the column an alias:

  ```python
  query = select(func.count(table.c.id).label("count"))
  row = await db_context.fetch_one(query)
  return row["count"] if row else 0
  ```

  This avoids KeyError when accessing the result.

- **SQLAlchemy func imports**: To avoid pylint errors about `func.count()` and `func.now()` not
  being callable, import func as:

  ```python
  from sqlalchemy.sql import functions as func
  ```

  instead of:

  ```python
  from sqlalchemy import func
  ```

  This resolves the "E1102: func.X is not callable" errors while maintaining the same functionality.

## File Management Guidance

- Put temporary files in the repo somewhere. scratch/ is available for truly temporary files but
  files of historical interest can go elsewhere

## DevContainer

The development environment runs using Docker Compose with persistent volumes for:

- `/workspace` - The project code
- `/home/claude` - Claude's home directory with settings and cache
- PostgreSQL data

### Building and Deploying

- To build and push the development container, use: `.devcontainer/build-and-push.sh [tag]`
- If no tag is provided, it defaults to timestamp format: `YYYYMMDD_HHMMSS`
- Example: `.devcontainer/build-and-push.sh` (uses timestamp tag)
- Example: `.devcontainer/build-and-push.sh v1.2.3` (uses custom tag)
- This script builds the container with podman and pushes to the registry

### Automatic Git Synchronization

The dev container automatically pulls the latest changes from git when Claude is invoked:

- Runs `git fetch` and `git pull --rebase` on startup
- Safely stashes and restores any local uncommitted changes
- If conflicts occur, reverts to the original state to avoid breaking the workspace
- This ensures the persistent workspace stays synchronized with the remote repository

### Container Architecture

The Docker Compose setup runs three containers:

1. **postgres** - PostgreSQL with pgvector extension for local development
2. **backend** - Runs the backend server and frontend dev server via `poe dev`
3. **claude** - Runs claude-code-webui on port 8080 with MCP servers configured
