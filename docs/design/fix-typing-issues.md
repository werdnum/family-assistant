Okay, this is a substantial list of type errors! Let's break them down systematically.

Based on the errors, we can group them into logical steps to address common causes and progressively improve type safety.

## Summary of Type Errors and Fixes

Here's a breakdown of the issues and the proposed fixes, grouped into incremental steps:

**Step 1: Add Missing Type Stubs and Address Import-Related Errors**

*   **Common Cause:** Several libraries are missing type stubs (e.g., `types-*` packages in `pyproject.toml`) or a `py.typed` marker file. This prevents Mypy from analyzing them, leading to `[import-untyped]` errors and subsequent `[attr-defined]` or `[misc]` errors when their components are used without type information. Also includes issues with conditional imports of modules like `sentence-transformers` and `markitdown`.
*   **Files to Fix:**
    *   `pyproject.toml`
    *   `src/family_assistant/tools/schema.py` (json_schema_for_humans)
    *   `src/family_assistant/storage/vector.py` (pgvector.sqlalchemy)
    *   `src/family_assistant/indexing/ingestion.py` (filetype)
    *   `src/family_assistant/utils/scraping.py` (playwright.async_api, MarkItDown)
    *   `src/family_assistant/embeddings.py` (SentenceTransformer, torch.Module)
    *   `src/family_assistant/calendar_integration.py` (vobject - for `readComponents`)
    *   `src/family_assistant/tools/__init__.py` (telegramify_markdown)
    *   `src/family_assistant/telegram_bot.py` (telegramify_markdown)
    *   `src/family_assistant/task_worker.py` (telegramify_markdown)
*   **Fixes:**
    1.  **Update `pyproject.toml`**:
        Add the following to `[project.optional-dependencies.dev]`:
        ```toml
        # Add to [project.optional-dependencies.dev]
        "types-PyYAML",       # Already there
        "types-python-dateutil", # Already there
        "types-pytz",         # Already there
        "types-passlib",      # Already there
        "types-aiofiles",     # Already there
        "types-vobject",      # Already there, ensure it's effective
        "types-filetype",     # New: For 'filetype' library
        # For libraries that might not have separate stubs, but ship their own or need ignore:
        # Playwright ships its own types. Ensure it's installed and import is correct.
        # Sentence-Transformers ships its own types.
        # MarkItDown - may require # type: ignore or checking if it has py.typed
        # json-schema-for-humans - may require # type: ignore
        # telegramify-markdown - may require # type: ignore
        # pgvector - check if recent versions include stubs, else # type: ignore
        ```
    2.  **Handle Libraries Without Dedicated Stubs:**
        *   For `json_schema_for_humans` in `src/family_assistant/tools/schema.py`: If `types-json-schema-for-humans` doesn't exist or doesn't resolve, add `# type: ignore[import-untyped]` to the import lines.
        *   For `pgvector.sqlalchemy` in `src/family_assistant/storage/vector.py`: Add `# type: ignore[import-untyped]` if stubs aren't found/included.
        *   For `filetype` in `src/family_assistant/indexing/ingestion.py`: If `types-filetype` doesn't resolve, use `# type: ignore[import-untyped]`.
        *   For `telegramify_markdown` (in `tools/__init__.py`, `telegram_bot.py`, `task_worker.py`): Add `# type: ignore[import-untyped]` if no stubs package exists.
    3.  **`src/family_assistant/utils/scraping.py`**:
        *   `playwright.async_api`: Ensure the import is `from playwright.async_api import async_playwright` or similar, as Playwright includes its own types. The error suggests an issue with how it's imported or found.
        *   `MarkItDown`: Fix conditional import. Change `MarkItDown = None` to `MarkItDownType: Optional[Type[ActualMarkItDownClass]] = None` and assign `MarkItDownType = ActualMarkItDownClass` in the `try` block. Use `if MarkItDownType:` for checks. Resolve `Function "MarkItDown" could always be true` by checking the availability flag (`MARKITDOWN_AVAILABLE`) or `if MarkItDownType is not None:`.
    4.  **`src/family_assistant/embeddings.py`**:
        *   `SentenceTransformer`, `torch.nn.Module`: Similar to `MarkItDown`, fix conditional imports. Use `SentenceTransformerClass: Optional[Type[ActualSentenceTransformer]] = None` etc.
        *   `L359: Name "SentenceTransformerEmbeddingGenerator" already defined`: This indicates a structural issue or a Mypy misinterpretation due to earlier errors. Ensure the class is defined only once within its conditional scope. If it's a re-import or copy-paste, remove the duplicate.
    5.  **`src/family_assistant/calendar_integration.py`**:
        *   `Module has no attribute "readComponents"`: This is from `vobject`. Ensure `types-vobject` is installed and working. If stubs are incomplete for `readComponents`, you might need `cast(Any, ics_data).readComponents()` or `# type: ignore[attr-defined]`.

