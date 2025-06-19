# Fixing Type Issues in Family Assistant

Okay, this is a significant list of type errors! Let's break them down into logical, incremental steps to address them. Many errors stem from missing type stubs, incorrect protocol implementations, and issues with how asynchronous context managers are handled.

Here's a plan:

**Summary of Error Categories:**

1.  **Missing Type Stubs & Incorrect Library Usage:**Errors related to `caldav`, `litellm`, `python-telegram-bot`, `docker`, and `playwright` where the type checker doesn't have enough information or specific library features are misused.
2.  **Async Context Manager and Database Context Handling:**Issues with `get_db_context`, `DatabaseContext`, and how they conform to `AbstractAsyncContextManager`, primarily affecting `async with` usage and dependency injection.
3.  **Protocol Conformance & Interface Mismatches:**Core protocols like `EmbeddingGenerator`, `Document`, `LLMInterface`, and `ToolsProvider` are not being correctly implemented by various classes, especially in main code and mocks.
4.  **SQLAlchemy Type Issues:**Problems with SQLAlchemy's `Mapped` types, `Result` object attributes, and usage of `sqlalchemy.orm`.
5.  **Unbound Variables & Faulty Logic:**Several instances of variables used before assignment, or significant code blocks (like in `storage/vector.py`) that seem broken.
6.  **Telegram Bot Specifics:**Optional attribute access and specific Telegram object handling.
7.  **Application and Tool Execution Context:**Mismatches in expected types for `Application` state/attributes and `ToolExecutionContext`.
8.  **Test-Specific Type Problems:**Mock object incompatibilities, fixture return type annotations, and issues with patching in tests.
9.  **General Argument Type Mismatches & Attribute Access:**Various functions being called with incorrect argument types or attempts to access non-existent attributes.

---

Here are the suggested fixes, grouped into logical incremental steps:

## Step 1: Install Missing Type Stubs and Correct Basic Library Usage

This step aims to provide the type checker with more information about external libraries, which should resolve a broad class of errors.

* **File to change:**`pyproject.toml`
* **Issue:**Missing type stubs for several libraries.
* **Fix:**Add the following to your `[project.optional-dependencies.dev]` section in `pyproject.toml`:

    ```toml
    # In [project.optional-dependencies.dev]
    "types-caldav",
    "python-telegram-bot-stubs",
    "sqlalchemy-stubs", // If issues persist after other SQLAlchemy fixes
    // types-docker is already present
    // litellm and playwright should ship with types, but we'll address usage issues below
    ```

    Then run `uv pip install .[dev]` to install them.

* **File to change:**`src/family_assistant/llm.py`
    * **Issue (L40):**Private import `from litellm import _turn_on_debug`.
    * **Fix:**Replace with a public API for enabling debug mode if available, or remove if not critical for type checking. If essential for debugging, consider `# type: ignore[attr-defined]` if it's a known private API you need.
    * **Issue (L249, multiple errors):**`litellm.acompletion` call structure. Arguments are being passed as a single dictionary instead of keyword arguments.
    * **Fix:**Modify the call to `litellm.acompletion`. Instead of passing `self.llm_config` directly as multiple arguments, spread it or pick specific arguments:

        ```python
        # Example - adjust based on actual self.llm_config content and acompletion signature
        params_for_litellm = self.llm_config.copy()
        model_name = params_for_litellm.pop("model", self.model) # Assuming self.model is the default
        # Ensure all keys in params_for_litellm are valid for acompletion
        response: ModelResponse = await litellm.acompletion(
            model=model_name,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            # Pass other relevant parameters from self.llm_config
            temperature=params_for_litellm.get("temperature"),
            max_tokens=params_for_litellm.get("max_tokens"),
            # ... etc.
            **params_for_litellm, # If there are other compatible params
        )
        ```

    * **Issue (L252-L276):**Accessing attributes like `choices`, `message`, `content`, `tool_calls`, `usage` on `litellm` response objects.
    * **Fix:**After stubs/correct `acompletion` call, verify the response structure. It's typically `response.choices[0].message.content`, `response.choices[0].message.tool_calls`, `response.usage.total_tokens`, etc. Adjust access patterns according to `litellm`'s `ModelResponse` and `Choice` objects.

