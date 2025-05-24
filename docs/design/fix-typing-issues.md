Okay, this is a substantial list of type errors! Let's break them down systematically.

Based on the errors, we can group them into logical steps to address common causes and progressively improve type safety.

## Summary of Type Errors and Fixes

Here's a breakdown of the issues based on the latest Mypy output and the proposed fixes, grouped into incremental steps:

**Step 1: Add Missing Type Stubs and Address Import-Related Errors**

*   **Common Cause:** Libraries missing type stubs or `py.typed` markers, issues with conditional imports, or incorrect import paths.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/tools/schema.py`:
        *   L12, L15: `Skipping analyzing "json_schema_for_humans..." [import-untyped]`.
        *   **Fix:** Add `# type: ignore[import-untyped]` to the import lines for `json_schema_for_humans.generate` and `json_schema_for_humans.generation_configuration` if `types-json-schema-for-humans` is not available or effective.
    *   `src/family_assistant/indexing/ingestion.py`:
        *   L10: `Skipping analyzing "filetype" [import-untyped]`.
        *   **Fix:** Add `types-filetype` to `pyproject.toml` dev dependencies. If not available/effective, use `# type: ignore[import-untyped]` for the `filetype` import.
    *   `src/family_assistant/utils/scraping.py`:
        *   L20: `Cannot find implementation or library stub for module named "playwright.async_api" [import-not-found]`.
        *   **Fix:** Ensure Playwright is installed and the import `from playwright.async_api import async_playwright` (or similar) is correct. Playwright ships its own types.
        *   L157: `Item "None" of "MarkItDown | None" has no attribute "convert_stream" [union-attr]`.
        *   **Fix:** Ensure `MarkItDownType` (or equivalent conditional import variable for `MarkItDown`) is checked for `None` before use, e.g., `if MarkItDownType is not None: MarkItDownType.convert_stream(...)`.
    *   `src/family_assistant/embeddings.py`:
        *   L21: `Cannot assign to a type [misc]`, `Incompatible types in assignment (expression has type "None", variable has type "type[SentenceTransformer]") [assignment]`.
        *   L22: `Incompatible types in assignment (expression has type "None", variable has type Module) [assignment]`.
        *   **Fix:** For conditional imports of `SentenceTransformer` and `torch.nn.Module`, ensure type hints for placeholder variables are `Optional[Type[ActualClass]]`, e.g., `SentenceTransformerClass: Optional[Type[SentenceTransformer]] = None`.
        *   L359: `Name "SentenceTransformerEmbeddingGenerator" already defined on line 255 [no-redef]`.
        *   **Fix:** Ensure `SentenceTransformerEmbeddingGenerator` is defined only once within its conditional scope (`if SENTENCE_TRANSFORMERS_AVAILABLE:`). Remove any duplicate definitions or re-imports.
    *   `src/family_assistant/calendar_integration.py`:
        *   L100, L208, L955: `Module has no attribute "readComponents" [attr-defined]` (from `vobject`).
        *   **Fix:** Ensure `types-vobject` is installed and effective. If stubs are incomplete, consider `cast(Any, ics_data).readComponents()` or `# type: ignore[attr-defined]` on the problematic lines.
    *   `tests/conftest.py`:
        *   L7: `Library stubs not installed for "docker" [import-untyped]`.
        *   **Fix:** Add `types-docker` to dev dependencies.
        *   L11: `Skipping analyzing "testcontainers.postgres" [import-untyped]`.
        *   **Fix:** Check if `types-testcontainers` or similar exist. If not, add `# type: ignore[import-untyped]` to the import.
    *   Test files using `assertpy` (e.g., `tests/unit/indexing/processors/test_network_processors.py:11`, `tests/functional/telegram/test_telegram_send_message_tool.py:10`, etc.):
        *   Error: `Library stubs not installed for "assertpy" [import-untyped]`.
        *   **Fix:** Add `types-assertpy` to dev dependencies.
    *   Test files using `telegramify_markdown` (e.g., `tests/functional/telegram/test_telegram_send_message_tool.py:9`):
        *   Error: `Skipping analyzing "telegramify_markdown" [import-untyped]`.
        *   **Fix:** Add `# type: ignore[import-untyped]` if no stubs package exists.

