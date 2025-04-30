"""
Task worker implementation for background processing.
"""

import asyncio
import logging
import random
import uuid
import zoneinfo # Add this import
from dateutil import rrule
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Callable, Awaitable

# Use absolute imports based on the package structure
from family_assistant import storage  # Import for task queue operations
from family_assistant.processing import ProcessingService  # Import the service
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.indexing.email_indexer import handle_index_email # Import email indexer
# Import the new document indexer CLASS
from family_assistant.indexing.document_indexer import DocumentIndexer
# Import functools for partial application
import functools
# Import Application for type hinting
from telegram.ext import Application

# Import tool definitions and context from the tools module
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
    ToolExecutionContext, # Import the context class
)
from telegramify_markdown import markdownify
from telegram.helpers import escape_markdown

# Import LLM interface for type hinting if needed elsewhere
# from family_assistant.llm import LLMInterface

logger = logging.getLogger(__name__)

# --- Constants ---
TASK_POLLING_INTERVAL = 5  # Seconds to wait between polling for tasks

# --- Events for coordination (can remain module-level) ---
shutdown_event = asyncio.Event()
new_task_event = asyncio.Event()  # Event to notify worker of immediate tasks

# --- Task Handler Functions (remain module-level for now) ---
# These functions will be registered with the TaskWorker instance.

# Example Task Handler (no external dependencies)
async def handle_log_message(db_context: DatabaseContext, payload: Any):
    """Simple task handler that logs the received payload."""
    logger.info(
        f"[Task Worker] Handling log_message task. Payload: {payload}"
    )  # db_context is available if needed
    # Simulate some work
    await asyncio.sleep(1)
    # In a real handler, you might interact with APIs, DB, etc.
    # If this function raises an exception, the task will be marked 'failed'.


# Note: Registration now happens in __main__.py using worker instance


# --- Helper Function (remains module-level) ---
def format_llm_response_for_telegram(response_text: str) -> str:
    """Converts LLM Markdown to Telegram MarkdownV2, with fallback."""
    try:
        # Attempt conversion
        converted = markdownify(response_text)
        # Basic check: ensure conversion didn't result in empty/whitespace only
        if converted and not converted.isspace():
            return converted
        else:
            logger.warning(
                f"Markdown conversion resulted in empty string for: {response_text[:100]}... Using original."
            )
            # Fallback to original text, escaped, if conversion is empty
            return escape_markdown(response_text, version=2)
    except Exception as md_err:
        logger.error(
            f"Failed to convert markdown: {md_err}. Falling back to escaped text. Original: {response_text[:100]}...",
            # exc_info=True, # Optional: Add full traceback if needed
        )
        # Fallback to escaping the original text
        return escape_markdown(response_text, version=2)


