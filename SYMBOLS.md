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

# from .processing import schedule_recurring_task_tool
async def schedule_recurring_task_tool(task_type: str, initial_schedule_time: str, recurrence_rule: str, payload: Dict[(str, Any)], max_retries: Optional[int]=3, description: Optional[str]):
    """Schedules a new recurring task.

    Args:
        task_type: The type of the task (e.g., 'send_daily_brief', 'check_reminders').
        initial_schedule_time: ISO 8601 datetime string for the *first* run.
        recurrence_rule: RRULE string specifying the recurrence (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0').
        payload: JSON object containing data needed by the task handler.
        max_retries: Maximum number of retries for each instance (default 3).
        description: A short, URL-safe description to include in the task ID (e.g., 'daily_brief')."""

# from .processing import schedule_future_callback_tool
async def schedule_future_callback_tool(callback_time: str, context: str, chat_id: int):
    """Schedules a future callback task to execute at the specified time.

    The payload will be enhanced to include the application reference."""

# from .processing import execute_function_call
async def execute_function_call(tool_call: Any, chat_id: int, mcp_sessions: Dict[(str, Any)], tool_name_to_server_id: Dict[(str, str)]) -> Dict[(str, Any)]:
    """Executes a function call requested by the LLM, checking local and MCP tools.

    Injects chat_id for specific local tools like schedule_future_callback."""

# from .processing import get_llm_response
async def get_llm_response(messages: List[Dict[(str, Any)]], chat_id: int, model: str, all_tools: List[Dict[(str, Any)]], mcp_sessions: Dict[(str, Any)], tool_name_to_server_id: Dict[(str, str)]) -> Tuple[(Optional[str], Optional[List[Dict[(str, Any)]]])]:
    """Sends the conversation history (and tools) to the LLM, handles potential tool calls,
    and returns the final response content along with details of any tool calls made.

    Args:
        messages: A list of message dictionaries.
        model: The identifier of the LLM model.

    Returns:
        A tuple containing:
        - The final response content string from the LLM (or None).
        - A list of dictionaries detailing executed tool calls (or None)."""

# from .main import load_config
def load_config():
    "Loads configuration from environment variables and prompts.yaml."

# from .main import load_mcp_config_and_connect
async def load_mcp_config_and_connect():
    "Loads MCP server config, connects to servers, and discovers tools."

# from .main import typing_notifications
async def typing_notifications(context: ?, chat_id: int, action: str=...):
    "Context manager to send typing notifications periodically."

# from .main import _generate_llm_response_for_chat
async def _generate_llm_response_for_chat(chat_id: int, trigger_content_parts: List[Dict[(str, Any)]], user_name: str, model_name: str) -> Tuple[(Optional[str], Optional[List[Dict[(str, Any)]]])]:
    """Prepares context, message history, calls the LLM, and returns the response content
    along with any tool call information.

    Args:
        chat_id: The target chat ID.
        trigger_content_parts: List of content parts (text, image_url) for the triggering message.
        user_name: The user name to format into the system prompt.

    Returns:
        A tuple: (LLM response string or None, List of tool call info dicts or None)."""

# from .main import start
async def start(update: Update, context: ?) -> ?:
    "Sends a welcome message when the /start command is issued."

# from .main import process_chat_queue
async def process_chat_queue(chat_id: int, context: ?) -> ?:
    "Processes the message buffer for a given chat."

# from .main import message_handler
async def message_handler(update: Update, context: ?) -> ?:
    "Buffers incoming messages and triggers processing if not already running."

# from .main import error_handler
async def error_handler(update: object, context: CallbackContext) -> ?:
    "Log the error and send a telegram message to notify the developer."

# from .main import shutdown_handler
async def shutdown_handler(signal_name: str):
    "Initiates graceful shutdown."

# from .main import reload_config_handler
def reload_config_handler(signum, frame):
    "Handles SIGHUP for config reloading (placeholder)."

# from .main import main_async
async def main_async(cli_args: ?) -> ?:
    "Initializes and runs the bot application."

# from .main import main
def main() -> int:
    "Sets up argument parsing, event loop, and signal handlers."

# from .web_server import read_root
async def read_root(request: Request):
    "Serves the main page listing all notes."

# from .web_server import add_note_form
async def add_note_form(request: Request):
    "Serves the form to add a new note."

