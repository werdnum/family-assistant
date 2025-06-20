# Code Reference

**Note:**This document is periodically generated and may be slightly out of date. It should be updated when making significant changes to the codebase.

## High-Level Overview

The Family Assistant is an LLM-powered application designed to centralize family information and automate tasks. It is built with Python, FastAPI for the web interface, and `python-telegram-bot` for Telegram integration. It uses SQLAlchemy for database interactions (supporting SQLite and PostgreSQL) and LiteLLM for LLM communication.

The core architecture consists of:

- **User Interfaces (`src/family_assistant/web/routers`, `src/family_assistant/telegram_bot.py`):**Handles user interaction via web UI, Telegram, and email webhooks.
- **Processing Layer (`src/family_assistant/processing.py`):**Orchestrates LLM calls, tool execution, and context management.
- **Tools (`src/family_assistant/tools`):**Defines and implements functions the LLM can call, including local Python functions and external MCP (Model Context Protocol) tools.
- **Storage (`src/family_assistant/storage`):**Manages all database interactions, including message history, notes, tasks, emails, and vector embeddings for documents.
- **Indexing (`src/family_assistant/indexing`):**Handles the ingestion and processing of documents and emails into a searchable format, including text extraction, chunking, and embedding generation.
- **LLM & Embeddings (`src/family_assistant/llm.py`, `src/family_assistant/embeddings.py`):**Provides interfaces and implementations for interacting with Large Language Models and generating vector embeddings.
- **Utilities (`src/family_assistant/utils`):**Common helper functions like web scraping.
- **Configuration (`src/family_assistant/__main__.py`):**Centralized loading and management of application settings.

## File-by-File Reference

### `src/family_assistant/assistant.py`

**Description:**Orchestrates the Family Assistant application's lifecycle, including dependency setup, service initialization, and graceful shutdown. It manages the complex wiring of LLM clients, tool providers, processing services, Telegram bot, web server, and task worker.

**Major Symbols:**

- `Assistant`: Main class for the application's core logic and lifecycle management.
- `setup_dependencies()`: Initializes and wires up all core application components.
- `start_services()`: Starts all long-running services and waits for shutdown.
- `stop_services()`: Gracefully stops all managed services.
- `initiate_shutdown()`: Sets the shutdown event to begin graceful shutdown.
- `is_shutdown_complete()`: Checks if the shutdown process has completed.

**Internal Dependencies:**

- `family_assistant.embeddings`
- `family_assistant.indexing.document_indexer`
- `family_assistant.indexing.email_indexer`
- `family_assistant.indexing.tasks`
- `family_assistant.llm`
- `family_assistant.processing`
- `family_assistant.storage`
- `family_assistant.storage.context`
- `family_assistant.task_worker`
- `family_assistant.telegram_bot`
- `family_assistant.tools`
- `family_assistant.tools.types`
- `family_assistant.utils.scraping`
- `family_assistant.web.app_creator`
- `family_assistant.context_providers`

### `src/family_assistant/__main__.py`

**Description:**The main entry point for the Family Assistant application. It handles configuration loading, argument parsing, and orchestrates the lifecycle of the `Assistant` class, which encapsulates service initialization (LLM, embedding, database, Telegram, web server, task worker) and graceful shutdown.

**Major Symbols:**

- `load_config()`: Loads configuration from defaults, `config.yaml`, and environment variables.
- `main()`: Synchronous entry point that calls `load_config` and runs the `Assistant`.
- `shutdown_handler()`: Gracefully shuts down services upon receiving signals.
- `reload_config_handler()`: Placeholder for SIGHUP to reload config.

**Internal Dependencies:**

- `family_assistant.embeddings` (EmbeddingGenerator, LiteLLMEmbeddingGenerator, MockEmbeddingGenerator, SentenceTransformerEmbeddingGenerator)
- `family_assistant.storage` (init_db, init_vector_db, get_db_context)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.indexing.document_indexer` (DocumentIndexer)
- `family_assistant.indexing.email_indexer` (EmailIndexer)
- `family_assistant.indexing.tasks` (handle_embed_and_store_batch)
- `family_assistant.llm` (LiteLLMClient, LLMInterface)
- `family_assistant.processing` (ProcessingService, ProcessingServiceConfig)
- `family_assistant.task_worker` (TaskWorker, handle_llm_callback, new_task_event, shutdown_event, original_handle_log_message)
- `family_assistant.tools` (CompositeToolsProvider, ConfirmingToolsProvider, LocalToolsProvider, MCPToolsProvider, ToolsProvider, _scan_user_docs, AVAILABLE_FUNCTIONS, TOOLS_DEFINITION)
- `family_assistant.tools.types` (ToolExecutionContext)
- `family_assistant.utils.scraping` (PlaywrightScraper)
- `family_assistant.web.app_creator` (app)
- `family_assistant.telegram_bot` (TelegramService)
- `family_assistant.context_providers` (CalendarContextProvider, KnownUsersContextProvider, NotesContextProvider)
- `family_assistant.assistant`

### `src/family_assistant/__init__.py`

**Description:**Package initialization file. Sets up basic logging configuration.

**Major Symbols:**

- `LOGGING_CONFIG`: Environment variable for logging configuration file.

**Internal Dependencies:**None (only standard library `logging`).

### `src/family_assistant/calendar_integration.py`

**Description:**Handles integration with CalDAV and iCalendar services for fetching and managing events. Provides helper functions for date/time formatting and tool implementations for calendar actions.

**Major Symbols:**

- `format_datetime_or_date()`: Formats `datetime` or `date` objects for display.
- `parse_event()`: Parses VCALENDAR data into a dictionary.
- `_fetch_ical_events_async()`: Asynchronously fetches and parses events from iCal URLs.
- `_fetch_caldav_events_sync()`: Synchronously fetches and parses events from CalDAV URLs.
- `fetch_upcoming_events()`: Orchestrates fetching events from all configured calendar sources.
- `format_events_for_prompt()`: Formats fetched events into strings suitable for LLM prompts.
- `add_calendar_event_tool()`: Tool to add a new event to a CalDAV calendar.
- `search_calendar_events_tool()`: Tool to search for calendar events.
- `modify_calendar_event_tool()`: Tool to modify an existing calendar event.
- `delete_calendar_event_tool()`: Tool to delete a calendar event.

**Internal Dependencies:**

- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/context_providers.py`

