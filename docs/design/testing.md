# Testing Design Document: Family Assistant

**1. Introduction**

The goal is to introduce a robust testing suite for the Family Assistant application. The primary focus will be on realistic functional and integration tests that verify the system's behavior, rather than exhaustive unit tests with heavy mocking. Key initial goals include testing database initialization and providing end-to-end smoke tests for core bot functionality.

Testing will necessitate refactoring the codebase to improve testability, primarily through dependency injection.

**2. Testing Strategy & Tools**

We will employ a multi-layered testing approach:

*   **Integration Tests:** Verify the interactions between different components of the system (e.g., processing logic interacting with storage, task worker handling tasks). These are crucial for ensuring components work together correctly.
*   **Functional / End-to-End (E2E) Tests:** Simulate user interactions and verify the overall behavior of the application from an external perspective (e.g., sending a message via a simulated interface, checking the response and database state; accessing web UI endpoints).
*   **Unit Tests:** While not the primary focus, simple unit tests might be used for specific, isolated logic (e.g., utility functions, complex parsing logic) if deemed necessary.

**Chosen Tools:**

*   **Test Runner:** `pytest` (with `pytest-asyncio` for async code).
*   **Database:** `testcontainers-python` to run a real PostgreSQL instance in a Docker container for integration and functional tests. This provides high fidelity.
*   **LLM:**
    *   **Initial:** Use the *real* configured LLM (via OpenRouter/LiteLLM) for initial E2E tests. This requires configuring API keys in the test environment but provides the most realistic smoke test.
    *   **Future:** Integrate `mockllm`. This tool can run as a separate server, mimicking OpenAI/Anthropic APIs based on a configuration file. It will allow for deterministic and faster testing of LLM interactions without actual API calls or costs.
*   **Telegram Interface:**
    *   **Initial:** We will *not* directly test the `python-telegram-bot` handlers using a framework like `ptbtestsuite` initially. Instead, we will refactor the handler logic (`message_handler`, `process_chat_queue`, `_generate_llm_response_for_telegram`) into core, testable functions. Our functional tests will call these core functions directly, simulating the data that would come from a Telegram update.
    *   **Future:** If more direct testing of the `python-telegram-bot` integration is needed, `ptbtestsuite` could be evaluated, but it seems potentially complex to integrate and may require significant refactoring beyond what's initially planned.
*   **Web Server (FastAPI):** `httpx` (async client) to send requests to the FastAPI application running within the test setup (potentially managed by testcontainers or run directly).
*   **MCP:** Direct testing of MCP server interactions is complex due to the `stdio_client` spawning external processes.
    *   **Initial:** Tests will focus on the *logic within the assistant* that prepares MCP calls and handles their responses. We will refactor the code to allow injecting mock `mcp_sessions` and `tool_name_to_server_id` during tests. Actual MCP server connections will be skipped in the test environment.
    *   **Future:** Mock MCP server processes could be developed if needed, potentially run via testcontainers.
