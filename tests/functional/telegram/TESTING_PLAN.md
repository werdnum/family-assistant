# Testing Plan: TelegramUpdateHandler End-to-End Tests

## 1. Goal

To verify the correct end-to-end behavior of the `TelegramUpdateHandler` class, ensuring it integrates correctly with the `ProcessingService` and the database layer (`storage`) under various user input scenarios, while isolating external network dependencies (Telegram API, real LLM).

## 2. Scope

*   **Under Test:** `family_assistant.telegram_bot.TelegramUpdateHandler`
*   **Real Dependencies:**
    *   `family_assistant.processing.ProcessingService` (and its dependencies like `ToolsProvider`, `prompts`, `config`, etc.)
    *   Database connection and interaction logic (`family_assistant.storage.*` modules via `DatabaseContext`). Tests will run against a dedicated test database instance (e.g., PostgreSQL via testcontainers or SQLite).
*   **Mocked Dependencies:**
    *   **Telegram API:** `telegram.ext.Application`, `telegram.Bot`, `telegram.Update`, `telegram.ext.ContextTypes.DEFAULT_TYPE`. Bot API calls (`send_message`, `send_chat_action`, etc.) will be mocked to verify interaction attempts and simulate realistic return values (e.g., mock `Message` objects).
    *   **LLM:** The `LLMInterface` implementation used by the real `ProcessingService` instance will be replaced with a mock (e.g., `RuleBasedMockLLMClient`, `PlaybackLLMClient`, `unittest.mock.AsyncMock`). The mock LLM will provide controlled outputs (text, tool calls, errors) to drive specific test scenarios.
    *   **`MessageBatcher`:** The `MessageBatcher` interface injected into the handler can be the real `NoBatchMessageBatcher` for simpler E2E tests, or a mocked one if testing the handler's interaction *with* the batcher is desired (less common for E2E).
    *   **(Minimal) `TelegramService`:** The `TelegramService` instance passed to the handler can often be a simple `unittest.mock.Mock` as its direct usage by the handler is minimal (mainly for error reporting).
## 3. Test Environment & Fixtures (`pytest`)

*   **Database Fixture:** Leverage existing `pytest` fixtures (e.g., `pg_vector_db_engine` from `tests/conftest.py`) that provide an initialized, ephemeral test database (`AsyncEngine`).
*   **`get_db_context_func`:** A test-specific helper or fixture that takes the test `AsyncEngine` and returns a function. When called, this function provides an `asynccontextmanager` yielding a `DatabaseContext` connected to the test database.
*   **Mock LLM Fixture:** Fixtures or setup code to instantiate and configure the chosen mock LLM client for each test scenario.
*   **Real `ProcessingService` Instance:** Test setup will instantiate the real `ProcessingService`, injecting the mock LLM client and ensuring other dependencies (like `ToolsProvider`) are configured to use the test database engine (via the patched `storage.base.engine` from `conftest.py`).
*   **Mock Telegram Objects Factory:** Helper functions or fixtures to create mock `Update` objects representing different user inputs (text, photo, reply, forward, callback query) and corresponding mock `Context` objects with the mock `Bot` attached.
*   **Mock `Bot` Configuration:** Setup code to configure the `AsyncMock` representing `telegram.Bot` with necessary mocked methods (`send_message`, `send_chat_action`, `edit_message_text`, etc.) and appropriate side effects (e.g., returning mock `Message` objects with IDs).

## 4. Test Structure (Typical Test Case)

1.  **Arrange:**
    *   Depend on the database fixture (`pg_vector_db_engine`).
    *   Depend on the **default database fixture (`test_db_engine`)**.
    *   Instantiate the real `ProcessingService` with the mock LLM.
    *   Instantiate mock `Application` and `Bot`, configuring the `Bot`'s mocked methods.
    *   Instantiate the chosen `MessageBatcher` implementation (e.g., `NoBatchMessageBatcher` for simplicity, passing the handler instance) and assign it to `handler.message_batcher`.
    *   Instantiate the `TelegramUpdateHandler` with the real `ProcessingService`, the `DatabaseContext` provider function (derived from the fixture), the chosen `MessageBatcher`, and mocked Telegram components.
    *   Create the specific mock `Update` and `Context` representing the user input for the scenario.