# from .web_server import edit_note_form
async def edit_note_form(request: Request, title: str):
    "Serves the form to edit an existing note."

# from .web_server import save_note
async def save_note(request: Request, title: str=..., content: str=..., original_title: Optional[str]=...):
    "Handles saving a new or updated note."

# from .web_server import delete_note_post
async def delete_note_post(request: Request, title: str):
    "Handles deleting a note."

# from .web_server import handle_mail_webhook
async def handle_mail_webhook(request: Request):
    """Receives incoming email via webhook (expects multipart/form-data).
    Logs the received form data for now."""

# from .web_server import view_message_history
async def view_message_history(request: Request):
    "Serves the page displaying message history."

# from .web_server import view_tasks
async def view_tasks(request: Request):
    "Serves the page displaying scheduled tasks."

# from .web_server import health_check
async def health_check():
    "Basic health check endpoint."

# from .task_worker import handle_log_message
async def handle_log_message(payload: Any):
    "Simple task handler that logs the received payload."

# from .task_worker import format_llm_response_for_telegram
def format_llm_response_for_telegram(response_text: str) -> str:
    "Converts LLM Markdown to Telegram MarkdownV2, with fallback."

# from .task_worker import handle_llm_callback
async def handle_llm_callback(payload: Any):
    "Task handler for LLM scheduled callbacks."

# from .task_worker import task_worker_loop
async def task_worker_loop(worker_id: str, wake_up_event: ?):
    "Continuously polls for and processes tasks."

# from .task_worker import register_task_handler
def register_task_handler(task_type: str, handler: Callable):
    "Register a new task handler function for a specific task type."

# from .task_worker import set_llm_response_generator
def set_llm_response_generator(generator_func):
    "Set the LLM response generator function from main.py"

# from .task_worker import set_mcp_state
def set_mcp_state(sessions, tools, tool_name_mapping):
    "Set MCP state from main.py"

# from .task_worker import get_task_handlers
def get_task_handlers():
    "Return the current task handlers dictionary"

# from .notes import get_all_notes
async def get_all_notes() -> List[Dict[(str, str)]]:
    "Retrieves all notes, with retries."

# from .notes import get_note_by_title
async def get_note_by_title(title: str) -> Optional[Dict[(str, Any)]]:
    "Retrieves a specific note by its title, with retries."

# from .notes import add_or_update_note
async def add_or_update_note(title: str, content: str):
    "Adds/updates a note, with retries."

# from .notes import delete_note
async def delete_note(title: str) -> bool:
    "Deletes a note by title, with retries."

# from .email import store_incoming_email
async def store_incoming_email(form_data: Dict[(str, Any)]):
    """Parses incoming email data (from Mailgun webhook form) and prepares it for storage.
    Stores the parsed data in the `received_emails` table.

    Args:
        form_data: A dictionary representing the form data received from the webhook."""

# from .testing import test_engine
async def test_engine() -> AsyncGenerator[(AsyncEngine, ?)]:
    """Create an in-memory SQLite database engine for testing.

    This fixture creates an isolated in-memory SQLite database for testing.
    It yields an AsyncEngine that can be used to create connections and
    execute queries. When the test is complete, the engine is disposed of.

    Yields:
        An AsyncEngine connected to an in-memory SQLite database."""

# from .testing import test_db_context
async def test_db_context(test_engine: AsyncEngine) -> AsyncGenerator[(DatabaseContext, ?)]:
    """Create a DatabaseContext with a test engine.

    This fixture creates a DatabaseContext using the test_engine fixture.
    It yields the context for use in tests.

    Args:
        test_engine: The test engine fixture.

    Yields:
        A DatabaseContext connected to the test engine."""

# from .testing import run_with_test_db
async def run_with_test_db(test_func: Callable[(?, T)], *args, **kwargs) -> T:
    """Run a test function with a test database.

    This function creates an in-memory SQLite database, initializes it with
    the application's schema, and then runs the provided test function with
    a DatabaseContext connected to the test database.

    Args:
        test_func: An async function that takes a DatabaseContext as its first
                 argument, followed by any additional arguments.
        *args: Positional arguments to pass to the test function.
        **kwargs: Keyword arguments to pass to the test function.

    Returns:
        The return value of the test function."""

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
async def init_vector_db():
    "Initializes the vector database components (extension, indexes). Tables are created by storage.init_db."