**Description:**Defines the `ContextProvider` protocol and provides concrete implementations for injecting dynamic context (like notes, calendar events, known users, and weather information) into the LLM's system prompt.

**Major Symbols:**

- `ContextProvider` (Protocol): Interface for context providers.
- `NotesContextProvider`: Provides context from stored notes.
- `CalendarContextProvider`: Provides context from calendar events.
- `KnownUsersContextProvider`: Provides context about configured known users.
- `WeatherContextProvider`: Provides context from the WillyWeather API.

**Internal Dependencies:**

- `family_assistant.calendar_integration`
- `family_assistant.storage`
- `family_assistant.storage.context` (DatabaseContext)
- `httpx`

### `src/family_assistant/embeddings.py`

**Description:**Defines the `EmbeddingGenerator` protocol and provides implementations for generating text embeddings using various models (LiteLLM, Sentence Transformers, Mock, Hashing Word).

**Major Symbols:**

- `EmbeddingResult`: Dataclass for embedding generation results.
- `EmbeddingGenerator` (Protocol): Interface for embedding generators.
- `LiteLLMEmbeddingGenerator`: Uses LiteLLM for API-based embedding models.
- `HashingWordEmbeddingGenerator`: Generates simple hash-based word embeddings.
- `SentenceTransformerEmbeddingGenerator`: Uses local `sentence-transformers` models.
- `MockEmbeddingGenerator`: A mock implementation for testing.

**Internal Dependencies:**None (only external libraries like `litellm`, `numpy`, `sentence_transformers`).

### `src/family_assistant/interfaces.py`

**Description:**Defines abstract interfaces (protocols) for communication channels, allowing for decoupled implementation of chat interactions.

**Major Symbols:**

- `ChatInterface` (Protocol): Interface for sending messages back to a chat.

**Internal Dependencies:**None.

### `src/family_assistant/llm.py`

**Description:**Defines the `LLMInterface` protocol and provides implementations for interacting with Large Language Models, primarily using LiteLLM. Handles message formatting, tool calls, and response parsing.

**Major Symbols:**

- `ToolCallFunction`: Dataclass for a function call within a tool call.
- `ToolCallItem`: Dataclass for a single tool call requested by the LLM.
- `LLMOutput`: Dataclass for standardized LLM response.
- `_sanitize_tools_for_litellm()`: Helper to remove unsupported fields from tool definitions.
- `LLMInterface` (Protocol): Interface for LLM clients.
- `LiteLLMClient`: Implements `LLMInterface` using LiteLLM.
- `RecordingLLMClient`: A wrapper that records LLM interactions.
- `PlaybackLLMClient`: Plays back recorded LLM interactions for deterministic testing.

**Internal Dependencies:**None (only external libraries like `litellm`).

### `src/family_assistant/processing.py`

**Description:**The core processing service that orchestrates LLM interactions, manages conversation history (filtered by processing profile), aggregates context, and executes tool calls. It supports multiple service profiles and can be configured with an injected clock for time-sensitive operations.

**Major Symbols:**

- `ProcessingServiceConfig`: Dataclass for service-specific configuration, including `id` and `delegation_security_level`.
- `ProcessingService`: Main class for handling chat interactions. Includes a `clock` attribute.
- `set_processing_services_registry()`: Sets the registry of all processing services.
- `_aggregate_context_from_providers()`: Gathers context from all registered providers.
- `process_message()`: Sends messages to the LLM, handles tool calls, and returns generated messages.
- `_format_history_for_llm()`: Formats database message history for the LLM.
- `handle_chat_interaction()`: Orchestrates a complete chat turn, from user input to final reply.

**Internal Dependencies:**

