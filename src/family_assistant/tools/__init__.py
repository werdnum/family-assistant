"""
Module defining interfaces and implementations for providing and executing tools (local and MCP).
"""

import asyncio
import json
import logging
import uuid
import os
import pathlib
import inspect
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone, date, time # Added date, time
from typing import List, Dict, Any, Optional, Protocol, Callable, Awaitable, Set # Added Awaitable, Set
from zoneinfo import ZoneInfo

import aiofiles
import caldav
import vobject

from dateutil import rrule
from dateutil.parser import isoparse
from mcp import ClientSession
from telegram.ext import Application
from sqlalchemy.sql import text

# Import storage functions needed by local tools
from family_assistant import storage
from family_assistant.storage import get_recent_history
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector_search import VectorSearchQuery, query_vector_store
from family_assistant.embeddings import EmbeddingGenerator
from datetime import timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Import calendar helper functions AND tool implementations
from family_assistant import calendar_integration

# Import the context from the new types file
from .types import ToolExecutionContext, ToolNotFoundError
from .mcp import MCPToolsProvider

logger = logging.getLogger(__name__)

MCPToolsProvider = MCPToolsProvider

# --- Custom Exceptions ---


class ToolConfirmationRequired(Exception):
    """
    Special exception raised by ConfirmingToolsProvider to signal that
    confirmation is needed before proceeding. Contains info for the UI layer.
    """
    def __init__(self, confirmation_prompt: str, tool_name: str, tool_args: Dict[str, Any]):
        self.confirmation_prompt = confirmation_prompt
        self.tool_name = tool_name
        self.tool_args = tool_args
        super().__init__(f"Confirmation required for tool '{tool_name}'")


class ToolConfirmationFailed(Exception):
    """Raised when user denies confirmation or it times out."""
    pass


# --- Tool Provider Interface ---


