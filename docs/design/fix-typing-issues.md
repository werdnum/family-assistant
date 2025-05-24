Okay, I've analyzed the Mypy errors. Here's a summary, grouped by common causes, with suggested fixes. I've structured these into logical incremental steps.

**General Note on Type Stubs:**
For libraries where official or community stubs (`types-*`) are not readily available or if installing them doesn't resolve the `import-untyped` or `import-not-found` errors, you might need to:
1.  Check if the library now includes a `py.typed` file (making it PEP 561 compatible).
2.  Create your own stub files (`.pyi`).
3.  Use `# type: ignore[import-untyped]` or `# type: ignore[import-not-found]` on the specific import lines as a temporary measure. I'll suggest this for libraries where stubs are less common.

---

**Step 1: Add Missing Type Stubs and Fix Basic Import/Definition Issues**

*   **Goal**: Resolve `import-untyped`, `import-not-found` errors by adding stubs or ignoring problematic imports, and fix simple redefinitions.
*   **Files to change**: `pyproject.toml`, `src/family_assistant/utils/scraping.py`, `src/family_assistant/embeddings.py`, `src/family_assistant/tools/__init__.py`, `src/family_assistant/tools/types.py`, `src/family_assistant/tools/schema.py`, `src/family_assistant/storage/vector.py`, `src/family_assistant/web/auth.py`, `src/family_assistant/indexing/ingestion.py`, `src/family_assistant/llm.py`, `src/family_assistant/calendar_integration.py`, `src/family_assistant/processing.py`, `src/family_assistant/task_worker.py`, `src/family_assistant/web/routers/documentation.py`, `src/family_assistant/web/routers/webhooks.py`, `src/family_assistant/web/routers/api_token_management.py`, `src/family_assistant/__main__.py`.

*   **Fixes for `pyproject.toml`**:
    Add the following to your `[project.optional-dependencies.dev]` section:
    ```toml
    "types-PyYAML",
    "types-python-dateutil",
    "types-pytz",
    "types-passlib",
    "types-aiofiles",
    "types-vobject", # For vobject if available, otherwise ignore
    # psycopg-binary is listed, but if you use pgvector, types-psycopg2 might be needed if not covered
    ```

