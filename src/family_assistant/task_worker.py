"""
Task worker implementation for background processing.
"""

import asyncio
import logging
import random
import traceback
import uuid
import zoneinfo  # Add this import
from collections.abc import Awaitable, Callable  # Import Union
from datetime import datetime, timedelta, timezone  # Added Union
from typing import Any

from dateutil import rrule

from family_assistant import storage
from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.interfaces import ChatInterface  # Import ChatInterface

# handle_index_email is now a method of EmailIndexer and registered in __main__.py
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import ToolExecutionContext

logger = logging.getLogger(__name__)

# --- Constants ---
TASK_POLLING_INTERVAL = 5  # Seconds to wait between polling for tasks

# --- Events for coordination (can remain module-level) ---
shutdown_event = asyncio.Event()
new_task_event = asyncio.Event()  # Event to notify worker of immediate tasks

# --- Task Handler Functions (remain module-level for now) ---
# These functions will be registered with the TaskWorker instance.


# Example Task Handler (no external dependencies)
async def handle_log_message(
    db_context: DatabaseContext, payload: dict[str, Any]
) -> None:
    """Simple task handler that logs the received payload."""
    logger.info(
        f"[Task Worker] Handling log_message task. Payload: {payload}"
    )  # db_context is available if needed
    # Simulate some work
    await asyncio.sleep(1)
    # In a real handler, you might interact with APIs, DB, etc.
    # If this function raises an exception, the task will be marked 'failed'.


# Note: Registration now happens in __main__.py using worker instance


