"""Task and callback management tools.

This module contains tools for scheduling, modifying, and managing
recurring tasks and future callbacks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from dateutil import rrule
from dateutil.parser import isoparse
from sqlalchemy import select, update

from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)


# Tool Definitions
TASK_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "schedule_reminder",
            "description": (
                "Schedule a reminder to be sent at a specific time. Use this tool when users ask to be reminded of something. "
                "Supports automatic follow-up reminders if the user doesn't respond."
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
                "Use this for continuing work or checking task status, NOT for reminders. For reminders, use schedule_reminder instead."
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
            "name": "schedule_recurring_task",
            "description": (
                "Schedule a recurring LLM callback that will trigger repeatedly based on a recurrence rule (RRULE string). Use this for tasks that need to happen on a regular schedule, like daily briefings, weekly check-ins, or periodic reminders. "
                "IMPORTANT: Each recurring task creates individual callback instances that can be managed using list_pending_callbacks, modify_pending_callback, and cancel_pending_callback tools. "
                "To stop a recurring task entirely, you must cancel all its pending instances."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "initial_schedule_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "The exact date and time (ISO 8601 format with timezone, e.g., '2025-05-15T08:00:00+00:00') when the *first* instance of the callback should run."
                        ),
                    },
                    "recurrence_rule": {
                        "type": "string",
                        "description": (
                            "An RRULE string defining the recurrence schedule according to RFC 5545 (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0' for 8:00 AM daily, 'FREQ=WEEKLY;BYDAY=MO' for every Monday)."
                        ),
                    },
                    "callback_context": {
                        "type": "string",
                        "description": (
                            "The context or instructions for the LLM when the callback triggers (e.g., 'Send a morning briefing with today's calendar events and weather', 'Check if any important emails arrived')."
                        ),
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": (
                            "Optional. Maximum number of retries for each instance if it fails (default: 3)."
                        ),
                        "default": 3,
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional. A short, URL-safe description to help identify the task (e.g., 'daily_brief', 'weekly_summary')."
                        ),
                    },
                },
                "required": [
                    "initial_schedule_time",
                    "recurrence_rule",
                    "callback_context",
                ],
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
                "\n- Individual instances of recurring tasks from schedule_recurring_task"
                "\nReturns task IDs, scheduled times, and context for each pending callback."
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
            "description": "Modifies the scheduled time or context of a specific pending LLM callback task. You must provide the task_id of the callback to modify.",
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
                "\n- Cancel individual instances of recurring tasks"
                "\n- Stop a recurring task by canceling all its pending instances (use list_pending_callbacks first to find them)"
                "\nNote: This cancels only the specific task instance identified by task_id."
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
    from family_assistant import storage

    # Get interface_type, conversation_id, and db_context from the execution context object
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id
    db_context = exec_context.db_context
    clock = (
        exec_context.clock or SystemClock()
    )  # Use context's clock or default to SystemClock

    try:
        # Parse the ISO 8601 string, ensuring it's timezone-aware
        scheduled_dt = isoparse(reminder_time)
        if scheduled_dt.tzinfo is None:
            logger.warning(
                f"Reminder time '{reminder_time}' lacks timezone. Assuming {exec_context.timezone_str}."
            )
            scheduled_dt = scheduled_dt.replace(
                tzinfo=ZoneInfo(exec_context.timezone_str)
            )

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
                if unit not in ["minute", "hour", "day"]:
                    raise ValueError(f"Unknown time unit: {unit}")
            except ValueError as e:
                raise ValueError(
                    f"Invalid follow-up interval: {follow_up_interval}"
                ) from e

        task_id = f"llm_callback_{uuid.uuid4()}"
        scheduling_time = clock.now()
        payload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
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

        await storage.enqueue_task(
            db_context=db_context,
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


async def schedule_recurring_task_tool(
    exec_context: ToolExecutionContext,
    initial_schedule_time: str,
    recurrence_rule: str,
    callback_context: str,
    max_retries: int | None = 3,
    description: str | None = None,
) -> str | None:
    """
    Schedules a recurring LLM callback task.

    Args:
        exec_context: The execution context containing db_context and timezone_str.
        initial_schedule_time: ISO 8601 datetime string for the *first* run.
        recurrence_rule: RRULE string specifying the recurrence (e.g., 'FREQ=DAILY;INTERVAL=1;BYHOUR=8;BYMINUTE=0').
        callback_context: The context/instructions for the LLM when the callback triggers.
        max_retries: Maximum number of retries for each instance (default 3).
        description: A short, URL-safe description to include in the task ID (e.g., 'daily_brief').
    """
    from family_assistant import storage

    # Hardcode task type to llm_callback
    task_type = "llm_callback"

    logger.info(
        f"Executing schedule_recurring_task_tool: type='{task_type}', initial='{initial_schedule_time}', rule='{recurrence_rule}'"
    )
    db_context = exec_context.db_context
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id
    clock = exec_context.clock or SystemClock()

    try:
        # Validate recurrence rule format (basic validation)
        try:
            # We don't need dtstart here, just parsing validity
            rrule.rrulestr(recurrence_rule)
        except ValueError as rrule_err:
            raise ValueError(
                f"Invalid recurrence_rule format: {rrule_err}"
            ) from rrule_err

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
        now_aware = datetime.now(
            initial_dt.tzinfo or timezone.utc
        )  # Use parsed timezone or UTC
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

        # Build the payload for llm_callback
        scheduling_time = clock.now()
        payload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "callback_context": callback_context,
            "scheduling_timestamp": scheduling_time.isoformat(),
        }

        # Enqueue the first instance using the db_context from exec_context
        await storage.enqueue_task(
            db_context=db_context,
            task_id=initial_task_id,
            task_type=task_type,
            payload=payload,
            scheduled_at=initial_dt,
            max_retries_override=max_retries,
            recurrence_rule=recurrence_rule,
        )
        logger.info(
            f"Scheduled initial recurring task {initial_task_id} (Type: {task_type}) starting at {initial_dt} with rule '{recurrence_rule}'"
        )
        return f"OK. Recurring callback '{initial_task_id}' scheduled starting {initial_schedule_time} with rule '{recurrence_rule}'."
    except ValueError as ve:
        logger.error(f"Invalid arguments for scheduling recurring task: {ve}")
        return f"Error: Invalid arguments provided. {ve}"
    except Exception as e:
        logger.error(f"Failed to schedule recurring task: {e}", exc_info=True)
        return "Error: Failed to schedule the recurring task."


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
    from family_assistant import storage

    # Get interface_type, conversation_id, and db_context from the execution context object
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id
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
                f"Callback time '{callback_time}' lacks timezone. Assuming {exec_context.timezone_str}."
            )
            scheduled_dt = scheduled_dt.replace(
                tzinfo=ZoneInfo(exec_context.timezone_str)
            )

        # Ensure it's in the future (optional, but good practice)
        if (
            scheduled_dt <= clock.now()
        ):  # Compare against the potentially mocked clock's now
            raise ValueError("Callback time must be in the future.")

        task_id = f"llm_callback_{uuid.uuid4()}"
        scheduling_time = clock.now()  # Use the clock from context
        payload = {
            "interface_type": interface_type,  # Store interface type
            "conversation_id": conversation_id,  # Store conversation ID
            "callback_context": context,
            "scheduling_timestamp": scheduling_time.isoformat(),  # Add scheduling timestamp
        }

        await storage.enqueue_task(
            db_context=db_context,
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
    from family_assistant import storage

    db_context = exec_context.db_context
    conversation_id = exec_context.conversation_id
    interface_type = exec_context.interface_type
    timezone_str = exec_context.timezone_str
    logger.info(
        f"Executing list_pending_callbacks_tool for {interface_type}:{conversation_id}, limit={limit}"
    )

    try:
        # Assuming storage.tasks_table is the correct SQLAlchemy Table object
        # and payload is a JSON/JSONB column.
        stmt = (
            select(
                storage.tasks_table.c.task_id,
                storage.tasks_table.c.scheduled_at,
                storage.tasks_table.c.payload,
            )
            .where(
                storage.tasks_table.c.task_type == "llm_callback",
                storage.tasks_table.c.status == "pending",
                storage.tasks_table.c.payload["interface_type"].astext
                == interface_type,  # type: ignore[index]
                storage.tasks_table.c.payload["conversation_id"].astext
                == conversation_id,  # type: ignore[index]
            )
            .order_by(storage.tasks_table.c.scheduled_at.asc())
            .limit(limit)
        )

        results = await db_context.fetch_all(stmt)

        if not results:
            return "No pending LLM callbacks found for this conversation."

        formatted_callbacks = ["Pending LLM callbacks:"]
        for row_proxy in results:
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
                    scheduled_at_utc = scheduled_at_utc.replace(tzinfo=timezone.utc)
                scheduled_at_local = scheduled_at_utc.astimezone(ZoneInfo(timezone_str))
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
    from family_assistant import storage

    db_context = exec_context.db_context
    conversation_id = exec_context.conversation_id
    interface_type = exec_context.interface_type
    timezone_str = exec_context.timezone_str
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

        updates: dict[str, Any] = {}
        if new_callback_time:
            try:
                scheduled_dt = isoparse(new_callback_time)
                if scheduled_dt.tzinfo is None:
                    scheduled_dt = scheduled_dt.replace(tzinfo=ZoneInfo(timezone_str))
                if scheduled_dt <= datetime.now(timezone.utc):  # Compare with UTC now
                    raise ValueError("New callback time must be in the future.")
                updates["scheduled_at"] = scheduled_dt.astimezone(
                    timezone.utc
                )  # Store as UTC
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
    from family_assistant import storage

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
        await storage.update_task_status(
            db_context=db_context,
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