*   **Fixes for specific files (add `# type: ignore[...]` or fix definitions)**:

    1.  **`src/family_assistant/tools/schema.py`**:
        ```python
        # src/family_assistant/tools/schema.py:12
        from json_schema_for_humans import generate # type: ignore[import-untyped]
        # src/family_assistant/tools/schema.py:13
        from json_schema_for_humans.generation_configuration import GenerationConfiguration # type: ignore[import-untyped]
        ```

    2.  **`src/family_assistant/storage/vector.py`**:
        ```python
        # src/family_assistant/storage/vector.py:16
        from pgvector.sqlalchemy import Vector # type: ignore[import-untyped]
        ```

    3.  **`src/family_assistant/web/auth.py`**:
        ```python
        # src/family_assistant/web/auth.py:9
        from passlib.context import CryptContext # CryptContext is usually here, if types-passlib doesn't cover it, use ignore
        ```
        (Mypy suggests `types-passlib`, ensure it's installed and covers this. If not, `# type: ignore[import-untyped]`)

    4.  **`src/family_assistant/indexing/ingestion.py`**:
        ```python
        # src/family_assistant/indexing/ingestion.py:10
        import filetype # type: ignore[import-untyped]
        ```

    5.  **`src/family_assistant/utils/scraping.py`**:
        *   Line 20: `playwright.async_api`
            ```python
            from playwright.async_api import async_playwright # type: ignore[import-not-found] # If playwright stubs are not found by mypy
            ```
            (Playwright typically ships with type info. This might be a path issue or mypy config. If `uv pip install playwright` and `playwright install` were run, this should ideally work. If not, ignore as a fallback.)
        *   Line 41: `markitdown_converter: type[MarkItDown] | None = None` - The `type[MarkItDown]` part is unusual for an instance variable. It should likely be `markitdown_converter: MarkItDown | None = None`.
            ```python
            # src/family_assistant/utils/scraping.py:41
            self.md_converter: MarkItDown | None = None # Adjusted from type[MarkItDown]
            # ... later in __init__ or a setup method:
            # if MARKITDOWN_AVAILABLE:
            #    self.md_converter = MarkItDown()
            ```
        *   Line 130: `Function "MarkItDown" could always be true`
            ```python
            # src/family_assistant/utils/scraping.py:130
            if self.md_converter is not None: # Was: if self.md_converter:
                # ...
            ```

    6.  **`src/family_assistant/llm.py`**:
        *   Line 12: `import aiofiles` (Ensure `types-aiofiles` is in `pyproject.toml` and installed)

    7.  **`src/family_assistant/embeddings.py`**:
        *   Line 21: `SENTENCE_TRANSFORMERS_AVAILABLE: bool = False` (This is likely correct as a flag)
        *   Line 21 `SentenceTransformer = None` and Line 22 `models = None`: These are global placeholders. Their usage needs to be guarded by `if SENTENCE_TRANSFORMERS_AVAILABLE:`. The errors `Cannot assign to a type` and `Incompatible types in assignment` mean you're assigning `None` to something that expects a type or a module.
            Change to:
            ```python
            # src/family_assistant/embeddings.py:21
            SentenceTransformer: type[sentence_transformers.SentenceTransformer] | None = None
            # src/family_assistant/embeddings.py:22
            models: Any | None = None # Or more specific type if 'sentence_transformers.models' has one
            # ...
            if SENTENCE_TRANSFORMERS_AVAILABLE:
                from sentence_transformers import SentenceTransformer as ActualSentenceTransformer
                from sentence_transformers import models as actual_models
                SentenceTransformer = ActualSentenceTransformer
                models = actual_models
            ```
        *   Line 359: `Name "SentenceTransformerEmbeddingGenerator" already defined on line 255`
            This means the class is defined twice. Ensure it's defined only once, likely within the `if SENTENCE_TRANSFORMERS_AVAILABLE:` block if it depends on it. If there's a base or mock version, they should have different names or be part of a conditional import.

    8.  **`src/family_assistant/calendar_integration.py`**:
        *   Line 10: `import vobject` (Ensure `types-vobject` is in `pyproject.toml` or use `# type: ignore[import-untyped]`)
        *   Line 12: `from dateutil import parser as dateutil_parser` (Ensure `types-python-dateutil` is in `pyproject.toml`)

    9.  **`src/family_assistant/tools/types.py`**:
        *   Line 39: `Name "embedding_generator" already defined on line 36`. Remove one of the definitions.

    10. **`src/family_assistant/tools/__init__.py`**:
        *   Line 20: `import aiofiles` (Covered by `types-aiofiles`)
        *   Line 21: `import telegramify_markdown # type: ignore[import-untyped]`
        *   Line 22, 23: `import dateutil`, `from dateutil import parser` (Covered by `types-python-dateutil`)
        *   Line 639: `Name "_scan_user_docs" already defined on line 610`. Remove one of the definitions.

    11. **`src/family_assistant/processing.py`**:
        *   Line 16: `import pytz` (Covered by `types-pytz`)

    12. **`src/family_assistant/telegram_bot.py`**:
        *   Line 20: `import telegramify_markdown # type: ignore[import-untyped]`

    13. **`src/family_assistant/task_worker.py`**:
        *   Line 15: `from dateutil import rrule, parser as dateutil_parser` (Covered by `types-python-dateutil`)
        *   Line 19: `import telegramify_markdown # type: ignore[import-untyped]`

    14. **`src/family_assistant/web/routers/documentation.py`**:
        *   Line 4: `import aiofiles` (Covered by `types-aiofiles`)

    15. **`src/family_assistant/web/routers/webhooks.py`**:
        *   Line 9: `import aiofiles` (Covered by `types-aiofiles`)
        *   Line 10: `from dateutil import parser as dateutil_parser` (Covered by `types-python-dateutil`)

    16. **`src/family_assistant/web/routers/api_token_management.py`**:
        *   Line 5: `from dateutil import tz as dateutil_tz` (Covered by `types-python-dateutil`)

    17. **`src/family_assistant/__main__.py`**:
        *   Line 14: `import yaml` (Covered by `types-PyYAML`)

---

**Step 2: Core SQLAlchemy and Database Interaction Logic**

*   **Goal**: Correct usage of SQLAlchemy connection, transaction, and query result types.
*   **Files to change**: `src/family_assistant/storage/context.py`, `src/family_assistant/storage/tasks.py`, `src/family_assistant/storage/vector.py`, `src/family_assistant/storage/notes.py`, `src/family_assistant/storage/message_history.py`, `src/family_assistant/storage/__init__.py`, `src/family_assistant/web/auth.py`, `src/family_assistant/web/dependencies.py`.

*   **Fixes**:
    1.  **`src/family_assistant/storage/context.py`**:
        *   Line 65: `self._transaction: AsyncTransaction | None = self.connection.begin()` -> `self._transaction: AsyncTransaction = await self.connection.begin()` (if `self.connection` is guaranteed). If `self.connection` can be `None`, then `self._transaction: AsyncTransaction | None = None` and `if self.connection: self._transaction = await self.connection.begin()`. The error `"None" has no attribute "__aenter__"` on line 67 suggests `self._transaction` can be `None` when `__aenter__` is called on it, which is problematic if it's meant to be an active transaction. The logic for initializing and using `_transaction` within `__aenter__` and `__aexit__` needs to be robust. It seems `self._transaction = self.connection.begin()` is not awaited.
            ```python
            # src/family_assistant/storage/context.py
            # Adjust __aenter__
            async def __aenter__(self) -> "DatabaseContext":
                if self._connection is None: # Ensure connection exists
                    self._connection = await self.engine.connect()
                # Transaction should be started here if managing it per context entry
                # If begin_nested is intended, it has different semantics
                if self.is_transactional and self._transaction is None: # Start transaction if not already in one (for nested calls)
                    self._transaction = await self._connection.begin() # Ensure awaited
                return self

            # src/family_assistant/storage/context.py:67: error: "None" has no attribute "__aenter__"
            # This error means self._transaction was None when it was used in an `async with self._transaction:` block.
            # Ensure transaction is started before being used as a context manager.
            # Typically:
            # async with self.connection.begin() as transaction:
            #    await transaction.execute(...)

            # Example within execute_with_retry, if transaction is per-operation
            # async with self._connection.begin() as transaction:
            #     result = await transaction.execute(query, params)
            #     if self.is_transactional: # or if query is not SELECT
            #         await transaction.commit() # or rollback on error
            ```
            The existing structure seems to imply a single transaction per `DatabaseContext` instance if `is_transactional`. The assignment `self._transaction = self._connection.begin()` should be `self._transaction = await self._connection.begin()` inside `__aenter__` if `_connection` is already established.

        *   Line 169 (`fetch_all`):
            ```python
            # src/family_assistant/storage/context.py:169
            # Change: return result.mappings().all()
            # To:
            return [dict(row) for row in result.mappings().all()]
            ```
        *   Line 186 (`fetch_one`):
            ```python
            # src/family_assistant/storage/context.py:186
            # Change: return result.mappings().one_or_none()
            # To:
            row = result.mappings().one_or_none()
            return dict(row) if row else None
            ```
        *   Line 206 (`on_commit`):
            ```python
            # src/family_assistant/storage/context.py:206
            if self._connection: # Add check
                sync_conn = self._connection.sync_connection
                # ...
            else:
                # Handle case where connection is None, e.g., raise error or return
                raise RuntimeError("Database connection not available for on_commit.")
            ```

    2.  **`src/family_assistant/storage/tasks.py`**:
        *   Lines 155, 168: Item `"None"` of `"AsyncConnection | None"` has no attribute `"execute"`.
            Ensure `self.connection` (if that's the source) is not `None` before calling `execute`. This usually means the connection wasn't established or was closed.
        *   Line 173 (`get_task_by_id_or_original`): Same `RowMapping` to `dict` conversion as `fetch_one`.
            ```python
            # src/family_assistant/storage/tasks.py:173
            row = (await self.connection.execute(stmt)).mappings().one_or_none() # Assuming self.connection is valid AsyncConnection
            return dict(row) if row else None
            ```
        *   Lines 214, 256, 327, 343 (`rowcount` errors): `Result[Any]` is too generic. The actual result of `execute` (often `CursorResult`) has `rowcount`.
            ```python
            # Example for src/family_assistant/storage/tasks.py:214
            # result: Result = await self.db_context.execute_with_retry(stmt) # This is likely from DatabaseContext
            # The execute_with_retry in DatabaseContext returns a CursorResult
            # So, the error is that the type hint for 'result' here is too vague or execute_with_retry's return is not correctly used
            # If db_context.execute_with_retry returns the CursorResult directly:
            cursor_result = await self.db_context.execute_with_retry(stmt)
            if cursor_result.rowcount == 0: # Access rowcount on CursorResult
                # ...
            ```
            The `execute_with_retry` in `DatabaseContext` returns `ScalarResult | Result | CursorResult | None`. You need to ensure you're getting a `CursorResult` if you expect `rowcount`.

    3.  **`src/family_assistant/storage/vector.py`**:
        *   Lines 261, 262 (`inserted_primary_key` errors): `inserted_primary_key` is a tuple (or sequence of tuples for multi-row inserts). Access it like `result.inserted_primary_key[0]` if it's a single value. More robustly, use `result.inserted_primary_key_rows[0][0]` for the first column of the first inserted row's PK.
            ```python
            # Example for src/family_assistant/storage/vector.py:261
            # result = await db_context.execute_with_retry(stmt) # Assuming execute_with_retry returns CursorResult
            # document_id = result.inserted_primary_key_rows[0][0] # Get first column of first row
            ```
        *   Lines 524, 768 (`rowcount` errors): Same as in `tasks.py`. Use `rowcount` on the `CursorResult`.
        *   Line 283: `ReturningInsert[tuple[int]]` vs `Insert`. If `stmt.returning(documents_table.c.id)` is used, the type of `stmt` changes. The variable `stmt` should probably be typed more specifically if its nature changes, or the receiving function must handle `ReturningInsert`. This error is often benign if the execution handles it correctly.
        *   Lines 658, 717, 718, 724 (`where`/`or_` arg type): SQLAlchemy conditions must be proper SQL expressions, not Python booleans.
            ```python
            # Example for src/family_assistant/storage/vector.py:658
            # Incorrect: where(documents_table.c.id == document_id is True)
            # Correct:   where(documents_table.c.id == document_id)

            # Example for src/family_assistant/storage/vector.py:717 (or_ issue)
            # Incorrect: or_(some_condition is True, another_condition is True)
            # Correct:   or_(some_condition, another_condition)
            # where some_condition is like (table.c.column == value)
            ```

    4.  **`src/family_assistant/storage/notes.py` & `src/family_assistant/storage/message_history.py`**:
        *   `rowcount` errors (e.g., `notes.py:142, 168`, `message_history.py:143`): Same fix as above, use `rowcount` on the `CursorResult`.

    5.  **`src/family_assistant/storage/__init__.py`**:
        *   Lines 378, 397: `SQLAlchemyError` vs `DBAPIError`. `SQLAlchemyError` is a base for many DB exceptions. `DBAPIError` is more specific. If you are trying to catch `DBAPIError`, ensure the `except` block is for `sqlalchemy.exc.DBAPIError`. If you want to catch broader SQLAlchemy errors, `SQLAlchemyError` is fine. The assignment `exc_val: DBAPIError | None = e` is problematic if `e` is a more general `SQLAlchemyError` or `Exception`.
            ```python
            # src/family_assistant/storage/__init__.py
            # except SQLAlchemyError as e:
            #    current_exc: DBAPIError | SQLAlchemyError | None = e # Make type broader or check isinstance
            # or
            # except DBAPIError as e:
            #    current_exc: DBAPIError | None = e
            ```

    6.  **`src/family_assistant/web/auth.py`**:
        *   Line 102: `Coroutine[...] has no attribute "__aenter__" / "__aexit__"`.
            ```python
            # src/family_assistant/web/auth.py:102
            # Change: async with get_db_context(engine=request.app.state.db_engine) as db_context:
            # To:
            async with await get_db_context(engine=request.app.state.db_engine) as db_context:
            ```
            (Because `get_db_context` is an `async def` function returning the context manager instance, you need to `await` the call to `get_db_context` itself.)
            Actually, `get_db_context` is already an `async def` that returns the context manager. The `Depends` mechanism in FastAPI handles calling it. The issue might be how `get_db_context` is defined or how `DatabaseContext` is implemented. If `get_db_context` returns `DatabaseContext()`, then `async with db_context_instance:` is correct. The error here is subtle.
            The dependency `get_user_from_api_token` is an `async def`. `get_db_context` is also `async def`.
            The line is `async with get_db_context(...) as db:`. This suggests `get_db_context` *returns* a coroutine that, when awaited, yields the async context manager.
            It should be `db_manager_coro = get_db_context(...)` then `async with await db_manager_coro as db:`.
            However, `get_db_context` is annotated to return `DatabaseContext`. And `DatabaseContext` has `async def __aenter__`.
            So, `async with actual_db_context_instance as db:` is the pattern.
            The problem is `get_db_context(engine=request.app.state.db_engine)` is a coroutine. It needs to be awaited to get the `DatabaseContext` instance.
            ```python
            db_context_instance = await get_db_context(engine=request.app.state.db_engine)
            async with db_context_instance as db:
                # ...
            ```
            This is unusual for FastAPI dependencies. A dependency usually *is* the resolved value.
            If `get_db_context` is a dependency itself injected into `get_user_from_api_token`, it would be:
            `async def get_user_from_api_token(..., db_context: DatabaseContext = Depends(get_db_context))`
            Then `async with db_context as session:` (if `db_context` is the manager).
            The current code `async with get_db_context(...)` is calling it directly.

    7.  **`src/family_assistant/web/dependencies.py`**:
        *   Line 31: `The return type of an async generator function should be "AsyncGenerator" or one of its supertypes`.
            ```python
            # src/family_assistant/web/dependencies.py
            from typing import AsyncGenerator # Add this import
            # ...
            async def get_db_context_dependency(...) -> AsyncGenerator[DatabaseContext, None]: # Change return type
                # ...
                yield db_context
                # ...
            ```

---

**Step 3: Fix `no_implicit_optional`, Data Type Mismatches (especially `vector_search`), and Related Return/Argument Types**

*   **Goal**: Ensure correct optional types, fix widespread type mismatches in `vector_search.py` by likely adjusting model field types, and correct function signatures.
*   **Files to change**: `src/family_assistant/storage/vector_search.py`, `src/family_assistant/web/routers/vector_search.py`, `src/family_assistant/web/routers/notes.py`, `src/family_assistant/web/routers/api.py`, `src/family_assistant/calendar_integration.py`, `src/family_assistant/llm.py`, `src/family_assistant/processing.py`, `src/family_assistant/task_worker.py`, `src/family_assistant/indexing/processors/llm_processors.py`.

*   **Fixes**:
    1.  **`src/family_assistant/web/routers/vector_search.py` (and potentially `src/family_assistant/storage/vector_search.py`)**:
        *   Lines 160, 161, 166, 167 (defaults `None` for `list[str]`):
            ```python
            # src/family_assistant/web/routers/vector_search.py
            embedding_types: list[str] | None = Query(None), # Add | None
            source_types: list[str] | None = Query(None),    # Add | None
            metadata_keys: list[str] | None = Query(None),   # Add | None
            metadata_values: list[str] | None = Query(None), # Add | None
            ```
        *   Line 268 (`search_type` literal):
            If `form_data.search_type` can be any string, you need validation before assigning to `VectorSearchQuery.search_type` if that field expects a `Literal`.
            ```python
            # src/family_assistant/web/routers/vector_search.py
            search_type_literal: Literal['semantic', 'keyword', 'hybrid']
            if form_data.search_type in get_args(Literal['semantic', 'keyword', 'hybrid']):
                search_type_literal = cast(Literal['semantic', 'keyword', 'hybrid'], form_data.search_type)
            else:
                raise HTTPException(status_code=400, detail="Invalid search_type")
            # ...
            query_obj = VectorSearchQuery(
                # ...
                search_type=search_type_literal,
            )
            ```
        *   Line 286 (`List item 0 has incompatible type "str | None"; expected "str"`):
            If `allowed_values_list` can contain `None` but the target expects `list[str]`:
            ```python
            # src/family_assistant/web/routers/vector_search.py
            # Example: If metadata_filters_parsed can have None values
            # metadata_filters_parsed = [ (k,v) for k,v in zip(...) if k is not None and v is not None ]
            # or filter them out before creating VectorSearchQuery
            ```

    2.  **`src/family_assistant/storage/vector_search.py` (Crucial: `int` assignment errors)**:
        The errors like `Incompatible types in assignment (expression has type "list[str]", target has type "int")` (lines 109, 112, 115, 118, 135, 144, 177, 178, 207, 253) indicate a fundamental mismatch. The target variables (fields of `VectorSearchQuery` or a similar internal structure) are typed as `int` but are being assigned `str`, `datetime`, `list[str]`, etc.
        **You must change the type annotations of these target fields.**
        Example: If `self.chunk_ids` is the target on line 109:
        ```python
        # In the class definition for VectorSearchQuery or similar
        # Old: chunk_ids: int | None = None
        # New: chunk_ids: list[str] | None = None

        # Old: created_at_start: int | None = None
        # New: created_at_start: datetime | None = None
        # (and similar for all other listed errors)
        ```
        This is a significant change impacting the data model used for vector search queries.

    3.  **`src/family_assistant/web/routers/notes.py`**:
        *   Line 93 (`db_context` default):
            ```python
            # src/family_assistant/web/routers/notes.py
            db_context: DatabaseContext | None = Depends(get_db_context_dependency_or_none), # Adjust dependency if needed
            # Or if it's not a dependency but an optional param:
            # db_context: DatabaseContext | None = None,
            ```
            If `get_db_context_dependency` is used, it should handle returning `None` or a valid context. The error suggests `None` is a direct default. The common FastAPI pattern is `db: Session = Depends(get_db)`. If optional, the dependency function itself might return `Optional[DatabaseContext]`.

    4.  **`src/family_assistant/web/routers/api.py`**:
        *   Line 202 (`db_context` default): Similar fix as `notes.py:93`.
        *   Line 76: `Unexpected keyword argument "calendar_config" for "ToolExecutionContext"`.
            Either remove `calendar_config=...` from the call, or add `calendar_config: YourCalendarConfigType | None = None` to the `ToolExecutionContext` dataclass definition in `src/family_assistant/tools/types.py`.

    5.  **`src/family_assistant/calendar_integration.py`**:
        *   Line 887 (`format_datetime_or_date` arg):
            The caller passes `Any | None`. The function expects `datetime | date`. The caller needs to ensure the type or the function needs to handle `Any | None` (e.g., by returning a default or raising an error if type is wrong).
        *   Lines 1068, 1148 (return `str | None` instead of `str`):
            Modify the functions to either guarantee a `str` return (e.g., `return value or ""`) or change their return type annotation to `str | None` and update callers.

    6.  **`src/family_assistant/llm.py`**:
        *   Lines 226, 227, 235, 236 (assignment to `dict[str, Any]`):
            These look like building the `record` dict.
            ```python
            # src/family_assistant/llm.py:226
            # If record["input_args"] should be a dict containing messages, tools, etc.
            record["input_args"] = { # Ensure this is initialized as a dict
                "method": "generate_response",
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "model_name": self.model_name,
                # ... other relevant args
            }
            # Instead of:
            # record["input_args"] = "generate_response" # Error: str to dict[str, Any]
            # record["input_args"] = messages # Error: list to dict[str, Any]
            ```
            Review how `record["input_args"]` and `record["output"]` are structured.
        *   Lines 269, 270 (Union attribute access on `ChatCompletion...MessageParam`):
            When accessing `message.content` or `message.tool_calls` from LiteLLM's completion messages, check the type of the message object or use `getattr` with a default.
            ```python
            # src/family_assistant/llm.py
            # Example for content
            content = getattr(message, "content", None)
            # Example for tool_calls
            tool_calls = getattr(message, "tool_calls", None)
            # Or, more type-safe if you know the structure from LiteLLM:
            # if isinstance(message, ChatCompletionAssistantMessage): # Use actual LiteLLM type
            #    tool_calls = message.tool_calls
            #    content = message.content
            ```

    7.  **`src/family_assistant/processing.py`**:
        *   Line 354 (`None` to `str` assignment for `user_name`):
            If `user_name` can be `None`, change its type to `str | None`. If it must be `str`, ensure a non-None value is assigned or raise an error.
            ```python
            # src/family_assistant/processing.py
            # user_name: str | None = "Unknown User" # If it can be None and has a default
            # or ensure user_name_from_trigger is always str
            ```

    8.  **`src/family_assistant/task_worker.py`**:
        *   Lines 396, 397 (Args to `ToolExecutionContext`):
            `payload.get("interface_type")` can return `None`. If `ToolExecutionContext` fields `interface_type` and `conversation_id` must be `str`, provide default values or raise an error if they are `None`.
            ```python
            # src/family_assistant/task_worker.py
            interface_type = payload.get("interface_type")
            if interface_type is None:
                interface_type = "unknown_interface" # Or raise error
            # ... similar for conversation_id
            context = ToolExecutionContext(
                # ...,
                interface_type=str(interface_type), # Cast if necessary, ensure not None
                conversation_id=str(conversation_id),
            )
            ```

    9.  **`src/family_assistant/indexing/processors/llm_processors.py`**:
        *   Lines 141, 443 (`tool_choice` arg type): OpenAI's `tool_choice` can be a string (`"auto"`, `"none"`, `"required"`) or a specific tool choice object `{"type": "function", "function": {"name": "my_function"}}`. The error `dict[str, Collection[str]]` vs `str | None` suggests the `tool_choice` dict being passed is not structured as OpenAI expects for specific function forcing. If you mean to force a tool, the structure is specific. If it's just "auto" or "none", pass the string. LiteLLM should handle this.

---

**Step 4: Addressing `None` Checks, Union Attribute Access, and Object State**

*   **Goal**: Add `None` checks or type guards before attribute access on `Optional` types or members of Union types, particularly in `telegram_bot.py`.
*   **Files to change**: `src/family_assistant/telegram_bot.py`, `src/family_assistant/calendar_integration.py`, `src/family_assistant/web/routers/webhooks.py`, `src/family_assistant/web/routers/api_token_management.py`, `src/family_assistant/indexing/processors/dispatch_processors.py`, `src/family_assistant/storage/tasks.py`.

*   **Fixes**:
    1.  **`src/family_assistant/telegram_bot.py` (Many `union-attr` errors)**:
        For every error like `Item "None" of "Chat | None" has no attribute "id"`, you need to ensure the object is not `None` before accessing the attribute.
        ```python
        # Example for src/family_assistant/telegram_bot.py:130
        # if update.message and update.message.chat: # Add checks
        #    chat_id = update.message.chat.id
        # else:
        #    # Handle missing message or chat
        #    return

        # Example for src/family_assistant/telegram_bot.py:309 (reply_text)
        # if update.message:
        #    await update.message.reply_text(...)
        # else:
        #    # Handle missing message

        # Example for src/family_assistant/telegram_bot.py:1002 (callback_query.answer)
        # if query: # query is CallbackQuery | None
        #    await query.answer()
        ```
        *   Lines 399-403 (`MessageOrigin` attributes): `python-telegram-bot` v21 changed how message origins are handled. Consult the documentation for `Message.origin` and its subtypes (e.g., `MessageOriginUser`, `MessageOriginChat`). You'll need to check `isinstance(update.message.origin, MessageOriginUser)` etc.
        *   Line 423 (`Dict entry 1 has incompatible type`):
            `self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2, **message_args)`
            If `message_args` is `{"disable_web_page_preview": "True"}` (string "True"), but FastAPI/Pydantic expects a boolean, this is the issue. Ensure `message_args` values have correct types. Or `parse_mode=ParseMode.MARKDOWN_V2` might be the issue if `message_args` contains something incompatible. The error `"str": "dict[str, str]"` suggests a value in `message_args` is a dict when a str is expected, or vice-versa.
        *   Lines 459, 767 (`await` on `AbstractAsyncContextManager`):
            This is similar to the `web/auth.py` error. If `get_db_context_func()` returns a coroutine that yields the manager:
            ```python
            # src/family_assistant/telegram_bot.py:459
            db_context_manager = await self.app_state.get_db_context_func()
            async with db_context_manager as db_context:
                # ...
            ```
            Or if `get_db_context_func` is the manager instance directly (but it's a func):
            This points to a pattern issue with how `get_db_context_func` is defined/used.

    2.  **`src/family_assistant/calendar_integration.py`**:
        *   Lines 196, 198, 203 (`Response | BaseException`):
            ```python
            # src/family_assistant/calendar_integration.py
            if isinstance(response_or_exc, httpx.Response):
                if response_or_exc.status_code != 200:
                    # ...
                calendar_data = response_or_exc.text
            else: # It's a BaseException
                logger.error(f"Failed to fetch iCal URL {url}: {response_or_exc}")
                continue
            ```

    3.  **`src/family_assistant/web/routers/webhooks.py` & `api_token_management.py`**:
        *   `datetime | None` attribute access (`tzinfo`, `replace`) and comparisons:
            ```python
            # src/family_assistant/web/routers/webhooks.py:87
            if parsed_email.date: # Check if not None
                if parsed_email.date.tzinfo is None or parsed_email.date.tzinfo.utcoffset(parsed_email.date) is None:
                    # src/family_assistant/web/routers/webhooks.py:88
                    timestamp_utc = parsed_email.date.replace(tzinfo=timezone.utc)
                else:
                    timestamp_utc = parsed_email.date.astimezone(timezone.utc)
            else:
                timestamp_utc = datetime.now(timezone.utc) # Default or handle error

            # src/family_assistant/web/routers/api_token_management.py:54 (comparison)
            if form_data.expires_at and form_data.expires_at < now_utc: # Ensure expires_at is not None
                # ...
            ```

    4.  **`src/family_assistant/indexing/processors/dispatch_processors.py`**:
        *   Line 136 (`Application | None`):
            ```python
            # src/family_assistant/indexing/processors/dispatch_processors.py
            if context.application and context.application.new_task_event: # Add checks
                context.application.new_task_event.set()
            ```

    5.  **`src/family_assistant/storage/tasks.py`** (dict attribute access, e.g. line 296):
        The `task_row: dict[str, Any]` should ideally be a Pydantic model or TypedDict for type safety.
        Short term: `status = task_row.get("status")`. Long term: define a `TaskData(TypedDict)` or Pydantic model.
        ```python
        # src/family_assistant/storage/tasks.py
        # Option 1: Use .get()
        status = task_row.get("status")
        if status == "scheduled" or status == "pending":
            # ...
        # Option 2: Define a TypedDict
        # class TaskRowData(TypedDict):
        # id: int
        # status: str
        # ...
        # task_data = cast(TaskRowData, task_row)
        # if task_data["status"] == ...
        ```

---

**Step 5: Protocol Conformance, Callback Signatures, Advanced Type Issues, and Remaining Errors**

*   **Goal**: Resolve complex type mismatches involving protocols, callback signatures, and other specific errors.
*   **Files to change**: Many, including `indexing/*`, `tools/__init__.py`, `telegram_bot.py`, `__main__.py`, `embeddings.py`, etc.

*   **Fixes**:
    1.  **Protocol Mismatches (`DocumentRecord` vs `Document` protocol)**:
        *   Files: `src/family_assistant/indexing/email_indexer.py:303`, `src/family_assistant/indexing/document_indexer.py:319`.
        *   The `Document` protocol (in `indexing/pipeline.py` or similar) likely expects plain types (e.g., `created_at: datetime`), but `DocumentRecord` (SQLAlchemy model) provides `Mapped[datetime]`.
        *   **Solution**: Create a Pydantic model that matches the `Document` protocol. Populate it from the `DocumentRecord` instance using `YourPydanticDocument.model_validate(doc_record_instance)` (Pydantic v2) or `YourPydanticDocument.from_orm(doc_record_instance)` (Pydantic v1) before passing to `IndexingPipeline.run`. Ensure the Pydantic model has `from_attributes = True` (or `orm_mode = True` for v1) in its config.

    2.  **`src/family_assistant/indexing/document_indexer.py`**:
        *   Lines 113-140 (Appending different processor types to `list[TitleExtractor]`):
            ```python
            # src/family_assistant/indexing/document_indexer.py
            # Change: processors: list[TitleExtractor] = []
            # To:
            processors: list[ContentProcessor] = []
            # ... then append TitleExtractor(), PDFTextExtractor(), etc.
            ```
        *   Line 155 (`list[TitleExtractor]` vs `list[ContentProcessor]`): This will be fixed by the above change.
        *   Line 259 (`int` to `str` for `document_id`): Change `document_id = db_doc.id` to `document_id = str(db_doc.id)`.

    3.  **`src/family_assistant/indexing/processors/file_processors.py:68`**: `Document` has no `id`.
        If the `Document` protocol is meant to represent any document that can be processed, and some stages need an ID that might only exist for persisted documents, the protocol or its usage needs refinement.
        *   Option A: Add `id: Any | None` (or `int | str | None`) to the `Document` protocol.
        *   Option B: The processor should receive a more specific type (e.g., `PersistedDocument(Document)`) if it relies on an `id`.
        *   Option C: Pass `original_document_id` separately if needed.

    4.  **`src/family_assistant/indexing/pipeline.py:190`**: `initial_content_ref` optionality.
        The `ContentProcessor.process` method signature:
        ```python
        # src/family_assistant/indexing/pipeline.py
        class ContentProcessor(Protocol):
            async def process(
                self,
                current_items: list[IndexableContent],
                original_document: "Document",
                initial_content_ref: IndexableContent | None, # Change to allow None if some processors don't need it
                context: "ToolExecutionContext",
            ) -> list[IndexableContent]: ...
        ```
        Update implementations if they now receive `IndexableContent | None`.

    5.  **Callback Signatures**:
        *   `ConfirmingToolsProvider` & `ProcessingService.generate_llm_response_for_chat` (tools/__init__.py:1793, telegram_bot.py:564):
            The `ConfirmingToolsProvider` calls its `confirmation_requester` with `(prompt_text, tool_name, tool_args, timeout)`.
            The `generate_llm_response_for_chat` in `ProcessingService` expects a callback with `(prompt_text: str, tool_name: str, tool_args: dict[str, Any])`.
            Align these. Either `ConfirmingToolsProvider` should not pass `timeout`, or the callback type in `ProcessingService` should accept it (e.g., `request_confirmation_callback: Callable[..., Awaitable[bool]] | None = None` and let the actual callback decide if it uses timeout).
            The error in `tools/__init__.py:1793` indicates `self.confirmation_requester` (which is `TelegramConfirmationUIManager.request_confirmation`) is being called incorrectly by `ConfirmingToolsProvider`. The `TelegramConfirmationUIManager.request_confirmation` *does* take `timeout`. The issue is that the *type hint* for `confirmation_requester` in `ConfirmingToolsProvider` or the type hint for `request_confirmation_callback` in `ProcessingService` doesn't match this.
            It seems `ProcessingService.generate_llm_response_for_chat` (line 404) defines `request_confirmation_callback: Callable[[str, str, dict[str, Any]], Awaitable[bool]] | None = None`.
            But `TelegramConfirmationUIManager.request_confirmation` (used as the callback) has `timeout: float`.
            The `Callable` in `ProcessingService` needs to be `Callable[[str, str, dict[str, Any], float], Awaitable[bool]] | None = None` if the `telegram_bot.py` implementation is the standard.

        *   `src/family_assistant/__main__.py:913` (`get_db_context_func` for `TelegramService`):
            `TelegramService` expects `Callable[..., AbstractAsyncContextManager[DatabaseContext, bool | None]]`.
            `get_db_context` is `Callable[..., Coroutine[Any, Any, DatabaseContext]]`.
            Since `DatabaseContext` *is* an async context manager, the issue is that `get_db_context` is an `async def`.
            `TelegramService` should likely expect `Callable[..., Awaitable[DatabaseContext]]`. If `TelegramService` is calling it like `ctx_mgr = await self.get_db_context_func()`, then this should work. The type hint in `TelegramService` might need to be `Callable[..., Awaitable[AbstractAsyncContextManager[DatabaseContext, Any]]]` or `Callable[..., Awaitable[DatabaseContext]]`.

        *   `src/family_assistant/__main__.py:954` (`register_task_handler`):
            Expected: `Callable[[ToolExecutionContext, Any], Awaitable[None]]`.
            Provided: `Callable[[DatabaseContext, dict[str, Any]], Coroutine[Any, Any, None]]` (for `process_document_indexing_task_from_payload`).
            The handler `process_document_indexing_task_from_payload` needs to be wrapped or adapted to match the expected signature. It needs to take `ToolExecutionContext` and `payload: Any`. It can get `DatabaseContext` from `ToolExecutionContext.get_db_context()`.

    6.  **`src/family_assistant/embeddings.py:291` (`SentenceTransformer` kwargs)**:
        ```python
        # src/family_assistant/embeddings.py
        def __init__(
            self, model_name_or_path: str, device: str | None = None, **model_kwargs: Any # Changed from object to Any
        ) -> None:
            # ...
            self.model = SentenceTransformer(
                model_name_or_path,
                device=device,
                **model_kwargs # Pass the more specific model_kwargs
            )
        ```
        This makes `model_kwargs` a dict for any other parameters `SentenceTransformer` might accept.

    7.  **Miscellaneous Errors**:
        *   `src/family_assistant/storage/message_history.py:356` & `src/family_assistant/web/routers/history.py:92` (var-annotated):
            Add type hints: `grouped_history: dict[str, list[dict[str, Any]]] = {}` (adjust types as needed).
        *   `src/family_assistant/telegram_bot.py:187` & `src/family_assistant/__main__.py:1078` (lambda type inference):
            Provide explicit types or convert to a `def`. E.g., `lambda x: cast(str, x).startswith("something")` or define a helper.
        *   `src/family_assistant/tools/mcp.py:256` (`BaseException` not iterable):
            Do not iterate over an exception object. Log it directly: `logger.error(f"Connection error: {e}")`.
        *   `src/family_assistant/calendar_integration.py:1044` (`value` attribute):
            Access iCalendar component values safely, e.g., `prop.dt` if `prop` is a DTSTART/DTEND property from `vobject`. Check `type(prop_value)` or `hasattr(prop_value, 'value')`.

---

This is a comprehensive list. Work through these steps methodically. For the `SQLAlchemy` and `vector_search.py` data type issues, careful consideration of your data models and query logic will be key. Good luck!
