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
    *   **(Minimal) `TelegramService`:** The `TelegramService` instance passed to the handler can be a simple `unittest.mock.Mock` as its direct usage by the handler is minimal (mainly for error reporting).

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
    *   Create the `get_test_db_context_func` linked to the test engine.
    *   Instantiate and configure the mock LLM client for the specific scenario (e.g., return text, request a specific tool call).
    *   Instantiate the real `ProcessingService` with the mock LLM.
    *   Instantiate mock `Application` and `Bot`, configuring the `Bot`'s mocked methods.
    *   Instantiate the `TelegramUpdateHandler` with the real `ProcessingService`, test `get_db_context_func`, and mocked Telegram components.
    *   Create the specific mock `Update` and `Context` representing the user input for the scenario.
2.  **Act:**
    *   Call the relevant handler method (e.g., `await handler.message_handler(mock_update, mock_context)`).
    *   **Crucially:** Implement logic to wait for the background `process_chat_queue` task spawned by `message_handler` to complete (e.g., polling `handler.processing_tasks`, using `asyncio.wait_for`).
3.  **Assert:**
    *   **Database State (Primary):** Use the `get_test_db_context_func` to query the test database and verify the expected state changes in relevant tables (`message_history`, `notes`, `tasks`, etc.). Check message content, roles, linkage (`turn_id`, `thread_root_id`), and updated fields (`interface_message_id`, `error_traceback`).
    *   **Bot API Calls (Primary):** Use `mock_bot.method.assert_called_with(...)` or similar assertions to verify that the handler attempted the correct interactions with the Telegram API. Check the content, formatting (parse mode), reply status, and any interactive elements (keyboards) of messages sent or edited by the bot. This verifies the user-facing behavior.
    *   **Database State (Secondary/Inferential):** Where possible, use subsequent interactions with the bot (as part of the same or a follow-up test) to infer database state changes (e.g., ask the bot to retrieve a note after creating it). Only perform direct database queries using `get_test_db_context_func` if the state change cannot be reasonably verified through bot interaction (e.g., checking internal `turn_id` linkage, specific task parameters, or error tracebacks not exposed to the user).
    *   **Mock LLM Calls (Optional/Debug):** Verify that the `ProcessingService` made the expected call to the mock LLM based on the input.
    *   **Mock LLM Calls (Optional/Debugging):** Verify that the `ProcessingService` made the expected call to the mock LLM based on the input. This is mainly for debugging test failures.

## 5. Key Scenarios to Test

*   **Basic Interaction:** Simple text message -> LLM text response.
*   **Photo Message:** Message with photo -> LLM response (verify image data passed to `ProcessingService`).
*   **Message Batching:** Send multiple messages quickly -> Verify they are processed as one batch.
*   **Tool Usage (Simulated):**
    *   User message -> Mock LLM requests `add_or_update_note` -> Verify `ProcessingService` executes tool -> Verify `notes` table in DB reflects the change -> Verify final confirmation message sent via mock Bot.
    *   User message -> Mock LLM requests `add_or_update_note` -> Verify `ProcessingService` executes tool -> **Verify confirmation message sent via mock Bot.** -> **Follow-up:** Ask bot about the note -> Verify bot responds with correct content. (Direct DB check only if necessary).
    *   User message -> Mock LLM requests `schedule_future_callback` -> Verify `ProcessingService` executes tool -> Verify `tasks` table in DB contains the scheduled task.
*   **Reply Context:** User replies to a previous message -> Verify `replied_to_interface_id` is passed to `ProcessingService` -> Verify `thread_root_id` is correctly determined and stored in `message_history`.
*   **Reply Context:** User replies to a previous message -> Verify `replied_to_interface_id` is passed to `ProcessingService` -> Verify bot's response is sent as a reply to the correct user message (`reply_to_message_id` in `send_message` call). (DB check for `thread_root_id` secondary).
*   **Error Handling:**
    *   Simulate error during `ProcessingService.generate_llm_response_for_chat` (via mock LLM or by mocking a tool execution to raise error) -> Verify error message sent via mock Bot -> Verify `error_traceback` stored in `message_history`.
    *   Simulate error sending message via mock Bot -> Verify error is logged and potentially reported via `error_handler`.
*   **Confirmation Flow (if not refactored out):**
    *   User message -> Mock LLM requires confirmation for a tool -> Verify handler calls `_request_confirmation_impl` -> Verify mock Bot sent message with keyboard.
    *   Simulate user clicking "Confirm" via mock `CallbackQuery` update -> Verify `confirmation_callback_handler` is called -> Verify mock Bot message is edited -> Verify tool execution proceeds (check DB state).
    *   Simulate user clicking "Cancel" -> Verify mock Bot message is edited -> Verify tool execution is skipped.
    *   Simulate confirmation timeout -> Verify mock Bot message is edited -> Verify tool execution is skipped.
*   **Authorization:** Message from unauthorized user ID -> Verify it's ignored (no processing, no DB changes).
*   **/start command:** Verify welcome message is sent.

## 6. Assertions

*   **Primary:** Focus on the state of the **test database** after the handler has processed the update. This reflects the actual outcome of the integrated system (Handler + ProcessingService + Storage).
    *   `message_history`: Correct number of rows, correct `role`, `content`, `interface_type`, `conversation_id`, `turn_id` linkage, `thread_root_id` propagation, `interface_message_id` update on final message, `tool_calls`/`tool_call_id`, `error_traceback`.
    *   `notes`: Rows created/updated/deleted based on simulated tool calls.
    *   `tasks`: Rows created based on simulated tool calls.
*   **Secondary:** Verify calls made to the **mocked `telegram.Bot` API**. Ensure the handler *attempted* to communicate correctly (send messages, actions, edit messages). Check key parameters like `chat_id`, `text`, `reply_to_message_id`, `parse_mode`, `reply_markup`.
*   **Tertiary:** Check calls made to the **mocked `LLMInterface`**. Useful for debugging and ensuring the `ProcessingService` received the correct input and context from the handler.

## 7. Prerequisites/Assumptions

*   Reliable `pytest` fixtures exist for setting up and tearing down a test database instance (`AsyncEngine`).
*   A method exists to obtain an `asynccontextmanager` function (`get_db_context_func`) that yields a `DatabaseContext` connected to the test database engine.
*   The test environment can instantiate the real `ProcessingService` and its dependencies (like `ToolsProvider`), potentially loading configuration and prompts.
*   A suitable mock LLM client implementation is available (`RuleBasedMockLLMClient`, `PlaybackLLMClient`, or standard mocks).

## 8. Potential Future Improvements (Refactoring)

As discussed previously, the following refactorings could simplify these tests further, although the current plan is feasible without them:

*   **Extract `MessageBatcher`:** Simplifies testing the core processing logic by allowing direct invocation of `process_chat_queue` with a prepared batch, removing the need to test the complex batching state machine within these E2E tests.
*   **Extract `ConfirmationUIManager`:** Drastically simplifies testing confirmation flows by mocking the manager instead of the detailed `Bot` interactions and `Future` management.
```