async def handle_llm_callback(
    exec_context: ToolExecutionContext, # Accept execution context
    payload: Any # Payload from the task queue
):
    """
    Task handler for LLM scheduled callbacks.
    Dependencies are accessed via the ToolExecutionContext.
    """
    # Access dependencies from the execution context
    processing_service = exec_context.processing_service # TaskWorker passes its own instance
    application = exec_context.application
    db_context = exec_context.db_context
    chat_id = exec_context.chat_id # Get chat_id from context

    # Basic validation of dependencies from context
    if not processing_service:
        logger.error("ProcessingService not found in ToolExecutionContext for handle_llm_callback.")
        raise ValueError("Missing ProcessingService dependency in context.")
    if not application:
        logger.error("Application not found in ToolExecutionContext for handle_llm_callback.")
        raise ValueError("Missing Application dependency in context.")
    if not db_context:
        logger.error("DatabaseContext not found in ToolExecutionContext for handle_llm_callback.")
        raise ValueError("Missing DatabaseContext dependency in context.")
    if not chat_id: # chat_id should be set by _process_task
        logger.error("Chat ID not found in ToolExecutionContext for handle_llm_callback.")
        raise ValueError("Missing Chat ID in context.")

    # Extract necessary info from payload
    # chat_id is now from context
    callback_context = payload.get("callback_context")

    # Validate payload content
    if not callback_context:
        logger.error(f"Invalid payload for llm_callback task (missing callback_context): {payload}")
        raise ValueError("Missing required field in payload: callback_context")

    logger.info(f"Handling LLM callback for chat_id {chat_id}")
    current_time_str = datetime.now(zoneinfo.ZoneInfo(exec_context.timezone_str)).strftime("%Y-%m-%d %H:%M:%S %Z") # Use timezone from context

    try:
        # Construct the trigger message content for the LLM
        trigger_text = f"System Callback Trigger:\n\nThe time is now {current_time_str}.\nYour scheduled context was:\n---\n{callback_context}\n---"
        # Prepare message history for the service call
        # TODO: Should we retrieve actual history here? For now, just send the trigger.
        # A more robust implementation might fetch recent history for better context.
        messages_for_llm = [
            {
                "role": "system",
                "content": "You are processing a scheduled callback.",
            },  # Minimal system prompt
            {"role": "user", "content": trigger_text},  # Treat trigger as user input
        ]

        # Tool definitions are fetched within process_message now
        # all_tools = local_tools_definition + mcp_tools # Removed

        # Call the ProcessingService directly, using dependencies from context
        # Unpack all expected return values (content, tool_info, reasoning, error)
        llm_response_content, tool_call_info, _ = (
            await processing_service.process_message( # Use service from context
                db_context=db_context,
                messages=messages_for_llm,
                chat_id=chat_id,
                application=application,
            )
        )

        if llm_response_content:
            # Send the LLM's response back to the chat
            formatted_response = format_llm_response_for_telegram(
                llm_response_content
            )
            sent_message = await application.bot.send_message(
                chat_id=chat_id,
                text=formatted_response,
                parse_mode="MARKDOWN_V2",
                # Note: We don't have an original message ID to reply to here.
            )
            logger.info(f"Sent LLM response for callback to chat {chat_id}.")

            # Store the callback trigger and response in history
            try:
                # Pseudo-ID for the trigger message (timestamp based?)
                trigger_msg_id = int(
                    datetime.now(timezone.utc).timestamp() * 1000
                )
                await storage.add_message_to_history(
                    db_context=db_context,
                    chat_id=chat_id,
                    message_id=trigger_msg_id,
                    timestamp=datetime.now(timezone.utc),
                    role="system",
                    content=trigger_text,
                )
                # Use the actual sent message ID if available and makes sense, else pseudo-ID
                response_msg_id = (
                    sent_message.message_id if sent_message else trigger_msg_id + 1
                )
                await storage.add_message_to_history(
                    db_context=db_context,
                    chat_id=chat_id,
                    message_id=response_msg_id,
                    timestamp=datetime.now(timezone.utc),
                    role="assistant",
                    content=llm_response_content,
                    tool_calls_info=tool_call_info,
                )
            except Exception as db_err:
                logger.error(
                    f"Failed to store callback history for chat {chat_id}: {db_err}",
                    exc_info=True,
                )

        else:
            logger.warning(
                f"LLM did not return a response content for callback in chat {chat_id}."
            )
            # Optionally send a generic failure message to the chat
            await application.bot.send_message(
                chat_id=chat_id,
                text="Sorry, I couldn't process the scheduled callback.",
            )
            # Raise an error to mark the task as failed if no response was generated
            raise RuntimeError("LLM failed to generate response content for callback.")

    except Exception as e:
        logger.error(
            f"Failed during LLM callback processing for chat {chat_id}: {e}",
            exc_info=True,
        )
        # Raise the exception to ensure the task is marked as failed
        raise


# Note: Registration now happens in __main__.py using worker instance


# --- Task Worker Class ---

