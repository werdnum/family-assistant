"""
Module defining interfaces and implementations for providing and executing tools (local and MCP).
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Protocol, Callable

from dateutil import rrule
from dateutil.parser import isoparse
from mcp import ClientSession  # Required for MCPToolsProvider type hint
from telegram.ext import Application  # Required for ToolExecutionContext

# Import storage functions needed by local tools
from family_assistant import storage
from family_assistant.storage.context import DatabaseContext # Import DatabaseContext

logger = logging.getLogger(__name__)

# --- Tool Execution Context ---


@dataclass
@dataclass
class ToolExecutionContext:
    """Context passed to tool execution functions."""

    chat_id: int
    db_context: DatabaseContext # Add database context
    application: Optional[Application] = (
        None  # Still needed for schedule_future_callback
    )


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


# --- Local Tool Implementations (Moved from processing.py) ---
# Refactored to accept context: ToolExecutionContext


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
                f"Initial schedule time '{initial_schedule_time}' lacks timezone. Assuming UTC."
            )
            initial_dt = initial_dt.replace(tzinfo=timezone.utc)

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
            db_context=exec_context.db_context, # Pass db_context
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
    db_context = exec_context.db_context # Get db_context

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
                f"Callback time '{callback_time}' lacks timezone. Assuming UTC."
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)

        # Ensure it's in the future (optional, but good practice)
        if scheduled_dt <= datetime.now(timezone.utc):
            raise ValueError("Callback time must be in the future.")

        task_id = f"llm_callback_{uuid.uuid4()}"
        payload = {
            "chat_id": chat_id,
            "callback_context": context,
            "_application_ref": application,  # Pass the global application reference
        }

        # TODO: Need access to the new_task_event from main.py to notify worker.
        # This refactor doesn't address passing the event down yet.
        await storage.enqueue_task(
            db_context=db_context, # Pass db_context
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


# --- Local Tool Definitions and Mappings (Moved from processing.py) ---

# Map tool names to their actual implementation functions
# Note: add_or_update_note comes from storage
AVAILABLE_FUNCTIONS: Dict[str, Callable] = {
    "add_or_update_note": storage.add_or_update_note,
    "schedule_future_callback": schedule_future_callback_tool,
    "schedule_recurring_task": schedule_recurring_task_tool,
}

# Define local tools in the format LiteLLM expects (OpenAI format)
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
]


# --- Tool Provider Implementations ---


class LocalToolsProvider:
    """Provides and executes locally defined Python functions as tools."""

    def __init__(
        self, definitions: List[Dict[str, Any]], implementations: Dict[str, Callable]
    ):
        self._definitions = definitions
        self._implementations = implementations
        logger.info(
            f"LocalToolsProvider initialized with {len(self._definitions)} tools: {list(self._implementations.keys())}"
        )

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
            # Extract db_context from the ToolExecutionContext
            db_context = context.db_context

            # Pass the ToolExecutionContext or db_context as needed
            if name in ["schedule_future_callback", "schedule_recurring_task"]:
                # These tools expect the full ToolExecutionContext
                result = await callable_func(context, **arguments)
            elif name == "add_or_update_note":
                # Storage function now needs db_context
                result = await callable_func(db_context=db_context, **arguments)
            else:
                # Fallback for other potential local tools - assume they don't need context
                logger.warning(
                    f"Executing local tool '{name}' without db_context (assuming it's not needed)."
                )
                result = await callable_func(**arguments) # Call without context

            # Ensure result is a string
            if result is None: # Handle None case explicitly
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
        self._definitions = mcp_definitions
        self._sessions = mcp_sessions
        self._tool_map = tool_name_to_server_id
        logger.info(
            f"MCPToolsProvider initialized with {len(self._definitions)} tools from {len(self._sessions)} sessions."
        )

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
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
