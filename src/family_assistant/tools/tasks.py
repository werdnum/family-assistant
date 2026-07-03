"""Task and callback management tools.

This module contains tools for scheduling, modifying, and managing
one-time reminders and future callbacks. Recurring schedules should be
created via the automations framework (``create_automation``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC
from typing import TYPE_CHECKING, Any

from dateutil.parser import isoparse
from sqlalchemy import select, update

from family_assistant import storage
from family_assistant.actions import ActionType, execute_action
from family_assistant.tools.automations import validate_action_scripts
from family_assistant.tools.stored_scripts import validate_script_action_config
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.task_worker import LlmCallbackPayload
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


logger = logging.getLogger(__name__)


# Tool Definitions
TASK_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "schedule_reminder",
            "description": (
                "Schedule a reminder to be sent at a specific time. Use this tool when users ask to be reminded of something. "
                "Supports automatic follow-up reminders if the user doesn't respond.\n\n"
                "Returns: A string indicating the result. "
                "On success, returns 'OK. Reminder scheduled for [time].' or 'OK. Reminder scheduled for [time] (with follow-ups every [interval], up to [N] times).' if follow-up enabled. "
                "On parameter error, returns 'Error: Invalid reminder parameters. [details]'. "
                "On other errors, returns 'Error: Failed to schedule the reminder.'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "The exact date and time (ISO 8601 format, including timezone, e.g., '2025-05-10T14:30:00+02:00') when the reminder should be sent."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The reminder message to send (e.g., 'Take your medication', 'Call mom', 'Submit the report')."
                        ),
                    },
                    "follow_up": {
                        "type": "boolean",
                        "description": (
                            "If true, will automatically send follow-up reminders if the user doesn't respond. Use for important reminders or when user says 'don't let me forget'."
                        ),
                        "default": False,
                    },
                    "follow_up_interval": {
                        "type": "string",
                        "description": (
                            "Time between follow-up reminders (e.g., '30 minutes', '1 hour'). Only used if follow_up is true."
                        ),
                        "default": "30 minutes",
                    },
                    "max_follow_ups": {
                        "type": "integer",
                        "description": (
                            "Maximum number of follow-up reminders to send. Only used if follow_up is true."
                        ),
                        "default": 2,
                    },
                },
                "required": ["reminder_time", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_future_callback",
            "description": (
                "Schedule a future trigger for yourself (the assistant) to continue processing or check on a task at a specified time. "
                "Use this for continuing work or checking task status, NOT for reminders. For reminders, use schedule_reminder instead.\n\n"
                "Returns: A string indicating the result. "
                "On success, returns 'OK. Callback scheduled for [time].'. "
                "On invalid time format or past time, returns 'Error: Invalid callback time provided. Ensure it's a future ISO 8601 datetime with timezone. [details]'. "
                "On other errors, returns 'Error: Failed to schedule the callback.'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "callback_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "The exact date and time (ISO 8601 format, including timezone, e.g., '2025-05-10T14:30:00+02:00') when the callback should be triggered."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "The specific instructions or information you need to remember for the callback (e.g., 'Check if the download finished', 'Continue analyzing the data')."
                        ),
                    },
                },
                "required": ["callback_time", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pending_callbacks",
            "description": (
                "Lists all pending LLM callback tasks for the current conversation, including:"
                "\n- One-time callbacks from schedule_future_callback"
                "\n- Reminder callbacks from schedule_reminder"
                "\nReturns task IDs, scheduled times, and context for each pending callback.\n\n"
                "Returns: A formatted string listing pending callbacks. "
                "If callbacks exist, returns 'Pending LLM callbacks:' followed by entries with '- Task ID: [id]\n  Scheduled At: [time]\n  Context: [context preview]'. "
                "If no callbacks found, returns 'No pending LLM callbacks found for this conversation.'. "
                "On error, returns 'Error: Failed to list pending callbacks. [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Optional. Maximum number of pending callbacks to list (default: 5).",
                        "default": 5,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_pending_callback",
            "description": (
                "Modifies the scheduled time or context of a specific pending LLM callback task. You must provide the task_id of the callback to modify.\n\n"
                "Returns: A string indicating the result. "
                "On success, returns 'Callback task [task_id] modified successfully.'. "
                "If neither new_callback_time nor new_context provided, returns 'Error: You must provide either a new_callback_time or a new_context to modify.'. "
                "If task not found, returns 'Error: Callback task with ID [task_id] not found.'. "
                "If task not an LLM callback, returns 'Error: Task [task_id] is not an LLM callback task.'. "
                "If task not pending, returns 'Error: Callback task [task_id] is not pending (current status: [status]). It cannot be modified.'. "
                "If task belongs to different conversation, returns 'Error: Callback task [task_id] does not belong to this conversation. Modification denied.'. "
                "On invalid new time, returns 'Error: Invalid new_callback_time. [details]'. "
                "On other errors, returns 'Error: Failed to modify callback task. [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The unique ID of the LLM callback task to modify (obtained from list_pending_callbacks or when it was scheduled).",
                    },
                    "new_callback_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Optional. The new exact date and time (ISO 8601 format, including timezone, e.g., '2025-06-01T10:00:00-07:00') for the callback. If omitted, the time is not changed.",
                    },
                    "new_context": {
                        "type": "string",
                        "description": "Optional. The new context or instructions for the callback. If omitted, the context is not changed.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_pending_callback",
            "description": (
                "Cancels a specific pending LLM callback task by its task_id. Use this to:"
                "\n- Cancel one-time future callbacks"
                "\n- Cancel scheduled reminders"
                "\nNote: This cancels only the specific task instance identified by task_id.\n\n"
                "Returns: A string indicating the result. "
                "On success, returns 'Callback task [task_id] cancelled successfully.'. "
                "If task not found, returns 'Error: Callback task with ID [task_id] not found.'. "
                "If task not an LLM callback, returns 'Error: Task [task_id] is not an LLM callback task.'. "
                "If task not pending, returns 'Error: Callback task [task_id] is not pending (current status: [status]). It cannot be cancelled.'. "
                "If task belongs to different conversation, returns 'Error: Callback task [task_id] does not belong to this conversation. Cancellation denied.'. "
                "On other errors, returns 'Error: Failed to cancel callback task. [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The unique ID of the LLM callback task to cancel (obtained from list_pending_callbacks or when it was scheduled).",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_action",
            "description": (
                "Schedule any action (wake_llm or script) to execute at a specific time. "
                "This is the general-purpose scheduling tool that complements schedule_future_callback. "
                "Use this when you need to schedule script execution or want more control over LLM callbacks.\n\n"
                "Returns: A string indicating the result. "
                "On success, returns 'OK. [action_type] action scheduled for [schedule_time]'. "
                "If invalid action_type, returns 'Error: Invalid action_type. Must be one of: [valid types]'. "
                "If wake_llm missing context, returns 'Error: wake_llm action requires 'context' in action_config'. "
                "If script missing script_code, returns 'Error: script action requires 'script_code' in action_config'. "
                "If schedule_time in past, returns 'Error: Schedule time must be in the future'. "
                "On invalid time format, returns 'Error: Invalid schedule_time format: [details]'. "
                "On other errors, returns 'Error: Failed to schedule action. [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "When to execute the action (ISO 8601 format with timezone, e.g., '2025-05-10T14:30:00+02:00')",
                    },
                    "action_type": {
                        "type": "string",
                        "enum": ["wake_llm", "script"],
                        "description": "Type of action to execute",
                        "default": "wake_llm",
                    },
                    "action_config": {
                        "type": "object",
                        "description": (
                            "Configuration for the action. "
                            "For wake_llm: {'context': 'message for LLM'}. "
                            "For script: {'script_code': 'Python code', 'timeout': 600}"
                        ),
                    },
                },
                "required": ["schedule_time", "action_config"],
            },
        },
    },
]


# Tool Implementations
async def schedule_reminder_tool(
    exec_context: ToolExecutionContext,
    reminder_time: str,
    message: str,
    follow_up: bool = False,
    follow_up_interval: str = "30 minutes",
    max_follow_ups: int = 2,
) -> str | None:
    """
    Schedules a reminder to be sent at a specific time.

    Args:
        exec_context: The ToolExecutionContext containing chat_id, application instance, and db_context.
        reminder_time: ISO 8601 formatted datetime string (including timezone).
        message: The reminder message to send.
        follow_up: If True, will automatically send follow-up reminders if no response.
        follow_up_interval: Time between follow-ups (e.g., "30 minutes", "1 hour").
        max_follow_ups: Maximum number of follow-up reminders.
    """

    # Get interface_type, conversation_id, and db_context from the execution context object
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id
    user_name = exec_context.user_name  # Extract user_name from context
    db_context = exec_context.db_context
    clock = (
        exec_context.clock or SystemClock()
    )  # Use context's clock or default to SystemClock

    try:
        # Parse the ISO 8601 string, ensuring it's timezone-aware
        scheduled_dt = isoparse(reminder_time)
        if scheduled_dt.tzinfo is None:
            logger.warning(
                f"Reminder time '{reminder_time}' lacks timezone. Assuming {exec_context.timezone}."
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=exec_context.timezone)

        # Ensure it's in the future
        if scheduled_dt <= clock.now():
            raise ValueError("Reminder time must be in the future.")

        # Validate follow-up interval format if follow-up is enabled
        if follow_up:
            interval_parts = follow_up_interval.lower().split()
            if len(interval_parts) != 2:
                raise ValueError(
                    f"Invalid follow-up interval format: {follow_up_interval}"
                )
            try:
                int(interval_parts[0])  # Validate it's a number
                unit = interval_parts[1].rstrip("s")
                if unit not in {"minute", "hour", "day"}:
                    raise ValueError(f"Unknown time unit: {unit}")
            except ValueError as e:
                raise ValueError(
                    f"Invalid follow-up interval: {follow_up_interval}"
                ) from e

        task_id = f"llm_callback_{uuid.uuid4()}"
        scheduling_time = clock.now()
        payload: LlmCallbackPayload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "user_name": user_name,  # Save user_name in payload
            "callback_context": message,
            "scheduling_timestamp": scheduling_time.isoformat(),
            "reminder_config": {
                "is_reminder": True,
                "follow_up": follow_up,
                "follow_up_interval": follow_up_interval,
                "max_follow_ups": max_follow_ups,
                "current_attempt": 1,
            },
        }
        if exec_context.user_id is not None:
            payload["created_by_user_id"] = exec_context.user_id

        await db_context.tasks.enqueue(
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_dt,
        )

        logger.info(
            f"Scheduled reminder task {task_id} for conversation {interface_type}:{conversation_id} at {scheduled_dt}"
        )

        follow_up_msg = ""
        if follow_up:
            follow_up_msg = f" (with follow-ups every {follow_up_interval}, up to {max_follow_ups} times)"

        return f"OK. Reminder scheduled for {reminder_time}{follow_up_msg}."

    except ValueError as ve:
        logger.error(f"Invalid reminder parameters: {ve}")
        return f"Error: Invalid reminder parameters. {ve}"
    except Exception as e:
        logger.error(f"Failed to schedule reminder: {e}", exc_info=True)
        return "Error: Failed to schedule the reminder."


async def schedule_future_callback_tool(
    exec_context: ToolExecutionContext,
    callback_time: str,
    context: str,  # This is the LLM context string
) -> str | None:
    """
    Schedules a task to trigger an LLM callback in a specific chat at a future time.

    Args:
        exec_context: The ToolExecutionContext containing chat_id, application instance, and db_context.
        callback_time: ISO 8601 formatted datetime string (including timezone).
        context: The context/prompt for the future LLM callback.
    """

    # Get interface_type, conversation_id, and db_context from the execution context object
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id
    user_name = exec_context.user_name  # Extract user_name from context
    db_context = exec_context.db_context
    clock = (
        exec_context.clock or SystemClock()
    )  # Use context's clock or default to SystemClock

    try:
        # Parse the ISO 8601 string, ensuring it's timezone-aware
        scheduled_dt = isoparse(callback_time)
        if scheduled_dt.tzinfo is None:
            # Or raise error, forcing LLM to provide timezone
            logger.warning(
                f"Callback time '{callback_time}' lacks timezone. Assuming {exec_context.timezone}."
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=exec_context.timezone)

        # Ensure it's in the future (optional, but good practice)
        if (
            scheduled_dt <= clock.now()
        ):  # Compare against the potentially mocked clock's now
            raise ValueError("Callback time must be in the future.")

        task_id = f"llm_callback_{uuid.uuid4()}"
        scheduling_time = clock.now()  # Use the clock from context
        payload: LlmCallbackPayload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "user_name": user_name,
            "callback_context": context,
            "scheduling_timestamp": scheduling_time.isoformat(),
        }
        if exec_context.user_id is not None:
            payload["created_by_user_id"] = exec_context.user_id

        await db_context.tasks.enqueue(
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_dt,
        )
        logger.info(
            f"Scheduled LLM callback task {task_id} for conversation {interface_type}:{conversation_id} at {scheduled_dt}"
        )
        return f"OK. Callback scheduled for {callback_time}."
    except ValueError as ve:
        logger.error(f"Invalid callback time format or value: {callback_time} - {ve}")
        return f"Error: Invalid callback time provided. Ensure it's a future ISO 8601 datetime with timezone. {ve}"
    except Exception as e:
        logger.error(f"Failed to schedule callback task: {e}", exc_info=True)
        return "Error: Failed to schedule the callback."


async def list_pending_callbacks_tool(
    exec_context: ToolExecutionContext,
    limit: int = 5,
) -> str:
    """
    Lists pending 'llm_callback' tasks for the current conversation.

    Args:
        exec_context: The execution context.
        limit: Maximum number of callbacks to list.

    Returns:
        A string listing pending callbacks or a message if none are found.
    """
    db_context = exec_context.db_context
    conversation_id = exec_context.conversation_id
    interface_type = exec_context.interface_type
    tz = exec_context.timezone
    logger.info(
        f"Executing list_pending_callbacks_tool for {interface_type}:{conversation_id}, limit={limit}"
    )

    try:
        # Filter pending llm_callback tasks for this conversation
        # Note: We'll fetch all pending llm_callback tasks and filter in Python
        # to avoid database-specific JSON syntax issues
        stmt = (
            select(
                storage.tasks_table.c.task_id,
                storage.tasks_table.c.scheduled_at,
                storage.tasks_table.c.payload,
            )
            .where(
                storage.tasks_table.c.task_type == "llm_callback",
                storage.tasks_table.c.status == "pending",
            )
            .order_by(storage.tasks_table.c.scheduled_at.asc())
        )

        results = await db_context.fetch_all(stmt)

        # Filter results in Python to match the conversation
        filtered_results = []
        for row in results:
            payload = row.get("payload", {})
            if (
                payload.get("interface_type") == interface_type
                and payload.get("conversation_id") == conversation_id
            ):
                filtered_results.append(row)
                if len(filtered_results) >= limit:
                    break

        if not filtered_results:
            return "No pending LLM callbacks found for this conversation."

        formatted_callbacks = ["Pending LLM callbacks:"]
        for row_proxy in filtered_results:
            # row_proxy is already a Mapping[str, Any] as per fetch_all's contract
            row: Mapping[str, Any] = row_proxy

            task_id = row.get("task_id")
            scheduled_at_utc = row.get("scheduled_at")
            payload = row.get("payload", {})
            callback_context = payload.get("callback_context", "No context available.")

            scheduled_at_local_str = "Unknown time"
            if scheduled_at_utc:
                # Ensure scheduled_at_utc is timezone-aware (should be if stored correctly)
                if scheduled_at_utc.tzinfo is None:
                    scheduled_at_utc = scheduled_at_utc.replace(tzinfo=UTC)
                scheduled_at_local = scheduled_at_utc.astimezone(tz)
                scheduled_at_local_str = scheduled_at_local.strftime(
                    "%Y-%m-%d %H:%M:%S %Z"
                )

            formatted_callbacks.append(
                f"- Task ID: {task_id}\n  Scheduled At: {scheduled_at_local_str}\n  Context: {callback_context[:100]}{'...' if len(callback_context) > 100 else ''}"
            )
        return "\n".join(formatted_callbacks)

    except Exception as e:
        logger.error(
            f"Error listing pending callbacks for {interface_type}:{conversation_id}: {e}",
            exc_info=True,
        )
        return f"Error: Failed to list pending callbacks. {e}"


async def modify_pending_callback_tool(
    exec_context: ToolExecutionContext,
    task_id: str,
    new_callback_time: str | None = None,
    new_context: str | None = None,
) -> str:
    """
    Modifies the scheduled time or context of a pending 'llm_callback' task.

    Args:
        exec_context: The execution context.
        task_id: The ID of the callback task to modify.
        new_callback_time: Optional. New ISO 8601 time for the callback.
        new_context: Optional. New context string for the callback.

    Returns:
        A string confirming modification or an error message.
    """
    db_context = exec_context.db_context
    conversation_id = exec_context.conversation_id
    interface_type = exec_context.interface_type
    tz = exec_context.timezone
    clock = exec_context.clock or SystemClock()
    logger.info(
        f"Executing modify_pending_callback_tool for task_id='{task_id}' in {interface_type}:{conversation_id}"
    )

    if not new_callback_time and not new_context:
        return "Error: You must provide either a new_callback_time or a new_context to modify."

    try:
        # Fetch the task to verify ownership and status
        task_stmt = select(storage.tasks_table).where(
            storage.tasks_table.c.task_id == task_id
        )
        task_row_proxy = await db_context.fetch_one(task_stmt)

        if not task_row_proxy:
            return f"Error: Callback task with ID '{task_id}' not found."

        # task_row_proxy is already a Mapping[str, Any] as per fetch_one's contract
        task: Mapping[str, Any] = task_row_proxy

        if task.get("task_type") != "llm_callback":
            return f"Error: Task '{task_id}' is not an LLM callback task."
        if task.get("status") != "pending":
            return f"Error: Callback task '{task_id}' is not pending (current status: {task.get('status')}). It cannot be modified."

        task_payload = task.get("payload", {})
        if (
            task_payload.get("interface_type") != interface_type
            or task_payload.get("conversation_id") != conversation_id
        ):
            return f"Error: Callback task '{task_id}' does not belong to this conversation. Modification denied."

        # ast-grep-ignore: no-dict-any - task update fields vary per operation
        updates: dict[str, Any] = {}
        if new_callback_time:
            try:
                scheduled_dt = isoparse(new_callback_time)
                if scheduled_dt.tzinfo is None:
                    scheduled_dt = scheduled_dt.replace(tzinfo=tz)
                if scheduled_dt <= clock.now():  # Use clock from context
                    raise ValueError("New callback time must be in the future.")
                updates["scheduled_at"] = scheduled_dt.astimezone(UTC)  # Store as UTC
            except ValueError as ve:
                return f"Error: Invalid new_callback_time. {ve}"

        if new_context:
            new_payload = task_payload.copy()
            new_payload["callback_context"] = new_context
            updates["payload"] = new_payload

        if not updates:
            return "No valid modifications specified."

        # Perform the update
        update_stmt = (
            update(storage.tasks_table)
            .where(storage.tasks_table.c.task_id == task_id)
            .values(**updates)
        )
        result = await db_context.execute_with_retry(update_stmt)

        if result and result.rowcount > 0:  # type: ignore
            # Notification happens automatically in enqueue_task when tasks are updated
            return f"Callback task '{task_id}' modified successfully."
        else:
            # This case should ideally not be reached if fetch_one found the task
            return f"Error: Failed to modify callback task '{task_id}'. It might have been processed or deleted."

    except Exception as e:
        logger.error(
            f"Error modifying callback task '{task_id}' for {interface_type}:{conversation_id}: {e}",
            exc_info=True,
        )
        return f"Error: Failed to modify callback task. {e}"


async def cancel_pending_callback_tool(
    exec_context: ToolExecutionContext, task_id: str
) -> str:
    """
    Cancels a pending 'llm_callback' task.

    Args:
        exec_context: The execution context.
        task_id: The ID of the callback task to cancel.

    Returns:
        A string confirming cancellation or an error message.
    """
    db_context = exec_context.db_context
    conversation_id = exec_context.conversation_id
    interface_type = exec_context.interface_type
    logger.info(
        f"Executing cancel_pending_callback_tool for task_id='{task_id}' in {interface_type}:{conversation_id}"
    )

    try:
        # Fetch the task to verify ownership and status
        task_stmt = select(storage.tasks_table).where(
            storage.tasks_table.c.task_id == task_id
        )
        task_row_proxy = await db_context.fetch_one(task_stmt)

        if not task_row_proxy:
            return f"Error: Callback task with ID '{task_id}' not found."

        # task_row_proxy is already a Mapping[str, Any] as per fetch_one's contract
        task: Mapping[str, Any] = task_row_proxy

        if task.get("task_type") != "llm_callback":
            return f"Error: Task '{task_id}' is not an LLM callback task."
        if task.get("status") != "pending":
            return f"Error: Callback task '{task_id}' is not pending (current status: {task.get('status')}). It cannot be cancelled."

        task_payload = task.get("payload", {})
        if (
            task_payload.get("interface_type") != interface_type
            or task_payload.get("conversation_id") != conversation_id
        ):
            return f"Error: Callback task '{task_id}' does not belong to this conversation. Cancellation denied."

        # Mark as 'failed' with a specific error message indicating cancellation
        await db_context.tasks.update_status(
            task_id=task_id,
            status="failed",  # Using 'failed' as 'cancelled' might not be a standard status
            error="Callback cancelled by user.",
        )
        return f"Callback task '{task_id}' cancelled successfully."

    except Exception as e:
        logger.error(
            f"Error cancelling callback task '{task_id}' for {interface_type}:{conversation_id}: {e}",
            exc_info=True,
        )
        return f"Error: Failed to cancel callback task. {e}"


async def schedule_action_tool(
    exec_context: ToolExecutionContext,
    schedule_time: str,
    action_type: str = "wake_llm",
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
    action_config: dict[str, Any] | None = None,
) -> str:
    """Schedule any action for future execution.

    Args:
        exec_context: The ToolExecutionContext containing execution details
        schedule_time: ISO 8601 formatted datetime string (including timezone)
        action_type: Type of action to execute ("wake_llm" or "script")
        action_config: Configuration for the action

    Returns:
        Success message or error description
    """
    if action_config is None:
        action_config = {}

    # Validate action type using enum
    try:
        action_type_enum = ActionType(action_type)
    except ValueError:
        return f"Error: Invalid action_type. Must be one of: {[e.value for e in ActionType]}"

    # Validate action config based on type
    if action_type_enum == ActionType.WAKE_LLM and "context" not in action_config:
        return "Error: wake_llm action requires 'context' in action_config"
    elif action_type_enum == ActionType.SCRIPT:
        script_error = await validate_script_action_config(
            exec_context.db_context, action_config
        )
        if script_error:
            return f"Error: {script_error}"
        validation_error = await validate_action_scripts(exec_context, action_config)
        if validation_error:
            return f"Error: {validation_error}"

    # Parse and validate time
    clock = exec_context.clock or SystemClock()
    try:
        scheduled_dt = isoparse(schedule_time)
        if scheduled_dt.tzinfo is None:
            logger.warning(
                f"Schedule time lacks timezone, assuming {exec_context.timezone}"
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=exec_context.timezone)

        if scheduled_dt <= clock.now():
            return "Error: Schedule time must be in the future"
    except ValueError as e:
        return f"Error: Invalid schedule_time format: {e}"

    # Use the shared action executor with scheduling
    try:
        default_profile_id = (
            exec_context.processing_service.app_config.default_service_profile_id
            if exec_context.processing_service
            else None
        )
        await execute_action(
            db_ctx=exec_context.db_context,
            action_type=action_type_enum,
            action_config=action_config,
            conversation_id=exec_context.conversation_id,
            interface_type=exec_context.interface_type,
            user_name=exec_context.user_name,  # Pass user_name
            context={"scheduled_via": "schedule_action tool"},
            scheduled_at=scheduled_dt,
            processing_profile_id=exec_context.processing_profile_id,
            created_by_user_id=exec_context.user_id,
            default_profile_id=default_profile_id,
        )

        return f"OK. {action_type} action scheduled for {schedule_time}"
    except Exception as e:
        logger.error(f"Error scheduling action: {e}", exc_info=True)
        return f"Error: Failed to schedule action. {e}"