*   **Mocking:** `unittest.mock` (part of Python's standard library) for any necessary targeted mocking within tests.

**3. Refactoring for Testability (Dependency Injection)**

The current extensive use of global variables and direct imports of dependencies hinders testability. We need to refactor to use dependency injection:

1.  **Database Engine:**
    *   Modify `storage/base.py`'s `get_engine()` to potentially return a test-specific engine if available (e.g., via context var or passed config), or remove the global engine entirely.
    *   Refactor all functions within `storage/*.py` (e.g., `add_or_update_note`, `enqueue_task`, `get_recent_history`) to accept an `sqlalchemy.ext.asyncio.AsyncEngine` or `sqlalchemy.ext.asyncio.AsyncSession` object as an argument instead of relying on the global `storage.base.engine`.
    *   Update `storage.init_db` to accept an `AsyncEngine` argument.
    *   Update all callers of storage functions (`main.py`, `web_server.py`, `task_worker.py`, `processing.py`) to obtain and pass the engine/session.

2.  **Configuration:**
    *   Eliminate global configuration variables loaded directly from `os.getenv` or `args` within modules like `main.py`, `processing.py`.
    *   Create a dedicated configuration object (e.g., a Pydantic model or a simple dictionary) loaded centrally (e.g., in `main.py`'s startup).
    *   Pass this configuration object explicitly to functions and classes that require configuration values (e.g., `_generate_llm_response_for_telegram`, `load_mcp_config_and_connect`, database initialization, LLM client).

3.  **LLM Client:**
    *   Create a simple wrapper class (e.g., `LLMClient`) responsible for interacting with `litellm.acompletion`.
    *   This class should be initialized with the necessary configuration (model name, API key).
    *   It should expose an async method like `generate(messages, tools, tool_choice)`.
    *   Inject an instance of this `LLMClient` into `processing.get_llm_response` instead of calling `litellm.acompletion` directly. This will allow swapping the real client with a mock client targeting `mockllm` in tests.

4.  **Telegram Core Logic Decoupling:**
    *   Refactor `message_handler` to primarily parse the `Update` object, extract essential data (chat_id, user_id, user_name, text, photo bytes, reply_to_message_id), and potentially enqueue this data or directly call a core processing function.
    *   Refactor `process_chat_queue` and `_generate_llm_response_for_telegram` into standalone, testable async functions. They should accept all necessary dependencies as arguments (e.g., `chat_id`, `trigger_content_parts`, `user_name`, `db_engine/session`, `llm_client`, `mcp_state`, `config`) and return the response content and tool call info. They should *not* depend directly on `telegram.ext.ContextTypes`.
    *   The `python-telegram-bot` handlers in `main.py` will become thin wrappers around these core functions, responsible only for interacting with the Telegram API (sending actions, messages) based on the results from the core logic.

5.  **MCP State:**
    *   Refactor `load_mcp_config_and_connect` to *return* the `mcp_sessions` and `tool_name_to_server_id` dictionaries instead of setting global variables.
    *   The main application setup (`main_async`) will hold this state.
    *   Pass this state explicitly as arguments to functions that need it (e.g., `get_llm_response`, `task_worker.set_mcp_state` should be removed in favor of passing state to the worker loop/handlers).
    *   Allow skipping the actual connection logic in `load_mcp_config_and_connect` based on configuration (for tests).

6.  **Task Worker:**
    *   Refactor `task_worker.task_worker_loop` to accept dependencies (DB engine, LLM generator function reference, MCP state, config, events) as arguments.
    *   Remove the `task_worker.set_...` functions.
    *   Task handlers (`handle_llm_callback`, etc.) should ideally also receive necessary dependencies via the loop or a context object, rather than relying on globals or module-level state.

7.  **Web Server (FastAPI):**
    *   Leverage FastAPI's built-in dependency injection (`Depends`).
    *   Create dependency provider functions (e.g., `get_db_session`, `get_config`) that can be easily overridden in tests.
    *   Endpoint functions (`save_note`, `view_message_history`, etc.) will declare their dependencies using `Depends`, receiving sessions, config, etc., automatically.

**4. Test Structure**

```
├── tests/
│   ├── __init__.py
│   ├── conftest.py         # Pytest fixtures (DB container, engine, session factory, config, etc.)
│   ├── integration/        # Tests for component interactions
│   │   ├── __init__.py
│   │   ├── test_storage.py
│   │   ├── test_processing.py
│   │   ├── test_task_worker.py
│   │   └── ...
│   ├── functional/         # End-to-end style tests simulating user flows
│   │   ├── __init__.py
│   │   ├── test_telegram_flow.py # Tests core logic triggered by messages
│   │   ├── test_web_api.py       # Tests FastAPI endpoints via HTTP client
│   │   └── ...
│   └── unit/               # Optional: For highly isolated unit tests
│       ├── __init__.py
│       └── ...
├── src/                    # Assuming source code is moved here (recommended)
│   ├── family_assistant/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── processing.py
│   │   ├── storage/
│   │   ├── web_server.py
│   │   └── ...
│   └── ...
├── pyproject.toml          # Or requirements.txt / requirements-dev.txt
├── Dockerfile
└── ...
```

*(Note: Moving source code into a `src/` directory is recommended practice when adding tests to avoid path issues.)*

**5. Proposed Task List (Initial Phase)**

*The first two refactoring steps (Database Access and LLM Access) are essential prerequisites for implementing the initial functional smoke tests, such as verifying the note saving/retrieval flow.*

1.  **Refactor Database Access (Prerequisite 1):**
    *   Implement dependency injection for the DB engine/session in `storage/*.py` and `storage.init_db`.
    *   Update callers in `main.py`, `web_server.py`, `task_worker.py`, `processing.py` to obtain and pass the engine/session.

2.  **Refactor LLM Access (Prerequisite 2):**
    *   Create and integrate the `LLMClient` wrapper class as described in Section 3.
    *   Modify `processing.get_llm_response` to accept an `LLMClient` instance.
    *   Update the caller (`main._generate_llm_response_for_chat`) to pass the `LLMClient`.

3.  **Setup Test Environment:**
    *   Add `pytest`, `pytest-asyncio`, `testcontainers`, `psycopg2-binary` (or `asyncpg`), `httpx`, `pytest-mock` (or use `unittest.mock`) to development dependencies.
    *   Create the basic `tests/` directory structure and `tests/conftest.py`.
    *   (Optional but Recommended) Move application code into a `src/` directory if not already done.

4.  **Test Database Initialization & Storage:**
    *   Create a fixture in `conftest.py` using `testcontainers` (or in-memory SQLite initially) to provide a temporary database engine/session factory for tests.
    *   Write `tests/integration/test_storage_init.py` to call the refactored `init_db` using the test engine.
    *   Write `tests/integration/test_storage.py` to test core storage functions (notes, tasks, history, email storage/retrieval) using the test DB fixture and the refactored storage functions.

5.  **Implement Initial Smoke Test (Note Save/Retrieve):**
    *   Write a basic test in `tests/functional/test_core_logic.py` (or similar).
    *   This test will likely:
        *   Use the test DB fixture.
        *   Use a mock `LLMClient` fixture (using `pytest-mock` or `unittest.mock`) configured to:
            *   Return a response requesting the `add_or_update_note` tool when given initial input.
            *   Return a simple confirmation after the tool call.
        *   Call a core processing function (e.g., a refactored version of `_generate_llm_response_for_chat` or `get_llm_response` directly) with the necessary dependencies (mock LLM client, test DB session).
        *   Assert that the `add_or_update_note` function was called with the expected arguments (via the mock LLM response).
        *   Assert that the note exists in the test database with the correct content after the call.
        *   (Optional) Extend the test to simulate asking about the note and verify the context provided back to the (mock) LLM includes the note.

6.  **Refactor Configuration:**
    *   Implement a configuration object/dict.
    *   Remove global config access and pass the config object explicitly where needed.

7.  **Refactor MCP State & Connection:**
    *   Modify `load_mcp_config_and_connect` to return state and be skippable.
    *   Pass MCP state explicitly.

8.  **Test Processing Logic (Further):**
    *   Expand `tests/integration/test_processing.py`.
    *   Test `get_llm_response` and `execute_function_call` more thoroughly. Use the test DB fixture. Provide mock MCP state. Use the mock `LLMClient`.

9.  **Refactor Task Worker:**
    *   Inject dependencies into the loop and handlers.

10. **Test Task Worker:**
    *   Write `tests/integration/test_task_worker.py`. Test dequeuing, handler execution (e.g., `handle_llm_callback`), and task status updates using the test DB. Mock LLM/MCP interactions triggered by handlers as needed.

11. **Refactor Telegram Handlers & Core Logic:**
    *   Decouple the core processing logic from `python-telegram-bot` handlers as described above.

12. **Test Core Telegram Flow:**
    *   Write `tests/functional/test_telegram_flow.py`.
    *   Call the refactored core processing function (simulating a message event).
    *   Use the test DB fixture and the mock `LLMClient`.
    *   Assert expected database changes (e.g., message history) and inspect the returned response content.

13. **Refactor Web Server:**
    *   Implement FastAPI dependency injection (`Depends`) for DB sessions, config, etc.

14. **Test Web API:**
    *   Write `tests/functional/test_web_api.py`.
    *   Use `httpx` to make requests to the FastAPI endpoints (running against the test DB). Test CRUD operations for notes, history/task views.

15. **CI Integration:**
    *   Set up a GitHub Actions workflow (or similar) to automatically run the test suite on pushes/PRs. Ensure the workflow can run Docker for `testcontainers` if used.

**6. Future Work**

*   Integrate `mockllm` for deterministic LLM testing.
*   Explore `ptbtestsuite` if direct testing of Telegram handlers becomes necessary.
*   Add tests for MCP tool interactions using mock MCP servers.
*   Expand E2E test coverage for more complex scenarios (e.g., recurring tasks, calendar interactions once implemented).
*   Implement tests for email ingestion flow once developed.

**7. Conclusion**

This plan provides a phased approach to introducing testing, prioritizing realistic integration and functional tests. The initial focus is on refactoring for testability via dependency injection and establishing foundational tests for the database, storage layer, and core processing logic. Subsequent steps will build upon this foundation to cover Telegram and Web interfaces, eventually incorporating mock external services for more comprehensive and deterministic testing.

## o4-mini review

* The initial focus on database initialization and storage layer refactoring provides a quick win—validate this layer first to build confidence.
* Splitting configuration, LLM wrapper, and MCP state early unlocks headless testing; consider bundling those refactors right after storage to enable mock-driven development.
* Prioritize core processing tests (`get_llm_response`, `execute_function_call`) before interface layers to iterate rapidly on business logic.
* Telegram handler decoupling can be delayed until core logic is stable; basic smoke tests on the thin wrapper suffice initially.
* Web server DI and HTTP tests add value later—once chat flows work reliably, layer in FastAPI tests to round out coverage.
* Integrate CI as soon as the first round of integration tests pass to catch regressions early and maintain momentum.

## claude review

* The storage-first approach is sound, but consider creating a `DatabaseContext` class early to encapsulate connection management, retries, and transactions. This would simplify dependency injection throughout the codebase and provide a clean testing seam.
* For the LLM wrapper, implement a protocol/interface first rather than a concrete class. This allows for multiple implementations (real, mock, cached responses) without changing consumer code.
* The task queue testing deserves special attention - consider adding specific fixtures for task creation/execution that can manipulate time (for scheduled tasks) and verify recurrence rules.
* Add explicit test categories for "happy path" vs. error handling. The current design focuses on functionality but doesn't emphasize resilience testing (network failures, malformed responses, etc.).
* For a hobbyist environment, consider implementing a simple record/replay mechanism for LLM interactions early. This would allow capturing real LLM responses during development and replaying them in tests, reducing API costs while maintaining realism.
* The MCP testing strategy could be enhanced with a simple in-process mock server implementation rather than just mocking the client side. This would provide more realistic testing of the protocol interactions.
* Consider adding property-based testing for specific components (especially the task scheduler and recurrence logic) to discover edge cases that might be missed in scenario-based tests.
* For calendar integration testing, add fixtures that provide mock CalDAV/iCal servers or pre-populated response data to avoid external dependencies.
* The proposed refactoring sequence is logical, but consider creating a minimal end-to-end test first (even with all the globals) to establish a baseline before refactoring. This provides confidence that functionality is preserved throughout the refactoring process.
* Add explicit test coverage for the database retry logic, which is critical for application resilience but often overlooked in testing.

## gemini review

*   **Refactoring Scope:** The proposed dependency injection refactoring is comprehensive but potentially large for a single engineer. Consider tackling it incrementally, focusing first on the areas yielding the highest testability gains (e.g., database access, LLM client) before refactoring less critical parts like configuration or MCP state handling.
*   **Database Testing:** Using `testcontainers` with PostgreSQL is robust but adds overhead (Docker dependency, startup time). For a hobbyist project, starting with SQLite in-memory (`sqlite+aiosqlite:///:memory:`) for most integration tests might be sufficient and faster, especially given the use of SQLAlchemy which abstracts many DB differences. Reserve `testcontainers` for final validation or if PostgreSQL-specific features (like pgvector) become central to the tested logic. The `DatabaseContext` suggestion from the Claude review is valuable here for abstracting the engine.
*   **LLM Testing:** The plan to start with the real LLM and move to `mockllm` is sound. The record/replay suggestion (Claude review) is highly practical for a solo developer to capture realistic interactions without constant API calls during test development. Prioritize implementing this simple record/replay mechanism early.
*   **Telegram Testing:** Decoupling the core logic is the right approach. Avoid `ptbtestsuite` unless absolutely necessary; testing the core functions directly provides the most value for the effort.
*   **MCP Testing:** Mocking the `mcp_sessions` and `tool_name_to_server_id` state, as planned, is the most pragmatic approach initially. Avoid building mock MCP servers unless tool interactions become highly complex or error-prone.
*   **Task Queue/Worker Testing:** Testing the task queue logic against the database is essential. Focus on verifying task state transitions (pending -> processing -> done/failed/retry), correct handler dispatch based on `task_type`, and payload integrity. Testing scheduled tasks and recurrence (as noted in the Claude review) requires careful fixture design, potentially mocking `datetime.now`.
*   **Web Server Testing:** Using `httpx` against the FastAPI app is standard. Ensure tests cover not just success cases but also error responses (404s, validation errors if applicable).
*   **Incremental Value:** The proposed task list is logical. Ensure each refactoring step is accompanied by corresponding tests *before* moving to the next refactoring. This maintains a working, tested state throughout the process. The idea of a baseline E2E test first (Claude review) is excellent for ensuring refactoring doesn't break existing functionality.
*   **Property-Based Testing:** While powerful (Claude review), property-based testing might be overkill initially for a solo project unless specific complex logic (like recurrence rule parsing/generation) warrants it. Focus on scenario-based tests first.