**Step 2: Refactor SQLAlchemy Usage and Database Interaction**

*   **Common Cause:** Mismatches between SQLAlchemy's actual return types (e.g., `RowMapping`, `Result`) and how they are used (e.g., expecting `dict`, direct attribute access on `Result`). Incorrect use of SQLAlchemy constructs or missing type annotations for variables holding query results.
*   **Files to Fix:**
    *   `src/family_assistant/storage/context.py`
    *   `src/family_assistant/storage/vector_search.py`
    *   `src/family_assistant/storage/tasks.py`
    *   `src/family_assistant/storage/vector.py`
    *   `src/family_assistant/storage/notes.py`
    *   `src/family_assistant/storage/message_history.py`
    *   `src/family_assistant/storage/__init__.py`
*   **Fixes:**
    1.  **`src/family_assistant/storage/context.py`**:
        *   L65 & L67 (`AsyncTransaction` and `__aenter__`): `self._connection.begin()` returns an `AsyncTransaction` object, not a context manager. The transaction should be explicitly committed/rolled back in `__aexit__`.
            ```python
            # In __aenter__
            if self._connection: # Or ensure it's always set
                self._transaction = await self._connection.begin() # Start transaction

            # In __aexit__
            if self._transaction:
                if exc_type is None: await self._transaction.commit()
                else: await self._transaction.rollback()
            # Remove 'async with self._transaction:'
            ```
        *   L169 (`fetch_all`): Convert `RowMapping` to `dict`. Change `return [dict(row) for row in result.mappings()]` or similar. The current error implies `result.mappings().all()` is being returned. It should be `[dict(row) for row in (await self._connection.execute(query, params)).mappings().all()]`.
        *   L186 (`fetch_one`): Convert `RowMapping` to `dict`. `row = (await ...).mappings().one_or_none(); return dict(row) if row else None`.
        *   L206 (`sync_connection`): Ensure `self._connection` is not `None` before `self._connection.sync_connection`. This part of `on_commit` might be called when the connection is already closed. Add a check: `if self._connection and self._connection.is_active:`.
    2.  **`src/family_assistant/storage/vector_search.py`**:
        *   Multiple `Incompatible types in assignment (..., target has type "int")` for `filters` dict: The `filters: dict[str, int]` type hint is too restrictive. Change it to `filters: dict[str, Any] = {}`.
    3.  **`src/family_assistant/storage/tasks.py`**:
        *   L155, L168 (`AsyncConnection` has no `execute`): `self._connection` in `DatabaseContext` should be non-None if `execute_with_retry` is called within an active context. This might indicate a logic flaw where connection is not ensured.
        *   L173 (`fetch_pending_task_with_retries`): Convert `RowMapping` to `dict` similar to `context.py` fixes.
        *   L214, L256, L327, L343 (`Result[Any]` has no `rowcount`): `Result` objects from DML statements (UPDATE, INSERT, DELETE) do have `rowcount`. Ensure the variable is indeed a `Result` object.
        *   L296-L329 (attribute errors on `dict[str, Any]` for `task_details`): Access items using dict notation, e.g., `task_details["status"]`, `task_details.get("retry_count")`, not `task_details.status`.
    4.  **`src/family_assistant/storage/vector.py`**:
        *   L261, L262 (`Result[Any]` has no `inserted_primary_key`): `Result` from an INSERT should have this. Ensure `result` is the direct object. `pk = result.inserted_primary_key; doc_id = pk[0] if pk else None`.
        *   L283 (`ReturningInsert` vs `Insert`): Broaden the type of `stmt` if it's reassigned after `.returning()`. E.g., `stmt: Union[Insert, ReturningInsert]` or use type inference if possible, or `stmt: ClauseElement`.
        *   L524, L768 (`rowcount` errors): Same as in `tasks.py`.
        *   L658, L717, L718, L724 (`where`/`or_` arg type `bool`): SQLAlchemy clauses must be `ColumnElement[bool]`. Python `True`/`False` are not valid directly. Use `sqlalchemy.true()`, `sqlalchemy.false()`, or column expressions (e.g., `my_table.c.col == True`). Review how `conditions` and `or_clause_conditions` are built.
    5.  **`src/family_assistant/storage/notes.py`**:
        *   L142, L168 (`rowcount` errors): Same as in `tasks.py`.
    6.  **`src/family_assistant/storage/message_history.py`**:
        *   L143 (`rowcount` error): Same as in `tasks.py`.
        *   L356 (`grouped_history` needs annotation): Add `grouped_history: dict[str, list[dict[str, Any]]] = defaultdict(list)`.
    7.  **`src/family_assistant/storage/__init__.py`**:
        *   L378, L397 (incompatible exception types): Broaden `db_exc: DBAPIError | None` to `db_exc: Exception | None` or `db_exc: SQLAlchemyError | None` to match the caught exceptions.

