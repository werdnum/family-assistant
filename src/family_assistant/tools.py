"""
Module defining interfaces and implementations for providing and executing tools (local and MCP).
"""

import asyncio
import json
import logging
import uuid
import inspect
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone, date, time # Added date, time
from typing import List, Dict, Any, Optional, Protocol, Callable
from zoneinfo import ZoneInfo

import caldav
import vobject

from dateutil import rrule
from dateutil.parser import isoparse
from mcp import ClientSession
from telegram.ext import Application
from sqlalchemy.sql import text

# Import storage functions needed by local tools
from family_assistant import storage
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext
from family_assistant.storage.vector_search import VectorSearchQuery, query_vector_store # Import vector search
from family_assistant.embeddings import EmbeddingGenerator # Import embedding generator type
from family_assistant.storage.message_history import get_recent_history # Import history function
from datetime import timedelta # Import timedelta


logger = logging.getLogger(__name__)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService

@dataclass
class ToolExecutionContext:
    """Context passed to tool execution functions."""

    chat_id: int
    db_context: DatabaseContext
    calendar_config: Dict[str, Any] # Add calendar config
    application: Optional[Application] = None
    processing_service: Optional["ProcessingService"] = None # Add processing service
    # Add other context elements as needed, e.g., timezone_str
    timezone_str: str = "UTC" # Default, should be overridden


# --- Custom Exception ---


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""

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


# --- Local Tool Implementations ---
# Refactored to accept context: ToolExecutionContext

