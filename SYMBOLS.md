# from .__main__ import load_config
def load_config(config_file_path: str=CONFIG_FILE_PATH) -> Dict[(str, Any)]:
    """Loads configuration according to the defined hierarchy:
    Defaults -> config.yaml -> Environment Variables.
    CLI arguments are applied *after* this function runs.

    Args:
        config_file_path: Path to the main YAML configuration file.

    Returns:
        A dictionary containing the resolved configuration."""

# from .__main__ import shutdown_handler
async def shutdown_handler(signal_name: str, telegram_service: Optional[TelegramService], tools_provider: Optional[ToolsProvider]):
    "Initiates graceful shutdown."

# from .__main__ import reload_config_handler
def reload_config_handler(signum, frame):
    "Handles SIGHUP for config reloading (placeholder)."

# from .__main__ import main_async
async def main_async(config: Dict[(str, Any)]) -> Tuple[(Optional[TelegramService], Optional[ToolsProvider])]:
    "Initializes and runs the bot application using the provided configuration."

# from .__main__ import main
def main() -> int:
    "Loads config, parses args, sets up event loop, and runs the application."

# from .calendar_integration import format_datetime_or_date
def format_datetime_or_date(dt_obj: ?, timezone_str: str, is_end: bool) -> str:
    "Formats datetime or date object into a user-friendly string, relative to the specified timezone."

# from .calendar_integration import parse_event
def parse_event(event_data: str, timezone_str: Optional[str]) -> Optional[Dict[(str, Any)]]:
    """Parses VCALENDAR data into a dictionary, including the UID.
    If timezone_str is provided, naive datetimes will be localized to that timezone."""

# from .calendar_integration import _fetch_ical_events_async
async def _fetch_ical_events_async(ical_urls: List[str], timezone_str: str) -> List[Dict[(str, Any)]]:
    "Asynchronously fetches and parses events from a list of iCal URLs."

# from .calendar_integration import _fetch_caldav_events_sync
def _fetch_caldav_events_sync(username: str, password: str, calendar_urls: List[str], timezone_str: str) -> List[Dict[(str, Any)]]:
    "Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."

# from .calendar_integration import fetch_upcoming_events
async def fetch_upcoming_events(calendar_config: Dict[(str, Any)], timezone_str: str) -> List[Dict[(str, Any)]]:
    "Fetches events from configured CalDAV and iCal sources and merges them."

# from .calendar_integration import format_events_for_prompt
def format_events_for_prompt(events: List[Dict[(str, Any)]], prompts: Dict[(str, str)], timezone_str: str) -> Tuple[(str, str)]:
    "Formats the fetched events into strings suitable for the prompt."

# from .calendar_integration import add_calendar_event_tool
async def add_calendar_event_tool(exec_context: ToolExecutionContext, summary: str, start_time: str, end_time: str, description: Optional[str], all_day: bool, recurrence_rule: Optional[str]) -> str:
    """Adds an event to the first configured CalDAV calendar.
    Can create recurring events if an RRULE string is provided."""

# from .calendar_integration import search_calendar_events_tool
async def search_calendar_events_tool(exec_context: ToolExecutionContext, query_text: str, start_date_str: Optional[str], end_date_str: Optional[str], limit: int=5) -> str:
    "Searches CalDAV events based on query text and date range."

# from .calendar_integration import modify_calendar_event_tool
async def modify_calendar_event_tool(exec_context: ToolExecutionContext, uid: str, calendar_url: str, new_summary: Optional[str], new_start_time: Optional[str], new_end_time: Optional[str], new_description: Optional[str], new_all_day: Optional[bool]) -> str:
    "Modifies a specific CalDAV event by UID."

# from .calendar_integration import delete_calendar_event_tool
async def delete_calendar_event_tool(exec_context: ToolExecutionContext, uid: str, calendar_url: str) -> str:
    "Deletes a specific CalDAV event by UID."

# from .calendar_integration import _fetch_event_details_sync
def _fetch_event_details_sync(username: str, password: str, calendar_url: str, uid: str, timezone_str: str) -> Optional[Dict[(str, Any)]]:
    """Synchronously fetches details for a single event by UID.
    Requires timezone_str to correctly parse naive datetimes."""

