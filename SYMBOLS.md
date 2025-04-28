# from .__main__ import load_config
def load_config():
    "Loads configuration from environment variables and prompts.yaml."

# from .__main__ import load_mcp_config_and_connect
async def load_mcp_config_and_connect():
    "Loads MCP server config, connects to servers, and discovers tools."

# from .__main__ import shutdown_handler
async def shutdown_handler(signal_name: str, telegram_service: Optional[TelegramService]):
    "Initiates graceful shutdown."

# from .__main__ import reload_config_handler
def reload_config_handler(signum, frame):
    "Handles SIGHUP for config reloading (placeholder)."

# from .__main__ import main_async
async def main_async(cli_args: ?) -> Optional[TelegramService]:
    "Initializes and runs the bot application."

# from .__main__ import main
def main() -> int:
    "Sets up argument parsing, event loop, and signal handlers."

# from .calendar_integration import format_datetime_or_date
def format_datetime_or_date(dt_obj: ?, is_end: bool) -> str:
    "Formats datetime or date object into a user-friendly string."

# from .calendar_integration import parse_event
def parse_event(event_data: str) -> Optional[Dict[(str, Any)]]:
    "Parses VCALENDAR data into a dictionary."

# from .calendar_integration import _fetch_ical_events_async
async def _fetch_ical_events_async(ical_urls: List[str]) -> List[Dict[(str, Any)]]:
    "Asynchronously fetches and parses events from a list of iCal URLs."

# from .calendar_integration import _fetch_caldav_events_sync
def _fetch_caldav_events_sync(username: str, password: str, calendar_urls: List[str]) -> List[Dict[(str, Any)]]:
    "Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."

# from .calendar_integration import fetch_upcoming_events
async def fetch_upcoming_events(calendar_config: Dict[(str, Any)]) -> List[Dict[(str, Any)]]:
    "Fetches events from configured CalDAV and iCal sources and merges them."

# from .calendar_integration import format_events_for_prompt
def format_events_for_prompt(events: List[Dict[(str, Any)]], prompts: Dict[(str, str)]) -> Tuple[(str, str)]:
    "Formats the fetched events into strings suitable for the prompt."

# from .processing import ProcessingService
class ProcessingService:
    """Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls."""

# from .llm import LLMOutput
class LLMOutput:
    "Standardized output structure from an LLM call."

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

# from .telegram_bot import TelegramUpdateHandler
class TelegramUpdateHandler:
    "Handles specific Telegram updates (messages, commands) and delegates processing."

# from .telegram_bot import TelegramService
class TelegramService:
    "Manages the Telegram bot application lifecycle and update handling."

# from .tools import ToolExecutionContext
class ToolExecutionContext:
    "Context passed to tool execution functions."

# from .tools import ToolNotFoundError
class ToolNotFoundError(LookupError):
    "Custom exception raised when a tool cannot be found by any provider."

# from .tools import ToolsProvider
class ToolsProvider(Protocol):
    "Protocol defining the interface for a tool provider."

# from .tools import schedule_recurring_task_tool
async def schedule_recurring_task_tool(exec_context: ToolExecutionContext, task_type: str, initial_schedule_time: str, recurrence_rule: str, payload: Dict[(str, Any)], max_retries: Optional[int]=3, description: Optional[str]):
    """Schedules a new recurring task.

    Args:
        task_type: The type of the task (e.g., 'send_daily_brief', 'check_reminders').
        initial_schedule_time: ISO 8601 datetime string for the *first* run.
        recurrence_rule: RRULE string specifying the recurrence (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0').
        payload: JSON object containing data needed by the task handler.
        max_retries: Maximum number of retries for each instance (default 3).
        description: A short, URL-safe description to include in the task ID (e.g., 'daily_brief')."""