**Step 2: Refactor SQLAlchemy Usage and Database Interaction**

*   **Common Cause:** Mismatches between SQLAlchemy's return types (`RowMapping`, `Result`) and their usage (expecting `dict`, direct attribute access), incorrect SQLAlchemy constructs, or missing type annotations.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/storage/context.py`:
        *   L65: `Incompatible types in assignment (_AsyncGeneratorContextManager vs AsyncTransaction | None) [assignment]`.
        *   L67: `"None" has no attribute "__aenter__" [attr-defined]`.
        *   **Fix:** `self._transaction = await self._connection.begin()` should assign an `AsyncTransaction`. The `async with self._transaction:` block is incorrect if `_transaction` is not an async context manager itself. Manage commit/rollback in `__aexit__` based on `self._transaction`.
        *   L169: `List comprehension has incompatible type List[RowMapping]; expected List[dict[str, Any]] [misc]`.
        *   **Fix:** Convert `RowMapping` to `dict`: `[dict(row) for row in (await self._session.execute(query, params)).mappings().all()]`.
        *   L186: `Incompatible return value type (got "RowMapping | None", expected "dict[str, Any] | None") [return-value]`.
        *   **Fix:** Convert `RowMapping` to `dict`: `row = (await ...).mappings().one_or_none(); return dict(row) if row else None`.
        *   L206: `Item "None" of "AsyncConnection | None" has no attribute "sync_connection" [union-attr]`.
        *   **Fix:** Add `if self._connection and self._connection.is_active:` before accessing `self._connection.sync_connection`.
    *   `src/family_assistant/storage/vector_search.py`:
        *   Multiple `Incompatible types in assignment (..., target has type "int")` for `filters` dict (e.g., L109, L112, L115, L118, L135, L144, L177, L178, L207, L253).
        *   **Fix:** Change `filters: dict[str, int]` to `filters: dict[str, Any] = {}`.
    *   `src/family_assistant/storage/tasks.py`:
        *   L155, L168: `Item "None" of "AsyncConnection | None" has no attribute "execute" [union-attr]`.
        *   **Fix:** Ensure `self._connection` is not `None` when `execute_with_retry` is called (likely an issue in `DatabaseContext` logic if connection can be `None` there).
        *   L173: `Incompatible return value type (got "RowMapping | Any", expected "dict[str, Any] | None") [return-value]`.
        *   **Fix:** Convert `RowMapping` to `dict` if a single row is fetched and returned.
        *   L214, L256, L327, L343: `"Result[Any]" has no attribute "rowcount" [attr-defined]`.
        *   **Fix:** `Result` from DML (UPDATE, INSERT, DELETE) should have `rowcount`. Ensure the variable is indeed a `Result` object from such an operation.
        *   L296-L329 (multiple `dict[str, Any]" has no attribute "status/retry_count/max_retries"`):
        *   **Fix:** Access dictionary items using `task_details["status"]` or `task_details.get("retry_count")`, not attribute access.
    *   `src/family_assistant/storage/vector.py`:
        *   L261, L262: `"Result[Any]" has no attribute "inserted_primary_key" [attr-defined]`.
        *   **Fix:** `Result` from an INSERT should have this. Ensure `result` is the direct object. Use `pk = result.inserted_primary_key; doc_id = pk[0] if pk else None`.
        *   L283: `Incompatible types in assignment (ReturningInsert vs Insert) [assignment]`.
        *   **Fix:** Broaden type of `stmt` to `Union[Insert, ReturningInsert]` or `ClauseElement`, or rely on type inference if `stmt` is not re-assigned before `.returning()`.
        *   L524, L768: `"Result[Any]" has no attribute "rowcount" [attr-defined]`. (Same as in `tasks.py`)
        *   L658, L717, L718, L724: `Argument ... has incompatible type "bool"; expected "ColumnElement[bool]..." [arg-type]`.
        *   **Fix:** Use SQLAlchemy expressions like `my_table.c.col == True` or `sqlalchemy.sql.expression.true()` instead of Python `True`/`False` directly in `where` or `or_` clauses.
    *   `src/family_assistant/storage/notes.py`:
        *   L142, L168: `"Result[Any]" has no attribute "rowcount" [attr-defined]`. (Same as in `tasks.py`)
    *   `src/family_assistant/storage/message_history.py`:
        *   L143: `"Result[Any]" has no attribute "rowcount" [attr-defined]`. (Same as in `tasks.py`)
        *   L356: `Need type annotation for "grouped_history" [var-annotated]`.
        *   **Fix:** `grouped_history: dict[str, list[dict[str, Any]]] = defaultdict(list)`.
    *   `src/family_assistant/storage/__init__.py`:
        *   L378, L397: `Incompatible types in assignment (SQLAlchemyError/Exception vs DBAPIError | None) [assignment]`.
        *   **Fix:** Broaden `db_exc: DBAPIError | None` to `db_exc: Exception | None` or `db_exc: SQLAlchemyError | None`.