- `family_assistant.context_providers` (ContextProvider)
- `family_assistant.interfaces` (ChatInterface)
- `family_assistant.llm` (LLMInterface, LLMOutput)
- `family_assistant.storage`
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.tools` (ToolExecutionContext, ToolNotFoundError, ToolsProvider)
- `family_assistant.utils.clock` (SystemClock)

### `src/family_assistant/task_worker.py`

**Description:**Implements a background task worker that processes tasks from a database queue. It includes retry logic, recurrence handling, and dispatches tasks to registered handlers. It uses an injected `Clock` for time-sensitive operations and supports skipping `llm_callback` tasks if the user has responded.

**Major Symbols:**

- `shutdown_event`: `asyncio.Event` to signal worker shutdown.
- `new_task_event`: `asyncio.Event` to notify worker of immediate tasks.
- `handle_log_message()`: Example task handler for logging.
- `handle_llm_callback()`: Task handler for LLM-scheduled callbacks, handling `skip_if_user_responded` and `scheduling_timestamp`.
- `TaskWorker`: Manages the task processing loop and handler registry. Its constructor accepts `shutdown_event_instance` and `clock`.
- `register_task_handler()`: Registers a handler for a specific task type.
- `get_task_handlers()`: Returns the current task handlers dictionary for this worker.
- `_process_task()`: Executes a dequeued task.
- `_handle_task_failure()`: Manages task retries and status updates on failure.
- `run()`: The main asynchronous loop for the task worker.

**Internal Dependencies:**

- `family_assistant.storage`
- `family_assistant.storage.context` (DatabaseContext, get_db_context)
- `family_assistant.embeddings` (EmbeddingGenerator)
- `family_assistant.interfaces` (ChatInterface)
- `family_assistant.processing` (ProcessingService)
- `family_assistant.tools` (ToolExecutionContext)
- `family_assistant.utils.clock` (Clock, SystemClock)

### `src/family_assistant/telegram_bot.py`

**Description:**Manages the Telegram bot's lifecycle, handles incoming updates (including slash commands and message splitting), and provides a Telegram-specific implementation of the `ChatInterface`. Includes message batching and confirmation UI. Developer error notifications via Telegram have been removed.

**Major Symbols:**

- `BatchProcessor` (Protocol): Interface for processing message batches.
- `MessageBatcher` (Protocol): Interface for buffering messages.
- `ConfirmationUIManager` (Protocol): Interface for requesting user confirmation.
- `DefaultMessageBatcher`: Buffers messages and processes them after a delay.
- `NoBatchMessageBatcher`: Processes messages immediately.
- `TelegramUpdateHandler`: Handles Telegram messages and commands, delegates to batcher. Includes `_send_message_chunks()`, `handle_unknown_command()`, and `handle_generic_slash_command()`.
- `TelegramConfirmationUIManager`: Implements `ConfirmationUIManager` using Telegram inline keyboards.
- `TelegramService`: Main class managing the Telegram bot application. Its constructor accepts `processing_services_registry` and `app_config`. Includes `_set_bot_commands()`.
- `TelegramChatInterface`: Implements `ChatInterface` for Telegram.

**Internal Dependencies:**

- `family_assistant.interfaces` (ChatInterface)
- `family_assistant.processing` (ProcessingService)
- `family_assistant.storage`
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.tools` (ToolConfirmationRequired, ToolConfirmationFailed)
- `telegramify_markdown`

### `src/family_assistant/web_server.py`

**Description:**A simple script to run the FastAPI web application using Uvicorn. Primarily for direct execution or development.

**Major Symbols:**

- `app`: The FastAPI application instance imported from `app_creator`.

**Internal Dependencies:**

- `family_assistant.web.app_creator` (app)

### `src/family_assistant/indexing/document_indexer.py`

**Description:**Orchestrates the document indexing pipeline. It takes raw document inputs (files, content parts, URLs), runs them through a series of configured processors, and manages the storage of processed content and embeddings.

**Major Symbols:**

- `DocumentIndexer`: Main class for document indexing.
- `process_document()`: Task handler method to process and index document content.

**Internal Dependencies:**

- `family_assistant.embeddings` (EmbeddingGenerator)
- `family_assistant.indexing.pipeline` (IndexableContent, IndexingPipeline, ContentProcessor)
- `family_assistant.indexing.processors.dispatch_processors` (EmbeddingDispatchProcessor)
- `family_assistant.indexing.processors.file_processors` (PDFTextExtractor)
- `family_assistant.indexing.processors.llm_processors` (LLMPrimaryLinkExtractorProcessor, LLMSummaryGeneratorProcessor)
- `family_assistant.indexing.processors.metadata_processors` (DocumentTitleUpdaterProcessor, TitleExtractor)
- `family_assistant.indexing.processors.network_processors` (WebFetcherProcessor)
- `family_assistant.indexing.processors.text_processors` (TextChunker)
- `family_assistant.llm` (LLMInterface)
- `family_assistant.storage.vector` (Document, get_document_by_id)
- `family_assistant.tools` (ToolExecutionContext)
- `family_assistant.utils.scraping` (Scraper)

### `src/family_assistant/indexing/email_indexer.py`

**Description:**Handles the specific indexing process for emails stored in the database, converting them into `EmailDocument` objects and running them through the main indexing pipeline.

**Major Symbols:**

- `EmailDocument`: Dataclass representing an email conforming to the `Document` protocol.
- `EmailIndexer`: Main class for email indexing.
- `handle_index_email()`: Task handler to fetch an email from DB and run it through the pipeline.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (IndexableContent, IndexingPipeline)
- `family_assistant.storage`
- `family_assistant.storage.email` (received_emails_table)
- `family_assistant.storage.vector` (Document, get_document_by_id)
- `family_assistant.tools` (ToolExecutionContext)

### `src/family_assistant/indexing/ingestion.py`

**Description:**Manages the initial ingestion of documents from various sources (uploaded files, URLs, content parts). It saves raw data, creates a document record in the database, and enqueues a background task for the full indexing pipeline.

**Major Symbols:**

- `IngestedDocument`: A simple class implementing the `Document` protocol for ingestion.
- `process_document_ingestion_request()`: Main function to handle ingestion requests.

**Internal Dependencies:**

- `family_assistant.storage`
- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/indexing/pipeline.py`

**Description:**Defines the core components and orchestration logic for the document indexing pipeline. It specifies the `IndexableContent` data structure and the `ContentProcessor` interface.

**Major Symbols:**

- `IndexableContent`: Dataclass representing a unit of data flowing through the pipeline.
- `ContentProcessor` (Protocol): Interface for pipeline stages.
- `IndexingPipeline`: Orchestrates the sequential execution of `ContentProcessor`s.

**Internal Dependencies:**

- `family_assistant.indexing.processors.text_processors` (TextChunker)
- `family_assistant.storage.vector` (Document)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/indexing/processors/__init__.py`

**Description:**Package initialization for content processors. Exposes common processor classes for easier import.

**Major Symbols:**

- `EmbeddingDispatchProcessor`
- `PDFTextExtractor`
- `LLMIntelligenceProcessor`
- `TitleExtractor`
- `TextChunker`
- `WebFetcherProcessor`

**Internal Dependencies:**