2.  **Act:**
    *   Call the relevant handler method (e.g., `await handler.message_handler(mock_update, mock_context)`).
    *   **Crucially:** If testing with `DefaultMessageBatcher`, wait for the processing task (`batcher.processing_tasks`) to complete. If using `NoBatchMessageBatcher`, the call to `handler.message_handler` will block until `handler.process_batch` completes.
3.  **Assert:**
    *   **Database State (Primary):** Use the `get_test_db_context_func` to query the test database and verify the expected state changes in relevant tables (`message_history`, `notes`, `tasks`, etc.). Check message content, roles, linkage (`turn_id`, `thread_root_id`), and updated fields (`interface_message_id`, `error_traceback`).
    *   **Bot API Calls (Primary):** Use `mock_bot.method.assert_called_with(...)` or similar assertions to verify that the handler produced the correct **user-facing output** via the Telegram API. Check the content, formatting (parse mode), reply status, and any interactive elements (keyboards) of messages sent or edited by the bot.
    *   **Mock LLM Input (Primary):** Verify that the `ProcessingService` made the expected call(s) to the mock LLM client. **Crucially, inspect the `messages` list passed to the mock LLM** to ensure the correct system prompt, user input, and **formatted message history** (including previous user messages, assistant responses, and tool interactions) were included. This indirectly verifies that history was stored and retrieved correctly.
    *   **(Optional/Debugging) Mock LLM Calls:** Verify that the `ProcessingService` made the expected call to the mock LLM based on the input. This is mainly for debugging test failures.
    *   **(Optional/Debugging) `MessageBatcher` State:** If testing `DefaultMessageBatcher`, check internal state like `message_buffers` or `processing_tasks` if needed.
## 5. Key Scenarios to Test

*   **Basic Interaction:** Simple text message -> Verify mock Bot sent the LLM's text response. -> Send another message -> Verify mock LLM received history including the first exchange.
*   **Photo Message:** Message with photo -> LLM response (verify image data passed to `ProcessingService`).
*   **Message Batching:** Send multiple messages quickly -> Verify they are processed as one batch.
*   **Tool Usage (Simulated):**
    *   User message -> Mock LLM requests `add_or_update_note` -> Verify `ProcessingService` executes tool -> **Verify confirmation message sent via mock Bot.** -> **Follow-up:** Ask bot about the note -> Mock LLM expects note content in context / provides it -> Verify bot responds with correct content.
    *   User message -> Mock LLM requests `schedule_future_callback` -> Verify `ProcessingService` executes tool -> **Verify confirmation message sent via mock Bot.** (Task creation not directly verifiable via bot API or standard LLM context).
*   **Reply Context:** User replies to a previous message -> Verify `replied_to_interface_id` is passed to `ProcessingService` -> Verify mock LLM receives history including the replied-to message and its context -> Verify bot's response is sent as a reply to the correct user message (`reply_to_message_id` in `send_message` call).
*   **Error Handling:**
    *   Simulate error during `ProcessingService.generate_llm_response_for_chat` (via mock LLM or by mocking a tool execution to raise error) -> Verify error message sent via mock Bot. -> Send subsequent message -> Verify mock LLM does *not* receive the traceback from the failed turn in its history context (unless designed to).
    *   Simulate error sending message via mock Bot -> Verify error is logged and potentially reported via `error_handler`.
*   **Confirmation Flow (if not refactored out):**
    *   User message -> Mock LLM requires confirmation for a tool -> Verify handler calls `_request_confirmation_impl` -> Verify mock Bot sent message with keyboard.
    *   Simulate user clicking "Confirm" via mock `CallbackQuery` update -> Verify `confirmation_callback_handler` is called -> Verify mock Bot message is edited -> Verify tool execution proceeds (check subsequent LLM calls or bot interactions).
    *   Simulate user clicking "Cancel" -> Verify mock Bot message is edited -> Verify tool execution is skipped (check subsequent LLM calls or bot interactions).
    *   Simulate confirmation timeout -> Verify mock Bot message is edited -> Verify tool execution is skipped (check subsequent LLM calls or bot interactions).
*   **Authorization:** Message from unauthorized user ID -> Verify it's ignored (no processing, no DB changes).
*   **/start command:** Verify welcome message is sent.