# from .vector import add_document
async def add_document(doc: Document, enriched_doc_metadata: Optional[Dict[(str, Any)]]) -> int:
    """Adds a document record to the database or retrieves the existing one based on source_id.

    Uses the provided Document object (conforming to the protocol) to populate initial fields.
    Allows overriding or augmenting metadata with an optional enriched_metadata dictionary.

    Args:
        doc: An object conforming to the Document protocol (which has a .metadata property).
        enriched_doc_metadata: Optional dictionary containing metadata potentially enriched by an LLM,
                           which will be merged with or override the data from doc.metadata.

    Returns:
        The database ID of the added or existing document."""

# from .vector import get_document_by_source_id
async def get_document_by_source_id(source_id: str) -> Optional[Dict[(str, Any)]]:
    "Retrieves a document by its source ID."

# from .vector import add_embedding
async def add_embedding(document_id: int, chunk_index: int, embedding_type: str, embedding: List[float], embedding_model: str, content: Optional[str], content_hash: Optional[str]):
    "Adds an embedding record linked to a document."

# from .vector import delete_document
async def delete_document(document_id: int):
    "Deletes a document and its associated embeddings."

# from .vector import query_vectors
async def query_vectors(query_embedding: List[float], embedding_model: str, keywords: Optional[str], filters: Optional[Dict[(str, Any)]], embedding_type_filter: Optional[List[str]], limit: int=10) -> List[Dict[(str, Any)]]:
    """Performs a hybrid search combining vector similarity and keyword search
    with metadata filtering.

    Args:
        query_embedding: The vector representation of the search query.
        embedding_model: Identifier of the model used for the query vector (must match indexed models).
        keywords: Keywords for full-text search.
        filters: Dictionary of filters to apply to the 'documents' table
                 (e.g., {"source_type": "email", "created_at_gte": datetime(...)})
        embedding_type_filter: List of allowed embedding types to search within.
        limit: The maximum number of results to return.

    Returns:
        A list of dictionaries, each representing a relevant document chunk/embedding
        with its metadata and scores. Returns an empty list if skeleton."""

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
async def enqueue_task(task_id: str, task_type: str, payload: Optional[Dict[(str, Any)]], scheduled_at: Optional[datetime], max_retries_override: Optional[int], recurrence_rule: Optional[str], original_task_id: Optional[str], notify_event: Optional[?]):
    "Adds a task, handles retry logic, optional notification."

# from .tasks import dequeue_task
async def dequeue_task(worker_id: str, task_types: List[str]) -> Optional[Dict[(str, Any)]]:
    "Atomically dequeues the next available task, handles retries."

# from .tasks import update_task_status
async def update_task_status(task_id: str, status: str, error: Optional[str]) -> bool:
    "Updates task status, handles retries."

# from .tasks import reschedule_task_for_retry
async def reschedule_task_for_retry(task_id: str, next_scheduled_at: datetime, new_retry_count: int, error: str) -> bool:
    "Reschedules a task for retry, handles retries."

# from .tasks import get_all_tasks
async def get_all_tasks(limit: int=100) -> List[Dict[(str, Any)]]:
    "Retrieves tasks, ordered by creation descending, handles retries."

# from .__init__ import init_db
async def init_db():
    "Initializes the database by creating all tables defined in the metadata."

# from .base import get_engine
def get_engine():
    "Returns the initialized SQLAlchemy async engine."

# from .message_history import add_message_to_history
async def add_message_to_history(chat_id: int, message_id: int, timestamp: datetime, role: str, content: str, tool_calls_info: Optional[List[Dict[(str, Any)]]]):
    "Adds a message to the history table, including optional tool call info, with retries."

# from .message_history import get_recent_history
async def get_recent_history(chat_id: int, limit: int, max_age: timedelta) -> List[Dict[(str, Any)]]:
    "Retrieves recent messages for a chat, including tool call info, with retries."

# from .message_history import get_message_by_id
async def get_message_by_id(chat_id: int, message_id: int) -> Optional[Dict[(str, Any)]]:
    "Retrieves a specific message by its chat and message ID, with retries."

# from .message_history import get_grouped_message_history
async def get_grouped_message_history() -> Dict[(int, List[Dict[(str, Any)]])]:
    "Retrieves all message history, grouped by chat_id and ordered by timestamp."