async def handle_llm_callback(
    exec_context: ToolExecutionContext,  # Accept execution context
    payload: dict[str, Any],  # Payload from the task queue
) -> None:
    """
    Task handler for LLM scheduled callbacks.
    Dependencies are accessed via the ToolExecutionContext.
    """
    # Access dependencies from the execution context
    processing_service: ProcessingService | None = (
        exec_context.processing_service  # TaskWorker passes its own instance
    )
    chat_interface: ChatInterface | None = exec_context.chat_interface
    db_context = exec_context.db_context
    # Get interface identifiers from context
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id

    # Basic validation of dependencies from context
    if not processing_service:
        logger.error(
            "ProcessingService not found in ToolExecutionContext for handle_llm_callback."
        )
        raise ValueError("Missing ProcessingService dependency in context.")
    if not chat_interface:
        logger.error(
            "ChatInterface not found in ToolExecutionContext for handle_llm_callback."
        )
        raise ValueError("Missing ChatInterface dependency in context.")
    if not db_context:
        logger.error(
            "DatabaseContext not found in ToolExecutionContext for handle_llm_callback."
        )
        raise ValueError("Missing DatabaseContext dependency in context.")
    if not conversation_id:  # conversation_id should be set by _process_task
        logger.error(
            "Conversation ID not found in ToolExecutionContext for handle_llm_callback."
        )  # Corrected error message
        raise ValueError("Missing Chat ID in context.")

    # Extract necessary info from payload
    # chat_id is now from context
    callback_context = payload.get("callback_context")

    # Validate payload content
    if not callback_context:
        logger.error(
            f"Invalid payload for llm_callback task (missing callback_context): {payload}"
        )
        raise ValueError("Missing required field in payload: callback_context")

    logger.info(
        f"Handling LLM callback for conversation {interface_type}:{conversation_id}"
    )
    current_time_str = datetime.now(
        zoneinfo.ZoneInfo(exec_context.timezone_str)
    ).strftime("%Y-%m-%d %H:%M:%S %Z")  # Use timezone from context

    try:
        # Construct the trigger message content for the LLM
        trigger_text = f"System Callback Trigger:\n\nThe time is now {current_time_str}.\nYour scheduled context was:\n---\n{callback_context}\n---"

        # Generate a turn ID for this callback execution
        callback_turn_id = str(uuid.uuid4())

        # Save the initial system trigger message for the callback to history
        callback_trigger_timestamp = datetime.now(timezone.utc)
        await storage.add_message_to_history(
            db_context=db_context,
            interface_type=interface_type,  # Should be "system_callback" or similar
            conversation_id=conversation_id,
            interface_message_id=None,  # System-generated, no direct interface ID
            turn_id=callback_turn_id,  # Assign the generated turn_id
            thread_root_id=None,  # Callbacks currently don't maintain prior thread root
            timestamp=callback_trigger_timestamp,
            role="system",  # Role for the trigger message
            content=trigger_text,
        )
        logger.info(
            f"Saved system trigger message for callback {callback_turn_id} to history."
        )

        # Call the ProcessingService.
        # NOTE: process_message returns the LIST of generated messages for the turn,
        # not just the final content string. We are now calling generate_llm_response_for_chat
        (
            generated_messages,  # This is List[Dict[str, Any]]
            final_reasoning_info,  # Capture reasoning
            processing_error_traceback,  # Capture error
        ) = await processing_service.generate_llm_response_for_chat(
            db_context=db_context,
            chat_interface=chat_interface,  # Pass ChatInterface
            interface_type=interface_type,
            conversation_id=conversation_id,
            turn_id=callback_turn_id,  # Pass the generated turn_id
            trigger_content_parts=[
                {"type": "text", "text": trigger_text}
            ],  # Pass the trigger text as content part
            trigger_interface_message_id=None,  # System trigger, no direct interface ID
            user_name="System",  # Callback initiated by system
            replied_to_interface_id=None,  # Not a reply in this context
            # No confirmation callback needed for system-triggered callbacks
            request_confirmation_callback=None,
        )
        # The generated_messages already have turn_id and other metadata set by generate_llm_response_for_chat
        # Find the final assistant message to send back to the user
        final_llm_content_to_send = None
        if generated_messages:
            for msg in reversed(generated_messages):
                if msg.get("content") and msg.get("role") == "assistant":
                    final_llm_content_to_send = msg["content"]
                    break

        if final_llm_content_to_send:
            # Send the LLM's response back to the chat
            # Determine target chat_id based on interface type
            # The conversation_id is used directly below.

            # TelegramChatInterface will handle MarkdownV2 formatting if specified.
            sent_message_id_str = await chat_interface.send_message(
                conversation_id=conversation_id,
                text=final_llm_content_to_send,
                parse_mode="MarkdownV2",  # Instruct interface to use Markdown
            )
            logger.info(
                f"Sent LLM response for callback to {interface_type}:{conversation_id}."
            )

            # The initial system trigger message for the callback was saved above.
            # Now, if the assistant's response was sent, update its interface_message_id.
            if sent_message_id_str:
                # Find the corresponding assistant message in generated_messages to update its interface_message_id
                # This assumes the last 'assistant' message in generated_messages is the one sent.
                assistant_msg_to_update = next(
                    (
                        m
                        for m in reversed(generated_messages)
                        if m.get("role") == "assistant" and m.get("internal_id")
                    ),
                    None,
                )
                if assistant_msg_to_update and assistant_msg_to_update.get(
                    "internal_id"
                ):
                    await storage.update_message_interface_id(
                        db_context=db_context,
                        internal_id=assistant_msg_to_update["internal_id"],
                        interface_message_id=sent_message_id_str,
                    )
                else:
                    logger.warning(
                        f"Could not find saved assistant message to update interface_id for callback to {interface_type}:{conversation_id}"
                    )
            else:  # Message sending failed
                logger.error(
                    f"Failed to send LLM callback response to {interface_type}:{conversation_id}"
                )

            # Save the *generated* messages (tool calls, assistant responses) from the LLM interaction
            # These messages already have turn_id, interface_type, conversation_id, and timestamp populated by generate_llm_response_for_chat
            for (
                msg_dict_to_save
            ) in generated_messages:  # Iterate over the list of dicts
                # Ensure all required fields are present or defaulted for add_message_to_history
                # msg_dict_to_save is already populated by generate_llm_response_for_chat
                await storage.add_message_to_history(
                    db_context=db_context,
                    **msg_dict_to_save,  # Pass directly as it's prepared by generate_llm_response_for_chat
                )

        else:
            # Case: No final_llm_content_to_send. This could be due to a processing error or empty LLM response.
            interface_type = exec_context.interface_type
            conversation_id = exec_context.conversation_id

            logger.warning(
                f"LLM turn completed for callback in {interface_type}:{conversation_id}, but final message had no content."
            )
            # Optionally send a generic failure message to the chat
            await chat_interface.send_message(
                conversation_id=conversation_id,
                text="Sorry, I couldn't process the scheduled callback.",
            )

            # Raise an error to mark the task as failed if no response was generated
            # Check if there was a processing_error_traceback first
            if processing_error_traceback:
                raise RuntimeError(
                    f"LLM callback failed. Traceback: {processing_error_traceback}"
                )
            else:  # No specific error from processing, but also no content
                raise RuntimeError(
                    "LLM failed to generate response content for callback."
                )

    except Exception as e:
        # Catch errors during the generate_llm_response_for_chat call or sending/saving messages
        # Need interface_type and conversation_id here
        interface_type = exec_context.interface_type
        conversation_id = exec_context.conversation_id

        logger.error(
            f"Failed during LLM callback processing for {interface_type}:{conversation_id}: {e}",
            exc_info=True,
        )
        # Raise the exception to ensure the task is marked as failed