## 6. Assertions
*   **Primary (Output):** Focus on the **calls made to the mocked `telegram.Bot` API**. Ensure the handler produced the correct user-facing output. Check key parameters like `chat_id`, `text` (content and formatting), `reply_to_message_id`, `parse_mode`, `reply_markup`.
*   **Primary (History Context):** Focus on the **`messages` argument passed to the mocked `LLMInterface`**. Verify that the correct system prompt, user input, and formatted message history (including roles, content, tool calls/responses) were provided as context for the LLM's generation. This validates the storage and retrieval logic indirectly.
## 7. Prerequisites/Assumptions
*   Reliable `pytest` fixtures exist for setting up and tearing down a test database instance (`AsyncEngine`), defaulting to SQLite (`test_db_engine`).
## 8. Potential Future Improvements (Refactoring)

The following refactoring could simplify these tests further:

*   **Extract `MessageBatcher`:** Done. This simplifies testing the core processing logic (`process_batch`) by allowing direct invocation. Testing `DefaultMessageBatcher` itself might require separate, more focused tests. Using `NoBatchMessageBatcher` in E2E tests makes them simpler.
*   **Extract `ConfirmationUIManager`:** Drastically simplifies testing confirmation flows by mocking the manager instead of the detailed `Bot` interactions and `Future` management.
## 9. Implementation Tasks

1.  **Refactor `MessageBatcher`:**
    *   Define a `BatchProcessor` protocol with a method like `async process_batch(chat_id: int, batch: List[Tuple[Update, Optional[bytes]]], context: ContextTypes.DEFAULT_TYPE) -> None`.
    *   Implement this protocol in `TelegramUpdateHandler`.
    *   Create a `MessageBatcher` class that takes a `BatchProcessor` instance during initialization.
    *   The `MessageBatcher` will manage the `chat_locks`, `message_buffers`, and `processing_tasks`. Its `add_to_batch` method will handle adding updates and triggering the `process_batch` method on the `BatchProcessor` via `asyncio.create_task`.
    *   Update `TelegramUpdateHandler.__init__` to accept a `MessageBatcher`. Update `TelegramService` to instantiate and inject it.
    *   Update `TelegramUpdateHandler.message_handler` to delegate buffering and task creation to `MessageBatcher.add_to_batch`.
    *   *(Optional):* Create a `NoBatchMessageBatcher` implementation that immediately calls `process_batch` for simpler testing setups if needed.
2.  **Refactor `ConfirmationUIManager`:**
    *   Define a `ConfirmationUIManager` protocol/interface with methods like `request_confirmation(chat_id: int, prompt_text: str, tool_name: str, tool_args: Dict[str, Any], timeout: float) -> bool`.
    *   Move the `pending_confirmations`, `confirmation_timeout`, `_request_confirmation_impl`, and `confirmation_callback_handler` logic from `TelegramUpdateHandler` into a concrete `TelegramConfirmationUIManager` class that implements the protocol. This class will need access to the `telegram.ext.Application` instance.
    *   Inject an instance of the `ConfirmationUIManager` protocol into `TelegramUpdateHandler` during initialization.
    *   Update `ProcessingService` or the `ConfirmingToolsProvider` callback mechanism to call the injected `confirmation_manager.request_confirmation`.
    *   Ensure the `TelegramConfirmationUIManager` registers the `confirmation_callback_handler` with the Telegram `Application`.
3.  **Create Test Fixture for `TelegramUpdateHandler`:**
    *   In `tests/functional/telegram/conftest.py` (or a dedicated test file), create a `pytest` fixture (e.g., `telegram_update_handler_fixture`).
    *   This fixture will depend on the default database fixture (`test_db_engine`), mock LLM fixtures, etc.
    *   It will instantiate mock `Application`, `Bot`, and potentially a mock `ConfirmationUIManager`.
    *   It will instantiate the real `ProcessingService` (with mock LLM).
    *   It will instantiate the `TelegramUpdateHandler` with all real and mocked dependencies injected.
    *   The fixture should yield the handler instance and potentially the mock objects (Bot, LLM, ConfirmationManager) for assertions.
4.  **Write Initial Test Case:**
    *   Create `tests/functional/telegram/test_telegram_handler.py`.
    *   Write a test function (e.g., `test_simple_text_message`) that uses the `telegram_update_handler_fixture`.
    *   Define the mock LLM response for a simple text input.
    *   Create mock `Update` and `Context` objects for a basic text message.
    *   Call `handler.message_handler(update, context)`.
    *   Wait for processing to complete (using logic derived from the `MessageBatcher`).
    *   Assert that the mock `Bot.send_message` was called with the expected text and parameters (Primary Output Assertion).
    *   Assert that the mock LLM received the expected input `messages` list (Primary History Context Assertion).
```