**Step 3: Correct Async/Await Usage and Context Managers**

*   **Common Cause:** Missing `await` for async function calls that return coroutines, or incorrectly `await`ing objects that are context managers instead of using `async with`.
*   **Files to Fix:**
    *   `src/family_assistant/web/auth.py`
    *   `src/family_assistant/telegram_bot.py`
*   **Fixes:**
    1.  **`src/family_assistant/web/auth.py`**:
        *   L102 (`Coroutine [...] has no attribute "__aenter__"`): `get_db_context` is an `async def` returning a `DatabaseContext` (which is an async CM). You need to `await` the call to `get_db_context`.
            Change: `async with get_db_context(...) as db_context_instance:`
            To: `async with await get_db_context(...) as db_context_instance:`
    2.  **`src/family_assistant/telegram_bot.py`**:
        *   L459, L767 (`Incompatible types in "await" (actual type "AbstractAsyncContextManager[DatabaseContext, bool | None]")`): This means an async context manager *instance* is being `await`ed directly.
            Example: `db_ctx_instance = await get_db_context(...)` (correctly gets the CM instance). Then, `result = await db_ctx_instance` (incorrect, should be `async with db_ctx_instance:`).
            Review these lines. If `get_db_context(...)` is called, ensure it's `await`ed to get the instance, and then that instance is used with `async with` or its methods are called, not `await`ed directly.

**Step 4: Resolve LLM and Embedding Related Type Issues**

*   **Common Cause:** Incorrect data structures passed to LLM functions, issues with `SentenceTransformer` constructor arguments due to generic `**kwargs`.
*   **Files to Fix:**
    *   `src/family_assistant/llm.py`
    *   `src/family_assistant/embeddings.py`
*   **Fixes:**
    1.  **`src/family_assistant/llm.py`**:
        *   L226-L236 (Incompatible assignments to `current_input_args` in `_log_no_match_error`): `current_input_args` is meant to be a dictionary. Initialize it as `current_input_args: dict[str, Any] = {"method": method_name}` and then add other keys/values like `current_input_args["messages"] = ...`.
        *   L269, L270 (Attribute errors on `ChatCompletion...MessageParam`): `message` is a dict (from LiteLLM). Access fields using `message.get("content")` and `message.get("tool_calls")`, and check for `None` before use.
    2.  **`src/family_assistant/embeddings.py`**:
        *   L291 (`SentenceTransformer` constructor arguments): `SentenceTransformer` does not take arbitrary `**kwargs: object`. Remove `**kwargs` from `SentenceTransformerEmbeddingGenerator.__init__` and the call to `SentenceTransformerClass`, or explicitly list the allowed optional arguments and pass them selectively from `kwargs`. Example: `self.model = SentenceTransformerClass(model_name_or_path, device=device)`.