# from .processing import ProcessingService
class ProcessingService:
    """Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls."""

# from .llm import LLMOutput
class LLMOutput:
    "Standardized output structure from an LLM call."

# from .llm import _sanitize_tools_for_litellm
def _sanitize_tools_for_litellm(tools: List[Dict[(str, Any)]]) -> List[Dict[(str, Any)]]:
    """Removes unsupported 'format' fields from string parameters in tool definitions
    before sending them to LiteLLM/OpenAI, which only supports 'enum' and 'date-time'.

    Args:
        tools: A list of tool definitions in OpenAI dictionary format.

    Returns:
        A new list of sanitized tool definitions."""

# from .llm import LLMInterface
class LLMInterface(Protocol):
    "Protocol defining the interface for interacting with an LLM."

# from .llm import LiteLLMClient
class LiteLLMClient:
    "LLM client implementation using the LiteLLM library."

# from .llm import RecordingLLMClient
class RecordingLLMClient:
    """An LLM client wrapper that records interactions (inputs and outputs)
    to a file while proxying calls to another LLM client."""

# from .llm import PlaybackLLMClient
class PlaybackLLMClient:
    """An LLM client that plays back previously recorded interactions from a file.
    Plays back recorded interactions by matching the input arguments."""

# from .embeddings import EmbeddingResult
class EmbeddingResult:
    "Represents the result of generating embeddings for a list of texts."

# from .embeddings import EmbeddingGenerator
class EmbeddingGenerator(Protocol):
    "Protocol defining the interface for generating text embeddings."

# from .embeddings import LiteLLMEmbeddingGenerator
class LiteLLMEmbeddingGenerator:
    "Embedding generator implementation using the LiteLLM library."

# from .embeddings import MockEmbeddingGenerator
class MockEmbeddingGenerator:
    """A mock embedding generator that returns predefined embeddings based on input text.
    Useful for testing without making actual API calls."""

# from .context_providers import ContextProvider
class ContextProvider(Protocol):
    "Interface for objects that can provide context segments for the LLM."

# from .context_providers import NotesContextProvider
class NotesContextProvider(ContextProvider):
    "Provides context from stored notes."

# from .context_providers import CalendarContextProvider
class CalendarContextProvider(ContextProvider):
    "Provides context from calendar events."

# from .telegram_bot import BatchProcessor
class BatchProcessor(Protocol):
    "Protocol defining the interface for processing a batch of messages."

# from .telegram_bot import MessageBatcher
class MessageBatcher(Protocol):
    "Protocol defining the interface for buffering messages."

# from .telegram_bot import ConfirmationUIManager
class ConfirmationUIManager(Protocol):
    "Protocol defining the interface for requesting user confirmation."

# from .telegram_bot import DefaultMessageBatcher
class DefaultMessageBatcher(MessageBatcher):
    "Buffers messages and processes them in batches to avoid race conditions."

# from .telegram_bot import TelegramUpdateHandler
class TelegramUpdateHandler:
    "Handles specific Telegram updates (messages, commands) and delegates processing."

# from .telegram_bot import TelegramConfirmationUIManager
class TelegramConfirmationUIManager(ConfirmationUIManager):
    "Implementation of ConfirmationUIManager using Telegram Inline Keyboards."

# from .telegram_bot import NoBatchMessageBatcher
class NoBatchMessageBatcher(MessageBatcher):
    "A simple batcher that processes each message immediately without buffering."

# from .telegram_bot import TelegramService
class TelegramService:
    "Manages the Telegram bot application lifecycle and update handling."

# from .web_server import get_embedding_generator_dependency
async def get_embedding_generator_dependency(request: Request) -> EmbeddingGenerator:
    "Retrieves the configured EmbeddingGenerator instance from app state."

# from .web_server import get_db
async def get_db() -> DatabaseContext:
    "FastAPI dependency to get a DatabaseContext."

# from .web_server import get_tools_provider_dependency
async def get_tools_provider_dependency(request: Request) -> ToolsProvider:
    "Retrieves the configured ToolsProvider instance from app state."

