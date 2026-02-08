"""
Task worker implementation for background processing.
"""

import asyncio
import json
import logging
import random
import shutil
import traceback
import uuid
import zoneinfo  # Add this import
from collections.abc import Awaitable, Callable  # Import Union
from datetime import UTC, datetime, timedelta  # Added Union
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles.os
from dateutil import rrule
from dateutil.parser import isoparse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

# Removed storage import - using repository pattern
from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.interfaces import ChatInterface  # Import ChatInterface
from family_assistant.scripting import (
    MontyEngine,
    ScriptError,
    ScriptTimeoutError,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.tools.types import CalendarConfig

if TYPE_CHECKING:
    from family_assistant.events.indexing_source import IndexingSource

# handle_index_email is now a method of EmailIndexer and registered in __main__.py
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import get_task_event
from family_assistant.storage.types import TaskDict
from family_assistant.tools import ToolExecutionContext
from family_assistant.utils.clock import Clock, SystemClock

logger = logging.getLogger(__name__)


async def _handle_schedule_automation_recurrence(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """
    Handle schedule automation recurrence after successful task execution.

    Checks if the task was triggered by a schedule automation and schedules
    the next instance via the after_task_execution callback.

    Args:
        exec_context: Tool execution context with DB access
        payload: Task payload that may contain automation_id and automation_type
    """
    automation_id = payload.get("automation_id")
    automation_type = payload.get("automation_type")

    if automation_id and automation_type == "schedule":
        try:
            clock = exec_context.clock or SystemClock()
            await exec_context.db_context.schedule_automations.after_task_execution(
                automation_id=int(automation_id),
                execution_time=clock.now(),
            )
            logger.info(
                f"Scheduled next instance for schedule automation {automation_id}"
            )
        except Exception as auto_err:
            logger.error(
                f"Failed to schedule next instance for automation {automation_id}: {auto_err}",
                exc_info=True,
            )
            # Don't raise - the automation already executed successfully


async def _schedule_reminder_follow_up(
    exec_context: ToolExecutionContext,
    original_context: str,
    follow_up_interval: str,
    current_attempt: int,
    max_follow_ups: int,
) -> None:
    """Helper function to schedule a follow-up reminder."""
    # Removed storage import - using repository pattern

    # Parse the follow-up interval
    interval_parts = follow_up_interval.lower().split()
    if len(interval_parts) != 2:
        logger.error(f"Invalid follow-up interval format: {follow_up_interval}")
        return

    try:
        amount = int(interval_parts[0])
        unit = interval_parts[1].rstrip("s")  # Remove plural 's'

        if unit == "minute":
            delta = timedelta(minutes=amount)
        elif unit == "hour":
            delta = timedelta(hours=amount)
        elif unit == "day":
            delta = timedelta(days=amount)
        else:
            logger.error(f"Unknown time unit in follow-up interval: {unit}")
            return
    except ValueError:
        logger.error(f"Invalid follow-up interval: {follow_up_interval}")
        return

    clock = exec_context.clock or SystemClock()
    next_reminder_time = clock.now() + delta

    # Use current time as scheduling timestamp for this follow-up task
    # When this follow-up runs, it will check for intervening messages since THIS timestamp,
    # not since the original reminder. This ensures each follow-up only cancels if user
    # responded after the previous follow-up was scheduled.
    current_scheduling_timestamp = clock.now().isoformat()

    task_id = f"llm_callback_{uuid.uuid4()}"
    payload = {
        "interface_type": exec_context.interface_type,
        "conversation_id": exec_context.conversation_id,
        "user_name": exec_context.user_name,  # Preserve user_name for follow-up
        "callback_context": original_context,
        "scheduling_timestamp": current_scheduling_timestamp,
        "reminder_config": {
            "is_reminder": True,
            "follow_up": True,
            "follow_up_interval": follow_up_interval,
            "max_follow_ups": max_follow_ups,
            "current_attempt": current_attempt + 1,
        },
    }

    await exec_context.db_context.tasks.enqueue(
        task_id=task_id,
        task_type="llm_callback",
        payload=payload,
        scheduled_at=next_reminder_time,
    )

    logger.info(
        f"Scheduled follow-up reminder {task_id} for {exec_context.interface_type}:{exec_context.conversation_id} "
        f"at {next_reminder_time} (attempt {current_attempt + 1} of {max_follow_ups + 1})"
    )


# --- Constants ---
TASK_POLLING_INTERVAL = 5  # Seconds to wait between polling for tasks
TASK_HANDLER_TIMEOUT = 300  # Seconds to wait for task handler execution (5 minutes)

# --- Events for coordination (can remain module-level) ---
# Note: shutdown_event removed - each TaskWorker instance now has its own
new_task_event = asyncio.Event()  # Event to notify worker of immediate tasks

# --- Task Handler Functions (remain module-level for now) ---
# These functions will be registered with the TaskWorker instance.


# Example Task Handler (no external dependencies)
async def handle_log_message(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    db_context: DatabaseContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
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
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],  # Payload from the task queue
) -> None:
    """
    Task handler for LLM scheduled callbacks and reminders.
    Dependencies are accessed via the ToolExecutionContext.
    """
    # Access dependencies from the execution context
    processing_service: ProcessingService | None = (
        exec_context.processing_service  # TaskWorker passes its own instance
    )
    chat_interface: ChatInterface | None = exec_context.chat_interface
    db_context = exec_context.db_context
    clock = exec_context.clock

    # Get interface identifiers from context
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id

    # Basic validation of dependencies from context
    if not clock:
        logger.error("Clock not found in ToolExecutionContext for handle_llm_callback.")
        raise ValueError("Missing Clock dependency in context.")
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
    scheduling_timestamp_str = payload.get("scheduling_timestamp")
    trigger_attachments = payload.get(
        "trigger_attachments"
    )  # From script wake_llm calls

    # Extract reminder configuration if present
    reminder_config = payload.get("reminder_config", {})
    is_reminder = reminder_config.get("is_reminder", False)
    follow_up_enabled = reminder_config.get("follow_up", False)
    follow_up_interval = reminder_config.get("follow_up_interval", "30 minutes")
    max_follow_ups = reminder_config.get("max_follow_ups", 2)
    current_attempt = reminder_config.get("current_attempt", 1)

    # Switch to specialized reminder profile if available
    if (
        is_reminder
        and processing_service
        and processing_service.processing_services_registry
    ):
        reminder_service = processing_service.processing_services_registry.get(
            "reminder"
        )
        if reminder_service:
            logger.info("Switching to 'reminder' profile for reminder task execution.")
            processing_service = reminder_service

    # Validate payload content
    if not callback_context:
        logger.error(
            f"Invalid payload for llm_callback task (missing callback_context): {payload}"
        )
        raise ValueError("Missing required field in payload: callback_context")

    if not scheduling_timestamp_str:
        logger.error(
            f"Invalid payload for llm_callback task (missing scheduling_timestamp): {payload}"
        )
        raise ValueError("Missing required field in payload: scheduling_timestamp")

    try:
        scheduling_timestamp_dt = isoparse(scheduling_timestamp_str)
        if scheduling_timestamp_dt.tzinfo is None:  # Ensure it's offset-aware
            scheduling_timestamp_dt = scheduling_timestamp_dt.replace(tzinfo=UTC)
    except ValueError as e:
        logger.error(
            f"Invalid scheduling_timestamp format in llm_callback task: {scheduling_timestamp_str}"
        )
        raise ValueError("Invalid scheduling_timestamp format") from e

    # For reminders with follow-up enabled, check if user responded since original scheduling
    intervening_messages = []
    if is_reminder and follow_up_enabled:
        # Check for intervening user messages since the original scheduling
        # If found, we'll cancel this reminder (initial or follow-up)
        stmt = (
            select(message_history_table.c.internal_id)
            .where(message_history_table.c.interface_type == interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .where(message_history_table.c.role == "user")
            .where(message_history_table.c.timestamp > scheduling_timestamp_dt)
            .limit(1)
        )
        intervening_messages = await db_context.fetch_all(stmt)

        if intervening_messages:
            # Follow-up reminder - user responded since scheduling, cancel this follow-up
            logger.info(
                f"User has responded since reminder was scheduled at {scheduling_timestamp_str} for conversation {interface_type}:{conversation_id}. Cancelling follow-up reminder (attempt {current_attempt})."
            )
            # User responded, so cancel this follow-up entirely
            return
    else:
        logger.info(
            f"Callback for conversation {interface_type}:{conversation_id} (scheduled at {scheduling_timestamp_str}) proceeding without checking for user response."
        )

    logger.info(
        f"Handling LLM callback for conversation {interface_type}:{conversation_id} (scheduled at {scheduling_timestamp_str})"
    )
    current_time_str = (
        clock
        .now()
        .astimezone(zoneinfo.ZoneInfo(exec_context.timezone_str))
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )  # Use timezone from context

    try:
        # Construct the trigger message content for the LLM
        if is_reminder:
            if current_attempt == 1:
                trigger_text = f"System: Reminder triggered\n\nThe time is now {current_time_str}.\nTask: Send a reminder about: {callback_context}"
            else:
                trigger_text = f"System: Follow-up reminder triggered (attempt {current_attempt} of {max_follow_ups + 1})\n\nThe time is now {current_time_str}.\nOriginal reminder: {callback_context}\nNote: User has not responded to previous reminder sent at {scheduling_timestamp_str}"
        else:
            trigger_text = f"System Callback Trigger:\n\nThe time is now {current_time_str}.\nYour scheduled context was:\n---\n{callback_context}\n---"

        # Generate a turn ID for this callback execution
        callback_turn_id = str(uuid.uuid4())

        # Save the initial system trigger message for the callback to history
        callback_trigger_timestamp = clock.now()
        await db_context.message_history.add(
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
        # NOTE: `handle_chat_interaction` now handles saving of all messages in the turn.
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=chat_interface,
            interface_type=interface_type,
            conversation_id=conversation_id,
            # turn_id is generated within handle_chat_interaction
            trigger_content_parts=[{"type": "text", "text": trigger_text}],
            trigger_interface_message_id=None,  # System trigger
            user_name=exec_context.user_name,  # Use preserved user name from context
            replied_to_interface_id=None,  # Not a reply
            request_confirmation_callback=None,  # No confirmation for system callbacks
            trigger_attachments=trigger_attachments,  # Pass attachments from script wake_llm
        )

        final_llm_content_to_send = result.text_reply
        final_assistant_message_internal_id = result.assistant_message_internal_id
        _final_reasoning_info = (
            result.reasoning_info
        )  # Not used directly by this handler
        processing_error_traceback = result.error_traceback
        response_attachment_ids = result.attachment_ids

        logger.debug(
            f"LLM callback result: text_reply='{final_llm_content_to_send}', "
            f"error='{processing_error_traceback}'"
        )

        sent_message_id_str = None
        # Send message if there's text content OR attachments
        if final_llm_content_to_send or response_attachment_ids:
            sent_message_id_str = await chat_interface.send_message(
                conversation_id=conversation_id,
                text=final_llm_content_to_send
                or "",  # Use empty string if no text but have attachments
                parse_mode="MarkdownV2",
                attachment_ids=response_attachment_ids,
            )
            logger.info(
                f"Sent LLM response for callback to {interface_type}:{conversation_id}."
            )
        else:
            # Case: No final_llm_content_to_send and no attachments.
            logger.warning(
                f"LLM turn completed for callback in {interface_type}:{conversation_id}, but final message had no content or attachments."
            )

        # Schedule follow-up reminder if needed (moved outside of text reply condition)
        logger.info(
            f"Follow-up scheduling check: is_reminder={is_reminder}, "
            f"follow_up_enabled={follow_up_enabled}, "
            f"current_attempt={current_attempt}, max_follow_ups={max_follow_ups}, "
            f"intervening_messages={len(intervening_messages) if intervening_messages else 0}, "
            f"has_text_reply={bool(final_llm_content_to_send)}"
        )
        if is_reminder and follow_up_enabled and current_attempt < max_follow_ups + 1:
            logger.info(
                f"Scheduling follow-up reminder for {interface_type}:{conversation_id} "
                f"(attempt {current_attempt + 1} of {max_follow_ups + 1})"
            )
            try:
                await _schedule_reminder_follow_up(
                    exec_context=exec_context,
                    original_context=callback_context,
                    follow_up_interval=follow_up_interval,
                    current_attempt=current_attempt,
                    max_follow_ups=max_follow_ups,
                )
                logger.info("Successfully scheduled follow-up reminder")
            except Exception as e:
                logger.error(
                    f"Failed to schedule follow-up reminder: {e}", exc_info=True
                )
        else:
            logger.debug(
                f"Not scheduling follow-up reminder for {interface_type}:{conversation_id}: "
                f"conditions not met"
            )

        # Update interface message ID if we sent a message successfully
        if sent_message_id_str and final_assistant_message_internal_id is not None:
            try:
                await db_context.message_history.update_interface_id(
                    internal_id=final_assistant_message_internal_id,
                    interface_message_id=sent_message_id_str,
                )
            except Exception as e:
                logger.error(
                    f"Failed to update interface_message_id for callback response: {e}",
                    exc_info=True,
                )
        elif sent_message_id_str:  # Message sent but no internal_id to update
            logger.warning(
                f"Sent LLM callback response to {interface_type}:{conversation_id}, but could not find internal_id ({final_assistant_message_internal_id}) to update its interface_message_id."
            )
        elif (
            final_llm_content_to_send or response_attachment_ids
        ):  # We expected to send a message but failed
            logger.error(
                f"Failed to send LLM callback response to {interface_type}:{conversation_id}"
            )
            # Raise an exception to mark the task as failed
            raise RuntimeError(
                f"Failed to send LLM callback response to {interface_type}:{conversation_id} via chat interface."
            )

        # Check if we should fail the task due to processing errors
        if processing_error_traceback:
            logger.error(
                f"LLM callback had processing errors for {interface_type}:{conversation_id}"
            )
            raise RuntimeError(
                f"LLM callback failed. Traceback: {processing_error_traceback}"
            )
        elif not final_llm_content_to_send and not is_reminder:
            # For non-reminder callbacks, we expect content to be generated
            logger.error(
                f"No content generated for non-reminder callback in {interface_type}:{conversation_id}"
            )
            raise RuntimeError("LLM failed to generate response content for callback.")

        # Handle schedule automation recurrence if this was from a schedule automation
        await _handle_schedule_automation_recurrence(exec_context, payload)

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
        calendar_config: CalendarConfig | None,
        timezone_str: str,
        embedding_generator: EmbeddingGenerator,
        shutdown_event_instance: asyncio.Event | None = None,  # Made optional
        clock: Clock | None = None,
        indexing_source: "IndexingSource | None" = None,
        engine: AsyncEngine
        | None = None,  # Add engine parameter for dependency injection
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_sources: dict[str, Any] | None = None,  # Add event sources
        handler_timeout: float = TASK_HANDLER_TIMEOUT,  # Configurable timeout per instance
    ) -> None:
        """Initializes the TaskWorker with its dependencies."""
        self.processing_service = processing_service
        self.chat_interface = chat_interface
        # Use provided shutdown_event_instance or create a new instance-specific event
        # Don't use the module-level shutdown_event as it persists across test runs
        self.shutdown_event = (
            shutdown_event_instance
            if shutdown_event_instance is not None
            else asyncio.Event()
        )
        self.calendar_config: CalendarConfig = (
            calendar_config if calendar_config else {}
        )
        self.timezone_str = timezone_str
        self.embedding_generator = embedding_generator
        self.clock = (
            clock if clock is not None else SystemClock()
        )  # Store the clock instance
        self.indexing_source = indexing_source
        self.event_sources = event_sources  # Store event sources
        self.engine = engine  # Store the engine for database operations
        self.handler_timeout = handler_timeout  # Store timeout per instance
        # Initialize handlers - specific handlers are registered externally
        # Update handler signature type hint
        self.task_handlers: dict[
            str, Callable[[ToolExecutionContext, Any], Awaitable[None]]
        ] = {}
        self.worker_id = f"worker-{uuid.uuid4()}"
        self.last_activity: datetime | None = None  # Track last activity time
        self._update_last_activity()  # Set initial activity
        logger.info(f"TaskWorker instance {self.worker_id} created.")

    def _update_last_activity(self) -> None:
        """Updates the last activity timestamp."""
        self.last_activity = self.clock.now()

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

    async def _handle_recurrence(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
    ) -> None:
        """Handles scheduling the next instance of a recurring task."""
        recurrence_rule_str = task.get("recurrence_rule")
        if not recurrence_rule_str:
            return

        task_id = task["task_id"]
        task_type = task["task_type"]
        payload = task["payload"]
        original_task_id = task.get("original_task_id") or task_id
        task_max_retries = task.get("max_retries", 3)

        logger.info(
            f"RECURRENCE PROCESSING: Task {task_id} has recurrence rule: {recurrence_rule_str}. Scheduling next instance."
        )
        try:
            # Use the *scheduled_at* time of the completed task as the base for the next occurrence
            last_scheduled_at = task.get("scheduled_at")
            if not last_scheduled_at:
                # If somehow scheduled_at is missing, use created_at as fallback
                last_scheduled_at = task.get("created_at", datetime.now(UTC))
                logger.warning(
                    f"RECURRENCE WARNING: Task {task_id} missing scheduled_at, using created_at ({last_scheduled_at}) for recurrence base."
                )
            # Ensure the base time is timezone-aware for rrule
            if last_scheduled_at.tzinfo is None:
                last_scheduled_at = last_scheduled_at.replace(tzinfo=UTC)
                logger.warning(
                    f"RECURRENCE WARNING: Made recurrence base time timezone-aware (UTC): {last_scheduled_at}"
                )

            # Convert UTC time to user's timezone before calculating recurrence
            # This ensures BYHOUR and other time-based rules work in the user's timezone
            user_tz = zoneinfo.ZoneInfo(self.timezone_str)
            last_scheduled_in_user_tz = last_scheduled_at.astimezone(user_tz)
            logger.debug(
                f"RECURRENCE DEBUG: Converting scheduled time from {last_scheduled_at} UTC to {last_scheduled_in_user_tz} {self.timezone_str} for recurrence calculation"
            )

            # Get current time in user timezone to avoid scheduling in the past
            current_time_in_user_tz = self.clock.now().astimezone(user_tz)

            # Calculate the next occurrence *after* the current time (not last scheduled time)
            # This prevents "catch up" behavior when the task runner restarts after downtime
            # Use the last scheduled time as dtstart so BYHOUR is interpreted correctly
            rule = rrule.rrulestr(
                recurrence_rule_str,
                dtstart=last_scheduled_in_user_tz,
            )
            next_scheduled_dt = rule.after(current_time_in_user_tz)

            # Convert the result back to UTC for storage
            if next_scheduled_dt:
                next_scheduled_dt = next_scheduled_dt.astimezone(UTC)
                logger.debug(
                    f"RECURRENCE DEBUG: Next occurrence calculated as {next_scheduled_dt} UTC"
                )

            if next_scheduled_dt:
                # For system tasks, reuse the original task ID to enable upsert behavior
                # For other tasks, generate a new unique task ID
                if original_task_id.startswith("system_"):
                    next_task_id = original_task_id
                    logger.info(
                        f"RECURRENCE SYSTEM: Calculated next occurrence for system task {original_task_id} at {next_scheduled_dt}. Reusing task ID for upsert."
                    )
                else:
                    # Format: <original_task_id>_recur_<next_iso_timestamp>
                    next_task_id = (
                        f"{original_task_id}_recur_{next_scheduled_dt.isoformat()}"
                    )
                    logger.info(
                        f"RECURRENCE NEW: Calculated next occurrence for {original_task_id} at {next_scheduled_dt}. New task ID: {next_task_id}"
                    )

                # Enqueue the next task instance
                await db_context.tasks.enqueue(
                    task_id=next_task_id,
                    task_type=task_type,
                    payload=payload,
                    scheduled_at=next_scheduled_dt,
                    max_retries_override=task_max_retries,
                    recurrence_rule=recurrence_rule_str,
                    original_task_id=original_task_id,
                )
                logger.info(
                    f"RECURRENCE SUCCESS: Successfully enqueued next recurring task instance {next_task_id} for original {original_task_id}."
                )
            else:
                logger.info(
                    f"RECURRENCE END: No further occurrences found for recurring task {original_task_id} based on rule '{recurrence_rule_str}'."
                )

        except Exception as recur_err:
            logger.error(
                f"RECURRENCE ERROR: Failed to calculate or enqueue next instance for recurring task {task_id} (Original: {original_task_id}): {recur_err}",
                exc_info=True,
            )
            # Don't mark the original task as failed, just log the recurrence error.

    async def _process_task(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
        wake_up_event: asyncio.Event,
    ) -> None:
        """Handles the execution, completion marking, and recurrence logic for a dequeued task."""
        logger.info(
            f"PROCESS START: Worker {self.worker_id} processing task {task['task_id']} (type: {task['task_type']})"
        )
        handler = self.task_handlers.get(task["task_type"])

        if not handler:
            # This shouldn't happen if dequeue_task respects task_types properly
            logger.error(
                f"PROCESS ERROR: Worker {self.worker_id} dequeued task {task['task_id']} but no handler found for type {task['task_type']}. Marking failed."
            )
            await db_context.tasks.update_status(
                task_id=task["task_id"],
                status="failed",
                error=f"No handler registered for type {task['task_type']}",
            )
            return  # Stop processing this task

        try:
            # --- Create Execution Context ---
            # Extract interface identifiers from payload
            # Need to define these *before* using them in logging etc.
            # payload_dict is guaranteed to be a dict
            payload_dict = task["payload"] or {}
            raw_interface_type: str | None = payload_dict.get("interface_type")
            raw_conversation_id: str | None = payload_dict.get("conversation_id")

            final_interface_type: str
            final_conversation_id: str

            if task["task_type"] == "llm_callback":
                if not raw_interface_type or not raw_conversation_id:
                    logger.error(
                        f"PROCESS ERROR: Task {task['task_id']} (llm_callback) missing interface_type or conversation_id in payload."
                    )
                    await db_context.tasks.update_status(
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

            # Extract user_name from payload if available, else default
            user_name = payload_dict.get("user_name", "TaskWorkerUser")

            exec_context = ToolExecutionContext(
                # Pass new identifiers
                interface_type=final_interface_type,
                conversation_id=final_conversation_id,
                user_name=user_name,  # Use user_name from payload or default
                turn_id=str(
                    uuid.uuid4()
                ),  # Generate a new turn_id for this task execution
                db_context=db_context,
                # Infrastructure fields (required - no defaults)
                processing_service=self.processing_service,
                clock=self.clock,
                home_assistant_client=self.processing_service.home_assistant_client
                if self.processing_service
                else None,
                event_sources=self.event_sources,
                attachment_registry=self.processing_service.attachment_registry
                if self.processing_service
                else None,
                camera_backend=None,
                # Optional fields (with defaults)
                chat_interface=self.chat_interface,
                timezone_str=self.timezone_str,
                processing_profile_id=(
                    self.processing_service.service_config.id
                    if self.processing_service
                    else None
                ),
                update_activity_callback=self._update_last_activity,  # Pass activity callback
                embedding_generator=self.embedding_generator,
                indexing_source=self.indexing_source,  # Pass the indexing source
                visibility_grants=(
                    self.processing_service.service_config.visibility_grants
                    if self.processing_service
                    else None
                ),
                default_note_visibility_labels=(
                    self.processing_service.service_config.default_note_visibility_labels
                    if self.processing_service
                    else None
                ),
            )
            # --- Execute Handler with Context ---
            logger.debug(
                f"HANDLER START: Worker {self.worker_id} executing handler for task {task['task_id']} with context."
            )
            # Pass the context and the original payload with timeout
            try:
                await asyncio.wait_for(
                    handler(exec_context, task["payload"]), timeout=self.handler_timeout
                )
                logger.debug(
                    f"HANDLER SUCCESS: Worker {self.worker_id} completed handler for task {task['task_id']}"
                )
            except TimeoutError:
                logger.error(
                    f"HANDLER TIMEOUT: Task {task['task_id']} (type: {task['task_type']}) timed out after {self.handler_timeout} seconds"
                )
                # Re-raise to trigger retry logic in _handle_task_failure
                raise

            # Task details for logging
            task_id = task["task_id"]
            original_task_id = task.get(
                "original_task_id", task_id
            )  # Use task_id if original is missing (first run)

            # Mark task as done
            await db_context.tasks.update_status(
                task_id=task_id,
                status="done",
            )
            logger.info(
                f"PROCESS SUCCESS: Worker {self.worker_id} completed task {task_id} (Original: {original_task_id})"
            )

            # --- Handle Recurrence ---
            await self._handle_recurrence(db_context, task)

        except Exception as handler_exc:
            await self._handle_task_failure(db_context, task, handler_exc)

    async def _handle_task_failure(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
        handler_exc: Exception,
    ) -> None:
        """Handles logging, retries, and marking tasks as failed."""
        current_retry = task.get("retry_count", 0)
        max_retries = task.get("max_retries", 3)  # Use DB default if missing somehow
        # Define interface/conversation ID for logging if available in payload
        payload_dict = task["payload"] or {}
        interface_info = (  # Create helper string for logging
            f" ({payload_dict.get('interface_type', 'unknown_if')}:"
            f"{payload_dict.get('conversation_id', 'unknown_cid')})"
            if payload_dict.get("interface_type")
            else ""
        )
        error_str = "\n".join(traceback.format_exception(handler_exc))
        logger.error(
            f"Worker {self.worker_id} failed task {task['task_id']}{interface_info} (Retry {current_retry}/{max_retries}) due to handler error: {error_str}",
            exc_info=True,
        )

        if current_retry < max_retries:
            # Calculate exponential backoff with jitter
            backoff_delay = (5 * (2**current_retry)) + random.uniform(0, 2)
            next_attempt_time = self.clock.now() + timedelta(seconds=backoff_delay)
            logger.info(
                f"Scheduling retry {current_retry + 1} for task {task['task_id']} at {next_attempt_time} (delay: {backoff_delay:.2f}s)"
            )
            try:
                await db_context.tasks.reschedule_for_retry(
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
                await db_context.tasks.update_status(
                    task_id=task["task_id"],
                    status="failed",
                    error=f"Handler Error: {error_str}. Reschedule Failed: {reschedule_err}",
                )
        else:
            # Handle case where the turn completed but the final assistant message had no content
            logger.warning(
                f"Task {task['task_id']} reached max retries ({max_retries}). Marking as failed."
            )
            await db_context.tasks.update_status(
                task_id=task["task_id"],
                status="failed",
                error=error_str,
            )
            # Handle recurrence even if task failed (after max retries)
            await self._handle_recurrence(db_context, task)

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
        except TimeoutError:
            # Event didn't fire, timeout reached, proceed to next polling cycle
            logger.debug(
                f"Worker {self.worker_id}: Wait timed out, continuing poll cycle."
            )
            # Continue the loop normally after timeout

    async def run(self, wake_up_event: asyncio.Event | None = None) -> None:
        """Continuously polls for and processes tasks.

        Args:
            wake_up_event: Optional override event for testing. If not provided,
                          uses the global task event from storage.
        """
        # Use provided event or get the global task event
        if wake_up_event is None:
            wake_up_event = get_task_event()

        logger.info(f"Task worker {self.worker_id} run loop started.")
        # Get task types handled by *this specific instance*
        task_types_handled = list(self.task_handlers.keys())
        if not task_types_handled:
            logger.warning(
                f"Task worker {self.worker_id} has no registered handlers. Exiting loop."
            )
            return

        while not self.shutdown_event.is_set():  # Use self.shutdown_event
            try:
                task = None  # Initialize task variable for the outer scope
                # Database context per iteration (starts a transaction)
                if not self.engine:
                    raise RuntimeError("Database engine not initialized")
                # Split task processing into separate transactions for better isolation
                # Transaction 1: Dequeue task (commits immediately)
                task = None
                async with get_db_context(engine=self.engine) as dequeue_context:
                    logger.debug(
                        "Polling for tasks on DB context: %s",
                        dequeue_context.engine.url,
                    )
                    try:  # Inner try for dequeue
                        task = await dequeue_context.tasks.dequeue(
                            worker_id=self.worker_id,
                            task_types=task_types_handled,
                            current_time=self.clock.now(),  # Pass current time from worker's clock
                        )
                    except Exception as e:
                        logger.error(
                            f"Error during task dequeue for worker {self.worker_id}: {e}",
                            exc_info=True,
                        )
                        # Continue to next iteration without processing

                # Process task in separate transaction if one was dequeued
                if task:
                    logger.debug("Dequeued task: %s", task["task_id"])
                    self._update_last_activity()  # Update activity when starting task processing
                    try:  # Inner try for task processing
                        # Transaction 2: Process task and update status (commits immediately)
                        async with get_db_context(
                            engine=self.engine
                        ) as process_context:
                            await self._process_task(
                                process_context, task, wake_up_event
                            )
                        self._update_last_activity()  # Update after successful task processing
                        # After successful task processing, immediately continue to check for more tasks
                        # This eliminates unnecessary delays between tasks
                        continue
                    except Exception as e:
                        logger.error(
                            f"Error during task processing for worker {self.worker_id}: {e}",
                            exc_info=True,
                        )
                        # Task processing failed, continue to next iteration
                        await asyncio.sleep(TASK_POLLING_INTERVAL)
                else:
                    # No task found, wait for next poll
                    logger.debug("No tasks found, waiting for next poll")
                    await self._wait_for_next_poll(wake_up_event)
                    self._update_last_activity()  # Update after polling cycle

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


async def handle_system_event_cleanup(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """
    Task handler for cleaning up old events from the database.
    """
    # cleanup_old_events is now accessed via db_context.events.cleanup_old_events

    # Get retention hours from payload or use default
    retention_hours = payload.get("retention_hours", 48)

    logger.info(f"Starting system event cleanup (retention: {retention_hours} hours)")

    try:
        deleted_count = await exec_context.db_context.events.cleanup_old_events(
            retention_hours
        )

        logger.info(
            f"System event cleanup completed. Deleted {deleted_count} events older than {retention_hours} hours."
        )
    except Exception as e:
        logger.error(f"Error during system event cleanup: {e}", exc_info=True)
        raise


async def handle_system_error_log_cleanup(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """
    Task handler for cleaning up old error logs from the database.
    """
    # cleanup_old_error_logs is now accessed via db_context.error_logs.cleanup_old

    # Get retention days from payload or use default
    retention_days = payload.get("retention_days", 30)

    logger.info(f"Starting system error log cleanup (retention: {retention_days} days)")

    try:
        deleted_count = await exec_context.db_context.error_logs.delete_old(
            datetime.now(UTC) - timedelta(days=retention_days)
        )

        logger.info(
            f"System error log cleanup completed. Deleted {deleted_count} error logs older than {retention_days} days."
        )
    except Exception as e:
        logger.error(f"Error during system error log cleanup: {e}", exc_info=True)
        raise


async def handle_worker_task_cleanup(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Task payload is dynamic
    payload: dict[str, Any],
) -> None:
    """Task handler for cleaning up old worker task records and directories.

    This handler:
    1. Deletes old task records from the database
    2. Removes old task directories from the filesystem

    Payload can include:
        retention_hours: Override the default retention period
        workspace_path: Override the default workspace path
    """
    # Get retention hours from payload or use default from config
    retention_hours = payload.get("retention_hours", 48)
    workspace_path = payload.get("workspace_path")

    # Try to get workspace path from app config if not in payload
    if not workspace_path and exec_context.processing_service:
        app_config = exec_context.processing_service.app_config
        if app_config.ai_worker_config.enabled:
            workspace_path = app_config.ai_worker_config.workspace_mount_path

    logger.info(f"Starting worker task cleanup (retention: {retention_hours} hours)")

    db_deleted = 0
    dirs_deleted = 0

    try:
        # Step 1: Clean up database records
        db_deleted = await exec_context.db_context.worker_tasks.cleanup_old_tasks(
            retention_hours
        )

        # Step 2: Clean up old task directories from filesystem
        if workspace_path:
            tasks_dir = Path(workspace_path) / "tasks"
            if await aiofiles.os.path.exists(tasks_dir):
                cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)

                # List directories in tasks/
                for entry in await aiofiles.os.listdir(tasks_dir):
                    task_path = tasks_dir / entry
                    if await aiofiles.os.path.isdir(task_path):
                        # Check directory modification time
                        stat_info = await aiofiles.os.stat(task_path)
                        mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=UTC)

                        if mtime < cutoff:
                            # Remove old task directory
                            try:
                                await asyncio.to_thread(shutil.rmtree, task_path)
                                dirs_deleted += 1
                                logger.debug(f"Removed old task directory: {task_path}")
                            except OSError as e:
                                logger.warning(
                                    f"Failed to remove task directory {task_path}: {e}"
                                )

        logger.info(
            f"Worker task cleanup completed. "
            f"Deleted {db_deleted} database records, {dirs_deleted} task directories "
            f"older than {retention_hours} hours."
        )
    except Exception as e:
        logger.error(f"Error during worker task cleanup: {e}", exc_info=True)
        raise


async def _process_script_wake_llm(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    wake_contexts: list[dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    event_data: dict[str, Any],
    listener_id: str | None,
) -> None:
    """Process wake_llm calls accumulated during script execution.

    Args:
        exec_context: The execution context with DB access
        wake_contexts: List of wake context dictionaries from script
        event_data: The original event data that triggered the script
        listener_id: ID of the event listener that ran the script
    """

    listener_id = listener_id or "scheduled"

    # Extract attachment IDs from all wake contexts
    all_attachment_ids: list[str] = []
    for ctx in wake_contexts:
        context_dict = ctx.get("context", {})
        attachments = context_dict.get("attachments", [])
        if isinstance(attachments, list):
            all_attachment_ids.extend(attachments)

    # Fetch attachment metadata if any attachments are referenced
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    trigger_attachments: list[dict[str, Any]] | None = None
    if all_attachment_ids:
        # Get attachment registry from execution context
        attachment_registry = getattr(exec_context, "attachment_registry", None)
        if not attachment_registry:
            # Try to get from app state or create if needed
            # For now, skip attachment processing if registry not available
            logger.warning(
                "AttachmentRegistry not available for script wake_llm with attachments"
            )
        else:
            trigger_attachments = []

            for attachment_id in all_attachment_ids:
                try:
                    # Get attachment metadata
                    attachment_metadata = await attachment_registry.get_attachment(
                        db_context=exec_context.db_context,
                        attachment_id=attachment_id,
                    )

                    if attachment_metadata:
                        # Determine attachment type from MIME type
                        attachment_type = "document"  # Default fallback
                        mime_type = attachment_metadata.mime_type
                        if mime_type.startswith("image/"):
                            attachment_type = "image"
                        elif mime_type.startswith("audio/"):
                            attachment_type = "audio"
                        elif mime_type.startswith("video/"):
                            attachment_type = "video"
                        elif mime_type.startswith("text/"):
                            attachment_type = "text"
                        elif mime_type in {
                            "application/pdf",
                            "application/msword",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        }:
                            attachment_type = "document"

                        # Add to trigger_attachments list in expected format
                        trigger_attachments.append({
                            "type": attachment_type,
                            "attachment_id": attachment_metadata.attachment_id,
                            "url": attachment_metadata.content_url,
                            "content_url": attachment_metadata.content_url,
                            "mime_type": attachment_metadata.mime_type,
                            "description": attachment_metadata.description,
                            "filename": attachment_metadata.metadata.get(
                                "original_filename", "attachment"
                            ),
                            "size": attachment_metadata.size,
                        })
                        logger.debug(
                            f"Added attachment {attachment_id} to wake_llm context"
                        )
                    else:
                        logger.warning(
                            f"Attachment {attachment_id} not found for script wake_llm"
                        )
                except Exception as e:
                    logger.error(
                        f"Error fetching attachment {attachment_id} for script wake_llm: {e}"
                    )
                    # Continue with other attachments

    # Combine all wake contexts into a single message
    combined_context = {
        "source": "script_wake_llm",
        "listener_id": listener_id,
        "accumulated_contexts": wake_contexts,
    }

    # Add event data if any context requested it
    include_event = any(ctx.get("include_event", True) for ctx in wake_contexts)
    if include_event:
        combined_context["event_data"] = event_data

    # Format the wake message (excluding attachments from text since they'll be handled separately)
    wake_message = "Script wake_llm call:\n\n"

    if len(wake_contexts) == 1:
        # Single context - show it directly (filter out attachments from display)
        ctx = wake_contexts[0]
        context_for_display = {
            k: v for k, v in ctx.get("context", {}).items() if k != "attachments"
        }
        if all_attachment_ids:
            context_for_display["attachment_ids"] = all_attachment_ids
        wake_message += json.dumps(context_for_display, indent=2)
    else:
        # Multiple contexts - show them as a list
        wake_message += f"Multiple wake requests ({len(wake_contexts)}):\n"
        for i, ctx in enumerate(wake_contexts, 1):
            context_for_display = {
                k: v for k, v in ctx.get("context", {}).items() if k != "attachments"
            }
            ctx_attachments = ctx.get("context", {}).get("attachments", [])
            if ctx_attachments:
                context_for_display["attachment_ids"] = ctx_attachments
            wake_message += f"\n{i}. {json.dumps(context_for_display, indent=2)}"

    if include_event:
        wake_message += f"\n\nTriggering event:\n{json.dumps(event_data, indent=2)}"

    # Generate unique task ID for the callback
    callback_task_id = f"script_wake_llm_{listener_id}_{uuid.uuid4().hex[:8]}"

    # Get current timestamp for scheduling
    scheduling_timestamp = datetime.now(UTC).isoformat()

    # Enqueue LLM callback task with attachment support
    payload = {
        "interface_type": exec_context.interface_type,
        "conversation_id": exec_context.conversation_id,
        "user_name": exec_context.user_name,  # Preserve user_name
        "callback_context": wake_message,
        "scheduling_timestamp": scheduling_timestamp,
        "metadata": combined_context,
    }

    # Add attachments to payload if any were found
    if trigger_attachments:
        payload["trigger_attachments"] = trigger_attachments
        logger.info(
            f"Added {len(trigger_attachments)} attachments to wake_llm callback"
        )

    await exec_context.db_context.tasks.enqueue(
        task_id=callback_task_id,
        task_type="llm_callback",
        payload=payload,
    )

    logger.info(
        f"Enqueued LLM callback for script wake_llm from listener {listener_id}"
    )


async def handle_script_execution(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """
    Task handler for executing scripts triggered by events.

    Executes user-defined scripts in response to events from Home Assistant,
    document indexing, and other sources. Scripts run with restricted tool access
    based on the event_handler processing profile.

    Args:
        exec_context: Execution context providing access to tools and services
        payload: Task payload containing:
            - script_code: The Python script to execute
            - event_data: Event data to pass to the script
            - config: Optional configuration (timeout, allowed_tools)
            - listener_id: ID of the event listener that triggered this
            - conversation_id: Conversation context for the script

    Raises:
        ScriptTimeoutError: If script execution exceeds the timeout
        ScriptError: If script has syntax errors or runtime errors
    """

    # Extract required fields from payload
    script_code = payload.get("script_code")
    event_data = payload.get("event_data", {})
    config = payload.get("config", {})
    listener_id = payload.get("listener_id")
    conversation_id = payload.get("conversation_id")

    # Validate required fields
    if not script_code:
        logger.error(
            f"Invalid payload for script_execution task (missing script_code): {payload}"
        )
        raise ValueError("Missing required field in payload: script_code")

    if listener_id:
        logger.info(
            f"Starting script execution for listener {listener_id} in conversation {conversation_id}"
        )
    else:
        logger.info(
            f"Starting scheduled script execution in conversation {conversation_id}"
        )

    # Get the event_handler processing service if available
    processing_service = exec_context.processing_service
    tools_provider = None

    if processing_service and hasattr(processing_service, "tools_provider"):
        # Use the event_handler profile's tools if available
        tools_provider = processing_service.tools_provider
        logger.debug(
            f"Using tools from processing service for script execution: {processing_service.service_config.id}"
        )
    else:
        logger.warning(
            "No processing service available for script execution, tools will be unavailable"
        )

    # Create script engine with configuration
    engine_config = ScriptConfig(
        max_execution_time=config.get("timeout", 600),  # Default 10 minutes
        allowed_tools=config.get("allowed_tools"),  # None means use profile defaults
        deny_all_tools=False,  # Scripts should have tool access
        enable_print=True,  # Allow print() for debugging
        enable_debug=False,  # Could be enabled based on config
    )

    engine = MontyEngine(
        tools_provider=tools_provider,
        config=engine_config,
    )

    # Prepare global variables for the script
    script_globals = {
        "event": event_data,
        "conversation_id": conversation_id,
        "listener_id": listener_id,
        "listener_name": config.get("listener_name", ""),  # Optional listener name
        # Note: trigger_count would need to be retrieved from DB if needed
    }

    # Execute the script
    try:
        logger.debug(
            f"Executing script for listener {listener_id} with event data: {event_data}"
        )

        result = await engine.evaluate_async(
            script_code,
            globals_dict=script_globals,
            execution_context=exec_context,
        )

        logger.info(
            f"Script execution completed successfully for listener {listener_id}. "
            f"Result type: {type(result).__name__}"
        )

        # Log script output if it returned something meaningful
        if result is not None:
            logger.debug(f"Script returned: {result}")

        # Check for any wake_llm calls made during script execution
        if hasattr(engine, "get_pending_wake_contexts"):
            wake_contexts = engine.get_pending_wake_contexts()
            if wake_contexts:
                logger.info(
                    f"Script requested LLM wake with {len(wake_contexts)} context(s)"
                )

                # Process accumulated wake contexts
                await _process_script_wake_llm(
                    exec_context=exec_context,
                    wake_contexts=wake_contexts,
                    event_data=event_data,
                    listener_id=listener_id,
                )

        # Handle schedule automation recurrence if this was from a schedule automation
        await _handle_schedule_automation_recurrence(exec_context, payload)

    except ScriptTimeoutError as e:
        logger.error(
            f"Script timeout for listener {listener_id} after {e.timeout_seconds} seconds: {e}"
        )
        # Re-raise to trigger task retry with exponential backoff
        raise

    except ScriptError as e:
        logger.error(
            f"Script error for listener {listener_id}: {e}",
            exc_info=True,
        )
        # Re-raise to trigger task retry
        raise

    except Exception as e:
        # Catch any unexpected errors
        logger.error(
            f"Unexpected error during script execution for listener {listener_id}: {e}",
            exc_info=True,
        )
        # Wrap in ScriptError for consistent handling
        raise ScriptError(f"Unexpected error: {e}") from e


async def handle_reindex_document(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """
    Task handler for re-indexing a document.
    """
    document_id = payload.get("document_id")
    if not document_id:
        raise ValueError("Missing 'document_id' in reindex_document task payload.")

    db_context = exec_context.db_context
    if not db_context:
        raise ValueError("Missing DatabaseContext dependency in context.")

    # 1. Delete existing embeddings
    await db_context.vector.delete_document_embeddings(document_id)

    # 2. Get the document record
    doc_record = await db_context.vector.get_document_by_id(document_id)
    if not doc_record:
        raise ValueError(f"Document with ID {document_id} not found.")

    # 3. Enqueue a new processing task for the existing document
    task_payload = {
        "document_id": doc_record.id,
        "url_to_scrape": doc_record.source_uri,
        "doc_metadata": {"force_title_update": True},
    }

    await db_context.tasks.enqueue(
        task_id=f"reindex-doc-{doc_record.id}-{uuid.uuid4()}",
        task_type="process_uploaded_document",
        payload=task_payload,
    )


__all__ = [
    "TaskWorker",
    "handle_log_message",
    "handle_llm_callback",
    "handle_system_event_cleanup",
    "handle_system_error_log_cleanup",
    "handle_script_execution",
    "handle_reindex_document",
]  # Export class and relevant handlers