**Step 5: Address Type Hinting and Protocol Mismatches in Core Logic (Processing, Tools, Telegram)**

*   **Common Cause:** Function arguments/return types not matching definitions or protocol expectations, incorrect attribute access on `None` or union types, signature mismatches for callbacks.
*   **Files to Fix:**
    *   `src/family_assistant/processing.py`
    *   `src/family_assistant/tools/types.py`
    *   `src/family_assistant/tools/mcp.py`
    *   `src/family_assistant/tools/__init__.py`
    *   `src/family_assistant/telegram_bot.py` (remaining specific issues)
    *   `src/family_assistant/task_worker.py`
*   **Fixes:**
    1.  **`src/family_assistant/processing.py`**:
        *   L354 (`tool_call_id: str = result.get("tool_call_id")`): `result.get()` can return `None`. Change `tool_call_id` type to `str | None` and handle the `None` case.
    2.  **`src/family_assistant/tools/types.py`**:
        *   L39 (`embedding_generator` redefinition): `ToolExecutionContext` dataclass has two fields named `embedding_generator`. Rename or remove one.
    3.  **`src/family_assistant/tools/mcp.py`**:
        *   L256 (`"BaseException" object is not iterable`): `e.args` is a tuple and is iterable. `list(e.args)` is fine. This might be Mypy confusion. Simplify error logging: `log_entry["error_message"] = str(e)`, `log_entry["error_args"] = repr(e.args)`.
    4.  **`src/family_assistant/tools/__init__.py`**:
        *   L639 (`_scan_user_docs` redefinition): Remove or rename the duplicate function.
        *   L833, L836 (`format_datetime_or_date` arg type): Ensure the value fetched from `event.get("DTSTART", {}).get("value")` is checked (`isinstance(value, (datetime, date))`) before passing.
        *   L1793 (Unexpected kwargs for `request_confirmation_callback`): The callback passed to `ConfirmingToolsProvider` should match the expected signature `(prompt_text: str, tool_name: str, tool_args: dict[str, Any])`. The call in `ConfirmingToolsProvider` should be `await self.request_confirmation_callback(prompt, name, arguments)`. The `timeout` is managed by the provider, not passed to the callback.
    5.  **`src/family_assistant/telegram_bot.py` (specific attribute/None errors)**:
        *   L130, L306, L309, L329, L380, L819, L865, L866, L873-L892, L1002-L1044, L1065, L1184: These are mostly `Item "None" of "..." has no attribute "..."`. Add explicit `None` checks before attribute access. E.g., `if update.message and update.message.chat: chat_id = update.message.chat.id`. For `forward_origin` (L399-L403), check its specific type (`isinstance(forward_origin, telegram.MessageOriginUser)`) before accessing attributes like `sender_user`. For `query.message.text_markdown_v2` (L1019), check `isinstance(query.message, telegram.Message)`.
        *   L187 (lambda inference): For `self.application.add_handler(MessageHandler(..., lambda u, c: self.queue_message(u, c)))`, try `lambda u, c: asyncio.create_task(self.queue_message(u,c))` if `queue_message` is async, or define a small async helper method.
        *   L423 (`Dict entry 1 has incompatible type`): This usually means a value in a dict doesn't match the expected value type. If `trigger_content_parts` is `list[dict[str, str]]`, then `{"type": "text", "text": message_text}` is fine if `message_text` is `str`. Check the type of `message_text` and the definition of `trigger_content_parts`. If it's `list[dict[str, Any]]` higher up, this specific error is odd.
        *   L564 (`request_confirmation_callback` signature mismatch): Adapt the callback using `functools.partial` or a lambda to match the expected `(prompt, name, args)` signature when passing `self.telegram_confirmation_ui_manager.request_confirmation`.
        *   L1123 (`message_batcher` is `None`): `TelegramUpdateHandler` expects a `MessageBatcher`. Ensure a valid batcher instance (e.g., `NoBatchMessageBatcher` or `DefaultMessageBatcher`) is passed, not `None`.
        *   L1133 (`NoBatchMessageBatcher` vs `DefaultMessageBatcher`): If `message_batcher` is typed as the concrete `DefaultMessageBatcher`, change its type hint to the protocol `MessageBatcher` so it can accept any conforming implementation.
    6.  **`src/family_assistant/task_worker.py`**:
        *   L396, L397 (`ToolExecutionContext` args `Any | None` vs `str`): If `interface_type` or `conversation_id` from `task_payload` can be `None`, `ToolExecutionContext` must accept `str | None`, or provide default string values (e.g., `"unknown"`) when constructing `ToolExecutionContext`.