# from .web_server import AuthMiddleware
class AuthMiddleware:

# from .web_server import SearchResultItem
class SearchResultItem(BaseModel):

# from .web_server import DocumentUploadResponse
class DocumentUploadResponse(BaseModel):

# from .web_server import read_root
async def read_root(request: Request, db_context: DatabaseContext=...):
    "Serves the main page listing all notes."

# from .web_server import add_note_form
async def add_note_form(request: Request):
    "Serves the form to add a new note."

# from .web_server import edit_note_form
async def edit_note_form(request: Request, title: str, db_context: DatabaseContext=...):
    "Serves the form to edit an existing note."

# from .web_server import save_note
async def save_note(request: Request, title: str=..., content: str=..., original_title: Optional[str]=..., db_context: DatabaseContext=...):
    "Handles saving a new or updated note."

# from .web_server import delete_note_post
async def delete_note_post(request: Request, title: str, db_context: DatabaseContext=...):
    "Handles deleting a note."

# from .web_server import handle_mail_webhook
async def handle_mail_webhook(request: Request, db_context: DatabaseContext=...):
    """Receives incoming email via webhook (expects multipart/form-data).
    Logs the received form data for now."""

# from .web_server import view_message_history
async def view_message_history(request: Request, db_context: DatabaseContext=..., page: int=..., per_page: int=...):
    "Serves the page displaying message history."

# from .web_server import view_tools
async def view_tools(request: Request):
    "Serves the page displaying available tools."

# from .web_server import view_tasks
async def view_tasks(request: Request, db_context: DatabaseContext=...):
    "Serves the page displaying scheduled tasks."

# from .web_server import health_check
async def health_check(request: Request):
    "Checks basic service health and Telegram polling status."

# from .web_server import vector_search_form
async def vector_search_form(request: Request, db_context: DatabaseContext=...):
    "Serves the vector search form."

# from .web_server import handle_vector_search
async def handle_vector_search(request: Request, semantic_query: Optional[str]=..., keywords: Optional[str]=..., search_type: str=..., embedding_model: Optional[str]=..., embedding_types: List[str]=..., source_types: List[str]=..., created_after: Optional[str]=..., created_before: Optional[str]=..., title_like: Optional[str]=..., metadata_keys: List[str]=..., metadata_values: List[str]=..., limit: int=..., rrf_k: int=..., db_context: DatabaseContext=..., embedding_generator: EmbeddingGenerator=...):
    "Handles the vector search form submission."

# from .web_server import ToolExecutionRequest
class ToolExecutionRequest(BaseModel):

# from .web_server import execute_tool_api
async def execute_tool_api(tool_name: str, request: Request, payload: ToolExecutionRequest, tools_provider: ToolsProvider=..., db_context: DatabaseContext=...):
    "Executes a specified tool with the given arguments."

# from .web_server import upload_document
async def upload_document(source_type: str=..., source_id: str=..., content_parts_json: str=..., source_uri: Optional[str]=..., title: Optional[str]=..., created_at_str: Optional[str]=..., metadata_json: Optional[str]=..., db_context: DatabaseContext=...):
    "API endpoint to upload document metadata and content parts for indexing."

# from .web_server import redirect_to_user_guide
async def redirect_to_user_guide():
    "Redirects the base /docs/ path to the main user guide."

# from .web_server import serve_documentation
async def serve_documentation(request: Request, filename: str):
    "Serves rendered Markdown documentation files from the docs/user directory."

# from .task_worker import handle_log_message
async def handle_log_message(db_context: DatabaseContext, payload: Any):
    "Simple task handler that logs the received payload."

# from .task_worker import format_llm_response_for_telegram
def format_llm_response_for_telegram(response_text: str) -> str:
    "Converts LLM Markdown to Telegram MarkdownV2, with fallback."

# from .task_worker import handle_llm_callback
async def handle_llm_callback(exec_context: ToolExecutionContext, payload: Any):
    """Task handler for LLM scheduled callbacks.
    Dependencies are accessed via the ToolExecutionContext."""

# from .task_worker import TaskWorker
class TaskWorker:
    "Manages the task processing loop and handler registry."

