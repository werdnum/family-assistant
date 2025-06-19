# Testing Design Document: Family Assistant

## 1. Introduction

The goal is to introduce a robust testing suite for the Family Assistant application. The primary focus will be on realistic functional and integration tests that verify the system's behavior. A key initial goal is establishing an end-to-end smoke test for core bot functionality *before*significant refactoring, ensuring baseline functionality is preserved.

Subsequent testing and refactoring will improve testability, primarily through dependency injection, and expand coverage.

## 2. Testing Strategy & Tools

We will employ a multi-layered testing approach:

* **Integration Tests:** Verify the interactions between different components of the system (e.g., processing logic interacting with storage, task worker handling tasks). These are crucial for ensuring components work together correctly.
* **Functional / End-to-End (E2E) Tests:** Simulate user interactions and verify the overall behavior of the application from an external perspective (e.g., sending a message via a simulated interface, checking the response and database state; accessing web UI endpoints).
* **Unit Tests:** While not the primary focus, simple unit tests might be used for specific, isolated logic (e.g., utility functions, complex parsing logic) if deemed necessary.

### Chosen Tools:

* **Test Runner:** `pytest` (with `pytest-asyncio` for async code).
* **Database:**
    * **Initial Smoke Test:** Use the default database configuration (`sqlite+aiosqlite:///family_assistant.db` file) or potentially an in-memory SQLite database (`sqlite+aiosqlite:///:memory:`) directly, avoiding the need for the production PostgreSQL connection or immediate refactoring.
    * **Integration Tests:** After initial setup and LLM mocking, refactor to use dependency injection (likely via `storage.context.DatabaseContext`) and `pytest` fixtures providing an in-memory SQLite database for faster, isolated tests.
    * **Future (PostgreSQL-specific):** Use `testcontainers-python` to run PostgreSQL if/when testing features that specifically require it (e.g., vector search with `pgvector`).