**Step 6: Fix Type Issues in Indexing Pipeline and Processors**

*   **Common Cause:** Mismatches in `ContentProcessor` implementations regarding argument types, `Document` protocol vs. concrete ORM model (`DocumentRecord`) incompatibilities.
*   **Files to Fix:**
    *   `src/family_assistant/indexing/pipeline.py`
    *   `src/family_assistant/indexing/email_indexer.py`
    *   `src/family_assistant/indexing/processors/llm_processors.py`
    *   `src/family_assistant/indexing/processors/file_processors.py`
    *   `src/family_assistant/indexing/processors/dispatch_processors.py`
    *   `src/family_assistant/indexing/document_indexer.py`
*   **Fixes:**
    1.  **`src/family_assistant/indexing/pipeline.py`**:
        *   L190 (`initial_content_ref` type mismatch for `ContentProcessor.process`): If `ContentProcessor` protocol defines `initial_content_ref: IndexableContent`, then implementations like `PDFTextExtractor` must match. If some processors can handle `None`, the protocol should be `initial_content_ref: IndexableContent | None`. Align processor signatures with the protocol.
    2.  **`src/family_assistant/indexing/email_indexer.py`**:
        *   L212 (`EmailDocument.from_row` expects `RowMapping`): If `db_context.fetch_one` now returns `dict[str, Any]`, then `EmailDocument` needs a `from_dict` method, or you must pass the `RowMapping` before it's converted.
        *   L303 (`DocumentRecord` vs `Document` protocol): Ensure `DocumentRecord` (SQLAlchemy model) correctly implements the `Document` protocol. Attributes like `created_at` (e.g., `Mapped[datetime | None]` vs `datetime | None`) and `metadata` must align. Add properties to `DocumentRecord` if needed to expose protocol-compliant attributes.
    3.  **`src/family_assistant/indexing/processors/llm_processors.py`**:
        *   L141, L443 (`tool_choice` type for `generate_response`): The `LLMInterface`'s `tool_choice` parameter should be `str | dict[str, Any] | None` (or a more specific `TypedDict` like LiteLLM's `ChatCompletionNamedToolChoiceParam`) to support structured tool choices like `{"type": "function", "function": {"name": "my_tool"}}`. Update the protocol and implementations.
    4.  **`src/family_assistant/indexing/processors/file_processors.py`**:
        *   L68 (`"Document" has no attribute "id"`): Add `id: int` (or appropriate type) to the `Document` protocol if it's expected on all original documents.
    5.  **`src/family_assistant/indexing/processors/dispatch_processors.py`**:
        *   L136 (`context.application` is `None`): Check `if context.application:` before `context.application.new_task_event.set()`.
    6.  **`src/family_assistant/indexing/document_indexer.py`**:
        *   L113-L140 (`list.append` type errors): Initialize `base_processors` with the protocol type: `base_processors: list[ContentProcessor] = [TitleExtractor(...)]`.
        *   L155 (`processors` arg type for `IndexingPipeline`): Ensure the list passed is `list[ContentProcessor]`.
        *   L259 (`int` assigned to `str` for `original_document_id`): Convert `original_document.id` to string: `str(original_document.id)`.
        *   L319 (`DocumentRecord` vs `Document` protocol): Same fix as for `email_indexer.py`.