# from .pipeline import IndexableContent
class IndexableContent:
    """Represents a unit of data flowing through the indexing pipeline,
    potentially ready for embedding."""

# from .pipeline import ContentProcessor
class ContentProcessor(Protocol):
    "Defines the contract for a stage in the document indexing pipeline."

# from .pipeline import IndexingPipeline
class IndexingPipeline:
    "Orchestrates the flow of IndexableContent through a series of ContentProcessors."

# from .tasks import handle_embed_and_store_batch
async def handle_embed_and_store_batch(exec_context: ?, payload: Dict[(str, Any)]) -> ?:
    """Task handler for embedding a batch of texts and storing them in the vector database.

    The payload is expected to contain:
    - document_id (int): The ID of the parent document.
    - texts_to_embed (List[str]): A list of text strings to embed.
    - embedding_metadata_list (List[Dict[str, Any]]): A list of metadata dictionaries,
      one for each text in texts_to_embed. Each dictionary should contain:
        - embedding_type (str): The type of embedding (e.g., 'title', 'content_chunk').
        - chunk_index (int): The index of this chunk for the given embedding_type.
        - original_content_metadata (Dict[str, Any]): Metadata from the content processor.
        - content_hash (Optional[str]): Hash of the content, if available.

    Args:
        exec_context: The tool execution context, providing db_context and embedding_generator.
        payload: The task payload containing data for embedding.

    Raises:
        ValueError: If the payload is malformed (e.g., lists have different lengths,
                    texts_to_embed is empty, or required keys are missing).
        SQLAlchemyError: If database operations fail.
        Exception: If embedding generation fails."""

# from .document_indexer import DocumentIndexer
class DocumentIndexer:
    """Handles the indexing process for documents, primarily those uploaded via API.
    Takes dependencies via constructor."""

# from .email_indexer import EmailDocument
class EmailDocument(Document):
    """Represents an email document conforming to the Document protocol
    for vector storage ingestion. Includes methods to convert from
    a received_emails table row."""

# from .email_indexer import handle_index_email
async def handle_index_email(exec_context: ToolExecutionContext, payload: Dict[(str, Any)]):
    """Task handler to index a specific email from the received_emails table.
    Receives ToolExecutionContext from the TaskWorker."""

# from .email_indexer import set_indexing_dependencies
def set_indexing_dependencies(pipeline: IndexingPipeline):
    "Sets the necessary dependencies for the email indexer."

# from .__init__ import ToolConfirmationRequired
class ToolConfirmationRequired(Exception):
    """Special exception raised by ConfirmingToolsProvider to signal that
    confirmation is needed before proceeding. Contains info for the UI layer."""

# from .__init__ import ToolConfirmationFailed
class ToolConfirmationFailed(Exception):
    "Raised when user denies confirmation or it times out."

# from .__init__ import ToolsProvider
class ToolsProvider(Protocol):
    "Protocol defining the interface for a tool provider."

# from .__init__ import schedule_recurring_task_tool
async def schedule_recurring_task_tool(exec_context: ToolExecutionContext, task_type: str, initial_schedule_time: str, recurrence_rule: str, payload: Dict[(str, Any)], max_retries: Optional[int]=3, description: Optional[str]):
    """Schedules a new recurring task.

    Args:
        exec_context: The execution context containing db_context and timezone_str.
        task_type: The type of the task (e.g., 'send_daily_brief', 'check_reminders').
        initial_schedule_time: ISO 8601 datetime string for the *first* run.
        recurrence_rule: RRULE string specifying the recurrence (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0').
        payload: JSON object containing data needed by the task handler.
        max_retries: Maximum number of retries for each instance (default 3).
        description: A short, URL-safe description to include in the task ID (e.g., 'daily_brief')."""

# from .__init__ import schedule_future_callback_tool
async def schedule_future_callback_tool(exec_context: ToolExecutionContext, callback_time: str, context: str):
    """Schedules a task to trigger an LLM callback in a specific chat at a future time.

    Args:
        exec_context: The ToolExecutionContext containing chat_id, application instance, and db_context.
        callback_time: ISO 8601 formatted datetime string (including timezone).
        context: The context/prompt for the future LLM callback."""

