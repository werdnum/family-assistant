"""
Task worker implementation for background processing.
"""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Callable

# Use absolute imports based on the package structure
from family_assistant import storage  # Import for task queue operations
from family_assistant.processing import (
    get_llm_response,
    TOOLS_DEFINITION as local_tools_definition,
)
from telegramify_markdown import markdownify
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

# --- Constants ---
TASK_POLLING_INTERVAL = 5  # Seconds to wait between polling for tasks

# --- Task Queue Handler Registry ---
# Maps task_type strings to async handler functions
# Handler functions should accept the task payload as an argument
# Example: async def handle_my_task(payload: Any): ...
TASK_HANDLERS: Dict[str, callable] = {}

# --- Events for coordination ---
shutdown_event = asyncio.Event()
new_task_event = asyncio.Event()  # Event to notify worker of immediate tasks

# --- Global state (references from main) ---
mcp_sessions: Dict[str, Any] = {}  # Will be set from main
mcp_tools: List[Dict[str, Any]] = []  # Will be set from main
tool_name_to_server_id: Dict[str, str] = {}  # Will be set from main


# Example Task Handler (can be moved elsewhere later)
async def handle_log_message(payload: Any):
    """Simple task handler that logs the received payload."""
    logger.info(f"[Task Worker] Handling log_message task. Payload: {payload}")
    # Simulate some work
    await asyncio.sleep(1)
    # In a real handler, you might interact with APIs, DB, etc.
    # If this function raises an exception, the task will be marked 'failed'.


# Register the example handler
TASK_HANDLERS["log_message"] = handle_log_message


# --- Helper Function ---
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


# Forward declaration of function that will be set from main.py
_generate_llm_response_for_chat = None