**Step 7: Resolve Type Issues in Web Layer (Routers, Dependencies)**

*   **Common Cause:** FastAPI dependency default arguments, query parameter types, return types of async generators.
*   **Files to Fix:**
    *   `src/family_assistant/web/dependencies.py`
    *   `src/family_assistant/web/routers/vector_search.py`
    *   `src/family_assistant/web/routers/notes.py`
    *   `src/family_assistant/web/routers/history.py`
    *   `src/family_assistant/web/routers/api.py`
*   **Fixes:**
    1.  **`src/family_assistant/web/dependencies.py`**:
        *   L31 (async generator return type): Change `get_db_context_dependency` return type to `AsyncGenerator[DatabaseContext, None]`.
    2.  **`src/family_assistant/web/routers/vector_search.py`**:
        *   L160, L161, L166, L167 (incompatible default `None` for `list[str]` args): Change types to `Optional[list[str]] = Query(None, ...)` or `list[str] | None = Query(None, ...)`.
        *   L268 (`search_type` `str` vs `Literal`): Validate `search_type` against `Literal['semantic', 'keyword', 'hybrid']` values before passing to `VectorSearchQuery`, or change route parameter type to this Literal if FastAPI handles it well. `valid_search_type = cast(SearchTypeLiteral, search_type)` after validation.
        *   L286 (list item `str | None` vs `str`): If `value` in `f"{key}:{value}"` can be `None`, ensure it's handled (e.g., `if key is not None and value is not None:`).
    3.  **`src/family_assistant/web/routers/notes.py`**:
        *   L93 (`db_context` default `None`): Change `db_context: DatabaseContext = Depends(get_db_context_dependency)`. Remove `= None`.
    4.  **`src/family_assistant/web/routers/history.py`**:
        *   L92 (`grouped_by_turn_id` needs annotation): Add `grouped_by_turn_id: dict[str, list[dict[str, Any]]] = defaultdict(list)`.
    5.  **`src/family_assistant/web/routers/api.py`**:
        *   L76 (unexpected kwarg `calendar_config`): Remove from `ToolExecutionContext` instantiation or add to its definition.
        *   L202 (`db_context` default `None`): Same fix as `notes.py`. `db_context: DatabaseContext = Depends(get_db_context_dependency)`.

**Step 8: Final Pass and Miscellaneous Fixes**

*   **Common Cause:** Isolated errors like lambda type inference, handler signature mismatches not caught in broader categories.
*   **Files to Fix:**
    *   `src/family_assistant/__main__.py`
*   **Fixes:**
    1.  **`src/family_assistant/__main__.py`**:
        *   L913 (`get_db_context_func` type for `TelegramService`): `TelegramService` expects `Callable[..., AbstractAsyncContextManager[...]]`. `get_db_context` is `async def ... -> DatabaseContext`.
            Pass `get_db_context` (the function itself) and ensure `TelegramService` calls it like `async with await self.get_db_context_func(self.engine) as db_ctx:`. The type hint in `TelegramService` might need to be `Callable[..., Awaitable[DatabaseContext]]`.
        *   L954 (`register_task_handler` signature): Registered handlers (e.g., `handle_process_document_task`) take `(db_context, payload)`. `TaskWorker` expects `(ToolExecutionContext, Any)`. Adapt handlers to take `ToolExecutionContext` and extract/create `db_context` from it, or have `TaskWorker` create the `ToolExecutionContext` and pass it. The latter is cleaner: change handler signatures to `async def my_handler(exec_context: ToolExecutionContext, payload: Any)`.
        *   L1078 (lambda inference for `default_factory`): `default_factory=lambda: os.getenv("TELEGRAM_BOT_NAME") or ""` should be fine. If Mypy insists, `default_factory=lambda: str(os.getenv("TELEGRAM_BOT_NAME", ""))`.

This structured approach should help in tackling these errors methodically. Remember to run Mypy after each step to see the progress. Good luck!