# from .__init__ import search_documents_tool
async def search_documents_tool(exec_context: ToolExecutionContext, embedding_generator: EmbeddingGenerator, query_text: str, source_types: Optional[List[str]], embedding_types: Optional[List[str]], limit: int=5) -> str:
    """Searches stored documents using hybrid vector and keyword search.

    Args:
        exec_context: The execution context containing the database context.
        embedding_generator: The embedding generator instance.
        query_text: The natural language query to search for.
        source_types: Optional list of source types to filter by (e.g., ['email', 'note']).
        embedding_types: Optional list of embedding types to filter by (e.g., ['content_chunk', 'summary']).
        limit: Maximum number of results to return.

    Returns:
        A formatted string containing the search results or an error message."""

# from .__init__ import get_full_document_content_tool
async def get_full_document_content_tool(exec_context: ToolExecutionContext, document_id: int) -> str:
    """Retrieves the full text content associated with a specific document ID.
    This is typically used after finding a relevant document via search_documents.

    Args:
        exec_context: The execution context containing the database context.
        document_id: The unique ID of the document (obtained from search results).

    Returns:
        A string containing the full concatenated text content of the document,
        or an error message if not found or content is unavailable."""

# from .__init__ import get_message_history_tool
async def get_message_history_tool(exec_context: ToolExecutionContext, limit: int=10, max_age_hours: int=24) -> str:
    """Retrieves recent message history for the current chat, with optional filters.

    Args:
        exec_context: The execution context containing chat_id and db_context.
        limit: Maximum number of messages to retrieve (default: 10).
        max_age_hours: Maximum age of messages in hours (default: 24).

    Returns:
        A formatted string containing the message history or an error message."""

# from .__init__ import _scan_user_docs
def _scan_user_docs() -> List[str]:
    "Scans the 'docs/user/' directory for allowed documentation files."

def _scan_user_docs() -> List[str]:
    "Scans the 'docs/user/' directory for allowed documentation files."

# from .__init__ import get_user_documentation_content_tool
async def get_user_documentation_content_tool(exec_context: ToolExecutionContext, filename: str) -> str:
    """Retrieves the content of a specified file from the user documentation directory ('docs/user/').

    Args:
        exec_context: The execution context (not directly used here, but available).
        filename: The name of the file within the 'docs/user/' directory (e.g., 'USER_GUIDE.md').

    Returns:
        The content of the file as a string, or an error message if the file is
        not found, not allowed, or cannot be read."""

# from .__init__ import _format_event_details_for_confirmation
def _format_event_details_for_confirmation(details: Optional[Dict[(str, Any)]], timezone_str: str) -> str:
    "Formats fetched event details for inclusion in confirmation prompts."

# from .__init__ import render_delete_calendar_event_confirmation
def render_delete_calendar_event_confirmation(args: Dict[(str, Any)], event_details: Optional[Dict[(str, Any)]], timezone_str: str) -> str:
    "Renders the confirmation message for deleting a calendar event."

# from .__init__ import render_modify_calendar_event_confirmation
def render_modify_calendar_event_confirmation(args: Dict[(str, Any)], event_details: Optional[Dict[(str, Any)]], timezone_str: str) -> str:
    "Renders the confirmation message for modifying a calendar event."

# from .__init__ import LocalToolsProvider
class LocalToolsProvider:
    "Provides and executes locally defined Python functions as tools."

# from .__init__ import CompositeToolsProvider
class CompositeToolsProvider:
    "Combines multiple tool providers into a single interface."

# from .__init__ import ConfirmingToolsProvider
class ConfirmingToolsProvider(ToolsProvider):
    """A wrapper provider that intercepts calls to specific tools,
    requests user confirmation via a callback, and then either executes
    the tool with the wrapped provider or returns a cancellation message."""

# from .types import ToolExecutionContext
class ToolExecutionContext:
    "Context passed to tool execution functions."

# from .types import ToolNotFoundError
class ToolNotFoundError(LookupError):
    "Custom exception raised when a tool cannot be found by any provider."

