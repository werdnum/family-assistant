# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Style

* Comments are used to explain implementation when it's unclear. Do NOT add comments that are self-evident from the code, or that explain the code's history (that's what commit history is for). No comments like `# Removed db_context`.

## Development Setup

### Installation

```bash
# Install the project in development mode with all dependencies
uv pip install -e '.[dev]'
```

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

**IMPORTANT**: `scripts/format-and-lint.sh` MUST pass before committing. NEVER use `git commit --no-verify` -- all lint failures must be fixed or properly disabled.

### Testing

* IMPORTANT: Write your tests as "end-to-end" as you can.
  * Use mock objects as little as possible. Use real databases (fixtures available in tests/conftest.py and tests/functional/telegram/conftest.py) and only mock external dependencies with no good fake implementations.
* Each test tests one independent behaviour of the system under test. Arrange, Act, Assert. NEVER Arrange, Act, Assert, Act, Assert, Act, Assert.

* ALWAYS run tests with `-xq` so there is less output to process. NEVER use `-s` or `-v` unless you have already tried with `-q` and you are sure there is information in the output of `-s` or `-v` that you need for debugging.

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

#### Database Backend Selection

By default, tests run with an in-memory SQLite database for speed. However, production uses PostgreSQL, so it's important to test with PostgreSQL to catch database-specific issues:

- Use `--postgres` flag to run tests with PostgreSQL instead of SQLite
- PostgreSQL container starts automatically when the flag is used (requires Docker)
- Tests that specifically need PostgreSQL features can use `pg_vector_db_engine` fixture, but will get a warning if run without `--postgres` flag
- The unified `test_db_engine` fixture automatically provides the appropriate database based on the flag

### Test Fixtures

The project provides a comprehensive set of pytest fixtures for testing different components. These fixtures are defined in `tests/conftest.py` and `tests/functional/telegram/conftest.py`.

#### Core Database Fixtures

**`test_db_engine`** (function scope, autouse)

- Automatically provides either SQLite or PostgreSQL database based on `--postgres` flag
- Default: Creates an in-memory SQLite database for each test
- With `--postgres` flag: Uses PostgreSQL container with pgvector support
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

- Legacy fixture that now delegates to `test_db_engine`
- Will use PostgreSQL if `--postgres` flag is provided, otherwise uses SQLite with a warning
- Maintained for backward compatibility with tests that specifically need PostgreSQL
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

**`test_client`** (function scope)

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

### Running the Application

```bash
# Main application entry point
python -m family_assistant

# Via setuptools script
family-assistant

# Web server only
uvicorn family_assistant.web_server:app --reload --host 0.0.0.0 --port 8000
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

`ast-grep` is available for making mechanical syntactic changes and is the tool of choice in most cases.

### Removing a Keyword Argument

**Task:** Reliably remove the `cache=...` keyword argument from all calls to `my_function`, regardless of its position.
*(This requires `--inline-rules` because a single pattern cannot handle all comma variations.)*

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

**Task:** Change calls from `mymodule.mymethod(object, ...)` to `object.mymethod(...)`.
*(This is a direct transformation suitable for the simpler `-p`/`-r` flags.)*

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

**Task:** Unify `send_json_payload(...)` and `post_data_as_json(...)` to `api_client.post(...)`, regardless of keyword argument order.
*(This requires `--inline-rules` to handle multiple conditions (`any`, `all`) and order-insensitivity (`has`).)*

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

**Task:** Convert `unittest` style assertions to modern `pytest` `assert` statements.
*(Using `--inline-rules` is best here to bundle multiple, related transformations into a single command.)*

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

## Architecture Overview

Family Assistant is an LLM-powered application designed to centralize family information management and automate tasks. It provides multiple interfaces (Telegram, Web UI, Email webhooks) and uses a modular architecture built with Python, FastAPI, and SQLAlchemy.

### Core Components

1. **Entry Point (`__main__.py`)**:
   - Handles configuration loading from multiple sources (defaults → config.yaml → environment variables → CLI args)
   - Manages application lifecycle through the `Assistant` class
   - Sets up signal handlers for graceful shutdown

2. **Assistant (`assistant.py`)**:
   - Orchestrates application lifecycle and dependency injection
   - Wires up all core components (LLM clients, tools, processing services, storage, etc.)
   - Manages service startup/shutdown coordination

3. **Processing Layer (`processing.py`)**:
   - Core business logic for handling chat interactions
   - Manages conversation history and context aggregation
   - Supports multiple service profiles with different LLM models, tools, and prompts
   - Executes tool calls and manages delegation between profiles

4. **User Interfaces**:
   - **Telegram Bot (`telegram_bot.py`)**: Primary interface with slash command support
   - **Web UI (`web/`)**: FastAPI-based web interface with routers for various features
   - **Email Webhook**: Receives and processes emails via `/webhook/mail`

5. **Storage Layer (`storage/`)**:
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

6. **Tools System (`tools/`)**:
   - Modular tool architecture with local Python functions and MCP (Model Context Protocol) integration
   - Tools organized by category: notes, calendar, documents, communication, tasks, etc.
   - Supports tool confirmation requirements and delegation security levels
   - Composite tool provider system for flexible tool management

7. **Task Queue (`task_worker.py`)**:
   - Database-backed async task queue for background processing
   - Supports scheduled tasks, retries with exponential backoff, and recurring tasks
   - Handles LLM callbacks, email indexing, embedding generation, and system maintenance

8. **Document Indexing (`indexing/`)**:
   - Pipeline-based document processing system
   - Supports multiple document types (PDFs, emails, web pages, notes)
   - Includes text extraction, chunking, embedding generation, and vector storage
   - Configurable processing pipeline with various processors

9. **Event System (`events/`)**:
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

- **Hierarchical configuration**: Code defaults → config.yaml → environment variables → CLI arguments
- **Service Profiles**: Multiple profiles with different LLMs, tools, and prompts
- **Tool Configuration**: Fine-grained control over available tools per profile
- **MCP Servers**: External tool integration via Model Context Protocol

### Key Design Patterns

- **Repository Pattern**: Data access logic encapsulated in repository classes, accessed via DatabaseContext
- **Dependency Injection**: Core services accept dependencies as constructor arguments
- **Protocol-based Interfaces**: Uses Python protocols for loose coupling (ChatInterface, LLMInterface, EmbeddingGenerator)
- **Async/Await**: Fully asynchronous architecture using asyncio
- **Context Managers**: Database operations use context managers for proper resource cleanup
- **Retry Logic**: Built-in retry mechanisms for transient failures
- **Event-Driven**: Loosely coupled components communicate via events

## Development Guidelines

- ALWAYS make a plan before you make any nontrivial changes.
- ALWAYS ask the user to approve the plan before you start work. In particular, you MUST stop and ask for approval before doing major rearchitecture or reimplementations, or making technical decisions that may require judgement calls.
- Significant changes should have the plan written to docs/design for approval and future documentation.
- When completing a user-visible feature, always update docs/user/USER_GUIDE.md and tell the assistant how it works in the system prompt in prompts.yaml or in tool descriptions. This is NOT optional or low priority.
- When solving a problem, always consider whether there's a better long term fix and ask the user whether they prefer the tactical pragmatic fix or the "proper" long term fix. Look out for design or code smells. Refactoring is relatively cheap in this project - cheaper than leaving something broken.

### Planning guidelines

* Always break plans down into meaningful milestones that deliver incremental value, or at least which can be tested independently. This is key to maintaining momentum.
* Do NOT give timelines in weeks or other units of time. Development on this project does not proceed in this manner as a hobby project predominantly developed using LLM assistance tools like Claude Code.

### Adding New Tools

See the detailed guide in `src/family_assistant/tools/README.md` for complete instructions on implementing new tools.

**IMPORTANT**: Tools must be registered in TWO places:

1. **In the code** (`src/family_assistant/tools/__init__.py`):
   - Add the tool function to `AVAILABLE_FUNCTIONS` dictionary
   - Add the tool definition to the appropriate `TOOLS_DEFINITION` list (e.g., `NOTE_TOOLS_DEFINITION`)

2. **In the configuration** (`config.yaml`):
   - Add the tool name to `enable_local_tools` list for each profile that should have access
   - If `enable_local_tools` is not specified for a profile, ALL tools are enabled by default

This dual registration system provides:

- **Security**: Different profiles can have different tool access (e.g., browser profile has only browser tools)
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

When adding new web UI endpoints that serve HTML pages:

1. Create your router in `src/family_assistant/web/routers/`
2. Always include `now_utc: datetime.now(timezone.utc)` in the template context when using `TemplateResponse`
3. **Important**: Add your new endpoint to the `BASE_UI_ENDPOINTS` list in `tests/functional/web/test_ui_endpoints.py` to ensure it's tested for basic accessibility

## Important Notes

- Always make sure you start with a clean working directory. Commit any uncommitted changes.
- NEVER revert existing changes without the user's explicit permission.
- Always check that linters and tests are happy when you're finished.
- Always commit changes after each major step. Prefer many small self contained commits as long as each commit passes lint checks.
- **Important**: When adding new imports, add the code that uses the import first, then add the import. Otherwise, a linter running in another tab might remove the import as unused before you add the code that uses it.
- Always use symbolic SQLAlchemy queries, avoid literal SQL text as much as possible. Literal SQL text may break across engines.
- **Database Access Pattern**: Use the repository pattern via DatabaseContext:

  ```python
  from family_assistant.storage.context import DatabaseContext
  
  async with DatabaseContext() as db:
      # Access repositories as properties
      await db.notes.add_or_update(title, content)
      tasks = await db.tasks.get_pending_tasks()
      await db.email.store_email(email_data)
  ```

  Avoid using the old module-level functions directly.
- **SQLAlchemy Count Queries**: When using `func.count()` in SQLAlchemy queries, always use `.label("count")` to give the column an alias:

  ```python
  query = select(func.count(table.c.id).label("count"))
  row = await db_context.fetch_one(query)
  return row["count"] if row else 0
  ```

  This avoids KeyError when accessing the result.

- **SQLAlchemy func imports**: To avoid pylint errors about `func.count()` and `func.now()` not being callable, import func as:

  ```python
  from sqlalchemy.sql import functions as func
  ```

  instead of:

  ```python
  from sqlalchemy import func
  ```

  This resolves the "E1102: func.X is not callable" errors while maintaining the same functionality.

## Test Fixtures

The project provides several pytest fixtures for testing. These are defined in various `conftest.py` files:

### Core Database Fixtures (tests/conftest.py)

- **`test_db_engine`** (function scope): Provides an in-memory SQLite database engine with schema initialized. Automatically patches `storage.base.engine` for the test duration.
  
- **`postgres_container`** (session scope): Starts a PostgreSQL container with pgvector extension for the entire test session. Reused across all PostgreSQL tests.

- **`pg_vector_db_engine`** (function scope): Provides a PostgreSQL database engine with vector support. Creates a clean schema for each test and handles proper cleanup.

### Task Worker Fixtures (tests/conftest.py)

- **`task_worker_manager`** (function scope): Manages TaskWorker lifecycle for testing background tasks. Returns a context manager that starts/stops the worker and provides task completion helpers.

### CalDAV Server Fixtures (tests/conftest.py)

- **`radicale_server_session`** (session scope): Starts a Radicale CalDAV server for the test session. Returns `(base_url, username, password)`.

- **`radicale_server`** (function scope): Creates a unique calendar for each test with automatic cleanup. Returns `(calendar_url, username, password)`.

### Telegram Bot Testing (tests/functional/telegram/conftest.py)

- **`telegram_handler_fixture`** (function scope): Comprehensive fixture for Telegram bot testing. Returns a named tuple with:
  - `handler`: The TelegramHandler instance
  - `mock_bot`: Mock telegram Bot
  - `mock_app`: Mock telegram Application
  - `mock_llm_client`: RuleBasedMockLLMClient
  - `mock_tools_provider`: CompositeToolProvider
  - `mock_processing_service`: ProcessingService
  - `db_engine`: Test database engine

### Web API Testing (tests/functional/web/conftest.py)

- **`db_context`**: Provides DatabaseContext for API tests
- **`mock_llm_client`**: RuleBasedMockLLMClient instance
- **`test_tools_provider`**: CompositeToolProvider with test tools
- **`test_processing_service`**: ProcessingService configured for testing
- **`app_fixture`**: FastAPI app with test dependencies
- **`test_client`**: HTTPX AsyncClient for API testing

### Indexing Pipeline Testing (tests/functional/indexing/conftest.py)

- **`mock_pipeline_embedding_generator`**: MockEmbeddingGenerator with deterministic embeddings for testing
- **`indexing_task_worker`**: TaskWorker configured for indexing tasks

### Mock Utilities

- **`RuleBasedMockLLMClient`**: A mock LLM client that returns responses based on rules. Useful for testing specific scenarios without API calls. Example:

  ```python
  mock_llm = RuleBasedMockLLMClient(
      rules=[
          ("weather", lambda q: "It will be sunny today"),
          ("time", lambda q: "The current time is 2:30 PM"),
      ]
  )
  ```

## Markdown

* Docs are rendered with redcarpet, which expects a blank line between paragraphs and other blocks (fenced code blocks, bulleted lists, etc).