**Step 3: Correct Async/Await Usage and Context Managers**

*   **Common Cause:** Missing `await` for async functions returning coroutines, or incorrectly `await`ing async context manager instances instead of using `async with`.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/web/auth.py`:
        *   L102: `"Coroutine[Any, Any, DatabaseContext]" has no attribute "__aenter__" / "__aexit__" [attr-defined]`.
        *   **Fix:** Change `async with get_db_context(...)` to `async with await get_db_context(...)`.
    *   `src/family_assistant/telegram_bot.py`:
        *   L459, L767: `Incompatible types in "await" (actual type "AbstractAsyncContextManager[DatabaseContext, bool | None]") [misc]`.
        *   **Fix:** If `get_db_context(...)` is called, ensure it's `await`ed to get the instance (e.g., `db_ctx_cm = await get_db_context(...)`), and then use `async with db_ctx_cm:`. Do not `await` the context manager instance itself.
    *   `tests/conftest.py`:
        *   L119 (for `db_context` fixture): `The return type of an async generator function should be "AsyncGenerator" or one of its supertypes [misc]`.
        *   **Fix:** Ensure the `db_context` async generator fixture has return type `AsyncGenerator[DatabaseContext, None]`.

**Step 4: Resolve LLM and Embedding Related Type Issues**

*   **Common Cause:** Incorrect data structures for LLM functions, `SentenceTransformer` constructor arguments, redefinitions in mock files.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/llm.py`:
        *   L226-L236: `Incompatible types in assignment (str/list vs dict[str, Any])` for `current_input_args`.
        *   **Fix:** Initialize `current_input_args: dict[str, Any] = {"method": method_name}` then add other keys: `current_input_args["messages"] = messages_param`.
        *   L269, L270: `Item "ChatCompletion...MessageParam" ... has no attribute "content"/"tool_calls" [union-attr]`.
        *   **Fix:** `message` is a dict from LiteLLM. Access fields using `message.get("content")` and `message.get("tool_calls")`, checking for `None`.
    *   `src/family_assistant/embeddings.py`:
        *   L291: Multiple `Argument 3 to "SentenceTransformer" has incompatible type "**dict[str, object]" [arg-type]`.
        *   **Fix:** Remove `**kwargs` from `SentenceTransformerEmbeddingGenerator.__init__` and its call to `SentenceTransformerClass`. Pass allowed arguments explicitly (e.g., `device=device`).
    *   `tests/mocks/mock_llm.py`:
        *   L17: `Name "LLMOutput" already defined [no-redef]`.
        *   L28: `Name "LLMInterface" already defined [no-redef]`.
        *   **Fix:** Review conditional imports. If these types are defined under `if TYPE_CHECKING:` and also in a fallback `else:`, ensure names are unique or structure prevents redefinition.
    *   `tests/functional/telegram/test_telegram_send_message_tool.py`, `tests/functional/telegram/test_telegram_handler.py`, `tests/functional/telegram/test_telegram_confirmation.py`:
        *   Multiple `LLMInterface" has no attribute "rules"/"_calls" [attr-defined]`.
        *   **Fix:** The `RuleBasedMockLLMClient` (which implements `LLMInterface`) should have these attributes if tests access them. Add them to `RuleBasedMockLLMClient` or adjust tests to use public methods for interaction/assertion.
    *   `tests/functional/indexing/test_email_indexing.py`, `tests/functional/indexing/test_document_indexing.py`:
        *   Multiple `MockEmbeddingGenerator" has no attribute "_test_query_..." [attr-defined]`.
        *   **Fix:** Add these test-specific query embeddings to `MockEmbeddingGenerator` or use its public methods to set up mock responses.