# from .mcp import MCPToolsProvider
class MCPToolsProvider:
    """Provides and executes tools hosted on MCP servers.
    Handles connection, fetching definitions, and execution."""

# from .schema import render_schema_as_html
def render_schema_as_html(schema_json_str: Optional[str]) -> str:
    """Renders a JSON schema (passed as a JSON string) as HTML using json-schema-for-humans.
    Uses temporary files for input and output.
    The JSON string input makes the function cacheable."""

# from .notes import get_all_notes
async def get_all_notes(db_context: DatabaseContext) -> List[Dict[(str, str)]]:
    "Retrieves all notes."

# from .notes import get_note_by_title
async def get_note_by_title(db_context: DatabaseContext, title: str) -> Optional[Dict[(str, Any)]]:
    "Retrieves a specific note by its title."

# from .notes import add_or_update_note
async def add_or_update_note(db_context: DatabaseContext, title: str, content: str) -> str:
    "Adds a new note or updates an existing note with the given title (upsert)."

# from .notes import delete_note
async def delete_note(db_context: DatabaseContext, title: str) -> bool:
    "Deletes a note by title."

# from .email import store_incoming_email
async def store_incoming_email(db_context: DatabaseContext, form_data: Dict[(str, Any)], notify_event: Optional[?]):
    """Parses incoming email data (from Mailgun webhook form) and prepares it for storage.
    Stores the parsed data in the `received_emails` table using the provided context,
    optionally notifying a worker event.

    Args:
        db_context: The DatabaseContext to use for the operation.
        form_data: A dictionary representing the form data received from the webhook."""

# from .vector_search import MetadataFilter
class MetadataFilter:
    "Represents a simple key-value filter for JSONB metadata."

# from .vector_search import VectorSearchQuery
class VectorSearchQuery:
    "Input schema for performing vector/keyword/hybrid searches."

# from .vector_search import query_vector_store
async def query_vector_store(db_context: DatabaseContext, query: VectorSearchQuery, query_embedding: Optional[List[float]]) -> List[Dict[(str, Any)]]:
    """Performs vector, keyword, or hybrid search based on the VectorSearchQuery input.

    Args:
        db_context: The database context manager.
        query: The VectorSearchQuery object containing all parameters and filters.
        query_embedding: The vector embedding for semantic search (required if query.search_type
                         involves 'semantic').

    Returns:
        A list of dictionaries representing the search results."""

# from .vector import Document
class Document(Protocol):
    "Defines the interface for documents that can be ingested into vector storage.Defines the interface for document objects that can be ingested into vector storage."

# from .vector import Base
class Base(DeclarativeBase):

# from .vector import DocumentRecord
class DocumentRecord(Base):
    "SQLAlchemy model for the 'documents' table, representing stored document metadata."

# from .vector import DocumentEmbeddingRecord
class DocumentEmbeddingRecord(Base):
    "SQLAlchemy model for the 'document_embeddings' table, representing stored embeddings."

# from .vector import init_vector_db
async def init_vector_db(db_context: DatabaseContext):
    """Initializes the vector database components (extension, indexes) using the provided context.
    Tables should be created separately via storage.init_db or metadata.create_all.

    Args:
        db_context: The DatabaseContext to use for executing initialization commands."""

# from .vector import add_document
async def add_document(db_context: DatabaseContext, doc: Document, enriched_doc_metadata: Optional[Dict[(str, Any)]]) -> int:
    """Adds a document record to the database or updates it based on source_id.

    Args:
        db_context: The DatabaseContext to use for the operation.
        doc: An object conforming to the Document protocol.
        enriched_doc_metadata: Optional dictionary containing enriched metadata.

    Returns:
        The database ID of the added or existing document."""

# from .vector import get_document_by_source_id
async def get_document_by_source_id(db_context: DatabaseContext, source_id: str) -> Optional[DocumentRecord]:
    "Retrieves a document ORM object by its source ID."

# from .vector import get_document_by_id
async def get_document_by_id(db_context: DatabaseContext, document_id: int) -> Optional[DocumentRecord]:
    "Retrieves a document ORM object by its internal primary key ID."