* **File to change:**`src/family_assistant/calendar_integration.py`
    * **Issue (L196-L203):**Accessing `status_code` / `text` on `BaseException` for `httpx` errors.
    * **Fix:**Change `except httpx.HTTPError as e:` or `except Exception as e:` to `except httpx.HTTPStatusError as e:`. Then `e.response.status_code` and `e.response.text` will be correctly typed.
    * **Issue (L689, L862, L1050, etc.):**`caldav.lib` is not a known attribute.
    * **Fix:**After installing `types-caldav`, these errors might resolve if `caldav.lib` is part of its typed API. More likely, exceptions are directly under `caldav.error` (e.g., `caldav.error.NotFoundError`). Update imports and exception handling:

        ```python
        from caldav import davclient # or specific client
        from caldav.lib import error as caldav_errors # Or from caldav import error as caldav_errors

        try:
            # ... caldav operations ...
        except caldav_errors.NotFoundError: # Or appropriate specific exception
            # ... handle ...
        ```

* **File to change:**`tests/conftest.py`
    * **Issue (L101):**`docker.errors` is not a known attribute.
    * **Fix:**Import specific exceptions: `from docker.errors import ImageNotFound, APIError # or other needed exceptions`.

* **File to change:**`src/family_assistant/utils/scraping.py`
    * **Issue (L20):**Private import usage for `PlaywrightContextManager`.
    * **Fix:**Playwright's async context manager is obtained via `async_playwright()`. The type of the object yielded by `async with async_playwright() as p:` is `Playwright`.
        Change the type hint for `async_playwright`:

        ```python
        from collections.abc import Callable
        from contextlib import AbstractAsyncContextManager # Or from typing_extensions for older Python
        from playwright.async_api import Playwright # Add this import

        # ...
        async_playwright_factory: Callable[[], AbstractAsyncContextManager[Playwright]] | None
        ```

        And update `_get_playwright_context_manager` if its return type hint was `PlaywrightContextManager`.

---

## Step 2: Refactor `get_db_context` and Correct Async Context Manager Usage

This step focuses on fixing the fundamental issue with how `DatabaseContext` is created and used, which causes numerous `__aenter__`/`__aexit__` errors.

* **File to change:**`src/family_assistant/storage/context.py`
    * **Issue:**`get_db_context` is an `async def` function that returns a `DatabaseContext` instance. This makes `get_db_context()` a coroutine, while it's often used as a factory for a context manager. `DatabaseContext` itself is an async context manager.
    * **Fix:**Make `get_db_context` a regular synchronous function.

        ```python
        # Change this:
        # async def get_db_context(
        #     engine: AsyncEngine | None = None, max_retries: int = 3, base_delay: float = 0.5
        # ) -> "DatabaseContext":
        # To this:
        def get_db_context(
            engine: AsyncEngine | None = None, max_retries: int = 3, base_delay: float = 0.5
        ) -> "DatabaseContext":
            # ... (any synchronous setup if needed) ...
            return DatabaseContext(
                engine or get_engine(), max_retries=max_retries, base_delay=base_delay
            )
        ```

* **Files to change:**`src/family_assistant/__main__.py` (L913), `src/family_assistant/telegram_bot.py` (for `TelegramUpdateHandler` init), `tests/functional/telegram/conftest.py` (L157, L173), `tests/functional/test_smoke_notes.py` (L166)
    * **Issue:**Parameters like `get_db_context_func` are typed expecting a complex async factory, or the call sites are incorrect due to `get_db_context` being async.
    * **Fix:**With `get_db_context` now being a synchronous factory, update type hints for `get_db_context_func` to `Callable[..., DatabaseContext]`.
        In `Application.__init__` and `TelegramUpdateHandler.__init__`:

        ```python
        # Example for Application or TelegramUpdateHandler
        from collections.abc import Callable
        # ...
        def __init__(self, ..., get_db_context_func: Callable[..., DatabaseContext], ...):
            self.get_db_context = get_db_context_func # Store the factory
        ```

        Calls like `async with self.get_db_context() as db:` will now correctly use the `DatabaseContext` instance returned by the factory.
        The errors in `__main__.py:913` and `tests/functional/telegram/conftest.py` regarding `CoroutineType` vs `AbstractAsyncContextManager` for `get_db_context_func` should resolve.