- `family_assistant.indexing.processors.dispatch_processors`
- `family_assistant.indexing.processors.file_processors`
- `family_assistant.indexing.processors.llm_processors`
- `family_assistant.indexing.processors.metadata_processors`
- `family_assistant.indexing.processors.network_processors`
- `family_assistant.indexing.processors.text_processors`

### `src/family_assistant/indexing/processors/dispatch_processors.py`

**Description:**Contains content processors responsible for dispatching items for embedding.

**Major Symbols:**

- `EmbeddingDispatchProcessor`: Identifies `IndexableContent` items of specified types and dispatches them for embedding via a task queue.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (ContentProcessor, IndexableContent)
- `family_assistant.storage.tasks` (enqueue_task)
- `family_assistant.storage.vector` (Document)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/indexing/processors/file_processors.py`

**Description:**Contains content processors for handling specific file types, such as PDF text extraction.

**Major Symbols:**

- `PDFTextExtractor`: Extracts text from PDF files using `markitdown`.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (IndexableContent)
- `family_assistant.storage.vector` (Document)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/indexing/processors/llm_processors.py`

**Description:**Contains content processors that leverage an LLM to extract structured information (e.g., summaries, primary links) from content.

**Major Symbols:**

- `LLMIntelligenceProcessor`: Base class for LLM-powered content extraction.
- `LLMSummaryGeneratorProcessor`: Specialized processor for generating concise summaries.
- `LLMPrimaryLinkExtractorProcessor`: Specialized processor for extracting primary URLs from content.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (ContentProcessor, IndexableContent)
- `family_assistant.llm` (LLMInterface)
- `family_assistant.storage.vector` (Document)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/indexing/processors/metadata_processors.py`

**Description:**Contains content processors focused on extracting or updating metadata for documents.

**Major Symbols:**

- `DocumentTitleUpdaterProcessorConfig`: Configuration for `DocumentTitleUpdaterProcessor`.
- `TitleExtractor`: Extracts the title from the original document.
- `DocumentTitleUpdaterProcessor`: Updates the main document's title in the database based on processed items.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (ContentProcessor, IndexableContent)
- `family_assistant.storage.vector` (Document, update_document_title_in_db)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/indexing/processors/network_processors.py`

**Description:**Contains content processors that interact with the network, primarily for fetching web content.

**Major Symbols:**

- `WebFetcherProcessorConfig`: Configuration for `WebFetcherProcessor`.
- `WebFetcherProcessor`: Fetches content from URLs using a `Scraper` instance.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (IndexableContent)
- `family_assistant.storage.vector` (Document)
- `family_assistant.tools.types` (ToolExecutionContext)
- `family_assistant.utils.scraping` (Scraper, ScrapeResult)

### `src/family_assistant/indexing/processors/text_processors.py`

**Description:**Contains content processors focused on text manipulation, such as chunking.

**Major Symbols:**

- `TextChunker`: Splits textual content into smaller chunks.

**Internal Dependencies:**

- `family_assistant.indexing.pipeline` (ContentProcessor, IndexableContent)
- `family_assistant.storage.vector` (Document)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/indexing/tasks.py`

**Description:**Defines task handlers specifically for the document indexing pipeline, such as embedding and storing batches of content.

**Major Symbols:**

- `handle_embed_and_store_batch()`: Task handler for generating and storing embeddings.

**Internal Dependencies:**

- `family_assistant.storage.vector` (add_embedding)
- `family_assistant.tools.types` (ToolExecutionContext)

### `src/family_assistant/storage/__init__.py`

**Description:**The main storage facade. It orchestrates database initialization (including Alembic migrations and vector DB setup) and re-exports functions from specific storage modules for a unified API.

**Major Symbols:**

- `init_db()`: Initializes the database schema and migrations.
- `VECTOR_STORAGE_ENABLED`: Boolean indicating if vector storage is enabled.
- Re-exports all major functions and table objects from `storage.api_tokens`, `storage.base`, `storage.context`, `storage.email`, `storage.message_history`, `storage.notes`, `storage.tasks`, `storage.vector`, `storage.vector_search`.

**Internal Dependencies:**

- `family_assistant.storage.api_tokens`
- `family_assistant.storage.base`
- `family_assistant.storage.context`
- `family_assistant.storage.email`
- `family_assistant.storage.message_history`
- `family_assistant.storage.notes`
- `family_assistant.storage.tasks`
- `family_assistant.storage.vector`
- `family_assistant.storage.vector_search`

### `src/family_assistant/storage/api_tokens.py`

**Description:**Provides CRUD operations for API tokens, including generation, hashing, storage, retrieval, and revocation.

**Major Symbols:**

- `TOKEN_PREFIX_LENGTH`: Length of the token prefix.
- `TOKEN_SECRET_LENGTH`: Length of the token secret.
- `_generate_token_part()`: Helper to generate random token parts.
- `generate_token_prefix()`: Generates a unique token prefix.
- `generate_token_secret()`: Generates the token secret.
- `add_api_token()`: Adds a new API token record.
- `create_and_store_api_token()`: Generates, hashes, and stores a new API token.
- `get_api_tokens_for_user()`: Retrieves all tokens for a user.
- `get_api_token_by_id_and_user()`: Retrieves a specific token by ID and user.
- `revoke_api_token()`: Revokes an API token.

**Internal Dependencies:**

- `family_assistant.storage.base` (api_tokens_table)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.auth` (pwd_context)

### `src/family_assistant/storage/base.py`

**Description:**Defines the shared SQLAlchemy metadata and engine for the entire application's database interactions.

**Major Symbols:**

- `metadata`: SQLAlchemy `MetaData` object.
- `engine`: SQLAlchemy `AsyncEngine` instance.
- `get_engine()`: Returns the initialized engine.
- `api_tokens_table`: SQLAlchemy `Table` definition for API tokens.

**Internal Dependencies:**None (only standard library and SQLAlchemy).

### `src/family_assistant/storage/context.py`