# from .vector import add_embedding
async def add_embedding(db_context: DatabaseContext, document_id: int, chunk_index: int, embedding_type: str, embedding: List[float], embedding_model: str, content: Optional[str], content_hash: Optional[str], embedding_doc_metadata: Optional[Dict[(str, Any)]]) -> ?:
    """Adds an embedding record linked to a document, updating if it already exists.

    Args:
        db_context: The DatabaseContext to use for the operation.
        document_id: ID of the parent document.
        chunk_index: Index of this chunk within the document for this embedding type.
        embedding_type: Type of embedding (e.g., 'title', 'content_chunk').
        embedding: The vector embedding.
        embedding_model: Name of the model used to generate the embedding.
        content: Optional textual content of the chunk.
        content_hash: Optional hash of the content.
        embedding_doc_metadata: Optional metadata specific to this embedding."""

# from .vector import delete_document
async def delete_document(db_context: DatabaseContext, document_id: int) -> bool:
    """Deletes a document and its associated embeddings (via CASCADE constraint).

    Returns:
        True if a document was deleted, False otherwise."""

# from .vector import query_vectors
async def query_vectors(db_context: DatabaseContext, query_embedding: List[float], embedding_model: str, keywords: Optional[str], filters: Optional[Dict[(str, Any)]], embedding_type_filter: Optional[List[str]], limit: int=10) -> List[Dict[(str, Any)]]:
    """Performs a hybrid search combining vector similarity and keyword search
    with metadata filtering using the provided DatabaseContext.

    Args:
        db_context: The DatabaseContext to use for the query.
        query_embedding: The vector representation of the search query.
        embedding_model: Identifier of the model used for the query vector.
        keywords: Keywords for full-text search.
        filters: Dictionary of filters for the 'documents' table.
        embedding_type_filter: List of allowed embedding types.
        limit: The maximum number of results.

    Returns:
        A list of dictionaries representing relevant document embeddings."""

# from .context import DatabaseContext
class DatabaseContext:
    """Context manager for database operations with retry logic.

    This class provides a centralized way to handle database connections,
    transactions, and retry logic for database operations."""

# from .context import get_db_context
async def get_db_context(engine: Optional[AsyncEngine], max_retries: int=3, base_delay: float=0.5) -> DatabaseContext:
    """Create and enter a database context.

    This function creates a DatabaseContext and enters its async context manager,
    returning the active context. This is intended to be used with an
    async with statement.

    Args:
        engine: Optional SQLAlchemy AsyncEngine for dependency injection.
        max_retries: Maximum number of retries for database operations.
        base_delay: Base delay in seconds for exponential backoff.

    Returns:
        An active DatabaseContext.

    Example:
        ```python
        async with get_db_context() as db:
            result = await db.fetch_all(...)
        ```"""

# from .tasks import enqueue_task
async def enqueue_task(db_context: DatabaseContext, task_id: str, task_type: str, payload: Optional[Dict[(str, Any)]], scheduled_at: Optional[datetime], max_retries_override: Optional[int], recurrence_rule: Optional[str], original_task_id: Optional[str], notify_event: Optional[?]):
    "Adds a task to the queue, optional notification."

# from .tasks import dequeue_task
async def dequeue_task(db_context: DatabaseContext, worker_id: str, task_types: List[str]) -> Optional[Dict[(str, Any)]]:
    "Atomically dequeues the next available task."

# from .tasks import update_task_status
async def update_task_status(db_context: DatabaseContext, task_id: str, status: str, error: Optional[str]) -> bool:
    "Updates task status."

# from .tasks import reschedule_task_for_retry
async def reschedule_task_for_retry(db_context: DatabaseContext, task_id: str, next_scheduled_at: datetime, new_retry_count: int, error: str) -> bool:
    "Reschedules a task for retry."

# from .tasks import get_all_tasks
async def get_all_tasks(db_context: DatabaseContext, limit: int=100) -> List[Dict[(str, Any)]]:
    "Retrieves tasks, ordered by creation descending."

# from .__init__ import _is_alembic_managed
async def _is_alembic_managed(engine: AsyncEngine) -> bool:
    "Checks if the database schema is managed by Alembic."