class TaskWorker:
    """Manages the task processing loop and handler registry."""

    def __init__(
        self,
        processing_service: ProcessingService,
        chat_interface: ChatInterface,
        new_task_event: asyncio.Event,  # Add new_task_event
        calendar_config: dict[str, Any],
        timezone_str: str,
        embedding_generator: EmbeddingGenerator,
    ) -> None:
        """Initializes the TaskWorker with its dependencies."""
        self.processing_service = processing_service
        self.chat_interface = chat_interface
        self.new_task_event = new_task_event  # Store the event
        self.calendar_config = calendar_config
        self.timezone_str = timezone_str
        self.embedding_generator = embedding_generator
        # Initialize handlers - specific handlers are registered externally
        # Update handler signature type hint
        self.task_handlers: dict[
            str, Callable[[ToolExecutionContext, Any], Awaitable[None]]
        ] = {}
        self.worker_id = f"worker-{uuid.uuid4()}"
        logger.info(f"TaskWorker instance {self.worker_id} created.")

    def register_task_handler(
        self,
        task_type: str,
        # Update handler signature type hint
        handler: Callable[[ToolExecutionContext, Any], Awaitable[None]],
    ) -> None:
        """Register a task handler function for a specific task type."""
        self.task_handlers[task_type] = handler
        logger.info(
            f"Worker {self.worker_id}: Registered handler for task type: {task_type}"
        )

    # Update return type hint
    def get_task_handlers(
        self,
    ) -> dict[str, Callable[[ToolExecutionContext, Any], Awaitable[None]]]:
        """Return the current task handlers dictionary for this worker."""
        return self.task_handlers

    async def _process_task(
        self,
        db_context: DatabaseContext,
        task: dict[str, Any],
        wake_up_event: asyncio.Event,
    ) -> None:
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
            return  # Stop processing this task

        try:
            # --- Create Execution Context ---
            # Extract interface identifiers from payload
            payload_dict = (
                task.get("payload", {}) if isinstance(task.get("payload"), dict) else {}
            )
            # Need to define these *before* using them in logging etc.
            raw_interface_type: str | None = payload_dict.get("interface_type")
            raw_conversation_id: str | None = payload_dict.get("conversation_id")

            final_interface_type: str
            final_conversation_id: str

            if task["task_type"] == "llm_callback":
                if not raw_interface_type or not raw_conversation_id:
                    logger.error(
                        f"Task {task['task_id']} (llm_callback) missing interface_type or conversation_id in payload."
                    )
                    await storage.update_task_status(
                        db_context,
                        task_id=task["task_id"],
                        status="failed",
                        error="Missing interface_type or conversation_id in payload for llm_callback",
                    )
                    return  # Stop processing
                final_interface_type = raw_interface_type
                final_conversation_id = raw_conversation_id
            else:
                # For other task types, provide defaults if None, to satisfy linter if it expects str
                final_interface_type = (
                    raw_interface_type
                    if raw_interface_type is not None
                    else "unknown_interface"
                )
                final_conversation_id = (
                    raw_conversation_id
                    if raw_conversation_id is not None
                    else "unknown_conversation"
                )

            exec_context = ToolExecutionContext(
                # Pass new identifiers
                interface_type=final_interface_type,
                conversation_id=final_conversation_id,
                turn_id=str(
                    uuid.uuid4()
                ),  # Generate a new turn_id for this task execution
                db_context=db_context,
                chat_interface=self.chat_interface,
                new_task_event=self.new_task_event,  # Pass the event
                timezone_str=self.timezone_str,
                processing_service=self.processing_service,
                embedding_generator=self.embedding_generator,
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
            payload = task["payload"]  # Keep payload for recurrence
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
                        last_scheduled_at = last_scheduled_at.replace(
                            tzinfo=timezone.utc
                        )
                        logger.warning(
                            f"Made recurrence base time timezone-aware (UTC): {last_scheduled_at}"
                        )

                    # Calculate the next occurrence *after* the last scheduled time
                    rule = rrule.rrulestr(
                        recurrence_rule_str,
                        dtstart=last_scheduled_at,
                    )
                    next_scheduled_dt = rule.after(last_scheduled_at)

                    if next_scheduled_dt:
                        # Generate a new unique task ID for the next instance
                        # Format: <original_task_id>_recur_<next_iso_timestamp>
                        next_task_id = (
                            f"{original_task_id}_recur_{next_scheduled_dt.isoformat()}"
                        )

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

    async def _handle_task_failure(
        self, db_context: DatabaseContext, task: dict[str, Any], handler_exc: Exception
    ) -> None:
        """Handles logging, retries, and marking tasks as failed."""
        current_retry = task.get("retry_count", 0)
        max_retries = task.get("max_retries", 3)  # Use DB default if missing somehow
        # Define interface/conversation ID for logging if available in payload
        payload_dict = (
            task.get("payload", {}) if isinstance(task.get("payload"), dict) else {}
        )
        (  # Create helper string for logging
            f" ({payload_dict.get('interface_type', 'unknown_if')}:"
            f"{payload_dict.get('conversation_id', 'unknown_cid')})"
            if payload_dict.get("interface_type")
            else ""
        )
        error_str = "\n".join(traceback.format_exception(handler_exc))
        logger.error(
            f"Worker {self.worker_id} failed task {task['task_id']} (Retry {current_retry}/{max_retries}) due to handler error: {error_str}",
            exc_info=True,
        )

        if current_retry < max_retries:
            # Calculate exponential backoff with jitter
            backoff_delay = (5 * (2**current_retry)) + random.uniform(0, 2)
            next_attempt_time = datetime.now(timezone.utc) + timedelta(
                seconds=backoff_delay
            )
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
            # Handle case where the turn completed but the final assistant message had no content
            logger.warning(
                f"Task {task['task_id']} reached max retries ({max_retries}). Marking as failed."
            )
            await storage.update_task_status(
                db_context=db_context,
                task_id=task["task_id"],
                status="failed",
                error=error_str,
            )

    async def _wait_for_next_poll(self, wake_up_event: asyncio.Event) -> None:
        """Waits for the polling interval or a wake-up event."""
        try:
            logger.debug(
                f"Worker {self.worker_id}: No tasks found, waiting for event or timeout ({TASK_POLLING_INTERVAL}s)..."
            )

            await asyncio.wait_for(wake_up_event.wait(), timeout=TASK_POLLING_INTERVAL)
            # If wait_for completes without timeout, the event was set
            logger.debug(f"Worker {self.worker_id}: Woken up by event.")
            wake_up_event.clear()  # Reset the event for the next notification
            await asyncio.sleep(
                0.1
            )  # Sometimes there is a slight delay before the task is actually visible.
        except asyncio.TimeoutError:
            # Event didn't fire, timeout reached, proceed to next polling cycle
            logger.debug(
                f"Worker {self.worker_id}: Wait timed out, continuing poll cycle."
            )
            pass  # Continue the loop normally after timeout

    async def run(self, wake_up_event: asyncio.Event) -> None:
        """Continuously polls for and processes tasks. Replaces task_worker_loop."""
        logger.info(f"Task worker {self.worker_id} run loop started.")
        # Get task types handled by *this specific instance*
        task_types_handled = list(self.task_handlers.keys())
        if not task_types_handled:
            logger.warning(
                f"Task worker {self.worker_id} has no registered handlers. Exiting loop."
            )
            return

        while not shutdown_event.is_set():
            try:
                task = None  # Initialize task variable for the outer scope
                # Database context per iteration (starts a transaction)
                async with get_db_context() as db_context:
                    logger.debug(
                        "Polling for tasks on DB context: %s", db_context.engine.url
                    )
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
                        await asyncio.sleep(TASK_POLLING_INTERVAL)

            # --- Exception handling for the outer try block (whole loop iteration) ---
            except asyncio.CancelledError:
                logger.info(
                    f"Task worker {self.worker_id} received cancellation signal."
                )
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
                await asyncio.sleep(
                    TASK_POLLING_INTERVAL * 2
                )  # Longer sleep after error

        logger.info(f"Task worker {self.worker_id} stopped.")


__all__ = [
    "TaskWorker",
    "handle_log_message",
    "handle_llm_callback",
]  # Export class and relevant handlers