**Description:**Provides a context manager (`DatabaseContext`) for managing database connections and transactions, including retry logic for transient errors.

**Major Symbols:**

- `DatabaseContext`: Asynchronous context manager for database operations.
- `execute_with_retry()`: Executes a query with retry logic.
- `fetch_all()`: Fetches all results as dictionaries.
- `fetch_one()`: Fetches one result as a dictionary.
- `on_commit()`: Registers a callback to be called on transaction commit.
- `get_db_context()`: Convenience function to create a `DatabaseContext` instance.

**Internal Dependencies:**

- `family_assistant.storage.base` (get_engine)

### `src/family_assistant/storage/email.py`

**Description:**Handles storage and retrieval of raw incoming email data received via webhooks. Defines the database schema for emails and provides functions for storing them.

**Major Symbols:**

- `AttachmentData`: Pydantic model for attachment metadata.
- `ParsedEmailData`: Pydantic model for parsed email data.
- `received_emails_table`: SQLAlchemy `Table` definition for received emails.
- `store_incoming_email()`: Stores parsed email data and enqueues an indexing task.

**Internal Dependencies:**

- `family_assistant.storage`
- `family_assistant.storage.base` (metadata)
- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/storage/message_history.py`

**Description:**Manages storage and retrieval of conversation message history, including user messages, assistant replies, tool calls, error tracebacks, and the processing profile used for each message.

**Major Symbols:**

- `message_history_table`: SQLAlchemy `Table` definition for message history, including `processing_profile_id`.
- `add_message_to_history()`: Adds a message record to history, accepting `processing_profile_id`.
- `update_message_interface_id()`: Updates the interface-specific ID of a message.
- `update_message_error_traceback()`: Updates error traceback for a message.
- `get_recent_history()`: Retrieves recent messages for a conversation, expanding turns, and filtering by `processing_profile_id`.
- `get_message_by_interface_id()`: Retrieves a message by its interface ID.
- `get_messages_by_turn_id()`: Retrieves all messages for a specific turn.
- `get_messages_by_thread_id()`: Retrieves all messages for a specific conversation thread, filtering by `processing_profile_id`.
- `get_grouped_message_history()`: Retrieves all history grouped by conversation.

**Internal Dependencies:**

- `family_assistant.storage.base` (metadata)
- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/storage/notes.py`

**Description:**Handles storage and retrieval of user-created notes.

**Major Symbols:**

- `notes_table`: SQLAlchemy `Table` definition for notes.
- `get_all_notes()`: Retrieves all notes.
- `get_note_by_title()`: Retrieves a specific note by title.
- `add_or_update_note()`: Adds a new note or updates an existing one (upsert).
- `delete_note()`: Deletes a note by title.

**Internal Dependencies:**

- `family_assistant.storage.base` (metadata)
- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/storage/tasks.py`

**Description:**Implements the database-backed task queue, providing functions for enqueuing, dequeuing (with injected current time for testability), updating status, and rescheduling tasks.

**Major Symbols:**

- `tasks_table`: SQLAlchemy `Table` definition for tasks.
- `enqueue_task()`: Adds a task to the queue.
- `dequeue_task()`: Atomically retrieves and locks the next available task, accepting `current_time`.
- `update_task_status()`: Updates a task's status.
- `reschedule_task_for_retry()`: Reschedules a failed task for retry.
- `manually_retry_task()`: Allows manual retry of failed tasks via UI.
- `get_all_tasks()`: Retrieves all tasks for display.

**Internal Dependencies:**

- `family_assistant.storage.base` (metadata)
- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/storage/vector.py`

**Description:**Provides the API for interacting with the vector storage database (PostgreSQL with pgvector). Handles storing document metadata, text chunks, and their embeddings.

**Major Symbols:**

- `Document` (Protocol): Defines the interface for documents.
- `Base`: SQLAlchemy declarative base for ORM models.
- `DocumentRecord`: SQLAlchemy ORM model for the `documents` table.
- `DocumentEmbeddingRecord`: SQLAlchemy ORM model for the `document_embeddings` table.
- `init_vector_db()`: Initializes vector database components (e.g., `vector` extension).
- `add_document()`: Adds or updates a document record.
- `get_document_by_source_id()`: Retrieves a document by its source ID.
- `get_document_by_id()`: Retrieves a document by its internal ID.
- `add_embedding()`: Adds or updates an embedding record.
- `delete_document()`: Deletes a document and its embeddings.
- `query_vectors()`: Performs hybrid vector and keyword search.
- `update_document_title_in_db()`: Updates a document's title.

**Internal Dependencies:**