* **File to change:**`src/family_assistant/web/dependencies.py`
    * **Issue (L31, L35):**FastAPI dependency `get_db` return type.
    * **Fix:**Ensure `get_db` is correctly typed as an async generator yielding `DatabaseContext`.

        ```python
        from collections.abc import AsyncIterator # Use AsyncIterator

        # ...
        async def get_db(
            # ... params ...
        ) -> AsyncIterator[DatabaseContext]: # Changed to AsyncIterator
            db_ctx = get_db_context() # Now a sync call
            async with db_ctx as db: # db_ctx is the DatabaseContext instance
                yield db
        ```

* **File to change:**`src/family_assistant/web/auth.py`
    * **Issue (L102):**Using `get_db_context()` with `async with`.
    * **Fix:**This should now work correctly after `get_db_context` is made synchronous:

        ```python
        async with get_db_context() as db_context: # get_db_context() returns DatabaseContext instance
            # ...
        ```

---

## Step 3: Critical Code Cleanup in `storage/vector.py`

This addresses a major structural problem.

* **File to change:**`src/family_assistant/storage/vector.py`
    * **Issue (L812-L981):**A large block of code, seemingly a corrupted duplication of `query_vector_store_in_db_only`'s body and a duplicate `update_document_title_in_db` definition, exists outside any function, causing numerous `UndefinedVariable`, indentation, `await` outside async, and `return` outside function errors.
    * **Fix:**
        1.  Delete the entire block of code starting from the line `distance_op = DocumentEmbeddingRecord.embedding.cosine_distance` (around L812) down to `return results_with_scores` (around L975 or just before the second `def update_document_title_in_db`).
        2.  Remove the duplicate definition of `update_document_title_in_db` (the one that appears after the problematic block). There should only be one definition of this function.
    * **Issue (L110, L147):**`sqlalchemy.orm` access.
    * **Fix:**These are generally correct. If errors persist after installing `sqlalchemy-stubs`, ensure direct imports: `from sqlalchemy.orm import declarative_base, Mapped`.
    * **Issue (L895, L954, L955, L961):**Using raw booleans (`True`/`False`) in SQLAlchemy `where()` or `or_()` clauses.
    * **Fix:**Use `sqlalchemy.sql.expression.true()` and `sqlalchemy.sql.expression.false()`.

        ```python
        from sqlalchemy import true as sql_true, false as sql_false, or_, and_ # etc.

        # Example
        # query = query.where(True) # Incorrect
        query = query.where(sql_true()) # Correct

        # combined_conditions = or_(True, existing_condition) # Incorrect
        # combined_conditions = or_(sql_true(), existing_condition) # Correct
        ```

        This applies to the `_build_document_filters` and `_build_embedding_filters` helper methods if they construct conditions this way.

---

## Step 4: Address Protocol Conformance and Interface Mismatches (LLM, Embedding, Document)