**Step 5: Address Type Hinting and Protocol Mismatches in Core Logic (Processing, Tools, Telegram, Calendar, Tests)**

*   **Common Cause:** Function arguments/return types not matching definitions, incorrect attribute access on `None` or union types, signature mismatches, missing annotations in test files.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/processing.py`:
        *   L354: `Incompatible types in assignment (None vs str)` for `tool_call_id`.
        *   **Fix:** Change `tool_call_id` type to `str | None` and handle `None`.
    *   `src/family_assistant/tools/types.py`:
        *   L39: `Name "embedding_generator" already defined on line 36 [no-redef]`.
        *   **Fix:** Rename or remove one of the `embedding_generator` fields in `ToolExecutionContext`.
    *   `src/family_assistant/tools/mcp.py`:
        *   L256: `"BaseException" object is not iterable [misc]`.
        *   **Fix:** Simplify error logging: `log_entry["error_message"] = str(e)`, `log_entry["error_args"] = repr(e.args)`.
    *   `src/family_assistant/tools/__init__.py`:
        *   L639: `Name "_scan_user_docs" already defined on line 610 [no-redef]`.
        *   **Fix:** Remove or rename the duplicate function.
        *   L833, L836: `Argument 1 to "format_datetime_or_date" has incompatible type "Any | None"; expected "datetime | date" [arg-type]`.
        *   **Fix:** Check `isinstance(value, (datetime, date))` before passing, or ensure `event.get("DTSTART", {}).get("value")` is correctly typed/parsed earlier.
        *   L1793: `Unexpected keyword argument ... for request_confirmation_callback [call-arg]`.
        *   **Fix:** The callback passed to `ConfirmingToolsProvider` should match `(prompt_text: str, tool_name: str, tool_args: dict[str, Any])`. Call it as `await self.request_confirmation_callback(prompt, name, arguments)`.
    *   `src/family_assistant/telegram_bot.py`:
        *   Numerous `Item "None" of "..." has no attribute "..." [union-attr]` (e.g., L130, L306, L309, L329, L380, L819, L865, L866, L873-L892, L1002-L1044, L1065, L1184).
        *   **Fix:** Add explicit `None` checks (e.g., `if update.message and update.message.chat:`). For `forward_origin` (L399-L403), check specific type like `isinstance(forward_origin, telegram.MessageOriginUser)`. For `query.message.text_markdown_v2` (L1019), check `isinstance(query.message, telegram.Message)`.
        *   L187: `Cannot infer type of lambda [misc]`.
        *   **Fix:** If `self.queue_message` is async, use `lambda u, c: asyncio.create_task(self.queue_message(u,c))`, or define a small async helper.
        *   L399-L403: `MessageOrigin" has no attribute "sender_user"/"sender_chat" [attr-defined]`.
        *   **Fix:** Use type guards (e.g. `if isinstance(forward_origin, MessageOriginUser): ... elif isinstance(forward_origin, MessageOriginChat): ...`) before accessing specific attributes.
        *   L423: `Dict entry 1 has incompatible type "str": "dict[str, str]"; expected "str": "str" [dict-item]`.
        *   **Fix:** Ensure `message_text` is `str`. If `trigger_content_parts` is `list[dict[str, Any]]`, this should be fine. If it's `list[dict[str, str]]`, then `{"type": "image_url", "image_url": {"url": base64_image_data_url}}` would be an issue. The error points to the text part, so `message_text` type is key.
        *   L564: `Argument "request_confirmation_callback" ... has incompatible type ... [arg-type]`.
        *   **Fix:** Adapt `self.telegram_confirmation_ui_manager.request_confirmation` using `functools.partial` or a lambda to match the expected `(prompt, name, args)` signature for `generate_llm_response_for_chat`.
        *   L1123: `Argument "message_batcher" to "TelegramUpdateHandler" has incompatible type "None" [arg-type]`.
        *   **Fix:** Pass a valid `MessageBatcher` instance (e.g., `NoBatchMessageBatcher()`).
        *   L1133: `Incompatible types in assignment (NoBatchMessageBatcher vs DefaultMessageBatcher) [assignment]`.
        *   **Fix:** Type `message_batcher` as the protocol `MessageBatcher`.
    *   `src/family_assistant/task_worker.py`:
        *   L396, L397: `Argument ... to "ToolExecutionContext" has incompatible type "Any | None"; expected "str" [arg-type]`.
        *   **Fix:** `ToolExecutionContext` fields `interface_type`, `conversation_id` must accept `str | None`, or provide default string values (e.g., `"unknown"`) from `task_payload.get("...", "unknown")`.
    *   `src/family_assistant/calendar_integration.py`:
        *   L196, L198, L203: `Item "BaseException" of "Response | BaseException" has no attribute "status_code"/"text" [union-attr]`.
        *   **Fix:** Check `isinstance(response_or_exc, httpx.Response)` before accessing response attributes.
        *   L887, (tools L833, L836): `Argument 1 to "format_datetime_or_date" has incompatible type "Any | None"; expected "datetime | date" [arg-type]`.
        *   **Fix:** Ensure the value from `event.get("DTSTART", {}).get("value")` is `datetime` or `date`. Add `isinstance` check or parse appropriately.
        *   L1044: `Item "dict[Any, Any]" of "Any | dict[Any, Any]" has no attribute "value" [union-attr]`.
        *   **Fix:** Check if `dt_val` is a dict and has 'value' before `dt_val.value`.
        *   L1068, L1148: `Incompatible return value type (got "str | None", expected "str") [return-value]`.
        *   **Fix:** Ensure these functions always return `str`, or change return type to `str | None` and update callers.
    *   `tests/helpers.py`:
        *   L162: `Need type annotation for "cols_to_select" [var-annotated]`.
        *   **Fix:** `cols_to_select: list[str] = [...]`.
    *   `tests/unit/test_processing_history_formatting.py`:
        *   L42, L43: `MockLLMClient`/`MockToolsProvider` incompatible with protocols `LLMInterface`/`ToolsProvider`.
        *   **Fix:** Ensure mock classes fully implement all methods (even if just `pass` or `NotImplementedError`) of their respective protocols.
        *   L129, L172: `Argument 1 to "_format_history_for_llm" ... has incompatible type "list[object]"; expected "list[dict[str, Any]]" [arg-type]`.
        *   **Fix:** Ensure test data passed matches `list[dict[str, Any]]`.
    *   `tests/functional/test_smoke_notes.py`, `tests/functional/test_mcp_integration.py`, `tests/functional/test_smoke_callback.py`, `tests/functional/telegram/conftest.py`, `tests/functional/indexing/test_email_indexing.py`, `tests/functional/indexing/test_document_indexing.py`:
        *   Multiple `Need type annotation for "dummy_..." / "test_app_config"` [var-annotated].
        *   **Fix:** Add `dict[str, Any]` or more specific types.
        *   Multiple argument type errors for context providers, handlers, services.
        *   **Fix:** Align fixture provisions with constructor/method signatures.
    *   `tests/conftest.py`:
        *   L116: `Value of type variable "_R" of function cannot be "AsyncEngine" [type-var]`.
        *   **Fix:** Review the `db_engine` fixture. If it's a generator, ensure correct `yield` type.
        *   L203: `Argument "processing_service" to "TaskWorker" has incompatible type "None" [arg-type]`.
        *   **Fix:** Ensure `task_worker_fixture` receives a valid `ProcessingService` instance.
    *   `tests/functional/telegram/test_telegram_send_message_tool.py`, `tests/functional/telegram/test_telegram_handler.py`:
        *   `CallbackContext[...] has no attribute "_bot" [attr-defined]`.
        *   **Fix:** Access via `context.bot`.
    *   `tests/functional/telegram/test_telegram_confirmation.py`:
        *   L173: `"None" object is not iterable [misc]`.
        *   **Fix:** Check for `None` before iterating, likely related to `mock_llm_client.rules`.