- `family_assistant.storage.base` (metadata)
- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/storage/vector_search.py`

**Description:**Defines the schema for vector search queries and implements the complex SQL query logic for hybrid (semantic + keyword) search.

**Major Symbols:**

- `MetadataFilter`: Dataclass for metadata key-value filters.
- `VectorSearchQuery`: Dataclass for defining search parameters.
- `query_vector_store()`: Executes the vector/keyword/hybrid search query.

**Internal Dependencies:**

- `family_assistant.storage.context` (DatabaseContext)

### `src/family_assistant/tools/__init__.py`

**Description:**The main tools module. It defines tool provider interfaces, implements local Python tools (including new callback management and cross-profile delegation tools), and orchestrates tool confirmation logic.

**Major Symbols:**

- `ConfirmationCallbackProtocol` (Protocol): Interface for confirmation callbacks.
- `ToolConfirmationRequired`: Exception raised when confirmation is needed.
- `ToolConfirmationFailed`: Exception raised when confirmation fails.
- `ToolsProvider` (Protocol): Interface for tool providers.
- `schedule_recurring_task_tool()`: Local tool to schedule recurring LLM callbacks with RRULE support.
- `schedule_future_callback_tool()`: Local tool to schedule one-time LLM callbacks at a specific time.
- `schedule_reminder_tool()`: Local tool to schedule reminders with optional follow-up support.
- `schedule_action_tool()`: Local tool to schedule any action type (wake_llm or script) at a specific time.
- `schedule_recurring_action_tool()`: Local tool to schedule recurring actions (wake_llm or script) using RRULE format.
- `list_pending_callbacks_tool()`: Local tool to list pending LLM callback tasks.
- `modify_pending_callback_tool()`: Local tool to modify a pending LLM callback task.
- `cancel_pending_callback_tool()`: Local tool to cancel a pending LLM callback task.
- `search_documents_tool()`: Local tool to search indexed documents.
- `get_full_document_content_tool()`: Local tool to retrieve full document content.
- `ingest_document_from_url_tool()`: Local tool to ingest documents from URLs.
- `get_message_history_tool()`: Local tool to retrieve message history.
- `_scan_user_docs()`: Helper to scan for user documentation files.
- `get_user_documentation_content_tool()`: Local tool to retrieve user documentation.
- `send_message_to_user_tool()`: Local tool to send messages to other users.
- `delegate_to_service_tool()`: Local tool to delegate a user request to another specialized assistant profile.
- `AVAILABLE_FUNCTIONS`: Dictionary mapping tool names to their implementations.
- `TOOLS_DEFINITION`: List of OpenAI-compatible tool definitions (schema).
- `TOOL_CONFIRMATION_RENDERERS`: Dictionary mapping tool names to confirmation prompt renderers.
- `LocalToolsProvider`: Implements `ToolsProvider` for local Python functions, with improved dependency injection.
- `CompositeToolsProvider`: Combines multiple `ToolsProvider` instances.
- `ConfirmingToolsProvider`: Wraps another provider to add user confirmation for sensitive tools.

**Internal Dependencies:**

- `family_assistant.calendar_integration`
- `family_assistant.embeddings` (EmbeddingGenerator)
- `family_assistant.indexing.ingestion` (process_document_ingestion_request)
- `family_assistant.storage`
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.storage.vector_search` (VectorSearchQuery, query_vector_store)
- `family_assistant.tools.mcp` (MCPToolsProvider)
- `family_assistant.tools.types` (ToolExecutionContext, ToolNotFoundError)
- `family_assistant.utils.clock` (SystemClock)

### `src/family_assistant/tools/mcp.py`

**Description:**Implements a `ToolsProvider` for integrating with MCP (Model Context Protocol) servers, allowing the LLM to call external tools. It includes configurable timeouts and per-server status tracking during initialization.

**Major Symbols:**

- `MCPToolsProvider`: Implements `ToolsProvider` for MCP servers.
- `initialize()`: Connects to MCP servers and discovers tools, with configurable timeout and per-server status tracking.
- `_format_mcp_definitions_to_dicts()`: Converts MCP tool definitions to OpenAI format.
- `execute_tool()`: Executes an MCP tool call.

**Internal Dependencies:**

- `family_assistant.tools.types` (ToolExecutionContext, ToolNotFoundError)

### `src/family_assistant/tools/schema.py`

**Description:**Provides functionality to render JSON schemas (for tool parameters) into human-readable HTML.

**Major Symbols:**

- `render_schema_as_html()`: Renders a JSON schema string to HTML.

**Internal Dependencies:**None (only external libraries like `json-schema-for-humans`).

### `src/family_assistant/tools/types.py`

**Description:**Defines common types and protocols used by the tool system, particularly the `ToolExecutionContext` dataclass, which now includes `user_name` and an injected `clock` instance.

**Major Symbols:**

- `ToolExecutionContext`: Dataclass containing context for tool execution, including `user_name` and `clock` attributes.
- `ToolNotFoundError`: Custom exception for when a tool is not found.

**Internal Dependencies:**