* **LLM:**
    * **Initial Smoke Test:** Use the *real*configured LLM (via LiteLLM) to verify the end-to-end flow works with actual LLM interaction. Requires API keys in the test environment.
    * **Mocking/Record-Replay:** Implement mocking using `litellm.CustomLLM` (see [LiteLLM Custom Provider Docs](https://docs.litellm.ai/docs/providers/custom_llm_server)). This allows creating mock providers for deterministic responses. LiteLLM's logging features may also aid in developing a record/replay mechanism based on captured real interactions. This will be prioritized after the initial smoke test.
* **Telegram Interface:**
    * **Initial:** We will *not*directly test the `python-telegram-bot` handlers using a framework like `ptbtestsuite` initially. Instead, we will refactor the handler logic (`message_handler`, `process_chat_queue`, `_generate_llm_response_for_telegram`) into core, testable functions. Our functional tests will call these core functions directly, simulating the data that would come from a Telegram update.
    * **Future:** If more direct testing of the `python-telegram-bot` integration is needed, `ptbtestsuite` could be evaluated, but it seems potentially complex to integrate and may require significant refactoring beyond what's initially planned.
* **Web Server (FastAPI):** `httpx` (async client) to send requests to the FastAPI application running within the test setup (potentially managed by testcontainers or run directly).
* **MCP:** Direct testing of MCP server interactions is complex due to the `stdio_client` spawning external processes.
    * **Initial:** Tests will focus on the *logic within the assistant*that prepares MCP calls and handles their responses. We will refactor the code to allow injecting mock `mcp_sessions` and `tool_name_to_server_id` during tests. Actual MCP server connections will be skipped in the test environment.
    * **Future:** Mock MCP server processes could be developed if needed, potentially run via testcontainers.
* **Mocking:** `unittest.mock` (part of Python's standard library) for any necessary targeted mocking within tests.

## 3. Refactoring for Testability (Dependency Injection)

The current extensive use of global variables and direct imports of dependencies hinders testability. We need to refactor to use dependency injection:

1.  **Database Engine:**
    * Modify `storage/base.py`'s `get_engine()` to potentially return a test-specific engine if available (e.g., via context var or passed config), or remove the global engine entirely.
    * Refactor all functions within `storage/*.py` (e.g., `add_or_update_note`, `enqueue_task`, `get_recent_history`) to accept an `sqlalchemy.ext.asyncio.AsyncEngine` or `sqlalchemy.ext.asyncio.AsyncSession` object as an argument instead of relying on the global `storage.base.engine`.
    * Update `storage.init_db` to accept an `AsyncEngine` argument.
    * Update all callers of storage functions (`main.py`, `web_server.py`, `task_worker.py`, `processing.py`) to obtain and pass the engine/session.

2.  **Configuration:**
    * Eliminate global configuration variables loaded directly from `os.getenv` or `args` within modules like `main.py`, `processing.py`.
    * Create a dedicated configuration object (e.g., a Pydantic model or a simple dictionary) loaded centrally (e.g., in `main.py`'s startup).
    * Pass this configuration object explicitly to functions and classes that require configuration values (e.g., `_generate_llm_response_for_telegram`, `load_mcp_config_and_connect`, database initialization, LLM client).

3.  **LLM Client / Mocking:**
    * Refactor the code calling `litellm.acompletion` (likely in `processing.get_llm_response` or `main._generate_llm_response_for_chat`) to allow specifying a model name that can be mapped to a custom LiteLLM provider during test setup (`litellm.custom_provider_map`).
    * Implement mock providers using `litellm.CustomLLM` for deterministic testing. This avoids needing a separate wrapper class initially.

4.  **Telegram Core Logic Decoupling:**
    * Refactor `message_handler` to primarily parse the `Update` object, extract essential data (chat_id, user_id, user_name, text, photo bytes, reply_to_message_id), and potentially enqueue this data or directly call a core processing function.
    * Refactor `process_chat_queue` and `_generate_llm_response_for_telegram` into standalone, testable async functions. They should accept all necessary dependencies as arguments (e.g., `chat_id`, `trigger_content_parts`, `user_name`, `db_engine/session`, `llm_client`, `mcp_state`, `config`) and return the response content and tool call info. They should *not*depend directly on `telegram.ext.ContextTypes`.
    * The `python-telegram-bot` handlers in `main.py` will become thin wrappers around these core functions, responsible only for interacting with the Telegram API (sending actions, messages) based on the results from the core logic.

5.  **MCP State:**
    * Refactor `load_mcp_config_and_connect` to *return*the `mcp_sessions` and `tool_name_to_server_id` dictionaries instead of setting global variables.
    * The main application setup (`main_async`) will hold this state.
    * Pass this state explicitly as arguments to functions that need it (e.g., `get_llm_response`, `task_worker.set_mcp_state` should be removed in favor of passing state to the worker loop/handlers).
    * Allow skipping the actual connection logic in `load_mcp_config_and_connect` based on configuration (for tests).

6.  **Task Worker:**
    * Refactor `task_worker.task_worker_loop` to accept dependencies (DB engine, LLM generator function reference, MCP state, config, events) as arguments.
    * Remove the `task_worker.set_...` functions.
    * Task handlers (`handle_llm_callback`, etc.) should ideally also receive necessary dependencies via the loop or a context object, rather than relying on globals or module-level state.

7.  **Web Server (FastAPI):**
    * Leverage FastAPI's built-in dependency injection (`Depends`).
    * Create dependency provider functions (e.g., `get_db_session`, `get_config`) that can be easily overridden in tests.
    * Endpoint functions (`save_note`, `view_message_history`, etc.) will declare their dependencies using `Depends`, receiving sessions, config, etc., automatically.

## 4. Test Structure

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

## 5. Proposed Task List (Revised Initial Phase)

This revised list prioritizes establishing a baseline test and LLM mocking before major database refactoring.

1.  **Implement Baseline Smoke Test (Note Save/Retrieve):**
    * Write a basic functional test (e.g., `tests/functional/test_smoke_notes.py`).
    * This test will run against the *current*codebase structure with minimal changes.

    * **Database:** Use the default database URL (`sqlite+aiosqlite:///family_assistant.db`) or modify `storage/base.py` slightly to use an in-memory DB (`sqlite+aiosqlite:///:memory:`) if easier for cleanup. Ensure `storage.init_db()` is called at the start of the test run (perhaps via a fixture).
    * **LLM:** Use the *real*configured LLM via LiteLLM. Requires API keys in the test environment.
    * **Logic:** Call the necessary functions to simulate a user adding a note (e.g., "Test Note Smoke [timestamp]") and then asking about it. This might involve directly calling parts of `main._generate_llm_response_for_chat` or related functions.
    * **Assertions:** Verify the note is created in the database and that the subsequent LLM interaction shows awareness of the note (either in the final response or the context sent to the LLM).
    * *Goal:* Establish a working end-to-end test before refactoring.

2.  **Setup Test Environment:**
    * Add necessary development dependencies: `pytest`, `pytest-asyncio`, `aiosqlite` (if not already present), `httpx`, `pytest-mock` (or use `unittest.mock`).
    * Create the basic `tests/` directory structure (`functional`, `integration`, `unit`, `mocks`).
    * Create `tests/conftest.py` for future fixtures.
    * (Optional but Recommended) Move application code into a `src/` directory if not already done.

3.  **Implement Basic LLM Mocking:**
    * Investigate and implement a mock LLM provider using `litellm.CustomLLM` (e.g., in `tests/mocks/mock_llm_provider.py`). This provider should return predefined responses suitable for testing the note-saving flow without real API calls.
    * Refactor the LLM call site (e.g., in `processing.get_llm_response`) to allow selecting the mock provider via a specific model name (e.g., `mock/test-completion-model`).
    * Configure `litellm.custom_provider_map` during test setup (likely in `conftest.py` or test modules) to register the mock provider.
    * Update the smoke test (or create new tests) to use the mock LLM provider, removing the dependency on real API keys for most tests going forward. Consider leveraging LiteLLM's logging for potential record/replay later.

4.  **Refactor Database Access & Add SQLite Fixtures:**
    * Refactor `storage/*.py` functions and `storage.init_db` to accept a `storage.context.DatabaseContext` instance instead of relying on the global engine. This encapsulates connection/session handling and retry logic.
    * Create fixtures in `conftest.py` for an in-memory SQLite engine (`test_engine`) and a `DatabaseContext` factory (`test_db_context`) using that engine (similar to `storage/testing.py`).
    * Update callers (`main.py`, `web_server.py`, `task_worker.py`, `processing.py`) to obtain and pass the `DatabaseContext`. This is a significant refactoring step.

5.  **Test Database Initialization & Storage Layer:**
    * Write `tests/integration/test_storage_init.py` to call the refactored `init_db` using the `test_engine` fixture.
    * Write `tests/integration/test_storage.py` to test core storage functions (notes, tasks, history, etc.) using the `test_db_context` fixture and the refactored storage functions. Ensure retry logic within `DatabaseContext` is testable (might require mocking `asyncio.sleep`).

6.  **Refactor Configuration:**
    * Implement a configuration object/dict.
    * Remove global config access and pass the config object explicitly where needed. Test configuration loading.

7.  **Refactor MCP State & Connection:**
    * Modify `load_mcp_config_and_connect` to return state and be skippable.
    * Pass MCP state explicitly. Add tests mocking MCP state/interactions.

8.  **Test Processing Logic (Further):**
    * Expand `tests/integration/test_processing.py`. Test `get_llm_response` and `execute_function_call` thoroughly using the `test_db_context` fixture, mock LLM provider, and mock MCP state.

9.  **Refactor Task Worker:**
    * Inject dependencies (`DatabaseContext`, config, mock LLM/MCP if needed) into the loop and handlers.

10. **Test Task Worker:**
    * Write `tests/integration/test_task_worker.py`. Test dequeuing, handler execution, and task status updates using `test_db_context` and mocks.

11. **Refactor Telegram Handlers & Core Logic:**
    * Decouple the core processing logic from `python-telegram-bot` handlers.

12. **Test Core Telegram Flow:**
    * Write `tests/functional/test_telegram_flow.py`. Call the refactored core processing function using `test_db_context` and mock LLM/MCP. Assert DB changes and response content.

13. **Refactor Web Server:**
    * Implement FastAPI dependency injection (`Depends`) for `DatabaseContext`, config, etc.

14. **Test Web API:**
    * Write `tests/functional/test_web_api.py` using `httpx` against the FastAPI app (configured with test dependencies).

15. **CI Integration:**
    * Set up GitHub Actions (or similar) to run `pytest`.

## 6. Future Work

* Integrate `mockllm` for deterministic LLM testing.
* Explore `ptbtestsuite` if direct testing of Telegram handlers becomes necessary.
* Add tests for MCP tool interactions using mock MCP servers.
* Expand E2E test coverage for more complex scenarios (e.g., recurring tasks, calendar interactions once implemented).
* Implement tests for email ingestion flow once developed.

## 7. Conclusion

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

* **Refactoring Scope:** The proposed dependency injection refactoring is comprehensive but potentially large for a single engineer. Consider tackling it incrementally, focusing first on the areas yielding the highest testability gains (e.g., database access, LLM client) before refactoring less critical parts like configuration or MCP state handling.
* **Database Testing:** Using `testcontainers` with PostgreSQL is robust but adds overhead (Docker dependency, startup time). For a hobbyist project, starting with SQLite in-memory (`sqlite+aiosqlite:///:memory:`) for most integration tests might be sufficient and faster, especially given the use of SQLAlchemy which abstracts many DB differences. Reserve `testcontainers` for final validation or if PostgreSQL-specific features (like pgvector) become central to the tested logic. The `DatabaseContext` suggestion from the Claude review is valuable here for abstracting the engine.
* **LLM Testing:** The plan to start with the real LLM and move to `mockllm` is sound. The record/replay suggestion (Claude review) is highly practical for a solo developer to capture realistic interactions without constant API calls during test development. Prioritize implementing this simple record/replay mechanism early.
* **Telegram Testing:** Decoupling the core logic is the right approach. Avoid `ptbtestsuite` unless absolutely necessary; testing the core functions directly provides the most value for the effort.
* **MCP Testing:** Mocking the `mcp_sessions` and `tool_name_to_server_id` state, as planned, is the most pragmatic approach initially. Avoid building mock MCP servers unless tool interactions become highly complex or error-prone.
* **Task Queue/Worker Testing:** Testing the task queue logic against the database is essential. Focus on verifying task state transitions (pending -> processing -> done/failed/retry), correct handler dispatch based on `task_type`, and payload integrity. Testing scheduled tasks and recurrence (as noted in the Claude review) requires careful fixture design, potentially mocking `datetime.now`.
* **Web Server Testing:** Using `httpx` against the FastAPI app is standard. Ensure tests cover not just success cases but also error responses (404s, validation errors if applicable).
* **Incremental Value:** The proposed task list is logical. Ensure each refactoring step is accompanied by corresponding tests *before*moving to the next refactoring. This maintains a working, tested state throughout the process. The idea of a baseline E2E test first (Claude review) is excellent for ensuring refactoring doesn't break existing functionality.
* **Property-Based Testing:** While powerful (Claude review), property-based testing might be overkill initially for a solo project unless specific complex logic (like recurrence rule parsing/generation) warrants it. Focus on scenario-based tests first.

## 8. Human Feedback & Practical Considerations

Based on developer feedback, the following practical considerations and priorities will guide implementation:

* **Initial Focus:** Implement a basic end-to-end smoke test (e.g., create/retrieve note) with minimal initial refactoring to establish a baseline and ensure core functionality isn't broken during subsequent changes.
* **Testing Trigger:** Integrate test execution into the pre-push workflow (e.g., via a `.build` script command) rather than setting up full CI initially.
* **LLM Mocking:** While `litellm` acts as an LLM abstraction layer, investigate creating a custom `litellm` provider (using `litellm.CustomLLM`, see [LiteLLM Custom Provider Docs](https://docs.litellm.ai/docs/providers/custom_llm_server)) for implementing mock responses or a record/replay mechanism. This seems preferable to a separate wrapper class if `litellm`'s built-in features are insufficient.
* **MCP Testing:** Low priority. If needed, create a minimal fake MCP server (e.g., echo/reverse input) to verify the basic interaction pattern rather than complex mocking.
* **Calendar Testing:** Lower priority. When implemented, testing can use a simple fake CalDAV server backed by static files.
* **Database Strategy:** Use SQLite (`sqlite+aiosqlite:///:memory:`) for faster initial tests where possible, ensuring graceful degradation if PostgreSQL-specific features are unavailable. However, tests for features like vector search will eventually require PostgreSQL (likely via `testcontainers`). Include tests for database retry logic.
* **Core Logic Isolation:** Prioritize refactoring and testing the core message processing logic (potentially the existing `processing` module functions). Consider exposing this core logic via a simple REST endpoint for easier manual testing and potential alternative integrations.
* **Test Scope & Goal:** Focus on integration and functional tests that prevent regressions and obvious breakages in key user flows. Aim for practical confidence rather than exhaustive 100% code coverage, aligning with the project's hobbyist nature.