**Step 6: Fix Type Issues in Indexing Pipeline and Processors (including related Test files)**

*   **Common Cause:** `ContentProcessor` mismatches, `Document` protocol vs. `DocumentRecord` issues, test data/fixture problems.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/indexing/pipeline.py`:
        *   L190: `Argument "initial_content_ref" to "process" of "ContentProcessor" has incompatible type "IndexableContent | None"; expected "IndexableContent" [arg-type]`.
        *   **Fix:** Change `ContentProcessor` protocol's `process` method signature for `initial_content_ref` to `IndexableContent | None` if some processors can handle `None`. Otherwise, ensure all callers pass non-None.
    *   `src/family_assistant/indexing/email_indexer.py`:
        *   L212: `Argument 1 to "from_row" of "EmailDocument" has incompatible type "dict[str, Any]"; expected "RowMapping" [arg-type]`.
        *   **Fix:** If `fetch_one` returns `dict`, `EmailDocument.from_row` needs to accept `dict` or be called with `RowMapping` before conversion.
        *   L303: `Argument "original_document" to "run" of "IndexingPipeline" has incompatible type "DocumentRecord"; expected "Document" [arg-type]`.
        *   **Fix:** Ensure `DocumentRecord` (SQLAlchemy model) correctly implements all properties/methods of the `Document` protocol. This might involve adding `@property` decorators to `DocumentRecord` to match expected attributes like `created_at: datetime | None` (not `Mapped[...]`) and `metadata: dict[str, Any] | None`.
    *   `src/family_assistant/indexing/processors/llm_processors.py`:
        *   L141, L443: `Argument "tool_choice" to "generate_response" of "LLMInterface" has incompatible type "dict[str, Collection[str]]"; expected "str | None" [arg-type]`.
        *   **Fix:** Update `LLMInterface` protocol for `tool_choice` to accept `str | dict[str, Any] | None` (or a more specific `TypedDict` like LiteLLM's `ChatCompletionNamedToolChoiceParam`).
    *   `src/family_assistant/indexing/processors/file_processors.py`:
        *   L68: `"Document" has no attribute "id" [attr-defined]`.
        *   **Fix:** Add `id: int` (or appropriate type) to the `Document` protocol.
    *   `src/family_assistant/indexing/processors/dispatch_processors.py`:
        *   L136: `Item "None" of "Application[...] | None" has no attribute "new_task_event" [union-attr]`.
        *   **Fix:** Check `if context.application:` before `context.application.new_task_event.set()`.
    *   `src/family_assistant/indexing/document_indexer.py`:
        *   L113-L140: `Argument 1 to "append" of "list" has incompatible type ...; expected "TitleExtractor" [arg-type]`.
        *   **Fix:** Initialize `base_processors: list[ContentProcessor] = [TitleExtractor(...)]`.
        *   L155: `Argument "processors" to "IndexingPipeline" has incompatible type "list[TitleExtractor]"; expected "list[ContentProcessor]" [arg-type]`.
        *   **Fix:** Ensure `base_processors` is `list[ContentProcessor]`.
        *   L259: `Incompatible types in assignment (int vs str)` for `original_document_id`.
        *   **Fix:** `original_document_id=str(original_document.id)`.
        *   L319: `Argument "original_document" to "run" of "IndexingPipeline" has incompatible type "DocumentRecord"; expected "Document" [arg-type]`. (Same as `email_indexer.py` L303).
    *   `tests/unit/indexing/processors/test_network_processors.py`:
        *   L235, L508: `Argument 1 to "open"/"remove" has incompatible type "str | None" [arg-type]`.
        *   **Fix:** Ensure path arguments are not `None` before calling `open`/`os.remove`.
    *   `tests/functional/indexing/test_indexing_pipeline.py`:
        *   L108: `Incompatible return value type (HashingWordEmbeddingGenerator vs MockEmbeddingGenerator) [return-value]`.
        *   **Fix:** Ensure fixture returns the expected type or adjust test expectations.
        *   L126, (and similar in `test_email_indexing.py`, `test_document_indexing.py`): `Argument "processing_service" to "TaskWorker" has incompatible type "None" [arg-type]`.
        *   **Fix:** Ensure `task_worker` fixture receives a valid `ProcessingService`.
        *   L225: `Item "None" of "DocumentRecord | None" has no attribute "id" [union-attr]`.
        *   **Fix:** Check for `None` before accessing `id`.
        *   L255, L461: `Argument "original_document" to "run" of "IndexingPipeline" has incompatible type "DocumentRecord | None"; expected "Document" [arg-type]`.
        *   **Fix:** Ensure a valid `Document` is passed, and it's not `None`.
    *   `tests/functional/indexing/processors/test_llm_intelligence_processor.py`:
        *   L22: `The return type of a generator function should be "Generator" or one of its supertypes [misc]`.
        *   **Fix:** Add `-> Generator[Any, None, None]` or more specific types to the generator fixture.
    *   `tests/functional/indexing/test_email_indexing.py`:
        *   L1517, L1779 (and similar in `test_document_indexing.py`): `Argument "application" to "TaskWorker" has incompatible type "FastAPI"; expected "Application[...]" [arg-type]`.
        *   **Fix:** Mock `application` to conform to `telegram.ext.Application` or adjust `TaskWorker` if it can accept a more generic app type.
        *   L488, L1354: `Value of type "dict[str, Any] | None" is not indexable [index]`.
        *   L493-L506, L1357-L1367: `Item "None" of "dict[str, Any] | None" has no attribute "get" [union-attr]`.
        *   **Fix:** Check for `None` before indexing or calling `.get()` on dicts that might be `None`.
        *   L1211, L1212: `Argument "chunk_size"/"chunk_overlap" to "TextChunker" has incompatible type "object" [arg-type]`.
        *   **Fix:** Ensure these are passed as `int`.
        *   L1592 (and `test_document_indexing.py` L877): `Unsupported operand types for > ("float" and "None") [operator]`.
        *   **Fix:** Ensure operands for comparison are not `None`.
    *   `tests/functional/indexing/test_document_indexing.py`:
        *   L167: `Value of type variable "_R" of function cannot be "AsyncClient" [type-var]`.
        *   L168: `The return type of an async generator function should be "AsyncGenerator" ... [misc]`.
        *   **Fix:** Adjust `async_client_fixture` return type to `AsyncGenerator[AsyncClient, None]`.
        *   L242: `Item "Application[...] | None" has no attribute "state" [union-attr]`.
        *   **Fix:** Check for `None` before `app.state`.
        *   L941, L1233: `"ScrapeResult" has no attribute "status_code" [attr-defined]`.
        *   **Fix:** Add `status_code` to `ScrapeResult` or adjust tests if it's not expected.

**Step 7: Resolve Type Issues in Web Layer (Routers, Dependencies)**

*   **Common Cause:** FastAPI dependency default arguments, query parameter types, async generator return types.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/web/dependencies.py`:
        *   L31: `The return type of an async generator function should be "AsyncGenerator" ... [misc]`.
        *   **Fix:** `async def get_db_context_dependency(...) -> AsyncGenerator[DatabaseContext, None]:`.
    *   `src/family_assistant/web/routers/vector_search.py`:
        *   L160, L161, L166, L167: `Incompatible default for argument ... (default has type "None", argument has type "list[str]") [assignment]`.
        *   **Fix:** Change types to `Optional[list[str]] = Query(None, ...)` or `list[str] | None = Query(None, ...)`.
        *   L268: `Argument "search_type" to "VectorSearchQuery" has incompatible type "str"; expected "Literal['semantic', 'keyword', 'hybrid']" [arg-type]`.
        *   **Fix:** Validate `search_type` against `Literal` values and `cast` it, or change route parameter type to this `Literal`.
        *   L286: `List item 0 has incompatible type "str | None"; expected "str" [list-item]`.
        *   **Fix:** Ensure items in `metadata_filters_list` are `str`, not `None`, e.g., `if key is not None and value is not None:`.
    *   `src/family_assistant/web/routers/notes.py`:
        *   L93: `Incompatible default for argument "db_context" (None vs DatabaseContext) [assignment]`.
        *   **Fix:** `db_context: DatabaseContext = Depends(get_db_context_dependency)`.
    *   `src/family_assistant/web/routers/history.py`:
        *   L92: `Need type annotation for "grouped_by_turn_id" [var-annotated]`.
        *   **Fix:** `grouped_by_turn_id: dict[str, list[dict[str, Any]]] = defaultdict(list)`.
    *   `src/family_assistant/web/routers/api.py`:
        *   L76: `Unexpected keyword argument "calendar_config" for "ToolExecutionContext" [call-arg]`.
        *   **Fix:** Remove from `ToolExecutionContext` instantiation or add to its definition.
        *   L202: `Incompatible default for argument "db_context" (None vs DatabaseContext) [assignment]`.
        *   **Fix:** `db_context: DatabaseContext = Depends(get_db_context_dependency)`.