- `family_assistant.embeddings` (EmbeddingGenerator)
- `family_assistant.interfaces` (ChatInterface)
- `family_assistant.processing` (ProcessingService)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.utils.clock`

### `src/family_assistant/utils/__init__.py`

**Description:**Package initialization for utility modules.

**Major Symbols:**None.

**Internal Dependencies:**None.

### `src/family_assistant/utils/scraping.py`

**Description:**Provides utilities for scraping web content using `httpx` and Playwright, including HTML to Markdown conversion.

**Major Symbols:**

- `ScrapeResult`: Dataclass for structured scraping results.
- `Scraper` (Protocol): Interface for web content scrapers.
- `PlaywrightScraper`: Implements `Scraper` using Playwright and `httpx`.
- `_convert_bytes_to_markdown()`: Helper to convert bytes to Markdown.
- `_fetch_with_playwright()`: Internal function to fetch content using Playwright.
- `_fetch_with_httpx()`: Internal function to fetch content using `httpx`.
- `check_playwright_is_functional()`: Checks if Playwright is functional.
- `MockScraper`: A mock implementation for testing.

**Internal Dependencies:**None (only external libraries like `httpx`, `playwright`, `markitdown`).

### `src/family_assistant/web/__init__.py`

**Description:**Package initialization for the web module.

**Major Symbols:**None.

**Internal Dependencies:**None.

### `src/family_assistant/web/app_creator.py`

**Description:**Creates and configures the FastAPI application, including middleware, static file serving, and router inclusion.

**Major Symbols:**

- `app`: The FastAPI application instance.

**Internal Dependencies:**

- `family_assistant.web.auth`
- `family_assistant.web.routers.api`
- `family_assistant.web.routers.api_token_management`
- `family_assistant.web.routers.documentation`
- `family_assistant.web.routers.documents_ui`
- `family_assistant.web.routers.health`
- `family_assistant.web.routers.history`
- `family_assistant.web.routers.notes`
- `family_assistant.web.routers.tasks_ui`
- `family_assistant.web.routers.tools_ui`
- `family_assistant.web.routers.ui_token_management`
- `family_assistant.web.routers.vector_search`
- `family_assistant.web.routers.webhooks`

### `src/family_assistant/web/auth.py`

**Description:**Handles authentication for the web interface, including OIDC integration and API token validation. Provides middleware for access control.

**Major Symbols:**

- `pwd_context`: `CryptContext` for password hashing.
- `AUTH_ENABLED`: Boolean indicating if authentication is enabled.
- `oauth`: `OAuth` instance for OIDC.
- `PUBLIC_PATHS`: List of regex patterns for publicly accessible paths.
- `User`: Type alias for user information.
- `get_current_user_optional()`: FastAPI dependency to get optional user.
- `get_user_from_api_token()`: Verifies API token and returns user info.
- `AuthMiddleware`: ASGI middleware for authentication and authorization.
- `auth_router`: FastAPI router for login/logout/callback.

**Internal Dependencies:**

- `family_assistant.storage.base` (api_tokens_table)
- `family_assistant.storage.context` (get_db_context)

### `src/family_assistant/web/dependencies.py`

**Description:**Defines FastAPI dependency injection functions for common resources like database context, LLM clients, and tool providers.

**Major Symbols:**

- `get_embedding_generator_dependency()`: Provides `EmbeddingGenerator`.
- `get_db()`: Provides `DatabaseContext`.
- `get_tools_provider_dependency()`: Provides `ToolsProvider`.
- `get_processing_service()`: Provides `ProcessingService`.
- `get_current_api_user()`: Provides user authenticated via API token.
- `get_current_active_user()`: Provides user authenticated via OIDC.

**Internal Dependencies:**

- `family_assistant.embeddings` (EmbeddingGenerator)
- `family_assistant.processing` (ProcessingService)
- `family_assistant.storage.context` (DatabaseContext, get_db_context)
- `family_assistant.tools` (ToolsProvider)
- `family_assistant.web.auth` (get_user_from_api_token)

### `src/family_assistant/web/models.py`

**Description:**Defines Pydantic models for API request and response bodies, ensuring data validation and clear API contracts.

**Major Symbols:**

- `SearchResultItem`: Pydantic model for a single search result item.
- `DocumentUploadResponse`: Pydantic model for document upload API response.
- `ApiTokenCreateRequest`: Pydantic model for API token creation request.
- `ApiTokenCreateResponse`: Pydantic model for API token creation response.
- `ChatPromptRequest`: Pydantic model for chat prompt request, including `profile_id`.
- `ChatMessageResponse`: Pydantic model for chat message response.

**Internal Dependencies:**None (only `pydantic`).

### `src/family_assistant/web/routers/__init__.py`

**Description:**Package initialization for web routers.

**Major Symbols:**None.

**Internal Dependencies:**None.

### `src/family_assistant/web/routers/api.py`

**Description:**Aggregates all API-related routers under a common `/api` prefix.

**Major Symbols:**

- `api_router`: Main API router.

**Internal Dependencies:**

- `family_assistant.web.routers.chat_api`
- `family_assistant.web.routers.documents_api`
- `family_assistant.web.routers.tools_api`

### `src/family_assistant/web/routers/api_token_management.py`

**Description:**FastAPI router for API endpoints related to API token management (e.g., creating new tokens).

**Major Symbols:**

- `router`: FastAPI router for API token management.
- `create_api_token()`: API endpoint to create a new API token.

**Internal Dependencies:**

- `family_assistant.storage.api_tokens`
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.dependencies` (get_current_active_user, get_db)
- `family_assistant.web.models` (ApiTokenCreateRequest, ApiTokenCreateResponse)

### `src/family_assistant/web/routers/chat_api.py`

**Description:**FastAPI router for the chat API endpoint, allowing external systems to interact with the assistant.

**Major Symbols:**

- `chat_api_router`: FastAPI router for chat API.
- `api_chat_send_message()`: API endpoint to send a message to the assistant, accepting `profile_id`.

**Internal Dependencies:**

- `family_assistant.processing` (ProcessingService)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.dependencies` (get_db, get_processing_service)
- `family_assistant.web.models` (ChatMessageResponse, ChatPromptRequest)

### `src/family_assistant/web/routers/documentation.py`

**Description:**FastAPI router for serving user documentation files (Markdown) via the web UI.

**Major Symbols:**

- `documentation_router`: FastAPI router for documentation.
- `redirect_to_user_guide()`: Redirects base `/docs/` to `USER_GUIDE.md`.
- `serve_documentation()`: Serves rendered Markdown documentation.

**Internal Dependencies:**

- `family_assistant.tools` (_scan_user_docs)
- `family_assistant.web.auth` (AUTH_ENABLED)
- `family_assistant.web.utils` (md_renderer)

### `src/family_assistant/web/routers/documents_api.py`

**Description:**FastAPI router for API endpoints related to document ingestion (e.g., uploading files, providing URLs).

**Major Symbols:**

- `documents_api_router`: FastAPI router for document API.
- `upload_document()`: API endpoint to upload and index a document.

**Internal Dependencies:**

- `family_assistant.indexing.ingestion` (process_document_ingestion_request)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.dependencies` (get_db)
- `family_assistant.web.models` (DocumentUploadResponse)

### `src/family_assistant/web/routers/documents_ui.py`

**Description:**FastAPI router for the web UI related to document management (e.g., upload form).

**Major Symbols:**

- `router`: FastAPI router for documents UI.
- `get_document_upload_form()`: Serves the document upload HTML form.
- `handle_document_upload()`: Handles form submission for document upload.

**Internal Dependencies:**

- `family_assistant.web.auth` (AUTH_ENABLED, User, get_current_user_optional)

### `src/family_assistant/web/routers/health.py`

**Description:**FastAPI router for health check endpoints, verifying basic service functionality and Telegram polling status.