async def handle_llm_callback(payload: Any):
    """Task handler for LLM scheduled callbacks."""
    global _generate_llm_response_for_chat  # Get the function from main

    if not _generate_llm_response_for_chat:
        logger.error(
            "Cannot handle LLM callback: _generate_llm_response_for_chat not set"
        )
        raise RuntimeError("Missing _generate_llm_response_for_chat function reference")

    application = payload.get(
        "_application_ref"
    )  # Special field for application reference

    if not application:
        logger.error(
            "Cannot handle LLM callback: Telegram application reference not set"
        )
        raise RuntimeError("Missing application reference in payload")

    chat_id = payload.get("chat_id")
    callback_context = payload.get("callback_context")

    if not chat_id or not callback_context:
        logger.error(f"Invalid payload for llm_callback task: {payload}")
        raise ValueError(
            "Missing required fields in payload: chat_id or callback_context"
        )

    logger.info(f"Handling LLM callback for chat_id {chat_id}")
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    message_to_send = f"System Callback: The time is now {current_time_str}.\n\nYou previously scheduled a callback with the following context:\n\n---\n{callback_context}\n---"

    try:
        # Send the message *as the bot* into the chat.
        # Construct the trigger message content for the LLM
        # Using a clear prefix to indicate it's a callback
        trigger_text = f"System Callback Trigger:\n\nThe time is now {current_time_str}.\nYour scheduled context was:\n---\n{callback_context}\n---"
        trigger_content_parts = [{"type": "text", "text": trigger_text}]

        # Generate the LLM response using the refactored function
        # Use a placeholder name like "System" or "Assistant" for the user_name in the prompt
        llm_response_content, tool_call_info = await _generate_llm_response_for_chat(
            chat_id=chat_id,
            trigger_content_parts=trigger_content_parts,
            user_name="System Trigger",  # Or "Assistant"? Needs testing for optimal LLM behavior.
        )

        if llm_response_content:  # Check if content exists
            # Send the LLM's response back to the chat
            formatted_response = format_llm_response_for_telegram(
                llm_response_content
            )  # Use content string
            sent_message = await application.bot.send_message(  # Store sent message result
                chat_id=chat_id,
                text=formatted_response,  # Use formatted content string
                parse_mode="MARKDOWN_V2",
                # Note: We don't have an original message ID to reply to here.
            )
            logger.info(f"Sent LLM response for callback to chat {chat_id}.")

            # Store the callback trigger and response in history
            try:
                # Pseudo-ID for the trigger message (timestamp based?)
                trigger_msg_id = int(
                    datetime.now(timezone.utc).timestamp() * 1000
                )  # Crude pseudo-ID
                await storage.add_message_to_history(
                    chat_id=chat_id,
                    message_id=trigger_msg_id,
                    timestamp=datetime.now(timezone.utc),
                    role="system",  # Or 'user'/'assistant' depending on how trigger_message was structured
                    content=trigger_text,
                )
                # Pseudo-ID for the bot response
                response_msg_id = trigger_msg_id + 1
                await storage.add_message_to_history(
                    chat_id=chat_id,
                    message_id=response_msg_id,
                    timestamp=datetime.now(timezone.utc),
                    role="assistant",
                    content=llm_response_content,  # Store the content string
                    tool_calls_info=tool_call_info,  # Store tool info too
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


# Register the callback handler
TASK_HANDLERS["llm_callback"] = handle_llm_callback


# --- Task Queue Worker ---
async def task_worker_loop(worker_id: str, wake_up_event: asyncio.Event):
    """Continuously polls for and processes tasks."""
    logger.info(f"Task worker {worker_id} started.")
    task_types_handled = list(TASK_HANDLERS.keys())

    while not shutdown_event.is_set():
        task = None
        try:
            # Dequeue a task of a type this worker handles
            task = await storage.dequeue_task(
                worker_id, task_types_handled
            )  # Use storage.dequeue_task

            if task:
                logger.info(
                    f"Worker {worker_id} processing task {task['task_id']} (type: {task['task_type']})"
                )
                handler = TASK_HANDLERS.get(task["task_type"])

                if handler:
                    try:
                        # Execute the handler with the payload
                        await handler(task["payload"])
                        task_id = task["task_id"]
                        task_type = task["task_type"]
                        payload = task["payload"]
                        recurrence_rule_str = task.get("recurrence_rule")
                        original_task_id = task.get(
                            "original_task_id", task_id
                        )  # Use task_id if original is missing (first run)
                        task_max_retries = task.get("max_retries", 3)

                        # Mark task as done
                        await storage.update_task_status(task_id, "done")
                        logger.info(
                            f"Worker {worker_id} completed task {task_id} (Original: {original_task_id})"
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
                                    last_scheduled_at = last_scheduled_at.replace(
                                        tzinfo=timezone.utc
                                    )
                                    logger.warning(
                                        f"Made recurrence base time timezone-aware (UTC): {last_scheduled_at}"
                                    )

                                # Import here to avoid potential circular imports if task_worker is imported elsewhere early
                                from dateutil import rrule

                                # Calculate the next occurrence *after* the last scheduled time
                                rule = rrule.rrulestr(
                                    recurrence_rule_str, dtstart=last_scheduled_at
                                )
                                next_scheduled_dt = rule.after(last_scheduled_at)

                                if next_scheduled_dt:
                                    # Generate a new unique task ID for the next instance
                                    # Format: <original_task_id>_recur_<next_iso_timestamp>
                                    next_task_id = f"{original_task_id}_recur_{next_scheduled_dt.isoformat()}"

                                    logger.info(
                                        f"Calculated next occurrence for {original_task_id} at {next_scheduled_dt}. New task ID: {next_task_id}"
                                    )

                                    # Enqueue the next task instance
                                    await storage.enqueue_task(
                                        task_id=next_task_id,
                                        task_type=task_type,  # Same type
                                        payload=payload,  # Same payload
                                        scheduled_at=next_scheduled_dt,
                                        max_retries_override=task_max_retries,  # Same retry policy
                                        recurrence_rule=recurrence_rule_str,  # Keep the rule
                                        original_task_id=original_task_id,  # Link back to original
                                        notify_event=new_task_event,  # Notify if immediate
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
                                # Potentially send notification to developer?

                    except Exception as handler_exc:
                        current_retry = task.get("retry_count", 0)
                        max_retries = task.get(
                            "max_retries", 3  # Use DB default if missing somehow
                        )  # Use DB default if missing somehow
                        error_str = str(handler_exc)
                        logger.error(
                            f"Worker {worker_id} failed task {task['task_id']} (Retry {current_retry}/{max_retries}) due to handler error: {error_str}",
                            exc_info=True,
                        )

                        if current_retry < max_retries:
                            # Calculate exponential backoff with jitter
                            # Base delay: 5 seconds, increases with retries
                            backoff_delay = (5 * (2**current_retry)) + random.uniform(
                                0, 2
                            )
                            next_attempt_time = datetime.now(timezone.utc) + timedelta(
                                seconds=backoff_delay
                            )
                            logger.info(
                                f"Scheduling retry {current_retry + 1} for task {task['task_id']} at {next_attempt_time} (delay: {backoff_delay:.2f}s)"
                            )
                            try:
                                await storage.reschedule_task_for_retry(
                                    task_id=task["task_id"],
                                    next_scheduled_at=next_attempt_time,
                                    new_retry_count=current_retry + 1,
                                    error=error_str,
                                )
                            except Exception as reschedule_err:
                                # If rescheduling fails, log critical error and mark as failed to avoid infinite loops
                                logger.critical(
                                    f"CRITICAL: Failed to reschedule task {task['task_id']} for retry after handler error. Marking as failed. Error: {reschedule_err}",
                                    exc_info=True,
                                )
                                await storage.update_task_status(
                                    task["task_id"],
                                    "failed",
                                    error=f"Handler Error: {error_str}. Reschedule Failed: {reschedule_err}",
                                )
                        else:
                            logger.warning(
                                f"Task {task['task_id']} reached max retries ({max_retries}). Marking as failed."
                            )
                            # Mark task as permanently failed
                            await storage.update_task_status(
                                task["task_id"], "failed", error=error_str
                            )
                else:
                    # This shouldn't happen if dequeue_task respects task_types properly
                    logger.error(
                        f"Worker {worker_id} dequeued task {task['task_id']} but no handler found for type {task['task_type']}. Marking failed."
                    )
                    await storage.update_task_status(  # Use storage.update_task_status
                        task["task_id"],
                        "failed",
                        error=f"No handler registered for type {task['task_type']}",
                    )

            else:
                # No task found, wait for the polling interval OR the wake-up event
                try:
                    logger.debug(
                        f"Worker {worker_id}: No tasks found, waiting for event or timeout ({TASK_POLLING_INTERVAL}s)..."
                    )
                    # Wait for the event to be set, with a timeout
                    await asyncio.wait_for(
                        wake_up_event.wait(), timeout=TASK_POLLING_INTERVAL
                    )
                    # If wait_for completes without timeout, the event was set
                    logger.debug(f"Worker {worker_id}: Woken up by event.")
                    wake_up_event.clear()  # Reset the event for the next notification
                except asyncio.TimeoutError:
                    # Event didn't fire, timeout reached, proceed to next polling cycle
                    logger.debug(
                        f"Worker {worker_id}: Wait timed out, continuing poll cycle."
                    )
                    pass  # Continue the loop normally after timeout

        except asyncio.CancelledError:
            logger.info(f"Task worker {worker_id} received cancellation signal.")
            # If a task was being processed, try to mark it as pending again?
            # Or rely on lock expiry/manual intervention for now.
            # For simplicity, we just exit.
            break  # Exit the loop cleanly on cancellation
        except Exception as e:
            logger.error(
                f"Task worker {worker_id} encountered an error: {e}", exc_info=True
            )
            # If an error occurs during dequeue or status update, wait before retrying
            await asyncio.sleep(TASK_POLLING_INTERVAL * 2)  # Longer sleep after error

    logger.info(f"Task worker {worker_id} stopped.")


# --- Module initialization ---
def register_task_handler(task_type: str, handler: Callable):
    """Register a new task handler function for a specific task type."""
    TASK_HANDLERS[task_type] = handler
    logger.info(f"Registered task handler for task type: {task_type}")


def set_llm_response_generator(generator_func):
    """Set the LLM response generator function from main.py"""
    global _generate_llm_response_for_chat
    _generate_llm_response_for_chat = generator_func
    logger.info("Set LLM response generator function")


def set_mcp_state(sessions, tools, tool_name_mapping):
    """Set MCP state from main.py"""
    global mcp_sessions, mcp_tools, tool_name_to_server_id
    mcp_sessions = sessions
    mcp_tools = tools
    tool_name_to_server_id = tool_name_mapping
    logger.info(f"Set MCP state: {len(sessions)} sessions, {len(tools)} tools")


def get_task_handlers():
    """Return the current task handlers dictionary"""
    return TASK_HANDLERS


logger.info(
    f"Task worker module initialized with {len(TASK_HANDLERS)} handlers: {list(TASK_HANDLERS.keys())}"
)