# from .__init__ import _log_current_revision
async def _log_current_revision(engine: AsyncEngine):
    "Logs the current Alembic revision stored in the database."

# from .__init__ import _get_alembic_config
def _get_alembic_config(engine: AsyncEngine) -> AlembicConfig:
    "Loads the Alembic configuration."

# from .__init__ import _run_alembic_command
async def _run_alembic_command(engine: AsyncEngine, config: AlembicConfig, command_name: str, *args):
    "Executes an Alembic command asynchronously using the engine's connection."

# from .__init__ import _create_initial_schema
async def _create_initial_schema(engine: AsyncEngine):
    "Creates all tables defined in the SQLAlchemy metadata."

# from .__init__ import _initialize_vector_storage
async def _initialize_vector_storage(engine: AsyncEngine):
    "Initializes vector database components if enabled."

# from .__init__ import init_db
async def init_db():
    """Initializes the database:
    - Checks if the database is managed by Alembic.
    - If managed, runs Alembic upgrade to head.
    - If not managed, creates the initial schema using SQLAlchemy metadata,
      stamps the database with the Alembic head revision, and initializes
      vector storage if enabled.
    - Includes retry logic for transient database errors."""

# from .base import get_engine
def get_engine():
    "Returns the initialized SQLAlchemy async engine."

# from .message_history import add_message_to_history
async def add_message_to_history(db_context: DatabaseContext, interface_type: str, conversation_id: str, interface_message_id: Optional[str], turn_id: Optional[str], thread_root_id: Optional[int], timestamp: datetime, role: str, content: Optional[str], tool_calls: Optional[List[Dict[(str, Any)]]], reasoning_info: Optional[Dict[(str, Any)]], error_traceback: Optional[str], tool_call_id: Optional[str]) -> Optional[Dict[(str, Any)]]:
    "Adds a message to the history table, including optional fields."

# from .message_history import update_message_interface_id
async def update_message_interface_id(db_context: DatabaseContext, internal_id: int, interface_message_id: str) -> bool:
    "Updates the interface_message_id for a message identified by its internal_id."

# from .message_history import get_recent_history
async def get_recent_history(db_context: DatabaseContext, interface_type: str, conversation_id: str, limit: int, max_age: timedelta) -> List[Dict[(str, Any)]]:
    """Retrieves recent messages for a conversation, including tool call info.
    If a message included by limit/max_age belongs to a turn, all other messages
    from that turn for the same conversation are also included, even if they
    would otherwise be outside the limit/max_age."""

# from .message_history import get_message_by_interface_id
async def get_message_by_interface_id(db_context: DatabaseContext, interface_type: str, conversation_id: str, interface_message_id: str) -> Optional[Dict[(str, Any)]]:
    "Retrieves a specific message by its chat and message ID, including all fields."

# from .message_history import get_messages_by_turn_id
async def get_messages_by_turn_id(db_context: DatabaseContext, turn_id: str) -> List[Dict[(str, Any)]]:
    "Retrieves all messages associated with a specific turn ID."

# from .message_history import get_messages_by_thread_id
async def get_messages_by_thread_id(db_context: DatabaseContext, thread_root_id: int) -> List[Dict[(str, Any)]]:
    "Retrieves all messages belonging to a specific conversation thread."

# from .message_history import get_grouped_message_history
async def get_grouped_message_history(db_context: DatabaseContext) -> Dict[(Tuple[(str, str)], List[Dict[(str, Any)]])]:
    "Retrieves all message history, grouped by (interface_type, conversation_id) and ordered by timestamp."

# from .dispatch_processors import EmbeddingDispatchProcessor
class EmbeddingDispatchProcessor(ContentProcessor):
    """Identifies IndexableContent items of specified types and dispatches them
    for embedding via the 'embed_and_store_batch' task."""

# from .text_processors import TextChunker
class TextChunker(ContentProcessor):
    "Splits textual content of IndexableContent items into smaller chunks."

# from .metadata_processors import TitleExtractor
class TitleExtractor(ContentProcessor):
    "Extracts the title from the original document and creates an IndexableContent item for it."