class TaskWorker:
    """Manages the task processing loop and handler registry."""

    def __init__(
        self,
        processing_service: ProcessingService,
        application: Application, # Add application dependency
        calendar_config: Dict[str, Any], # Add calendar config
        timezone_str: str, # Add timezone string
    ):
        """Initializes the TaskWorker with its dependencies."""
        self.processing_service = processing_service
        self.application = application # Store application instance
        self.calendar_config = calendar_config # Store calendar config
        self.timezone_str = timezone_str # Store timezone string
        # Initialize handlers - specific handlers are registered externally
        # Update handler signature type hint
        self.task_handlers: Dict[str, Callable[[ToolExecutionContext, Any], Awaitable[None]]] = {}
        self.worker_id = f"worker-{uuid.uuid4()}"
        logger.info(f"TaskWorker instance {self.worker_id} created.")

    def register_task_handler(
        self,
        task_type: str,
        # Update handler signature type hint
        handler: Callable[[ToolExecutionContext, Any], Awaitable[None]]
    ):
        """Register a task handler function for a specific task type."""
        self.task_handlers[task_type] = handler
        logger.info(f"Worker {self.worker_id}: Registered handler for task type: {task_type}")

    # Update return type hint
    def get_task_handlers(self) -> Dict[str, Callable[[ToolExecutionContext, Any], Awaitable[None]]]:
         """Return the current task handlers dictionary for this worker."""
         return self.task_handlers

    async def _process_task(self, db_context: DatabaseContext, task: Dict[str, Any], wake_up_event: asyncio.Event):
        """Handles the execution, completion marking, and recurrence logic for a dequeued task."""
        logger.info(
            f"Worker {self.worker_id} processing task {task['task_id']} (type: {task['task_type']})"
        )
        handler = self.task_handlers.get(task["task_type"])

        if not handler:
            # This shouldn't happen if dequeue_task respects task_types properly
            logger.error(
                f"Worker {self.worker_id} dequeued task {task['task_id']} but no handler found for type {task['task_type']}. Marking failed."
            )
            await storage.update_task_status(
                db_context=db_context,
                task_id=task["task_id"],
                status="failed",
                error=f"No handler registered for type {task['task_type']}",
            )
            return # Stop processing this task

        try:
            # --- Create Execution Context ---
            # Extract chat_id from payload if available (needed for context)
            # Default to 0 or None if not applicable for the task type?
            # For llm_callback, it should be present.
            chat_id = task.get("payload", {}).get("chat_id", 0) # Default to 0 if missing
            if not chat_id and task["task_type"] == "llm_callback":
                 logger.error(f"Task {task['task_id']} (llm_callback) missing chat_id in payload. Cannot create context.")
                 # Mark task as failed? Or raise error?
                 await storage.update_task_status(
                     db_context=db_context,
                     task_id=task["task_id"],
                     status="failed",
                     error="Missing chat_id in payload for llm_callback",
                 )
                 return # Stop processing

            exec_context = ToolExecutionContext(
                chat_id=chat_id,
                db_context=db_context,
                calendar_config=self.calendar_config,
                application=self.application,
                processing_service=self.processing_service, # Pass processing service
                timezone_str=self.timezone_str,
            )
            # --- Execute Handler with Context ---
            logger.debug(
                f"Worker {self.worker_id} executing handler for task {task['task_id']} with context."
            )
            # Pass the context and the original payload
            await handler(exec_context, task["payload"])

            # Task details for logging and recurrence
            task_id = task["task_id"]
            task_type = task["task_type"]
            payload = task["payload"] # Keep payload for recurrence
            recurrence_rule_str = task.get("recurrence_rule")
            original_task_id = task.get(
                "original_task_id", task_id
            )  # Use task_id if original is missing (first run)
            task_max_retries = task.get("max_retries", 3)

            # Mark task as done
            await storage.update_task_status(
                db_context=db_context,
                task_id=task_id,
                status="done",
            )
            logger.info(
                f"Worker {self.worker_id} completed task {task_id} (Original: {original_task_id})"
            )

            # --- Handle Recurrence ---
            if recurrence_rule_str:
                logger.info(
                    f"Task {task_id} has recurrence rule: {recurrence_rule_str}. Scheduling next instance."
                )
                try:
                    # Use the *scheduled_at* time of the completed task as the base for the next occurrence
                    last_scheduled_at = task.get("scheduled_at")
                    if not last_scheduled_at:
                        # If somehow scheduled_at is missing, use created_at as fallback
                        last_scheduled_at = task.get(
                            "created_at", datetime.now(timezone.utc)
                        )
                        logger.warning(
                            f"Task {task_id} missing scheduled_at, using created_at ({last_scheduled_at}) for recurrence base."
                        )
                    # Ensure the base time is timezone-aware for rrule
                    if last_scheduled_at.tzinfo is None:
                        last_scheduled_at = (
                            last_scheduled_at.replace(
                                tzinfo=timezone.utc
                            )
                        )
                        logger.warning(
                            f"Made recurrence base time timezone-aware (UTC): {last_scheduled_at}"
                        )

                    # Calculate the next occurrence *after* the last scheduled time
                    rule = rrule.rrulestr(
                        recurrence_rule_str,
                        dtstart=last_scheduled_at,
                    )
                    next_scheduled_dt = rule.after(
                        last_scheduled_at
                    )

                    if next_scheduled_dt:
                        # Generate a new unique task ID for the next instance
                        # Format: <original_task_id>_recur_<next_iso_timestamp>
                        next_task_id = f"{original_task_id}_recur_{next_scheduled_dt.isoformat()}"

                        logger.info(
                            f"Calculated next occurrence for {original_task_id} at {next_scheduled_dt}. New task ID: {next_task_id}"
                        )

                        # Enqueue the next task instance
                        await storage.enqueue_task(
                            db_context=db_context,
                            task_id=next_task_id,
                            task_type=task_type,
                            payload=payload,
                            scheduled_at=next_scheduled_dt,
                            max_retries_override=task_max_retries,
                            recurrence_rule=recurrence_rule_str,
                            original_task_id=original_task_id,
                            notify_event=wake_up_event,
                        )
                        logger.info(
                            f"Successfully enqueued next recurring task instance {next_task_id} for original {original_task_id}."
                        )
                    else:
                        logger.info(
                            f"No further occurrences found for recurring task {original_task_id} based on rule '{recurrence_rule_str}'."
                        )

                except Exception as recur_err:
                    logger.error(
                        f"Failed to calculate or enqueue next instance for recurring task {task_id} (Original: {original_task_id}): {recur_err}",
                        exc_info=True,
                    )
                    # Don't mark the original task as failed, just log the recurrence error.

        except Exception as handler_exc:
            await self._handle_task_failure(db_context, task, handler_exc)


    async def _handle_task_failure(self, db_context: DatabaseContext, task: Dict[str, Any], handler_exc: Exception):
        """Handles logging, retries, and marking tasks as failed."""
        current_retry = task.get("retry_count", 0)
        max_retries = task.get(
            "max_retries",
            3,  # Use DB default if missing somehow
        )
        error_str = str(handler_exc)
        logger.error(
            f"Worker {self.worker_id} failed task {task['task_id']} (Retry {current_retry}/{max_retries}) due to handler error: {error_str}",
            exc_info=True,
        )

        if current_retry < max_retries:
            # Calculate exponential backoff with jitter
            backoff_delay = (5 * (2**current_retry)) + random.uniform(0, 2)
            next_attempt_time = datetime.now(timezone.utc) + timedelta(seconds=backoff_delay)
            logger.info(
                f"Scheduling retry {current_retry + 1} for task {task['task_id']} at {next_attempt_time} (delay: {backoff_delay:.2f}s)"
            )
            try:
                await storage.reschedule_task_for_retry(
                    db_context=db_context,
                    task_id=task["task_id"],
                    next_scheduled_at=next_attempt_time,
                    new_retry_count=current_retry + 1,
                    error=error_str,
                )
            except Exception as reschedule_err:
                # If rescheduling fails, log critical error and mark as failed
                logger.critical(
                    f"CRITICAL: Failed to reschedule task {task['task_id']} for retry after handler error. Marking as failed. Error: {reschedule_err}",
                    exc_info=True,
                )
                await storage.update_task_status(
                    db_context=db_context,
                    task_id=task["task_id"],
                    status="failed",
                    error=f"Handler Error: {error_str}. Reschedule Failed: {reschedule_err}",
                )
        else:
            logger.warning(
                f"Task {task['task_id']} reached max retries ({max_retries}). Marking as failed."
            )
            await storage.update_task_status(
                db_context=db_context,
                task_id=task["task_id"],
                status="failed",
                error=error_str,
            )

    async def _wait_for_next_poll(self, wake_up_event: asyncio.Event):
        """Waits for the polling interval or a wake-up event."""
        try:
            logger.debug(
                f"Worker {self.worker_id}: No tasks found, waiting for event or timeout ({TASK_POLLING_INTERVAL}s)..."
            )

            await asyncio.wait_for(
                wake_up_event.wait(), timeout=TASK_POLLING_INTERVAL
            )
            # If wait_for completes without timeout, the event was set
            logger.debug(f"Worker {self.worker_id}: Woken up by event.")
            wake_up_event.clear()  # Reset the event for the next notification
            await asyncio.sleep(0.1) # Sometimes there is a slight delay before the task is actually visible.
        except asyncio.TimeoutError:
            # Event didn't fire, timeout reached, proceed to next polling cycle
            logger.debug(
                f"Worker {self.worker_id}: Wait timed out, continuing poll cycle."
            )
            pass  # Continue the loop normally after timeout

    async def run(self, wake_up_event: asyncio.Event):
        """Continuously polls for and processes tasks. Replaces task_worker_loop."""
        logger.info(f"Task worker {self.worker_id} run loop started.")
        # Get task types handled by *this specific instance*
        task_types_handled = list(self.task_handlers.keys())
        if not task_types_handled:
            logger.warning(f"Task worker {self.worker_id} has no registered handlers. Exiting loop.")
            return

        while not shutdown_event.is_set():
            try:
                task = None # Initialize task variable for the outer scope
                # Database context per iteration (starts a transaction)
                async with await get_db_context() as db_context:
                    logger.debug("Polling for tasks on DB context: %s", db_context.engine.url)
                    try:  # Inner try for dequeue, task processing, and waiting logic
                        task = await storage.dequeue_task(
                        db_context=db_context,
                        worker_id=self.worker_id,
                        task_types=task_types_handled,
                    )

                        if task:
                            logger.debug("Dequeued task: %s", task["task_id"])
                            await self._process_task(db_context, task, wake_up_event)
                        else:
                            logger.debug("No tasks found, waiting for next poll")
                            await self._wait_for_next_poll(wake_up_event)

                    # --- Exception handling for the inner try block (catches dequeue or helper errors) ---
                    except Exception as e:
                        logger.error(
                            f"Error during task processing or DB operation within context for worker {self.worker_id}: {e}",
                            exc_info=True,
                        )
                        # If an error occurs *within* the db_context block (e.g., during dequeue, handler execution, or waiting),
                        # the context manager will handle rollback/commit based on the exception.
                        # We might still want a delay before the next iteration's context attempt.
                        await asyncio.sleep(
                            TASK_POLLING_INTERVAL
                        )

            # --- Exception handling for the outer try block (whole loop iteration) ---
            except asyncio.CancelledError:
                logger.info(f"Task worker {self.worker_id} received cancellation signal.")
                # If a task was being processed when cancelled, it might remain locked.
                # Rely on lock expiry/manual intervention for now.
                # For simplicity, we just exit.
                break  # Exit the loop cleanly on cancellation
            except Exception as e:
                logger.error(
                    f"Task worker {self.worker_id} encountered an unexpected error outside DB context: {e}",
                    exc_info=True,
                )
                # If an error occurs outside the db_context (e.g., getting context itself), wait before retrying
                await asyncio.sleep(TASK_POLLING_INTERVAL * 2)  # Longer sleep after error

        logger.info(f"Task worker {self.worker_id} stopped.")


__all__ = ["TaskWorker", "handle_log_message", "handle_llm_callback", "handle_index_email"] # Export class and handlers