# from .tools import schedule_future_callback_tool
async def schedule_future_callback_tool(exec_context: ToolExecutionContext, callback_time: str, context: str):
    """Schedules a task to trigger an LLM callback in a specific chat at a future time.

    Args:
        exec_context: The ToolExecutionContext containing chat_id, application instance, and db_context.
        callback_time: ISO 8601 formatted datetime string (including timezone).
        context: The context/prompt for the future LLM callback."""

# from .tools import LocalToolsProvider
class LocalToolsProvider:
    "Provides and executes locally defined Python functions as tools."

# from .tools import MCPToolsProvider
class MCPToolsProvider:
    "Provides and executes tools hosted on MCP servers."

# from .tools import CompositeToolsProvider
class CompositeToolsProvider:
    "Combines multiple tool providers into a single interface."

# from .web_server import get_db
async def get_db() -> DatabaseContext:
    "FastAPI dependency to get a DatabaseContext."

# from .web_server import get_embedding_generator_dependency
async def get_embedding_generator_dependency(request: Request) -> EmbeddingGenerator:
    "Retrieves the configured EmbeddingGenerator instance from app state."

# from .web_server import SearchResultItem
class SearchResultItem(BaseModel):

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
async def view_message_history(request: Request, db_context: DatabaseContext=...):
    "Serves the page displaying message history."

# from .web_server import view_tasks
async def view_tasks(request: Request, db_context: DatabaseContext=...):
    "Serves the page displaying scheduled tasks."

# from .web_server import health_check
async def health_check():
    "Basic health check endpoint."

# from .web_server import vector_search_form
async def vector_search_form(request: Request, db_context: DatabaseContext=...):
    "Serves the vector search form."

# from .web_server import handle_vector_search
async def handle_vector_search(request: Request, semantic_query: Optional[str]=..., keywords: Optional[str]=..., search_type: str=..., embedding_model: Optional[str]=..., embedding_types: List[str]=..., source_types: List[str]=..., created_after: Optional[str]=..., created_before: Optional[str]=..., title_like: Optional[str]=..., metadata_keys: List[str]=..., metadata_values: List[str]=..., limit: int=..., rrf_k: int=..., db_context: DatabaseContext=..., embedding_generator: EmbeddingGenerator=...):
    "Handles the vector search form submission."

# from .task_worker import handle_log_message
async def handle_log_message(db_context: DatabaseContext, payload: Any):
    "Simple task handler that logs the received payload."

# from .task_worker import format_llm_response_for_telegram
def format_llm_response_for_telegram(response_text: str) -> str:
    "Converts LLM Markdown to Telegram MarkdownV2, with fallback."

# from .task_worker import handle_llm_callback
async def handle_llm_callback(db_context: DatabaseContext, payload: Any):
    "Task handler for LLM scheduled callbacks."

# from .task_worker import task_worker_loop
async def task_worker_loop(worker_id: str, wake_up_event: ?):
    "Continuously polls for and processes tasks."

# from .task_worker import register_task_handler
def register_task_handler(task_type: str, handler: Callable[(?, Awaitable[?])]):
    "Register a new task handler function for a specific task type."

# from .task_worker import set_processing_service
def set_processing_service(service: ProcessingService):
    "Set the ProcessingService instance from main.py"

# from .task_worker import get_task_handlers
def get_task_handlers():
    "Return the current task handlers dictionary"

# from .email_indexer import EmailDocument
class EmailDocument(Document):
    """Represents an email document conforming to the Document protocol
    for vector storage ingestion. Includes methods to convert from
    a received_emails table row."""

# from .email_indexer import handle_index_email
async def handle_index_email(db_context: DatabaseContext, payload: Dict[(str, Any)]):
    "Task handler to index a specific email from the received_emails table."