async def add_calendar_event_tool(
    exec_context: ToolExecutionContext,
    summary: str,
    start_time: str,
    end_time: str,
    description: Optional[str] = None,
    all_day: bool = False,
) -> str:
    """
    Adds an event to the first configured CalDAV calendar.
    """
    logger.info(f"Executing add_calendar_event_tool: {summary}")
    calendar_config = exec_context.calendar_config
    caldav_config = calendar_config.get("caldav")

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot add calendar event."

    username = caldav_config.get("username")
    password = caldav_config.get("password")
    calendar_urls = caldav_config.get("calendar_urls", [])

    if not username or not password or not calendar_urls:
        return "Error: CalDAV configuration is incomplete (missing user, pass, or URL). Cannot add event."

    target_calendar_url = calendar_urls[0] # Use the first configured URL
    logger.info(f"Targeting CalDAV calendar: {target_calendar_url}")

    try:
        # Parse start and end times
        if all_day:
            # For all-day events, parse as date objects
            dtstart = isoparse(start_time).date()
            dtend = isoparse(end_time).date()
            # Basic validation: end date must be after start date for all-day
            if dtend <= dtstart:
                 raise ValueError("End date must be after start date for all-day events.")
        else:
            # For timed events, parse as datetime objects, require timezone
            dtstart = isoparse(start_time)
            dtend = isoparse(end_time)
            if dtstart.tzinfo is None or dtend.tzinfo is None:
                raise ValueError("Start and end times must include timezone information (e.g., +02:00 or Z).")
            # Basic validation: end time must be after start time
            if dtend <= dtstart:
                raise ValueError("End time must be after start time for timed events.")

        # Create VEVENT component using vobject
        cal = vobject.iCalendar()
        cal.add('vevent')
        vevent = cal.vevent
        vevent.add('uid').value = str(uuid.uuid4())
        vevent.add('summary').value = summary
        vevent.add('dtstart').value = dtstart # vobject handles date vs datetime
        vevent.add('dtend').value = dtend     # vobject handles date vs datetime
        vevent.add('dtstamp').value = datetime.now(timezone.utc)
        if description:
            vevent.add('description').value = description

        event_data = cal.serialize()
        logger.debug(f"Generated VEVENT data:\n{event_data}")

        # Connect to CalDAV server and save event (synchronous, run in executor)
        def save_event_sync():
            logger.debug(f"Connecting to CalDAV: {target_calendar_url}")
            # Need to create client and get calendar object within the sync function
            with caldav.DAVClient(
                url=target_calendar_url, username=username, password=password
            ) as client:
                # Get the specific calendar object
                # This assumes target_calendar_url is the *direct* URL to the calendar collection
                target_calendar = client.calendar(url=target_calendar_url)
                if not target_calendar:
                     # This error handling might be tricky inside the sync function
                     # Let's rely on exceptions for now.
                     raise ConnectionError(f"Failed to obtain calendar object for URL: {target_calendar_url}")

                logger.info(f"Saving event to calendar: {target_calendar.url}")
                # Use the save_event method which takes the VCALENDAR string
                new_event = target_calendar.save_event(event_data)
                logger.info(f"Event saved successfully. URL: {getattr(new_event, 'url', 'N/A')}")
                return f"OK. Event '{summary}' added to the calendar."

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, save_event_sync)
            return result
        except (caldav.lib.error.DAVError, ConnectionError, Exception) as sync_err:
            logger.error(f"Error during synchronous CalDAV save operation: {sync_err}", exc_info=True)
            # Provide a more specific error if possible
            if "authentication" in str(sync_err).lower():
                return "Error: Failed to add event due to CalDAV authentication failure."
            elif "not found" in str(sync_err).lower():
                 return f"Error: Failed to add event. Calendar not found at URL: {target_calendar_url}"
            else:
                return f"Error: Failed to add event to CalDAV calendar. {sync_err}"

    except ValueError as ve:
        logger.error(f"Invalid arguments for adding calendar event: {ve}")
        return f"Error: Invalid arguments provided. {ve}"
    except Exception as e:
        logger.error(f"Unexpected error adding calendar event: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while adding the event. {e}"


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
        task_type: The type of the task (e.g., 'send_daily_brief', 'check_reminders').
        initial_schedule_time: ISO 8601 datetime string for the *first* run.
        recurrence_rule: RRULE string specifying the recurrence (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0').
        payload: JSON object containing data needed by the task handler.
        max_retries: Maximum number of retries for each instance (default 3).
        description: A short, URL-safe description to include in the task ID (e.g., 'daily_brief').
    """
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
                f"Initial schedule time '{initial_schedule_time}' lacks timezone. Assuming %s.",
                exec_context.timezone,
            )
            initial_dt = initial_dt.replace(tzinfo=ZoneInfo(exec_context.timezone))

        # Ensure it's in the future (optional, but good practice)
        if initial_dt <= datetime.now(timezone.utc):
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
            db_context=exec_context.db_context,  # Pass db_context
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
                f"Callback time '{callback_time}' lacks timezone. Assuming %s.", exec_context.timezone
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=ZoneInfo(exec_context.timezone))

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


# --- Local Tool Definitions and Mappings (Moved from processing.py) ---

# Map tool names to their actual implementation functions
# Note: add_or_update_note comes from storage
AVAILABLE_FUNCTIONS: Dict[str, Callable] = {
    "add_or_update_note": storage.add_or_update_note,
    "schedule_future_callback": schedule_future_callback_tool,
    "schedule_recurring_task": schedule_recurring_task_tool,
    "search_documents": search_documents_tool,
    "get_full_document_content": get_full_document_content_tool,
    "add_calendar_event": add_calendar_event_tool,
    "get_message_history": get_message_history_tool, # Add the new history tool
}

# Define local tools in the format LiteLLM expects (OpenAI format)
# TODO: Dynamically fetch valid source_types and embedding_types for descriptions?
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
                    # chat_id is removed from parameters, it will be inferred from the current context later
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
]


# --- Tool Provider Implementations ---


import inspect # Needed for signature inspection

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


class MCPToolsProvider:
    """Provides and executes tools hosted on MCP servers."""

    def __init__(
        self,
        mcp_definitions: List[Dict[str, Any]],
        mcp_sessions: Dict[str, ClientSession],
        tool_name_to_server_id: Dict[str, str],
    ):
        # Sanitize definitions before storing them
        sanitized_definitions = self._sanitize_mcp_definitions(mcp_definitions)
        self._definitions = sanitized_definitions
        self._sessions = mcp_sessions
        self._tool_map = tool_name_to_server_id
        logger.info(
            f"MCPToolsProvider initialized with {len(self._definitions)} sanitized tools from {len(self._sessions)} sessions."
        )

    def _sanitize_mcp_definitions(self, definitions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Removes unsupported 'format' fields from string parameters in tool definitions.
        Google's API only supports 'enum' and 'date-time' for string formats.
        """
        sanitized = []
        for tool_def in definitions:
            try:
                # Deep copy to avoid modifying original dicts if they are reused elsewhere
                # Though in this context, it might not be strictly necessary
                # sanitized_tool_def = copy.deepcopy(tool_def) # Consider adding import copy
                sanitized_tool_def = json.loads(json.dumps(tool_def)) # Simple deep copy via JSON

                func_def = sanitized_tool_def.get("function", {})
                params = func_def.get("parameters", {})
                properties = params.get("properties", {})

                props_to_delete_format = []

                for param_name, param_details in properties.items():
                    if isinstance(param_details, dict):
                        param_type = param_details.get("type")
                        param_format = param_details.get("format")

                        if param_type == "string" and param_format and param_format not in ["enum", "date-time"]:
                            logger.warning(
                                f"Sanitizing tool '{func_def.get('name', 'UNKNOWN')}': Removing unsupported format '{param_format}' from string parameter '{param_name}'."
                            )
                            # Don't modify while iterating, mark for deletion
                            props_to_delete_format.append(param_name)

                # Perform deletion after iteration
                for param_name in props_to_delete_format:
                    if param_name in properties and isinstance(properties[param_name], dict):
                         del properties[param_name]['format']

                sanitized.append(sanitized_tool_def)
            except Exception as e:
                logger.error(f"Error sanitizing tool definition: {tool_def}. Error: {e}", exc_info=True)
                # Decide whether to skip the tool or add the original unsanitized one
                sanitized.append(tool_def) # Add original if sanitization fails

        return sanitized

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        # Definitions are already sanitized during init
        return self._definitions

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        server_id = self._tool_map.get(name)
        if not server_id:
            raise ToolNotFoundError(f"MCP tool '{name}' not found in tool map.")

        session = self._sessions.get(server_id)
        if not session:
            # This case should ideally be prevented by ensuring sessions are active,
            # but handle defensively.
            logger.error(
                f"Session for server '{server_id}' (tool '{name}') not found or inactive."
            )
            raise ToolNotFoundError(f"Session for MCP tool '{name}' is unavailable.")

        logger.info(
            f"Executing MCP tool '{name}' on server '{server_id}' with args: {arguments}"
        )
        try:
            mcp_result = await session.call_tool(name=name, arguments=arguments)

            # Process MCP result content
            response_parts = []
            if mcp_result.content:
                for content_item in mcp_result.content:
                    if hasattr(content_item, "text") and content_item.text:
                        response_parts.append(content_item.text)
                    # Handle other content types if needed (e.g., image, resource)

            result_str = (
                "\n".join(response_parts)
                if response_parts
                else "Tool executed successfully."
            )

            if mcp_result.isError:
                logger.error(
                    f"MCP tool '{name}' on server '{server_id}' returned an error: {result_str}"
                )
                return f"Error executing tool '{name}': {result_str}"  # Prepend error indication
            else:
                logger.info(
                    f"MCP tool '{name}' on server '{server_id}' executed successfully."
                )
                return result_str
        except Exception as e:
            logger.error(
                f"Error calling MCP tool '{name}' on server '{server_id}': {e}",
                exc_info=True,
            )
            return f"Error calling MCP tool '{name}': {e}"


class CompositeToolsProvider:
    """Combines multiple tool providers into a single interface."""

    def __init__(self, providers: List[ToolsProvider]):
        self._providers = providers
        self._tool_definitions: Optional[List[Dict[str, Any]]] = None
        self._validated = False  # Flag to track if validation has run
        logger.info(
            f"CompositeToolsProvider initialized with {len(providers)} providers. Validation will occur on first use."
        )

    # Removed synchronous _validate_providers method

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        # Cache definitions after first async fetch and validation
        if self._tool_definitions is None:
            all_definitions = []
            all_names = set()
            logger.info(
                "Fetching tool definitions from providers for the first time..."
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
                                    raise ValueError(
                                        f"Duplicate tool name '{name}' found in provider {i} ({type(provider).__name__}). Tool names must be unique across all providers."
                                    )
                                all_names.add(name)

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
                f"Fetched and cached {len(self._tool_definitions)} tool definitions from providers."
            )
        return self._tool_definitions

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        logger.debug(f"Composite provider attempting to execute tool '{name}'...")
        for provider in self._providers:
            try:
                # Attempt to execute with the current provider
                result = await provider.execute_tool(name, arguments, context)
                logger.debug(
                    f"Tool '{name}' executed successfully by {type(provider).__name__}."
                )
                return result  # Return immediately on success
            except ToolNotFoundError:
                logger.debug(
                    f"Tool '{name}' not found in provider {type(provider).__name__}. Trying next."
                )
                continue  # Try the next provider
            except Exception as e:
                # Handle unexpected errors during execution attempt
                logger.error(
                    f"Error executing tool '{name}' with provider {type(provider).__name__}: {e}",
                    exc_info=True,
                )
                # Return an error string immediately, as something went wrong beyond just not finding the tool
                return f"Error during execution attempt with {type(provider).__name__}: {e}"

        # If loop completes, no provider handled the tool
        logger.error(f"Tool '{name}' not found in any registered provider.")
        raise ToolNotFoundError(f"Tool '{name}' not found in any provider.")


# Removed set_application_instance helper