class ToolsProvider(Protocol):
    """Protocol defining the interface for a tool provider."""

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Returns a list of tool definitions in LLM-compatible format."""
        ...

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        """
        Executes the specified tool.

        Args:
            name: The name of the tool to execute.
            arguments: A dictionary of arguments for the tool.
            context: The execution context (chat_id, application, etc.).

        Returns:
            A string result suitable for the LLM tool response message.

        Raises:
            ToolNotFoundError: If the tool `name` is not handled by this provider.
            Exception: If an error occurs during tool execution.
        """
        ...

    # Add the optional close method to the protocol
    async def close(self):
        """Optional method to clean up resources used by the provider."""
        ...


# --- Local Tool Implementations ---
# Refactored to accept context: ToolExecutionContext

# Calendar tool implementations (add, search, modify, delete) moved to calendar_integration.py

async def schedule_recurring_task_tool(
    exec_context: ToolExecutionContext,  # Renamed to avoid conflict
    task_type: str,
    initial_schedule_time: str,
    recurrence_rule: str,
    payload: Dict[str, Any],
    max_retries: Optional[int] = 3,
    description: Optional[str] = None,  # Optional description for the task ID
):
    """
    Schedules a new recurring task.

    Args:
        exec_context: The execution context containing db_context and timezone_str.
        task_type: The type of the task (e.g., 'send_daily_brief', 'check_reminders').
        initial_schedule_time: ISO 8601 datetime string for the *first* run.
        recurrence_rule: RRULE string specifying the recurrence (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0').
        payload: JSON object containing data needed by the task handler.
        max_retries: Maximum number of retries for each instance (default 3).
        description: A short, URL-safe description to include in the task ID (e.g., 'daily_brief').
    """
    logger.info(f"Executing schedule_recurring_task_tool: type='{task_type}', initial='{initial_schedule_time}', rule='{recurrence_rule}'")
    db_context = exec_context.db_context # Get db_context from execution context

    try:
        # Validate recurrence rule format (basic validation)
        try:
            # We don't need dtstart here, just parsing validity
            rrule.rrulestr(recurrence_rule)
        except ValueError as rrule_err:
            raise ValueError(f"Invalid recurrence_rule format: {rrule_err}")

        # Parse the initial schedule time
        initial_dt = isoparse(initial_schedule_time)
        if initial_dt.tzinfo is None:
            logger.warning(
                f"Initial schedule time '{initial_schedule_time}' lacks timezone. Assuming {exec_context.timezone_str}."
            )
            initial_dt = initial_dt.replace(tzinfo=ZoneInfo(exec_context.timezone_str))

        # Ensure it's in the future (optional, but good practice)
        # Comparing offset-aware with offset-naive will raise TypeError if initial_dt is naive
        # Ensure comparison is done with aware datetime
        now_aware = datetime.now(initial_dt.tzinfo or timezone.utc) # Use parsed timezone or UTC
        if initial_dt <= now_aware:
            raise ValueError("Initial schedule time must be in the future.")

        # Generate the *initial* unique task ID
        base_id = f"recurring_{task_type}"
        if description:
            safe_desc = "".join(
                c if c.isalnum() or c in ["-", "_"] else "_"
                for c in description.lower()
            )
            base_id += f"_{safe_desc}"
        initial_task_id = f"{base_id}_{uuid.uuid4()}"

        # Enqueue the first instance using the db_context from exec_context
        await storage.enqueue_task(
            db_context=db_context,  # Pass db_context
            task_id=initial_task_id,
            task_type=task_type,
            payload=payload,
            scheduled_at=initial_dt,
            max_retries_override=max_retries,  # Correct argument name
            recurrence_rule=recurrence_rule,
            # original_task_id=None, # Let enqueue_task handle setting it to initial_task_id
            # notify_event=new_task_event # No immediate notification needed usually
        )
        logger.info(
            f"Scheduled initial recurring task {initial_task_id} (Type: {task_type}) starting at {initial_dt} with rule '{recurrence_rule}'"
        )
        return f"OK. Recurring task '{initial_task_id}' scheduled starting {initial_schedule_time} with rule '{recurrence_rule}'."
    except ValueError as ve:
        logger.error(f"Invalid arguments for scheduling recurring task: {ve}")
        return f"Error: Invalid arguments provided. {ve}"
    except Exception as e:
        logger.error(f"Failed to schedule recurring task: {e}", exc_info=True)
        return "Error: Failed to schedule the recurring task."


# Removed duplicated/incorrect tool implementations above this line.
# The correct schedule_future_callback_tool follows.


async def schedule_future_callback_tool(
    exec_context: ToolExecutionContext,  # Use execution context
    callback_time: str,
    context: str,  # This is the LLM context string
):
    """
    Schedules a task to trigger an LLM callback in a specific chat at a future time.

    Args:
        exec_context: The ToolExecutionContext containing chat_id, application instance, and db_context.
        callback_time: ISO 8601 formatted datetime string (including timezone).
        context: The context/prompt for the future LLM callback.
    """
    # Get application instance, chat_id, and db_context from the execution context object
    application = exec_context.application
    chat_id = exec_context.chat_id
    db_context = exec_context.db_context  # Get db_context

    if not application:
        logger.error(
            "Application context not available in ToolExecutionContext for schedule_future_callback_tool."
        )
        # Raise error instead of returning string to allow Composite provider to potentially try others?
        # Or return error string as before? Let's return error string for now.
        return "Error: Application context not available."

    try:
        # Parse the ISO 8601 string, ensuring it's timezone-aware
        scheduled_dt = isoparse(callback_time)
        if scheduled_dt.tzinfo is None:
            # Or raise error, forcing LLM to provide timezone
            logger.warning(
                f"Callback time '{callback_time}' lacks timezone. Assuming {exec_context.timezone_str}."
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=ZoneInfo(exec_context.timezone_str))

        # Ensure it's in the future (optional, but good practice)
        if scheduled_dt <= datetime.now(timezone.utc):
            raise ValueError("Callback time must be in the future.")

        task_id = f"llm_callback_{uuid.uuid4()}"
        payload = {
            "chat_id": chat_id,
            "callback_context": context,
            # Application instance should not be stored in payload.
            # It will be injected into the task handler at runtime.
        }

        # TODO: Need access to the new_task_event from main.py to notify worker.
        # This refactor doesn't address passing the event down yet.
        await storage.enqueue_task(
            db_context=db_context,  # Pass db_context
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_dt,
            # notify_event=new_task_event # Needs event passed down
        )
        logger.info(
            f"Scheduled LLM callback task {task_id} for chat {chat_id} at {scheduled_dt}"
        )
        return f"OK. Callback scheduled for {callback_time}."
    except ValueError as ve:
        logger.error(f"Invalid callback time format or value: {callback_time} - {ve}")
        return f"Error: Invalid callback time provided. Ensure it's a future ISO 8601 datetime with timezone. {ve}"
    except Exception as e:
        logger.error(f"Failed to schedule callback task: {e}", exc_info=True)
        return "Error: Failed to schedule the callback."


async def search_documents_tool(
    exec_context: ToolExecutionContext,
    embedding_generator: EmbeddingGenerator, # Injected by LocalToolsProvider
    query_text: str,
    source_types: Optional[List[str]] = None,
    embedding_types: Optional[List[str]] = None,
    limit: int = 5, # Default limit for LLM tool
) -> str:
    """
    Searches stored documents using hybrid vector and keyword search.

    Args:
        exec_context: The execution context containing the database context.
        embedding_generator: The embedding generator instance.
        query_text: The natural language query to search for.
        source_types: Optional list of source types to filter by (e.g., ['email', 'note']).
        embedding_types: Optional list of embedding types to filter by (e.g., ['content_chunk', 'summary']).
        limit: Maximum number of results to return.

    Returns:
        A formatted string containing the search results or an error message.
    """
    logger.info(f"Executing search_documents_tool with query: '{query_text}'")
    db_context = exec_context.db_context
    # Use the provided generator's model name
    embedding_model = embedding_generator.model_name

    try:
        # 1. Generate query embedding
        if not query_text:
            return "Error: Query text cannot be empty."
        embedding_result = await embedding_generator.generate_embeddings([query_text])
        if not embedding_result.embeddings or len(embedding_result.embeddings) == 0:
            return "Error: Failed to generate embedding for the query."
        query_embedding = embedding_result.embeddings[0]

        # 2. Construct the search query object
        search_query = VectorSearchQuery(
            search_type='hybrid',
            semantic_query=query_text,
            keywords=query_text, # Use same text for keywords in this simplified tool
            embedding_model=embedding_model,
            source_types=source_types or [], # Use empty list if None
            embedding_types=embedding_types or [], # Use empty list if None
            limit=limit,
            # Use default rrf_k, metadata_filters, etc.
        )

        # 3. Execute the search
        results = await query_vector_store(
            db_context=db_context,
            query=search_query,
            query_embedding=query_embedding,
        )

        # 4. Format results for LLM
        if not results:
            return "No relevant documents found matching the query and filters."

        formatted_results = ["Found relevant documents:"]
        for i, res in enumerate(results):
            title = res.get('title') or 'Untitled Document'
            source = res.get('source_type', 'Unknown Source')
            # Truncate snippet for brevity
            snippet = res.get('embedding_source_content', '')
            if snippet:
                snippet = (snippet[:10000] + '...') if len(snippet) > 10000 else snippet
                snippet_text = f"\n  Snippet: {snippet}"
            else:
                snippet_text = ""

            formatted_results.append(
                f"{i+1}. Title: {title} (Source: {source}){snippet_text}"
            )

        return "\n".join(formatted_results)

    except Exception as e:
        logger.error(f"Error executing search_documents_tool: {e}", exc_info=True)
        return f"Error: Failed to execute document search. {e}"


async def get_full_document_content_tool(
    exec_context: ToolExecutionContext,
    document_id: int,
) -> str:
    """
    Retrieves the full text content associated with a specific document ID.
    This is typically used after finding a relevant document via search_documents.

    Args:
        exec_context: The execution context containing the database context.
        document_id: The unique ID of the document (obtained from search results).

    Returns:
        A string containing the full concatenated text content of the document,
        or an error message if not found or content is unavailable.
    """
    logger.info(f"Executing get_full_document_content_tool for document ID: {document_id}")
    db_context = exec_context.db_context

    try:
        # Query for content embeddings associated with the document ID, ordered by chunk index
        # Prioritize 'content_chunk' type, but could potentially fetch others if needed.
        # Using raw SQL for potential performance and direct access to embedding content.
        # Ensure table/column names match your schema.
        stmt = text("""
            SELECT content
            FROM document_embeddings
            WHERE document_id = :doc_id
              AND embedding_type = 'content_chunk' -- Assuming this type holds the main content
              AND content IS NOT NULL
            ORDER BY chunk_index ASC;
        """)
        results = await db_context.fetch_all(stmt, {"doc_id": document_id})

        if not results:
            # Check if the document exists at all, maybe it has no content embeddings?
            doc_check_stmt = text("SELECT id FROM documents WHERE id = :doc_id")
            doc_exists = await db_context.fetch_one(doc_check_stmt, {"doc_id": document_id})
            if doc_exists:
                logger.warning(f"Document ID {document_id} exists, but no 'content_chunk' embeddings with text content found.")
                # TODO: Future enhancement: Check document source_type and potentially fetch content
                # from original source (e.g., received_emails table) if no embedding content exists.
                return f"Error: Document {document_id} found, but no text content is available for retrieval via this tool."
            else:
                logger.warning(f"Document ID {document_id} not found.")
                return f"Error: Document with ID {document_id} not found."

        # Concatenate content from all chunks
        full_content = "".join([row['content'] for row in results])

        if not full_content.strip():
             logger.warning(f"Document ID {document_id} content chunks were empty or whitespace.")
             return f"Error: Document {document_id} found, but its text content appears to be empty."

        logger.info(f"Retrieved full content for document ID {document_id} (Length: {len(full_content)}).")
        # Return only the content for now. Future versions could return a dict with content_type.
        return full_content

    except Exception as e:
        logger.error(f"Error executing get_full_document_content_tool for ID {document_id}: {e}", exc_info=True)
        return f"Error: Failed to retrieve content for document ID {document_id}. {e}"


async def get_message_history_tool(
    exec_context: ToolExecutionContext,
    limit: int = 10,
    max_age_hours: int = 24,
) -> str:
    """
    Retrieves recent message history for the current chat, with optional filters.

    Args:
        exec_context: The execution context containing chat_id and db_context.
        limit: Maximum number of messages to retrieve (default: 10).
        max_age_hours: Maximum age of messages in hours (default: 24).

    Returns:
        A formatted string containing the message history or an error message.
    """
    chat_id = exec_context.chat_id
    db_context = exec_context.db_context
    logger.info(f"Executing get_message_history_tool for chat {chat_id} (limit={limit}, max_age_hours={max_age_hours})")

    try:
        max_age_delta = timedelta(hours=max_age_hours)
        history_messages = await get_recent_history(
            db_context=db_context,
            chat_id=chat_id,
            limit=limit,
            max_age=max_age_delta,
        )

        if not history_messages:
            return "No message history found matching the specified criteria."

        # Format the history for the LLM
        formatted_history = ["Retrieved message history:"]
        for msg in history_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp")
            time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S %Z") if timestamp else "Unknown Time"

            # Basic formatting, include full content
            formatted_history.append(f"[{time_str}] {role.capitalize()}: {content}")

            # Include tool call info if present (simplified)
            if role == "assistant" and msg.get("tool_calls_info_raw"):
                tool_calls = msg.get("tool_calls_info_raw", [])
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                         if isinstance(call, dict):
                            func_name = call.get('function_name', 'unknown_tool')
                            args = call.get('arguments', {})
                            resp = call.get('response_content', '')
                            formatted_history.append(f"  -> Called Tool: {func_name}({json.dumps(args)}) -> Response: {resp}") # Include full response

        return "\n".join(formatted_history)

    except Exception as e:
        logger.error(f"Error executing get_message_history_tool for chat {chat_id}: {e}", exc_info=True)
        return f"Error: Failed to retrieve message history. {e}"


# --- Documentation Tool Helper ---

def _scan_user_docs() -> List[str]:
    """Scans the 'docs/user/' directory for allowed documentation files."""
    docs_user_dir = pathlib.Path("docs") / "user"
    allowed_extensions = {".md", ".txt"}
    available_files = []
    if docs_user_dir.is_dir():
        try:
            for item in os.listdir(docs_user_dir):
                item_path = docs_user_dir / item
                if item_path.is_file() and any(item.endswith(ext) for ext in allowed_extensions):
                    available_files.append(item)
        except OSError as e:
            logger.error(f"Error scanning documentation directory '{docs_user_dir}': {e}", exc_info=True)
    else:
        logger.warning(f"User documentation directory not found: '{docs_user_dir}'")
    logger.info(f"Found user documentation files: {available_files}")
    return available_files

# --- User Documentation Tool Implementation ---

# --- Documentation Tool Helper ---

def _scan_user_docs() -> List[str]:
    """Scans the 'docs/user/' directory for allowed documentation files."""
    docs_user_dir = pathlib.Path("docs") / "user"
    allowed_extensions = {".md", ".txt"}
    available_files = []
    if docs_user_dir.is_dir():
        try:
            for item in os.listdir(docs_user_dir):
                item_path = docs_user_dir / item
                if item_path.is_file() and any(item.endswith(ext) for ext in allowed_extensions):
                    available_files.append(item)
        except OSError as e:
            logger.error(f"Error scanning documentation directory '{docs_user_dir}': {e}", exc_info=True)
    else:
        logger.warning(f"User documentation directory not found: '{docs_user_dir}'")
    logger.info(f"Found user documentation files: {available_files}")
    return available_files

# --- User Documentation Tool Implementation ---

async def get_user_documentation_content_tool(
    exec_context: ToolExecutionContext,
    filename: str,
) -> str:
    """
    Retrieves the content of a specified file from the user documentation directory ('docs/user/').

    Args:
        exec_context: The execution context (not directly used here, but available).
        filename: The name of the file within the 'docs/user/' directory (e.g., 'USER_GUIDE.md').

    Returns:
        The content of the file as a string, or an error message if the file is
        not found, not allowed, or cannot be read.
    """
    logger.info(f"Executing get_user_documentation_content_tool for filename: '{filename}'")

    # Basic security: Prevent directory traversal and limit to allowed extensions
    allowed_extensions = {".md", ".txt"}
    if ".." in filename or not any(filename.endswith(ext) for ext in allowed_extensions):
        logger.warning(f"Attempted access to disallowed filename: '{filename}'")
        return f"Error: Access denied. Invalid filename or extension '{filename}'."

    # Construct the full path relative to the project root (assuming standard structure)
    # Assumes the script runs from the project root or similar context.
    docs_user_dir = pathlib.Path("docs") / "user"
    file_path = (docs_user_dir / filename).resolve()

    # Security Check: Ensure the resolved path is still within the intended directory
    if docs_user_dir.resolve() not in file_path.parents:
         logger.error(f"Resolved path '{file_path}' is outside the allowed directory '{docs_user_dir.resolve()}'.")
         return f"Error: Access denied. Invalid path for filename '{filename}'."

    try:
        async with aiofiles.open(file_path, mode='r', encoding='utf-8') as f:
            content = await f.read()
        logger.info(f"Successfully read content from '{filename}'.")
        return content
    except FileNotFoundError:
        logger.warning(f"User documentation file not found: '{file_path}'")
        return f"Error: Documentation file '{filename}' not found."
    except Exception as e:
        logger.error(f"Error reading user documentation file '{filename}': {e}", exc_info=True)
        return f"Error: Failed to read documentation file '{filename}'. {e}"

# --- Local Tool Definitions and Mappings (Moved from processing.py) ---

# Map tool names to their actual implementation functions
# Note: add_or_update_note comes from storage
AVAILABLE_FUNCTIONS: Dict[str, Callable] = {
    "add_or_update_note": storage.add_or_update_note,
    "schedule_future_callback": schedule_future_callback_tool,
    "schedule_recurring_task": schedule_recurring_task_tool,
    "search_documents": search_documents_tool,
    "get_full_document_content": get_full_document_content_tool,
    "get_message_history": get_message_history_tool,
    "get_user_documentation_content": get_user_documentation_content_tool,
    # Calendar tools now imported from calendar_integration module
    "add_calendar_event": calendar_integration.add_calendar_event_tool,
    "search_calendar_events": calendar_integration.search_calendar_events_tool,
    "modify_calendar_event": calendar_integration.modify_calendar_event_tool,
    "delete_calendar_event": calendar_integration.delete_calendar_event_tool,
}

# --- Tool Confirmation Renderers ---

def _format_event_details_for_confirmation(details: Optional[Dict[str, Any]], timezone_str: str) -> str:
    """Formats fetched event details for inclusion in confirmation prompts."""
    if not details:
        return "Event details not found."
    summary = details.get('summary', 'No Title')
    # Pass timezone_str to the formatting function
    start_str = calendar_integration.format_datetime_or_date(details.get('start'), timezone_str, is_end=False)
    end_str = calendar_integration.format_datetime_or_date(details.get('end'), timezone_str, is_end=True)
    all_day = details.get('all_day', False)
    if all_day:
        # All-day events typically don't need timezone formatting, but pass it anyway for consistency
        # Or adjust format_datetime_or_date to handle date objects without requiring timezone_str
        # Assuming format_datetime_or_date handles date objects gracefully.
        return f"'{summary}' (All Day: {start_str})"
    else:
        return f"'{summary}' ({start_str} - {end_str})"

def render_delete_calendar_event_confirmation(args: Dict[str, Any], event_details: Optional[Dict[str, Any]], timezone_str: str) -> str:
    """Renders the confirmation message for deleting a calendar event."""
    event_desc = _format_event_details_for_confirmation(event_details, timezone_str) # Pass timezone
    cal_url = args.get('calendar_url', 'Unknown Calendar')
    # Use MarkdownV2 compatible formatting
    return (
        f"Please confirm you want to *delete* the event:\n"
        f"Event: {telegramify_markdown.escape_markdown(event_desc)}"
        # Removed calendar URL line: f"From Calendar: `{telegramify_markdown.escape_markdown(cal_url)}`"
    )

def render_modify_calendar_event_confirmation(args: Dict[str, Any], event_details: Optional[Dict[str, Any]], timezone_str: str) -> str:
    """Renders the confirmation message for modifying a calendar event."""
    event_desc = _format_event_details_for_confirmation(event_details, timezone_str) # Pass timezone
    cal_url = args.get('calendar_url', 'Unknown Calendar')
    changes = []
    # Use MarkdownV2 compatible formatting for code blocks/inline code
    if args.get('new_summary'): changes.append(f"\\- Set summary to: `{telegramify_markdown.escape_markdown(args['new_summary'])}`")
    if args.get('new_start_time'): changes.append(f"\\- Set start time to: `{telegramify_markdown.escape_markdown(args['new_start_time'])}`")
    if args.get('new_end_time'): changes.append(f"\\- Set end time to: `{telegramify_markdown.escape_markdown(args['new_end_time'])}`")
    if args.get('new_description'): changes.append(f"\\- Set description to: `{telegramify_markdown.escape_markdown(args['new_description'])}`")
    if args.get('new_all_day') is not None: changes.append(f"\\- Set all\\-day status to: `{args['new_all_day']}`")

    return (
        f"Please confirm you want to *modify* the event:\n"
        f"Event: {telegramify_markdown.escape_markdown(event_desc)}\n"
        # Removed calendar URL line: f"From Calendar: `{telegramify_markdown.escape_markdown(cal_url)}`\n"
        f"With the following changes:\n" + "\n".join(changes)
    )

# Update the Callable signature to include timezone_str
TOOL_CONFIRMATION_RENDERERS: Dict[str, Callable[[Dict[str, Any], Optional[Dict[str, Any]], str], str]] = {
    "delete_calendar_event": render_delete_calendar_event_confirmation,
    "modify_calendar_event": render_modify_calendar_event_confirmation,
}


# --- Helper to Fetch Event Details by UID (moved to calendar_integration.py) ---


# --- Tool Definitions ---
# Define local tools in the format LiteLLM expects (OpenAI format)
# Hardcoding common examples for now.
TOOLS_DEFINITION: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_or_update_note",
            "description": "Add a new note or update an existing note with the given title. Use this to remember information provided by the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The unique title of the note.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content of the note.",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_future_callback",
            "description": "Schedule a future trigger for yourself (the assistant) to continue processing or follow up on a topic at a specified time within the current chat context. Use this if the user asks you to do something later, or if a task requires waiting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "callback_time": {
                        "type": "string",
                        "description": "The exact date and time (ISO 8601 format, including timezone, e.g., '2025-05-10T14:30:00+02:00') when the callback should be triggered.",
                    },
                    "context": {
                        "type": "string",
                        "description": "The specific instructions or information you need to remember for the callback (e.g., 'Follow up on the flight booking status', 'Check if the user replied about the weekend plan').",
                    },
                },
                "required": ["callback_time", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_recurring_task",
            "description": "Schedule a task that will run repeatedly based on a recurrence rule (RRULE string). Use this for tasks that need to happen on a regular schedule, like sending a daily summary or checking for updates periodically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "description": "The identifier for the task handler that should process this task (e.g., 'send_daily_brief').",
                    },
                    "initial_schedule_time": {
                        "type": "string",
                        "description": "The exact date and time (ISO 8601 format with timezone, e.g., '2025-05-15T08:00:00+00:00') when the *first* instance of the task should run.",
                    },
                    "recurrence_rule": {
                        "type": "string",
                        "description": "An RRULE string defining the recurrence schedule according to RFC 5545 (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0' for 8:00 AM daily, 'FREQ=WEEKLY;BYDAY=MO' for every Monday).",
                    },
                    "payload": {
                        "type": "object",
                        "description": "A JSON object containing any necessary data or parameters for the task handler.",
                        "additionalProperties": True,  # Allow any structure within the payload
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": "Optional. Maximum number of retries for each instance if it fails (default: 3).",
                        "default": 3,
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional. A short, URL-safe description to help identify the task (e.g., 'daily_brief').",
                    },
                },
                "required": [
                    "task_type",
                    "initial_schedule_time",
                    "recurrence_rule",
                    "payload",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search previously stored documents (emails, notes, files) using semantic and keyword matching. Returns titles and snippets of the most relevant documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "The natural language query describing the information to search for.",
                    },
                    "source_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Filter results to only include documents from specific sources. Common sources: 'email', 'note', 'google_drive', 'pdf', 'image'. Use ONLY if you are certain about the source type, otherwise omit this filter.",
                    },
                    "embedding_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Filter results based on the type of content that was embedded. Common types: 'content_chunk', 'summary', 'title', 'ocr_text'. Use ONLY if necessary (e.g., searching only titles), otherwise omit this filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional. Maximum number of results to return (default: 5).",
                        "default": 5,
                    },
                },
                "required": ["query_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_document_content",
            "description": "Retrieves the full text content of a specific document using its unique document ID (obtained from a previous search). Use this when you need the complete text after identifying a relevant document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "The unique identifier of the document whose full content is needed.",
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": "Adds a new event to the primary family calendar (requires CalDAV configuration). Use this to schedule appointments, reminders with duration, or block out time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "The title or brief summary of the event.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "The start date or datetime of the event in ISO 8601 format. MUST include timezone offset (e.g., '2025-05-20T09:00:00+02:00' for timed event, '2025-05-21' for all-day).",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "The end date or datetime of the event in ISO 8601 format. MUST include timezone offset (e.g., '2025-05-20T10:30:00+02:00' for timed event, '2025-05-22' for all-day - note: end date is exclusive for all-day).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional. A more detailed description or notes for the event.",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": "Set to true if this is an all-day event, false or omit if it has specific start/end times. Determines if start/end times are treated as dates or datetimes.",
                        "default": False,
                    },
                },
                "required": ["summary", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_message_history",
            "description": "Retrieve past messages from the current conversation history. Use this if you need context from earlier in the conversation that might not be in the default short-term history window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Optional. The maximum number of messages to retrieve (most recent first). Default is 10.",
                        "default": 10,
                    },
                    "max_age_hours": {
                        "type": "integer",
                        "description": "Optional. Retrieve messages only up to this many hours old. Default is 24.",
                        "default": 24,
                    },
                },
                "required": [], # No parameters are strictly required, defaults will be used
            },
        },
    },
    # --- Add New Calendar Tools Here ---
        {
        "type": "function",
        "function": {
            "name": "get_user_documentation_content",
            "description": "Retrieves the content of a specific user documentation file. Use this to answer questions about how the assistant works or what features it has, based on the official documentation.\nAvailable files: {available_doc_files}", # Placeholder added
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The exact filename of the documentation file to retrieve (e.g., 'USER_GUIDE.md'). Must end in .md or .txt.",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_events",
            "description": "Search for calendar events based on a query and optional date range. Returns a list of matching events with their details and unique IDs (UIDs). Use this *first* when a user asks to modify or delete an event, to identify the correct event UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "Keywords from the user's request describing the event (e.g., 'dentist appointment', 'team meeting').",
                    },
                    "start_date_str": {
                        "type": "string",
                        "description": "Optional. The start date for the search range (ISO 8601 format, e.g., '2025-05-20'). Defaults to today if omitted.",
                    },
                    "end_date_str": {
                        "type": "string",
                        "description": "Optional. The end date for the search range (ISO 8601 format, e.g., '2025-05-22'). Defaults to start_date + 2 days if omitted.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional. Maximum number of events to return (default: 5).",
                        "default": 5,
                    }
                },
                "required": ["query_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_calendar_event",
            "description": "Modifies an existing calendar event identified by its UID. Requires the UID obtained from search_calendar_events. Only provide parameters for the fields that need changing. Does *not* currently support modifying recurring events reliably (may affect only the specified instance).",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "The unique ID (UID) of the event to modify, obtained from search_calendar_events.",
                    },
                    "calendar_url": {
                        "type": "string",
                        "description": "The URL of the calendar containing the event, obtained from search_calendar_events.",
                    },
                    "new_summary": {
                        "type": "string",
                        "description": "Optional. The new title/summary for the event.",
                    },
                    "new_start_time": {
                        "type": "string",
                        "description": "Optional. The new start date or datetime (ISO 8601 format with timezone for timed events, e.g., '2025-05-20T11:00:00+02:00' or '2025-05-21').",
                    },
                    "new_end_time": {
                        "type": "string",
                        "description": "Optional. The new end date or datetime (ISO 8601 format with timezone for timed events, e.g., '2025-05-20T11:30:00+02:00' or '2025-05-22').",
                    },
                    "new_description": {
                        "type": "string",
                        "description": "Optional. The new detailed description for the event.",
                    },
                    "new_all_day": {
                        "type": "boolean",
                        "description": "Optional. Set to true if the event should become an all-day event, false if it should become timed. Requires appropriate new_start/end_time.",
                    },
                },
                "required": ["uid", "calendar_url"], # Require UID and URL, at least one 'new_' field should be provided logically
            },
            "requires_confirmation": True,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Deletes a specific calendar event identified by its UID. Requires the UID obtained from search_calendar_events. Does *not* currently support deleting recurring events reliably (may affect only the specified instance).",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "The unique ID (UID) of the event to delete, obtained from search_calendar_events.",
                    },
                     "calendar_url": {
                        "type": "string",
                        "description": "The URL of the calendar containing the event, obtained from search_calendar_events.",
                    },
                },
                "required": ["uid", "calendar_url"],
            },
            "requires_confirmation": True,
        },
    },
]


# --- Tool Provider Implementations ---


import inspect # Needed for signature inspection
import uuid # For confirmation IDs
import telegramify_markdown # For escaping confirmation prompts

class LocalToolsProvider:
    """Provides and executes locally defined Python functions as tools."""

    def __init__(
        self,
        definitions: List[Dict[str, Any]],
        implementations: Dict[str, Callable],
        embedding_generator: Optional[EmbeddingGenerator] = None, # Accept generator
        calendar_config: Optional[Dict[str, Any]] = None, # Accept calendar config
    ):
        self._definitions = definitions
        self._implementations = implementations
        self._embedding_generator = embedding_generator # Store generator
        self._calendar_config = calendar_config # Store calendar config
        logger.info(
            f"LocalToolsProvider initialized with {len(self._definitions)} tools: {list(self._implementations.keys())}"
        )
        if self._embedding_generator:
             logger.info(f"LocalToolsProvider configured with embedding generator: {type(self._embedding_generator).__name__}")

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return self._definitions

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        if name not in self._implementations:
            raise ToolNotFoundError(f"Local tool '{name}' not found.")

        callable_func = self._implementations[name]
        logger.info(f"Executing local tool '{name}' with args: {arguments}")
        try:
            # Prepare arguments, potentially injecting context or generator
            call_args = arguments.copy()
            sig = inspect.signature(callable_func)
            needs_exec_context = False
            needs_db_context = False
            needs_embedding_generator = False

            for param_name, param in sig.parameters.items():
                # Check if the function expects the full context object
                if param.annotation is ToolExecutionContext:
                    needs_exec_context = True
                if param.annotation is DatabaseContext and param_name == "db_context":
                    needs_db_context = True
                elif param.annotation is EmbeddingGenerator and param_name == "embedding_generator":
                    needs_embedding_generator = True

            # Inject dependencies based on flags
            # Always check for exec_context first
            if needs_exec_context:
                call_args['exec_context'] = context

            # Check for and inject other specific dependencies.
            if needs_db_context:
                 # Only inject if not already covered by exec_context (though harmless if redundant)
                 if 'db_context' not in call_args:
                     call_args['db_context'] = context.db_context
            if needs_embedding_generator:
                if self._embedding_generator:
                    call_args['embedding_generator'] = self._embedding_generator
                else:
                    logger.error(f"Tool '{name}' requires an embedding generator, but none was provided to LocalToolsProvider.")
                    return f"Error: Tool '{name}' cannot be executed because the embedding generator is missing."

            # Clean up arguments not expected by the function signature
            # (Ensures we don't pass exec_context if only db_context was needed, etc.)
            expected_args = set(sig.parameters.keys())
            args_to_remove = set(call_args.keys()) - expected_args
            for arg_name in args_to_remove:
                # Only remove if it wasn't part of the original LLM arguments
                if arg_name not in arguments:
                    del call_args[arg_name]


            # Execute the function with prepared arguments
            result = await callable_func(**call_args)

            # Ensure result is a string
            if result is None:  # Handle None case explicitly
                result_str = "Tool executed successfully (returned None)."
                logger.info(f"Local tool '{name}' returned None.")
            elif not isinstance(result, str):
                result_str = str(result)
                logger.warning(
                    f"Tool '{name}' returned non-string result ({type(result)}), converted to: '{result_str[:100]}...'"
                )
            else:
                result_str = result

            if "Error:" not in result_str:
                logger.info(f"Local tool '{name}' executed successfully.")
            else:
                logger.warning(f"Local tool '{name}' reported an error: {result_str}")
            return result_str
        except Exception as e:
            logger.error(f"Error executing local tool '{name}': {e}", exc_info=True)
            # Re-raise or return formatted error string? Returning error string for now.
            return f"Error executing tool '{name}': {e}"

    async def close(self):
        """Local provider has no resources to clean up."""
        logger.debug("Closing LocalToolsProvider (no-op).")
        pass # Explicitly pass for clarity


class CompositeToolsProvider:
    """Combines multiple tool providers into a single interface."""

    def __init__(self, providers: List[ToolsProvider]):
        self._providers = providers
        self._providers = providers
        self._tool_definitions: Optional[List[Dict[str, Any]]] = None
        self._tool_map: Dict[str, ToolsProvider] = {} # Map tool name to provider
        self._validated = False  # Flag to track if validation has run
        logger.info(
            f"CompositeToolsProvider initialized with {len(providers)} providers. Validation and mapping will occur on first use."
        )

    # Removed synchronous _validate_providers method

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        # Cache definitions after first async fetch and validation
        if self._tool_definitions is None:
            all_definitions = []
            all_names = set()
            self._tool_map = {} # Reset map on re-fetch
            logger.info(
                "Fetching tool definitions and building tool map from providers for the first time..."
            )
            for i, provider in enumerate(self._providers):
                try:
                    # Fetch definitions asynchronously
                    definitions = await provider.get_tool_definitions()
                    all_definitions.extend(definitions)

                    # Perform validation as definitions are fetched (only on first run)
                    if not self._validated:
                        for tool_def in definitions:
                            # Ensure the definition is a dictionary before accessing keys
                            if not isinstance(tool_def, dict):
                                logger.warning(
                                    f"Provider {i} ({type(provider).__name__}) returned non-dict item in definitions: {tool_def}"
                                )
                                continue
                            function_def = tool_def.get("function", {})
                            if not isinstance(function_def, dict):
                                logger.warning(
                                    f"Provider {i} ({type(provider).__name__}) returned non-dict 'function' field: {function_def}"
                                )
                                continue
                            name = function_def.get("name")
                            if name:
                                if name in all_names:
                                    # Raise error immediately if duplicate found during fetch
                                    error_msg = f"Duplicate tool name '{name}' found in provider {i} ({type(provider).__name__}). Tool names must be unique across all providers."
                                    logger.error(error_msg)
                                    raise ValueError(error_msg)
                                all_names.add(name)
                                self._tool_map[name] = provider # Map name to provider instance

                except Exception as e:
                    logger.error(
                        f"Failed to get or validate tool definitions from provider {type(provider).__name__}: {e}",
                        exc_info=True,
                    )
                    # If fetching/validation fails for one provider, re-raise the error
                    # to prevent using potentially incomplete/invalid toolset.
                    raise  # Re-raise the exception

            # If loop completes without validation error
            if not self._validated:
                logger.info(
                    f"Tool name collision check passed for {len(all_names)} unique tools."
                )
                self._validated = True  # Mark validation as complete

            self._tool_definitions = all_definitions
            logger.info(
                f"Fetched and cached {len(self._tool_definitions)} tool definitions from {len(self._providers)} providers. Mapped {len(self._tool_map)} tools."
            )
        return self._tool_definitions

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        # Ensure definitions are loaded and map is built
        if not self._validated:
            await self.get_tool_definitions() # This also builds the map

        logger.debug(f"Composite provider attempting to execute tool '{name}'...")
        provider = self._tool_map.get(name)

        if not provider:
            logger.error(f"Tool '{name}' not found in any registered provider via tool map.")
            raise ToolNotFoundError(f"Tool '{name}' not found in any provider.")

        try:
            # Attempt to execute with the mapped provider
            result = await provider.execute_tool(name, arguments, context)
            logger.debug(
                f"Tool '{name}' executed successfully by mapped provider {type(provider).__name__}."
            )
            return result # Return immediately on success
        except ToolNotFoundError:
            # This shouldn't happen if the map is correct, but handle defensively
            logger.error(
                f"Tool '{name}' mapped to {type(provider).__name__}, but provider raised ToolNotFoundError. This indicates an internal inconsistency."
            )
            raise # Re-raise the unexpected ToolNotFoundError
        except Exception as e:
            # Handle unexpected errors during execution attempt
            logger.error(
                f"Error executing tool '{name}' with mapped provider {type(provider).__name__}: {e}",
                exc_info=True,
            )
            # Return an error string immediately, as something went wrong beyond just not finding the tool
            return f"Error during execution attempt with {type(provider).__name__}: {e}"

        # # If loop completes, no provider handled the tool -- This part is replaced by map lookup
        # logger.error(f"Tool '{name}' not found in any registered provider.")
        # raise ToolNotFoundError(f"Tool '{name}' not found in any provider.")

    async def close(self):
        """Closes all contained providers concurrently."""
        logger.info(f"Closing CompositeToolsProvider and its {len(self._providers)} providers...")
        close_tasks = [provider.close() for provider in self._providers]
        results = await asyncio.gather(*close_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            provider_type = type(self._providers[i]).__name__
            if isinstance(result, Exception):
                logger.error(f"Error closing provider {provider_type}: {result}", exc_info=result)
            else:
                logger.debug(f"Provider {provider_type} closed successfully.")
        logger.info("CompositeToolsProvider finished closing providers.")


# --- Confirming Tools Provider Wrapper ---

class ConfirmingToolsProvider(ToolsProvider):
    """
    A wrapper provider that intercepts calls to specific tools,
    requests user confirmation via a callback, and then either executes
    the tool with the wrapped provider or returns a cancellation message.
    """
    DEFAULT_CONFIRMATION_TIMEOUT = 3600.0 # 1 hour

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        confirmation_timeout: float = DEFAULT_CONFIRMATION_TIMEOUT,
        calendar_config: Optional[Dict[str, Any]] = None, # Needed for fetching details
    ):
        self.wrapped_provider = wrapped_provider
        self.confirmation_timeout = confirmation_timeout
        self.calendar_config = calendar_config # Store calendar config
        self._tool_definitions: Optional[List[Dict[str, Any]]] = None
        self._tools_requiring_confirmation: Set[str] = set()
        logger.info(
            f"ConfirmingToolsProvider initialized, wrapping {type(wrapped_provider).__name__}. Timeout: {confirmation_timeout}s."
        )

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        # Fetch definitions from the wrapped provider and identify tools needing confirmation
        if self._tool_definitions is None:
            definitions = await self.wrapped_provider.get_tool_definitions()
            self._tools_requiring_confirmation = set()
            for tool_def in definitions:
                func_def = tool_def.get("function", {})
                if func_def.get("requires_confirmation"):
                    name = func_def.get("name")
                    if name:
                        self._tools_requiring_confirmation.add(name)
            self._tool_definitions = definitions
            logger.info(
                f"ConfirmingToolsProvider identified {len(self._tools_requiring_confirmation)} tools requiring confirmation: {self._tools_requiring_confirmation}"
            )
        return self._tool_definitions

    async def _get_event_details_for_confirmation(self, tool_name: str, arguments: Dict[str, Any], context: ToolExecutionContext) -> Optional[Dict[str, Any]]:
        """
        Fetches event details if the tool is calendar-related and requires it.
        Requires the execution context to get the timezone.
        """
        if tool_name not in ["modify_calendar_event", "delete_calendar_event"]:
            return None # Only fetch for relevant tools

        uid = arguments.get("uid")
        calendar_url = arguments.get("calendar_url")
        if not uid or not calendar_url:
            logger.warning(f"Cannot fetch event details for {tool_name}: Missing uid or calendar_url in arguments.")
            return None

        if not self.calendar_config:
            logger.warning(f"Cannot fetch event details for {tool_name}: Calendar config not provided to ConfirmingToolsProvider.")
            return None

        caldav_config = self.calendar_config.get("caldav")
        if not caldav_config:
            logger.warning(f"Cannot fetch event details for {tool_name}: CalDAV config missing.")
            return None

        username = caldav_config.get("username")
        password = caldav_config.get("password")
        if not username or not password:
            logger.warning(f"Cannot fetch event details for {tool_name}: CalDAV user/pass missing.")
            return None

        try:
            loop = asyncio.get_running_loop()
            # Pass the timezone string from the context to the sync helper (now in calendar_integration)
            details = await loop.run_in_executor(
                None,
                calendar_integration._fetch_event_details_sync, # Call the moved function
                username, password, calendar_url, uid, context.timezone_str # Pass timezone
            )
            return details
        except Exception as e:
            logger.error(f"Error fetching event details for confirmation: {e}", exc_info=True)
            return None # Return None on error, confirmation prompt will show basic info

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        # Ensure definitions are loaded to know which tools need confirmation
        if self._tool_definitions is None:
            await self.get_tool_definitions()

        if name in self._tools_requiring_confirmation:
            logger.info(f"Tool '{name}' requires user confirmation.")
            if not context.request_confirmation_callback:
                logger.error(f"Cannot request confirmation for tool '{name}': No callback provided in ToolExecutionContext.")
                return f"Error: Tool '{name}' requires confirmation, but the system is not configured to ask for it."

            # 1. Fetch event details if needed for rendering, passing the context
            event_details = await self._get_event_details_for_confirmation(name, arguments, context)

            # 2. Get the renderer
            renderer = TOOL_CONFIRMATION_RENDERERS.get(name)
            if not renderer:
                logger.error(f"No confirmation renderer found for tool '{name}'. Using default prompt.")
                # Fallback prompt - escape user-provided args carefully
                args_str = json.dumps(arguments, indent=2, default=str)
                confirmation_prompt = f"Please confirm executing tool `{telegramify_markdown.escape_markdown(name)}` with arguments:\n```json\n{telegramify_markdown.escape_markdown(args_str)}\n```"
            else:
                # Pass timezone_str from context to the renderer
                confirmation_prompt = renderer(arguments, event_details, context.timezone_str)

            # 3. Request confirmation via callback (which handles Future creation/waiting)
            try:
                logger.debug(f"Requesting confirmation for tool '{name}' via callback.")
                # The callback is expected to handle the timeout internally via asyncio.wait_for
                user_confirmed = await context.request_confirmation_callback(
                    prompt_text=confirmation_prompt,
                    tool_name=name, # Pass tool name for context if needed by callback
                    tool_args=arguments, # Pass args for context if needed by callback
                    # Timeout is handled by the callback implementation now
                )

                if user_confirmed:
                    logger.info(f"User confirmed execution for tool '{name}'. Proceeding.")
                    # Execute the tool using the wrapped provider
                    return await self.wrapped_provider.execute_tool(name, arguments, context)
                else:
                    logger.info(f"User cancelled execution for tool '{name}'.")
                    return f"OK. Action cancelled by user for tool '{name}'."

            except asyncio.TimeoutError:
                 logger.warning(f"Confirmation request for tool '{name}' timed out.")
                 return f"Action cancelled: Confirmation request for tool '{name}' timed out."
            except Exception as conf_err:
                 logger.error(f"Error during confirmation request for tool '{name}': {conf_err}", exc_info=True)
                 return f"Error during confirmation process for tool '{name}': {conf_err}"
        else:
            # Tool does not require confirmation, execute directly
            logger.debug(f"Tool '{name}' does not require confirmation. Executing directly.")
            return await self.wrapped_provider.execute_tool(name, arguments, context)

    async def close(self):
        """Closes the wrapped provider."""
        logger.info(f"Closing ConfirmingToolsProvider by closing wrapped provider {type(self.wrapped_provider).__name__}...")
        await self.wrapped_provider.close()
        logger.info("ConfirmingToolsProvider finished closing wrapped provider.")