# from .email_indexer import set_indexing_dependencies
def set_indexing_dependencies(embedding_generator: EmbeddingGenerator, llm_client: Optional[LLMInterface]):
    "Sets the necessary dependencies for the email indexer."

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
async def store_incoming_email(db_context: DatabaseContext, form_data: Dict[(str, Any)]):
    """Parses incoming email data (from Mailgun webhook form) and prepares it for storage.
    Stores the parsed data in the `received_emails` table using the provided context.

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

# from .vector import add_embedding
async def add_embedding(db_context: DatabaseContext, document_id: int, chunk_index: int, embedding_type: str, embedding: List[float], embedding_model: str, content: Optional[str], content_hash: Optional[str]) -> ?:
    "Adds an embedding record linked to a document, updating if it already exists."

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

# from .__init__ import init_db
async def init_db():
    "Initializes the database by creating all tables defined in the metadata."

# from .vector. import Document
class Document(Protocol):
    "Defines the interface for documents that can be ingested into vector storage.Defines the interface for document objects that can be ingested into vector storage."

# from .vector. import Base
class Base(DeclarativeBase):

# from .vector. import DocumentRecord
class DocumentRecord(Base):
    "SQLAlchemy model for the 'documents' table, representing stored document metadata."

# from .vector. import DocumentEmbeddingRecord
class DocumentEmbeddingRecord(Base):
    "SQLAlchemy model for the 'document_embeddings' table, representing stored embeddings."

# from .vector. import init_vector_db
async def init_vector_db(db_context: DatabaseContext):
    """Initializes the vector database components (extension, indexes) using the provided context.
    Tables should be created separately via storage.init_db or metadata.create_all.

    Args:
        db_context: The DatabaseContext to use for executing initialization commands."""

# from .vector. import add_document
async def add_document(db_context: DatabaseContext, doc: Document, enriched_doc_metadata: Optional[Dict[(str, Any)]]) -> int:
    """Adds a document record to the database or updates it based on source_id.

    Args:
        db_context: The DatabaseContext to use for the operation.
        doc: An object conforming to the Document protocol.
        enriched_doc_metadata: Optional dictionary containing enriched metadata.

    Returns:
        The database ID of the added or existing document."""

# from .vector. import get_document_by_source_id
async def get_document_by_source_id(db_context: DatabaseContext, source_id: str) -> Optional[DocumentRecord]:
    "Retrieves a document ORM object by its source ID."

# from .vector. import add_embedding
async def add_embedding(db_context: DatabaseContext, document_id: int, chunk_index: int, embedding_type: str, embedding: List[float], embedding_model: str, content: Optional[str], content_hash: Optional[str]) -> ?:
    "Adds an embedding record linked to a document, updating if it already exists."

# from .vector. import delete_document
async def delete_document(db_context: DatabaseContext, document_id: int) -> bool:
    """Deletes a document and its associated embeddings (via CASCADE constraint).

    Returns:
        True if a document was deleted, False otherwise."""

# from .vector. import query_vectors
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

# from .base import get_engine
def get_engine():
    "Returns the initialized SQLAlchemy async engine."

# from .message_history import add_message_to_history
async def add_message_to_history(db_context: DatabaseContext, chat_id: int, message_id: int, timestamp: datetime, role: str, content: str, tool_calls_info: Optional[List[Dict[(str, Any)]]]):
    "Adds a message to the history table, including optional tool call info."

# from .message_history import get_recent_history
async def get_recent_history(db_context: DatabaseContext, chat_id: int, limit: int, max_age: timedelta) -> List[Dict[(str, Any)]]:
    "Retrieves recent messages for a chat, including tool call info."

# from .message_history import get_message_by_id
async def get_message_by_id(db_context: DatabaseContext, chat_id: int, message_id: int) -> Optional[Dict[(str, Any)]]:
    "Retrieves a specific message by its chat and message ID."

# from .message_history import get_grouped_message_history
async def get_grouped_message_history(db_context: DatabaseContext) -> Dict[(int, List[Dict[(str, Any)]])]:
    "Retrieves all message history, grouped by chat_id and ordered by timestamp."