**Major Symbols:**

- `health_router`: FastAPI router for health checks.
- `health_check()`: Endpoint to check service health.

**Internal Dependencies:**None (only `telegram.error`).

### `src/family_assistant/web/routers/history.py`

**Description:**FastAPI router for the web UI displaying message history.

**Major Symbols:**

- `history_router`: FastAPI router for message history.
- `view_message_history()`: Serves the message history page.

**Internal Dependencies:**

- `family_assistant.storage` (get_grouped_message_history)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.auth` (AUTH_ENABLED)
- `family_assistant.web.dependencies` (get_db)

### `src/family_assistant/web/routers/notes.py`

**Description:**FastAPI router for the web UI managing notes (listing, adding, editing, deleting).

**Major Symbols:**

- `notes_router`: FastAPI router for notes.
- `read_root()`: Serves the main notes list page.
- `add_note_form()`: Serves the form to add a new note.
- `edit_note_form()`: Serves the form to edit an existing note.
- `save_note()`: Handles saving (add/update) a note.
- `delete_note_post()`: Handles deleting a note.

**Internal Dependencies:**

- `family_assistant.storage` (add_or_update_note, delete_note, get_all_notes, get_note_by_title)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.auth` (AUTH_ENABLED)
- `family_assistant.web.dependencies` (get_db)

### `src/family_assistant/web/routers/tasks_ui.py`

**Description:**FastAPI router for the web UI displaying background tasks and allowing manual retries.

**Major Symbols:**

- `tasks_ui_router`: FastAPI router for tasks UI.
- `view_tasks()`: Serves the page displaying scheduled tasks.
- `retry_task_manually_endpoint()`: Handles manual task retry requests.

**Internal Dependencies:**

- `family_assistant.storage` (get_all_tasks)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.storage.tasks` (manually_retry_task)
- `family_assistant.web.auth` (AUTH_ENABLED)
- `family_assistant.web.dependencies` (get_db)

### `src/family_assistant/web/routers/tools_api.py`

**Description:**FastAPI router for the API endpoint to execute tools.

**Major Symbols:**

- `tools_api_router`: FastAPI router for tools API.
- `ToolExecutionRequest`: Pydantic model for tool execution request.
- `execute_tool_api()`: API endpoint to execute a specified tool.

**Internal Dependencies:**

- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.tools` (ToolExecutionContext, ToolNotFoundError, ToolsProvider)
- `family_assistant.web.dependencies` (get_db, get_tools_provider_dependency)

### `src/family_assistant/web/routers/tools_ui.py`

**Description:**FastAPI router for the web UI displaying available tools and their schemas.

**Major Symbols:**

- `tools_ui_router`: FastAPI router for tools UI.
- `view_tools()`: Serves the page displaying available tools.

**Internal Dependencies:**

- `family_assistant.tools.schema` (render_schema_as_html)
- `family_assistant.web.auth` (AUTH_ENABLED)

### `src/family_assistant/web/routers/ui_token_management.py`

**Description:**FastAPI router for the web UI to manage API tokens (view, revoke).

**Major Symbols:**

- `router`: FastAPI router for UI token management.
- `manage_api_tokens_ui()`: Serves the API token management page.
- `revoke_api_token_ui()`: Handles revocation of an API token.

**Internal Dependencies:**

- `family_assistant.storage.api_tokens`
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.web.auth` (AUTH_ENABLED)
- `family_assistant.web.dependencies` (get_current_active_user, get_db)

### `src/family_assistant/web/routers/vector_search.py`

**Description:**FastAPI router for the web UI providing a vector search debugger and document detail view.

**Major Symbols:**

- `vector_search_router`: FastAPI router for vector search.
- `vector_search_form()`: Serves the vector search form.
- `document_detail_view()`: Serves the detailed view for a single document.
- `handle_vector_search()`: Handles vector search form submission.

**Internal Dependencies:**

- `family_assistant.embeddings` (EmbeddingGenerator)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.storage.vector` (DocumentRecord, get_document_by_id)
- `family_assistant.storage.vector_search` (MetadataFilter, VectorSearchQuery, query_vector_store)
- `family_assistant.web.auth` (AUTH_ENABLED)
- `family_assistant.web.dependencies` (get_db, get_embedding_generator_dependency)

### `src/family_assistant/web/routers/webhooks.py`

**Description:**FastAPI router for handling incoming webhooks, specifically for email ingestion.

**Major Symbols:**

- `webhooks_router`: FastAPI router for webhooks.
- `handle_mail_webhook()`: Receives and processes incoming email webhooks.

**Internal Dependencies:**

- `family_assistant.storage` (store_incoming_email)
- `family_assistant.storage.context` (DatabaseContext)
- `family_assistant.storage.email` (AttachmentData, ParsedEmailData)
- `family_assistant.web.dependencies` (get_db)

### `src/family_assistant/web/utils.py`

**Description:**Provides general utility functions for the web module, such as a Markdown renderer.

**Major Symbols:**

- `md_renderer`: `MarkdownIt` instance for rendering Markdown.

**Internal Dependencies:**None (only `markdown-it`).

### `contrib/scrape_mcp.py`

**Description:**A standalone script that runs an MCP server providing a web scraping tool. It uses Playwright to render JavaScript-heavy pages and MarkItDown to convert HTML to Markdown. This script is intended to be run separately and connected to the main Family Assistant application via MCP configuration.

**Major Symbols:**

- `check_playwright_async_wrapper()`: Checks if Playwright is functional.
- `serve()`: Sets up and runs the MCP server.
- `list_tools()`: MCP endpoint to list available tools.
- `call_tool()`: MCP endpoint to execute the scraping tool.

**Internal Dependencies:**

- `family_assistant.utils.scraping` (PlaywrightScraper, Scraper)