**Step 8: Final Pass and Miscellaneous Fixes (`__main__.py`)**

*   **Common Cause:** Lambda type inference, handler signature mismatches.
*   **Files to Fix & Specific Errors:**
    *   `src/family_assistant/__main__.py`:
        *   L913: `Argument "get_db_context_func" to "TelegramService" has incompatible type ... [arg-type]`.
        *   **Fix:** `TelegramService` constructor expects `Callable[..., AbstractAsyncContextManager[DatabaseContext, bool | None]]`. `get_db_context` is `async def ... -> DatabaseContext`. Wrap `get_db_context` or adjust `TelegramService`'s expectation. A simple wrapper: `lambda **kwargs: get_db_context(engine=kwargs.get("engine", main_engine), ...)`.
        *   L954: `Argument 2 to "register_task_handler" of "TaskWorker" has incompatible type ... [arg-type]`.
        *   **Fix:** `TaskWorker` expects handler `Callable[[ToolExecutionContext, Any], Awaitable[None]]`. Current handlers like `handle_process_document_task` take `(DatabaseContext, payload)`. Adapt handlers to take `ToolExecutionContext` and create/get `DatabaseContext` within, or have `TaskWorker` create `ToolExecutionContext` and pass it. The latter is cleaner: change handler signatures to `async def my_handler(exec_context: ToolExecutionContext, payload: Any)`.
        *   L1078: `Cannot infer type of lambda [misc]`.
        *   **Fix:** `default_factory=lambda: str(os.getenv("TELEGRAM_BOT_NAME", ""))`.

This structured approach should help in tackling these errors methodically. Remember to run Mypy after each step to see the progress. Good luck!