* **Files to change:**`src/family_assistant/__main__.py` (L704), `src/family_assistant/embeddings.py` (L261)
    * **Issue:**`SentenceTransformerEmbeddingGenerator` and `EmbeddingGenerator` protocol mismatch. The error in `__main__.py` mentions `model_name` is not present on `SentenceTransformerEmbeddingGenerator` for the protocol. The "obscured" error in `embeddings.py` points to a naming conflict or confusion due to conditional definition.
    * **Fix for `embeddings.py` (L261):**The `SentenceTransformerEmbeddingGenerator` is defined inside `if SENTENCE_TRANSFORMERS_AVAILABLE:`. This can confuse linters. Ensure it's uniquely named or provide a clear stub if the library is unavailable.

        ```python
        # In embeddings.py
        if SENTENCE_TRANSFORMERS_AVAILABLE:
            # Ensure this class correctly implements EmbeddingGenerator
            class SentenceTransformerEmbeddingGenerator(EmbeddingGenerator): # Explicitly inherit
                def __init__(
                    self, model_name_or_path: str, device: str | None = None, **kwargs: object
                ) -> None:
                    # ...
                    self.model_name = model_name_or_path # Store if needed for protocol
                    # ...
                async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
                    # ... implementation ...
        else:
            # Optional: Define a placeholder if needed for type checking in all paths
            class SentenceTransformerEmbeddingGenerator(EmbeddingGenerator):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    raise NotImplementedError("sentence-transformers not available")
                async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
                    raise NotImplementedError("sentence-transformers not available")
        ```

    * **Fix for `__main__.py` (L704):**The `EmbeddingGenerator` protocol in `embeddings.py` expects `async def generate_embeddings(...)`. The error says `"generate_embeddings" is not present`. This implies the `SentenceTransformerEmbeddingGenerator` instance being created in `__main__.py` (or the class definition it's using) doesn't match. Ensure the class has the method. The `model_name` part of the error is confusing as it's not in the `EmbeddingGenerator` protocol. It might be an issue with how the instance is created or used later in `__main__.py`. Double-check the constructor call and subsequent usage.

* **File to change:**`tests/mocks/mock_llm.py`
    * **Issue (L13 and throughout tests):**`LLMOutput` and `LLMInterface` types from `tests.mocks.mock_llm` are incompatible with those from `family_assistant.llm`. This is due to the try-except import fallback.
    * **Fix:**Remove the try-except import for `LLMInterface` and `LLMOutput`. Always import from the main application.

        ```python
        # In tests/mocks/mock_llm.py
        # Remove the try-except block:
        # try:
        # from family_assistant.llm import LLMInterface as LLMInterface_real, LLMOutput as LLMOutput_real # noqa: E402
        # except ImportError:
        #     # ... fallback definitions ...
        # else:
        #     LLMInterface = LLMInterface_real
        #     LLMOutput = LLMOutput_real

        # Always import directly:
        from family_assistant.llm import LLMInterface, LLMOutput # noqa: E402
        from typing import Any, Callable, Coroutine, Literal # etc.

        # Ensure RuleBasedMockLLMClient and other mocks correctly implement this imported LLMInterface
        # and use this imported LLMOutput.
        class RuleBasedMockLLMClient(LLMInterface):
            # ...
            async def generate_response(
                self,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                tool_choice: str | None = "auto",
                # ... any other params from LLMInterface ...
            ) -> Coroutine[Any, Any, LLMOutput]: # Ensure return type is the imported LLMOutput
                # ...
                # return matched_rule_output
        ```

        This will fix many `RuleBasedMockLLMClient is not assignable to LLMInterface` errors in test files.

* **Files to change:**`src/family_assistant/indexing/document_indexer.py` (L319), `src/family_assistant/indexing/email_indexer.py` (L303), `tests/functional/indexing/test_indexing_pipeline.py` (L255, L460)
    * **Issue:**Passing `DocumentRecord` (SQLAlchemy model with `Mapped[str]`) to functions expecting `Document` (protocol, likely expecting resolved `str`).
    * **Fix:**At runtime, SQLAlchemy typically resolves `Mapped[str]` to `str` upon attribute access. This might be a type checker limitation.
        1.  Ensure the `Document` protocol (defined likely in `src/family_assistant/storage/vector_store_protocol_corrected.py` or a similar place, or should be if not) expects `str` for its attributes like `source_type`, `source_id`.
        2.  At the call site where a `DocumentRecord` is passed as a `Document`, if the error persists:

            ```python
            from typing import cast
            from family_assistant.storage.vector_store_protocol_corrected import Document # Adjust import

            # ...
            document_record: DocumentRecord = get_document_record()
            # pipeline.run(..., original_document=document_record, ...) # Causes error
            pipeline.run(..., original_document=cast(Document, document_record), ...)
            # Or, if it's a specific argument:
            # pipeline.run(..., original_document=document_record, ...) # type: ignore[arg-type]
            ```

        This assumes the runtime shapes are compatible.

* **File to change:**`src/family_assistant/__main__.py` (L954)
    * **Issue:**Task handler signature mismatch. `_handle_embed_and_store_batch` takes `(DatabaseContext, payload)` but `TaskWorker.register_task_handler` expects `(ToolExecutionContext, Any)`.
    * **Fix:**Modify `_handle_embed_and_store_batch` signature and logic:

        ```python
        from family_assistant.tools.types import ToolExecutionContext # Add import

        async def _handle_embed_and_store_batch(
            context: ToolExecutionContext, payload: dict[str, Any]
        ) -> None:
            async with context.db_provider() as db_context: # Get db_context from ToolExecutionContext
                # ... existing logic using db_context and payload ...
        ```

---

## Step 5: Address Unbound Variables, Remaining Attribute Access, and Argument Mismatches

* **File to change:**`src/family_assistant/__main__.py`
    * **Issue (L739):**`"None" is not awaitable`. This is because `run_migrations_online_async_wrapper` returns `None` if not using PostgreSQL, and this `None` is added to `tasks_to_run`.
    * **Fix (L729-L739):**

        ```python
        migrator_task = None
        if str(DATABASE_URL).startswith("postgresql"):
            migrator_task = run_migrations_online_async_wrapper()

        tasks_to_run = [ # Removed other tasks for brevity, add them back
            # ... other tasks ...
        ]
        if migrator_task:
            tasks_to_run.append(migrator_task)
        if task_worker: # Assuming task_worker might also be conditional
            tasks_to_run.append(task_worker.run(wake_up_event))

        if tasks_to_run: # Ensure tasks_to_run is not empty
            await asyncio.gather(*tasks_to_run)
        else:
            logger.info("No main tasks to run (e.g., no DB migration, no task worker).")
        ```

* **Files with unbound variables:**
    *`src/family_assistant/indexing/processors/llm_processors.py` (L177, L499 `arguments_str`): Initialize `arguments_str: str | None = None` before the `try` block.
    *`src/family_assistant/indexing/processors/text_processors.py` (L158 `output_embedding_type`): Initialize before use, e.g., `output_embedding_type: str = ""`.
    *`src/family_assistant/processing.py` (L441 `final_reasoning_info`): Initialize `final_reasoning_info: dict[str, Any] | None = None`.
    *`src/family_assistant/tools/schema.py` (L86-89 `infile_path`, `outfile_path`): Initialize these paths, e.g., `infile_path: Path | None = None`.
    *`src/family_assistant/utils/scraping.py` (L161 `ActualMarkItDownClass`):
        `MarkItDownType: type["ActualMarkItDownClass"] | None = None` and then `_MarkItDown_cls: MarkItDownType | None = None`. If `markitdown` is imported, `_MarkItDown_cls` is set. The reference to `ActualMarkItDownClass` in `_convert_bytes_to_markdown` likely means `_MarkItDown_cls` should be checked for `None` before use.

        ```python
        if self._MarkItDown_cls:
             md = self._MarkItDown_cls(file_path=temp_pdf_path) # Call the class
             # ...
        else:
            # Handle case where markitdown is not available
            logger.warning("MarkItDown library not available for PDF processing.")
            return "Error: PDF processing library not available."
        ```
    *`src/family_assistant/telegram_bot.py` (L763 `user_message_id`): Initialize `user_message_id: int | None = None`.

* **Attribute Access and Argument Errors:**
    *`src/family_assistant/indexing/processors/dispatch_processors.py` (L136): `context.application.new_task_event`.

        * **Fix:**`ToolExecutionContext.application` refers to `telegram.ext.Application`. Ensure `new_task_event` is an attribute you've added to this `Application` instance (e.g., in `__main__.py` via `app.new_task_event = asyncio.Event()`). If `application` can be `None` in `ToolExecutionContext`, check for it.
    *`src/family_assistant/indexing/processors/file_processors.py` (L68), `src/family_assistant/indexing/processors/metadata_processors.py` (L94, L100): `original_document.id`.

        * **Fix:**If `original_document` is typed as the `Document` protocol, ensure `id: int` (or appropriate type) is part of the protocol definition.
    *`src/family_assistant/storage/context.py` (L201): `self.connection.is_active`.

        * **Fix:**This attribute should exist on `aiosqlite.Connection` or `asyncpg.Connection`. Ensure `self.connection` is not `None` and is correctly typed.
    *`src/family_assistant/storage/tasks.py` (L345), `src/family_assistant/storage/vector.py` (L529): `result.rowcount`.

        * **Fix:**`SQLAlchemy` `Result` objects have `rowcount`. Ensure `result` is not `None`. This might be a stub issue if `sqlalchemy-stubs` are not yet effective.
    *`src/family_assistant/tools/mcp.py` (L256): `"BaseException" is not iterable`.

        * **Fix:**Likely `for detail in e.args:` or similar if `e` is an exception with details. `mcp.exceptions.MCPError` might have specific fields.
    *`src/family_assistant/tools/mcp.py` (L350-351): `content_part.text` on `ImageContent`/`EmbeddedResource`.

        * **Fix:**These `mcp` types likely don't have a `.text` attribute. Check `mcp` documentation for how to get textual descriptions or relevant data from these content parts. It might be `content_part.description` or you might need to handle them differently.
    *`src/family_assistant/indexing/document_indexer.py` (L259): `metadata_dict[key] = int_value`. If `metadata_dict` expects string values, use `str(int_value)`.
    *`src/family_assistant/indexing/email_indexer.py` (L212): `EmailDocument.from_row(dict_row)`. `from_row` expects `RowMapping`.

        * **Fix:**If `dict_row` is a `dict`, you might need to convert it or ensure `EmailDocument.from_row` can handle a `dict`. `cast(RowMapping, dict_row)` is an option if the structure is compatible.
    *`src/family_assistant/indexing/pipeline.py` (L190): `processor.process(..., initial_content_ref=parent_ref)`. `parent_ref` can be `None`.

        * **Fix:**Ensure processors whose `process` method expects a non-optional `initial_content_ref` are called with a valid one, or update their signatures to accept `IndexableContent | None`.
    *`src/family_assistant/indexing/processors/llm_processors.py` (L141, L443): `tool_choice={"type": "function", ...}`.

        * **Fix:**The `litellm.acompletion` function *does*support this dictionary format for `tool_choice`. This error suggests the type stub for `litellm` might be too strict or outdated for this parameter. If `litellm` stubs are up-to-date, this might require a `# type: ignore[arg-type]`.
    *`src/family_assistant/llm.py` (L226-L236, dictionary assignments): e.g., `record_args["method_name"] = method_name`.

        * **Fix:**These assignments look correct for a `dict[str, Any]`. If errors persist, it could be a very specific type inference issue. Explicitly annotate `record_args: dict[str, Any] = {}`. If this is already the case, this might be a `pyright` quirk or a more complex type interaction.
    *`src/family_assistant/web/routers/api.py` (L76, L80): Call signature mismatches for `process_message` (missing `turn_id`) and `fetch_upcoming_events` (using `calendar_config` which might be removed/renamed).

        * **Fix:**Update the calls to match the current function signatures.
    *`src/family_assistant/web/routers/vector_search.py` (L160, L161, L166, L167): Passing `None` from `Form(None)` to list parameters.

        * **Fix:**Use `embedding_types=embedding_type or []` when constructing `VectorSearchQuery` or other objects.
    *`src/family_assistant/web/routers/vector_search.py` (L268): `search_type_form` (str) to `VectorSearchQuery.search_type` (Literal).

        * **Fix:**Validate `search_type_form` and cast or map to the Literal type.

            ```python
            from typing import cast, Literal
            SearchTypeLiteral = Literal['semantic', 'keyword', 'hybrid']
            # ...
            query_search_type = cast(SearchTypeLiteral, search_type_form) # Add validation
            ```
    *`src/family_assistant/web/routers/vector_search.py` (L286): `generate_embeddings(texts=[query_text])` where `query_text` can be `None`.

        * **Fix:**Ensure `query_text` is `str` before passing: `if query_text: embeddings = await emb_gen.generate_embeddings(texts=[query_text])`.
    *`src/family_assistant/task_worker.py` (L396, L397): `task_payload.get(...)` can return `None`.

        * **Fix:**Provide defaults or raise error if required: `interface_type = task_payload.get("interface_type", "unknown")`.
    *`src/family_assistant/telegram_bot.py` (L421): `final_messages_for_llm.append({"role": "user", "content": content_parts})`.

        * **Fix:**`content_parts` is `list[dict[str, str]]`. `litellm` user message content can be a string or a list of content blocks for multimodal. If `content_parts` represents multimodal content, it's likely correct. The error suggests `final_messages_for_llm` is typed too narrowly (`list[dict[str, str]]`). It should be `list[dict[str, Any]]` or the more specific `list[ChatCompletionMessageParam]` from `litellm.types`.
    *`src/family_assistant/telegram_bot.py` (L564): `request_confirmation_callback` signature mismatch (extra `timeout`).

        * **Fix:**Align the callback signature in `TelegramConfirmationUIManager.request_confirmation` with what `ProcessingService.generate_llm_response_for_chat` expects, or vice-versa. It seems `ProcessingService` expects `(prompt_text, tool_name, tool_args)` but `TelegramConfirmationUIManager` provides `(..., timeout)`.
    *`src/family_assistant/telegram_bot.py` (L1123): `message_batcher=None` passed to `TelegramUpdateHandler`.

        * **Fix:**`TelegramUpdateHandler.__init__` expects `message_batcher: MessageBatcher`. Ensure a valid `MessageBatcher` (e.g., `NoBatchMessageBatcher()`) is always passed if `telegram_app_config.message_batcher` can be `None`.
    *`src/family_assistant/tools/__init__.py` (L833, L836): `event.get("DTSTART", {}).get("dt")` can be `None`.

        * **Fix:**Check for `None` before calling `format_datetime_or_date`.

            ```python
            dt_val = event.get("DTSTART", {}).get("dt")
            if dt_val:
                start_str = format_datetime_or_date(dt_val, timezone_str)
            ```
    *`src/family_assistant/tools/__init__.py` (L1794): `get_user_from_username_or_id()` missing arguments.

        * **Fix:**Provide the required arguments for this function call.

---

## Step 6: Address Telegram-Specific and Test Environment Issues

* **File to change:**`src/family_assistant/telegram_bot.py` (numerous `reportOptionalMemberAccess` errors)
    * **Issue:**Accessing attributes on `telegram` objects that might be `None` (e.g., `update.message`, `query.message`).
    * **Fix:**After installing `python-telegram-bot-stubs`, these will be more apparent. Use conditional access:

        ```python
        if update.message and update.message.chat: # Check both message and chat
            chat_id = update.message.chat.id
        # For query.message.reply_text:
        if query and query.message:
            await query.message.reply_text(...)
        ```

        For `MessageOrigin` attributes (L399-L403), check stubs for correct access (e.g. `update.effective_message.forward_from_user`, `update.effective_message.forward_from_chat`).
        `query.message.text_markdown_v2` (L1019): `text_markdown_v2` might not be a direct attribute or could be on `query.message.effective_attachment` if it's a caption.

* **Fixture Return Types in Tests:**(`tests/conftest.py`, `tests/functional/...`)
    * **Issue:**Fixtures yielding values are typed with the yielded type, not `Iterator` or `AsyncIterator`.
    * **Fix:**

        ```python
        from collections.abc import Iterator, AsyncIterator # Use these

        @pytest.fixture
        def my_path_fixture() -> Iterator[Path]: # Not -> Path
            p = Path("...")
            yield p
            # cleanup

        @pytest_asyncio.fixture
        async def my_async_client_fixture() -> AsyncIterator[AsyncClient]: # Not -> AsyncClient
            async with AsyncClient(...) as client:
                yield client
        ```

        Apply this to affected fixtures in:
        `tests/conftest.py` (L119, L160 `db_engine`)
        `tests/functional/indexing/processors/test_llm_intelligence_processor.py` (L22, L30 `temp_file_path_fixture`)
        `tests/functional/indexing/test_document_indexing.py` (L171, L211 `test_app_client_with_docs`)

* **`with patch(...)` errors in tests:**(`tests/functional/telegram/test_telegram_confirmation.py`, `tests/unit/indexing/processors/test_network_processors.py`)
    * **Issue:**`Object of type "Generator[None, None, None]" cannot be used with "with"`. This often happens when patching a non-context-manager or if a `contextlib.contextmanager` decorated fixture is not typed correctly.
    * **Fix for `test_network_processors.py` (L235, `open`):**`mock_open_with_interrupt` should be compatible with `open`. If using `unittest.mock.mock_open`, it is a context manager. Ensure `mock_open_with_interrupt` itself is structured correctly. If it's a generator, it needs `@contextlib.contextmanager`.
    * **Fix for `test_telegram_confirmation.py` (L155 etc.):**If `mock_confirmation_timeout` is a fixture using `@contextlib.contextmanager`, its type hint should be `Iterator[None]` or `ContextManager[None]`.

        ```python
        from contextlib import contextmanager
        from collections.abc import Iterator

        @contextmanager
        def mock_confirmation_timeout() -> Iterator[None]: # Type hint for the generator
            # ... setup ...
            yield
            # ... teardown ...
        ```

    *For `os.remove` (L508 in `test_network_processors.py`): `os.remove(temp_file_path)` argument should be checked if `temp_file_path` can be `None`.

* **Mock Attribute Assignments in Tests:**(`tests/functional/indexing/test_document_indexing.py` L162, L163, etc.)
    * **Issue:**Assigning to attributes not defined on `MockEmbeddingGenerator`.
    * **Fix:**Either define these attributes in `MockEmbeddingGenerator` or use `mocker.patch.object(mock_embedding_generator, '_test_query_semantic_embedding', return_value=...)`.

* **`Application` type mismatch in `ToolExecutionContext` for tests:**(`tests/functional/indexing/...`)
    * **Issue:**`ToolExecutionContext` expects `telegram.ext.Application`, but `FastAPI` app instance is passed.
    * **Fix:**In test setups for indexing (where the Telegram bot part might not be relevant), provide a mock `telegram.ext.Application` or `None` if `ToolExecutionContext.application` is optional and handled.

        ```python
        from unittest.mock import MagicMock
        # ...
        mock_bot_app = MagicMock(spec=telegram.ext.Application)
        # If new_task_event is needed:
        mock_bot_app.new_task_event = asyncio.Event()
        tool_exec_context = ToolExecutionContext(application=mock_bot_app, ...)
        ```

* **`tests/conftest.py` (L203):**`TelegramApplication(processing_service=None, ...)`
    * **Fix:**`TelegramApplication.__init__` likely expects a `ProcessingService` instance. Provide a mock: `mock_processing_service = MagicMock(spec=ProcessingService)`.

* **`tests/functional/telegram/test_telegram_handler.py` (L45):**`context._bot = mock_bot`.
    * **Fix:**If `context.bot` is a public property with a setter, use that. Otherwise, this is internal manipulation for testing and might need `# type: ignore[assignment]` or `object.__setattr__(context, '_bot', mock_bot)`.

* **`tests/unit/test_processing_history_formatting.py` (L42, L43):**Mock types vs Protocol.
    * **Fix:**
        *`MockLLMClient` needs to implement `async def format_user_message_with_file(...)` as per `LLMInterface`.
        *`MockToolsProvider` needs `async def close(...)`. Its `execute_tool` return type must be `str` (or whatever `ToolsProvider` protocol dictates).

This is a comprehensive list. Work through these steps methodically, re-running the type checker after each major step or file group to see which errors resolve. Good luck!
