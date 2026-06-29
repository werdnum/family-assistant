"""
Task worker implementation for background processing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import traceback
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta  # Added Union
from pathlib import Path
from typing import TYPE_CHECKING, Any, Required, TypedDict, cast

import aiofiles.os
from dateutil import rrule
from dateutil.parser import isoparse
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from sqlalchemy import func, select, update

# Removed storage import - using repository pattern
from family_assistant.a2a.client import (
    A2AClientError,
    A2APermanentError,
    A2ATaskNotFoundError,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageAttachmentMetadata,
    SystemMessage,
    UserMessage,
)
from family_assistant.processing import (
    PENDING,
    PollableDelegationService,
    ProcessingService,
    RemoteSubmission,
)
from family_assistant.scripting import (
    MontyEngine,
    ScriptError,
    ScriptTimeoutError,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.storage.delegation_runs import TERMINAL_DELEGATION_STATUSES
from family_assistant.tools.services import short_error_summary
from family_assistant.tools.types import CalendarConfig, EventSourcesById

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from zoneinfo import ZoneInfo

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.processing.types import (
        ChatInteractionResult,
        RequestConfirmationCallback,
    )
    from family_assistant.scripting.monty_engine import WakeRequest
    from family_assistant.services.confirmation_waiters import (
        ConfirmationResultWaiterRegistry,
    )
    from family_assistant.services.notifier import Notifier
    from family_assistant.storage.repositories.confirmation_requests import (
        ConfirmationRequestRow,
    )
    from family_assistant.storage.repositories.delegation_runs import DelegationRunDict
    from family_assistant.storage.types import MessageHistoryRow, TaskDict
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools import ToolsProvider
    from family_assistant.web.conversation_stream_hub import ConversationStreamHub

# handle_index_email is now a method of EmailIndexer and registered in __main__.py
from family_assistant.processing.utils import get_file_extension_from_mime_type
from family_assistant.services.deferred_tool_confirmation import (
    build_deferred_confirmation_callback,
)
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import (
    enqueue_task,
    notify_other_workers,
    register_worker_wake_event,
    tasks_table,
    unregister_worker_wake_event,
)
from family_assistant.tools import ToolExecutionContext
from family_assistant.tools.confirmation import TOOL_CONFIRMATION_RENDERERS
from family_assistant.tools.stored_scripts import AUTOMATION_RUNTIME_GLOBALS
from family_assistant.tools.types import (
    ConfirmationOutcome,
    RequestConfirmationCallback,
    ToolResult,
)
from family_assistant.utils.clock import Clock, SystemClock

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ReminderConfig(TypedDict, total=False):
    """Configuration for reminder follow-up behavior."""

    is_reminder: bool
    follow_up: bool
    follow_up_interval: str
    max_follow_ups: int
    current_attempt: int


class LlmCallbackPayload(TypedDict, total=False):
    """Payload for llm_callback tasks.

    Fields marked Required must be present in every llm_callback payload.
    The type checker enforces this at construction sites annotated with this type.
    """

    interface_type: Required[str]
    conversation_id: Required[str]
    # ast-grep-ignore: no-dict-any - actions.py passes a dict with trigger context
    callback_context: Required[str | dict[str, Any]]
    scheduling_timestamp: Required[str]
    user_name: str
    trigger_attachments: list[MessageAttachmentMetadata]
    reminder_config: ReminderConfig
    # ast-grep-ignore: no-dict-any - Arbitrary context metadata from script wake_llm calls
    metadata: dict[str, Any]
    automation_id: str | int
    automation_type: str
    # Owner of the schedule/reminder. Confirm-gated tool calls made on the woken
    # turn are deferred to a durable confirmation addressed to this user. Absent
    # for legacy tasks queued before this field existed, in which case confirm-gated
    # tools cannot be approved.
    created_by_user_id: str


class ScriptExecutionPayload(TypedDict, total=False):
    """Payload for script_execution tasks."""

    script_code: str
    script_name: str
    # ast-grep-ignore: no-dict-any - User-defined parameters passed as script globals
    script_parameters: dict[str, Any]
    # ast-grep-ignore: no-dict-any - Event data from external sources (Home Assistant, webhooks) with arbitrary structure
    event_data: dict[str, Any]
    # ast-grep-ignore: no-dict-any - User-defined script configuration with arbitrary keys (timeout, allowed_tools, etc.)
    config: dict[str, Any]
    listener_id: str
    conversation_id: str
    interface_type: str
    automation_id: str | int
    automation_type: str
    task_name: str
    # Creator provenance: scripts run under the profile (and on behalf of the
    # user) that created the automation, so validation and execution agree.
    processing_profile_id: str
    created_by_user_id: str


class SystemEventCleanupPayload(TypedDict, total=False):
    """Payload for system_event_cleanup tasks."""

    retention_hours: int


class SystemErrorLogCleanupPayload(TypedDict, total=False):
    """Payload for system_error_log_cleanup tasks."""

    retention_days: int


class WorkerTaskCleanupPayload(TypedDict, total=False):
    """Payload for worker_task_cleanup tasks."""

    retention_hours: int
    workspace_path: str


class CompletedAutomationCleanupPayload(TypedDict, total=False):
    """Payload for completed_automation_cleanup tasks."""

    retention_hours: int


class ScheduleAutomationAdvancePayload(TypedDict, total=False):
    """Payload for retryable schedule automation advancement tasks."""

    automation_id: Required[str]
    source_task_id: Required[str]
    execution_time: Required[str]
    schedule_next: bool


@dataclass(frozen=True)
class ScheduleAutomationAdvanceRequest:
    """Internal request to enqueue schedule advancement after source commit."""

    automation_id: str
    source_task_id: str
    execution_time: datetime
    schedule_next: bool = True


class ReindexDocumentPayload(TypedDict, total=False):
    """Payload for reindex_document tasks."""

    document_id: int


class ConfirmationToolExecutionPayload(TypedDict, total=False):
    """Payload for durable confirmation tool execution tasks."""

    confirmation_request_id: str


class DelegatedProfileRunPayload(TypedDict):
    """Payload for delegated_profile_run tasks."""

    delegation_id: str
    interface_type: str
    conversation_id: str
    user_name: str


class DelegationRunCleanupPayload(TypedDict, total=False):
    """Payload for delegation_run_cleanup tasks."""

    running_timeout_seconds: float


class DelegationPollPayload(TypedDict):
    """Payload for delegation_poll tasks (one poll of an awaiting_remote run)."""

    delegation_id: str
    interface_type: str
    conversation_id: str
    user_name: str


# Task type for the per-run, self-rescheduling poll of an awaiting_remote
# delegation (the submit-then-poll A2A path).
DELEGATION_POLL_TASK_TYPE = "delegation_poll"
SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE = "schedule_automation_advance"
SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY = "_schedule_automation_advance"

# Delegation runs left "running" longer than this are considered stranded (a
# crash or exhausted retries) and are failed + notified by the reaper. It must
# comfortably exceed the handler timeout plus retry backoff so it never reaps a
# run that is still legitimately executing.
DELEGATION_RUN_STALE_SECONDS = 3600.0

# Submit-then-poll tuning for awaiting_remote delegations. The base interval is
# applied with light exponential backoff up to the max; a run is given up on
# (remote cancelled, run failed + notified) once it has been awaiting longer
# than the wall-clock cap. These are module-level defaults; per-remote overrides
# come from RemoteServiceConfig.
DELEGATION_POLL_INTERVAL_SECONDS = 10.0
DELEGATION_POLL_MAX_INTERVAL_SECONDS = 60.0
DELEGATION_MAX_ASYNC_SECONDS = 3600.0
# Default grace after which a first submit has returned (mirrors the
# RemoteServiceConfig.timeout_seconds default); used by the reaper to recover a
# stuck NULL-id run without racing an in-flight submit.
DELEGATION_SUBMIT_GRACE_SECONDS = 300.0


def _delegation_poll_backoff(attempts: int, base_interval: float) -> float:
    """Light exponential backoff for poll rescheduling, capped at the max."""
    delay = base_interval * (2 ** min(max(attempts - 1, 0), 5))
    return min(delay, DELEGATION_POLL_MAX_INTERVAL_SECONDS)


def _as_aware_utc(value: datetime) -> datetime:
    """Treat a tz-naive datetime (e.g. read back from SQLite) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class NonRetryableTaskError(RuntimeError):
    """Raised when task failure handling should skip retries."""


def _parse_payload_datetime(value: str, field_name: str) -> datetime:
    """Parse an ISO datetime payload field as aware UTC."""
    try:
        parsed = isoparse(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO datetime string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _poll_interval_for(target_service: PollableDelegationService) -> float:
    """Base poll interval for a remote target (config override or default)."""
    return getattr(
        target_service.service_config,
        "poll_interval_seconds",
        DELEGATION_POLL_INTERVAL_SECONDS,
    )


def _max_async_for(target_service: PollableDelegationService) -> float:
    """Wall-clock cap for a remote target (config override or default)."""
    return getattr(
        target_service.service_config,
        "max_async_seconds",
        DELEGATION_MAX_ASYNC_SECONDS,
    )


def _submit_grace_for(target_service: PollableDelegationService) -> float:
    """Time after which a first submit has definitely returned for a target.

    The per-HTTP-call timeout: past it, a run still at a NULL remote id is stuck
    (its only poll was lost), not mid-submit, so the reaper may safely re-submit
    it without racing an in-flight submit into a duplicate.
    """
    return getattr(
        target_service.service_config,
        "timeout_seconds",
        DELEGATION_SUBMIT_GRACE_SECONDS,
    )


# Interfaces whose clients read the conversation from durable message history
# (web SSE / polling, native iOS, the non-streaming API) rather than receiving
# a pushed chat message. Terminal delegation notifications for these are stored
# in history + push-notified + stream-tickled, never sent via a ChatInterface
# (which for these would be NullChatInterface and silently drop the result).
_HISTORY_NOTIFICATION_INTERFACES = frozenset({"web", "api", "ios"})


class ConfirmationNotificationError(RuntimeError):
    """Raised when a confirmation result notification cannot be delivered."""


class DelegationNotificationError(RuntimeError):
    """Raised when a terminal delegation notification cannot be delivered.

    Rolls back the isolated notification transaction so ``notified_at`` stays
    NULL and the run is retried instead of being recorded as delivered.
    """


async def _schedule_reminder_follow_up(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - callback_context from LlmCallbackPayload can be str or dict
    original_context: str | dict[str, Any],
    follow_up_interval: str,
    current_attempt: int,
    max_follow_ups: int,
    created_by_user_id: str | None = None,
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
    payload: LlmCallbackPayload = {
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
    if created_by_user_id is not None:
        payload["created_by_user_id"] = created_by_user_id

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
CONFIRMATION_CANCELLATION_CLEANUP_TIMEOUT = 5.0

# Per-task-type handler timeout overrides (seconds). Task types not listed here
# fall back to ``handler_timeout`` (TASK_HANDLER_TIMEOUT). A delegated profile run
# can park waiting on a human confirmation far longer than 300s; raising its timeout
# is safe now that a pool of workers keeps servicing the queue (the confirmation
# approval enqueues a separate task that a sibling worker runs), so the parked run
# is unblocked rather than starving other background work.
#
# Note: ``delegated_profile_run`` is registered on a feature branch and may not exist
# on every deployment yet. Listing it here is forward-compatible and harmless: the
# override only applies when a task of that type is actually processed.
DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES: dict[str, float] = {
    "delegated_profile_run": 600,  # 10 minutes for confirmation-gated delegated runs
}

# --- Events for coordination (can remain module-level) ---
# Note: shutdown_event removed - each TaskWorker instance now has its own
new_task_event = asyncio.Event()  # Event to notify worker of immediate tasks

# --- Task Handler Functions (remain module-level for now) ---
# These functions will be registered with the TaskWorker instance.


# Example Task Handler (no external dependencies)
async def handle_log_message(
    db_context: DatabaseContext,
    # ast-grep-ignore: no-dict-any - Debug handler that accepts arbitrary payloads for logging
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
    exec_context: ToolExecutionContext,
    payload: LlmCallbackPayload,
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
        if isinstance(reminder_service, ProcessingService):
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
            .where(message_history_table.c.is_internal.is_(False))
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
        clock.now().astimezone(exec_context.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
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
        await db_context.message_history.add_message(
            SystemMessage(content=trigger_text),
            interface_type=interface_type,
            conversation_id=conversation_id,
            turn_id=callback_turn_id,
            timestamp=callback_trigger_timestamp,
        )
        logger.info(
            f"Saved system trigger message for callback {callback_turn_id} to history."
        )

        # Call the ProcessingService.
        # NOTE: `handle_chat_interaction` now handles saving of all messages in the turn.
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=chat_interface,
            chat_interfaces=exec_context.chat_interfaces,
            confirmation_ui_managers=exec_context.confirmation_ui_managers,
            interface_type=interface_type,
            conversation_id=conversation_id,
            # turn_id is generated within handle_chat_interaction
            trigger_content_parts=[{"type": "text", "text": trigger_text}],
            trigger_interface_message_id=None,  # System trigger
            user_name=exec_context.user_name,  # Use preserved user name from context
            replied_to_interface_id=None,  # Not a reply
            request_confirmation_callback=build_deferred_confirmation_callback(
                target_user_id=payload.get("created_by_user_id"),
                source_prefix="From a scheduled action — approve to run:",
                missing_owner_message=lambda tool_name: (
                    "This scheduled action has no recorded owner, so the "
                    f"confirm-gated tool '{tool_name}' cannot be approved and "
                    "was not run."
                ),
            ),
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

        if processing_error_traceback:
            logger.error(
                f"LLM callback had processing errors for {interface_type}:{conversation_id}"
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

        if processing_error_traceback:
            error_message = (
                f"LLM callback failed. Traceback: {processing_error_traceback}"
            )
            if sent_message_id_str:
                raise NonRetryableTaskError(
                    f"LLM callback failed after delivering error reply. "
                    f"Traceback: {processing_error_traceback}"
                )
            raise RuntimeError(error_message)

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
                    created_by_user_id=payload.get("created_by_user_id"),
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

        # Check if we should fail the task due to missing generated content
        if not final_llm_content_to_send and not is_reminder:
            # For non-reminder callbacks, we expect content to be generated
            logger.error(
                f"No content generated for non-reminder callback in {interface_type}:{conversation_id}"
            )
            raise RuntimeError("LLM failed to generate response content for callback.")

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
        raise


class TaskWorker:
    """Manages the task processing loop and handler registry."""

    def __init__(
        self,
        processing_service: ProcessingService,
        chat_interface: ChatInterface,
        calendar_config: CalendarConfig | None,
        timezone: ZoneInfo,
        embedding_generator: EmbeddingGenerator,
        shutdown_event_instance: asyncio.Event | None = None,  # Made optional
        clock: Clock | None = None,
        indexing_source: IndexingSource | None = None,
        engine: AsyncEngine
        | None = None,  # Add engine parameter for dependency injection
        event_sources: EventSourcesById | None = None,
        handler_timeout: float = TASK_HANDLER_TIMEOUT,  # Configurable timeout per instance
        handler_timeout_overrides: dict[str, float] | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        notification_dispatcher: Notifier | None = None,
        stream_hub: ConversationStreamHub | None = None,
    ) -> None:
        """Initializes the TaskWorker with its dependencies."""
        self.processing_service = processing_service
        self.chat_interface = chat_interface
        self.chat_interfaces = chat_interfaces
        self.confirmation_result_waiters = confirmation_result_waiters
        self.confirmation_ui_managers = confirmation_ui_managers
        self.notification_dispatcher = notification_dispatcher
        self.stream_hub = stream_hub
        # Strong references to in-flight stream-hub publish tasks scheduled from
        # on_commit hooks, so the event loop doesn't garbage-collect them mid-flight.
        self._hub_publish_tasks: set[asyncio.Task[object]] = set()
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
        self.timezone = timezone
        self.embedding_generator = embedding_generator
        self.clock = (
            clock if clock is not None else SystemClock()
        )  # Store the clock instance
        self.indexing_source = indexing_source
        self.event_sources = event_sources  # Store event sources
        self.engine = engine  # Store the engine for database operations
        self.handler_timeout = handler_timeout  # Store timeout per instance
        # Per-task-type timeout overrides; falls back to handler_timeout when a
        # task type is not present. Defaults give confirmation-gated delegated runs
        # a longer budget (see DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES).
        self.handler_timeout_overrides: dict[str, float] = (
            dict(DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES)
            if handler_timeout_overrides is None
            else dict(handler_timeout_overrides)
        )
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

    def _timeout_for_task_type(self, task_type: str) -> float:
        """Resolve the handler timeout for a task type.

        Returns the per-task-type override if one is configured, otherwise the
        instance default ``handler_timeout``.
        """
        return self.handler_timeout_overrides.get(task_type, self.handler_timeout)

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

    async def handle_delegated_profile_run(
        self,
        exec_context: ToolExecutionContext,
        payload: DelegatedProfileRunPayload,
    ) -> None:
        """Execute a durable asynchronous profile delegation run."""
        delegation_id = payload.get("delegation_id")
        if not delegation_id:
            raise ValueError("delegated_profile_run payload missing delegation_id")

        clock = exec_context.clock or self.clock
        run = await exec_context.db_context.delegation_runs.get_by_delegation_id(
            delegation_id
        )
        if run is None:
            raise ValueError(f"Delegation run '{delegation_id}' not found")

        if run["status"] in TERMINAL_DELEGATION_STATUSES:
            # A terminal run is re-entered only when a prior attempt's
            # notification delivery failed; retry it (keyed on notified_at).
            if run["notified_at"] is None:
                await self._notify_delegation_if_needed(exec_context, run)
            else:
                logger.info(
                    "Delegation run %s already terminal (%s) and notified.",
                    delegation_id,
                    run["status"],
                )
            return

        if run["status"] == "running":
            # A previous attempt marked the run running but did not finish (crash
            # or handler timeout). The delegated turn has non-idempotent external
            # side effects, so fail it rather than re-execute.
            logger.warning(
                "Delegation run %s was already running on entry; a prior attempt "
                "was interrupted. Failing without re-executing to avoid duplicate "
                "side effects.",
                delegation_id,
            )
            await self._fail_delegation_run(
                exec_context,
                delegation_id=delegation_id,
                error=(
                    "The delegated run was interrupted before completion and "
                    "cannot be safely retried."
                ),
            )
            return

        processing_service = exec_context.processing_service
        registry = (
            processing_service.processing_services_registry
            if processing_service is not None
            else None
        )
        target_service = registry.get(run["target_service_id"]) if registry else None
        if target_service is None:
            await self._fail_delegation_run(
                exec_context,
                delegation_id=delegation_id,
                error=f"Target service profile '{run['target_service_id']}' not found.",
            )
            return

        allowed_sources = target_service.service_config.allowed_delegation_sources
        if (
            allowed_sources is not None
            and run["source_profile_id"] not in allowed_sources
        ):
            await self._fail_delegation_run(
                exec_context,
                delegation_id=delegation_id,
                error=(
                    f"Profile '{run['source_profile_id']}' is no longer permitted "
                    f"to delegate to '{run['target_service_id']}'."
                ),
            )
            return

        if isinstance(target_service, PollableDelegationService):
            # Remote (pollable) target: submit without blocking and hand off to
            # the self-rescheduling poll task instead of running the turn inline.
            if run["status"] == "awaiting_remote":
                # A retry of a run that already claimed awaiting_remote.
                if run["remote_task_id"]:
                    # We already learned the remote-assigned id, so re-attach by
                    # enqueuing a poll rather than re-submitting (which would
                    # create a duplicate remote task).
                    await self._enqueue_delegation_poll(
                        exec_context,
                        run,
                        delay_seconds=_poll_interval_for(target_service),
                    )
                else:
                    # No remote id yet — the first submit's response was lost;
                    # re-submit to (re)create the task and learn its id.
                    await self._resubmit_awaiting_remote(
                        exec_context, run, target_service
                    )
            else:
                await self._submit_pollable_delegation(
                    exec_context, run, target_service
                )
            return

        # Commit the running transition in its own transaction so the waiting
        # caller sees it and no row lock is held across the long delegated turn.
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            started = await isolated_db.delegation_runs.mark_running(
                delegation_id,
                clock.now(),
            )
        if started is None:
            # The conditional mark_running matched no row: the run is no longer
            # queued — the stale-run reaper failed it, or a sibling worker
            # claimed it first — so do not (re-)execute the delegated turn here.
            logger.warning(
                "Delegation run %s was no longer queued when claiming it for "
                "execution (reaper or sibling worker raced); skipping.",
                delegation_id,
            )
            return

        try:
            content_parts = cast(
                "list[ContentPartDict]",
                run["content_parts_json"],
            )
            chat_interface = self._chat_interface_for_interface(
                exec_context,
                run["interface_type"],
            )
            request_confirmation_callback = (
                self._build_delegation_confirmation_callback(exec_context, run)
            )
            async with exec_context.db_context.create_isolated_context() as run_db:
                result = await target_service.handle_chat_interaction(
                    db_context=run_db,
                    interface_type=run["interface_type"],
                    conversation_id=run["conversation_id"],
                    trigger_content_parts=content_parts,
                    trigger_interface_message_id=None,
                    user_name=run["user_name"] or exec_context.user_name,
                    user_id=run["user_id"],
                    replied_to_interface_id=None,
                    chat_interface=chat_interface,
                    chat_interfaces=exec_context.chat_interfaces,
                    confirmation_ui_managers=exec_context.confirmation_ui_managers,
                    request_confirmation_callback=request_confirmation_callback,
                    subconversation_id=run["subconversation_id"],
                )
        except Exception:
            # A timeout cancellation (CancelledError) is intentionally NOT caught
            # here: it propagates so the task is retried, and the retry's
            # "running" entry guard fails the run without re-executing. The
            # stale-run reaper is the backstop if no retry occurs.
            error = traceback.format_exc()
            logger.error(
                "Delegated profile run %s raised during execution.",
                delegation_id,
                exc_info=True,
            )
            await self._fail_delegation_run(
                exec_context,
                delegation_id=delegation_id,
                error=error,
            )
            return

        await self._finalize_delegation_run(exec_context, delegation_id, result)

    async def _finalize_delegation_run(
        self,
        exec_context: ToolExecutionContext,
        delegation_id: str,
        result: ChatInteractionResult,
    ) -> None:
        """Persist a delegation run's terminal result and notify if needed.

        Shared by the inline (local) path and the poll (remote) path. The
        terminal transition is an atomic CAS on non-terminal status, so a poll
        that finishes after the cleanup reaper already failed the same run loses
        the race (``None``) and does not resurrect/overwrite it or double-notify.
        """
        clock = exec_context.clock or self.clock
        completed_at = clock.now()
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            if result.error_traceback:
                terminal_run = await isolated_db.delegation_runs.mark_failed(
                    delegation_id=delegation_id,
                    error=result.error_traceback,
                    completed_at=completed_at,
                )
            else:
                terminal_run = await isolated_db.delegation_runs.mark_completed(
                    delegation_id=delegation_id,
                    result_text=result.text_reply,
                    result_attachment_ids=result.attachment_ids or [],
                    completed_at=completed_at,
                )
        if terminal_run is None:
            # Already terminal (a concurrent reaper/poll won) or gone; the winner
            # delivers the notification.
            logger.info(
                "Delegation run %s was already terminal when finalizing; skipping.",
                delegation_id,
            )
            return
        await self._notify_delegation_if_needed(exec_context, terminal_run)

    async def _submit_pollable_delegation(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        target_service: PollableDelegationService,
    ) -> None:
        """Submit a queued run to a remote (pollable) target and hand off to poll.

        Claims ``queued -> awaiting_remote`` (with a NULL remote task id, since
        the remote assigns the id) BEFORE submitting, so the retry-guard prevents
        a duplicate concurrent submit and the wall-clock cap starts. Per A2A spec
        §3.4.2 the submit carries no client task id; the remote-assigned id from
        the response is reconciled into the run. A submit whose response is lost
        leaves the run ``awaiting_remote`` with a NULL id, which the next poll
        recovers by re-submitting. Finalizes inline if a synchronous remote
        returned a terminal task on submit.
        """
        delegation_id = run["delegation_id"]
        clock = exec_context.clock or self.clock
        content_parts = cast("list[ContentPartDict]", run["content_parts_json"])

        remote_context_id = target_service.remote_context_id(
            run["conversation_id"], run["subconversation_id"]
        )
        # Claim queued -> awaiting_remote (NULL id) before submit; the real id is
        # reconciled from the submit response in _after_submission.
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            awaiting = await isolated_db.delegation_runs.mark_awaiting_remote(
                delegation_id,
                remote_task_id=None,
                remote_context_id=remote_context_id,
                started_at=clock.now(),
            )
        if awaiting is None:
            # No longer queued — the reaper failed it or a sibling worker claimed
            # it first. Do not submit.
            logger.warning(
                "Delegation run %s was no longer queued when claiming it for "
                "async submit (reaper or sibling worker raced); skipping.",
                delegation_id,
            )
            return

        try:
            submission = await target_service.submit_async(
                content_parts,
                conversation_id=run["conversation_id"],
                subconversation_id=run["subconversation_id"],
            )
        except Exception as exc:
            await self._handle_submit_failure(
                exec_context, awaiting, target_service, exc
            )
            return

        await self._after_submission(exec_context, awaiting, target_service, submission)

    async def _handle_submit_failure(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        target_service: PollableDelegationService,
        exc: Exception,
        *,
        poll_delay_seconds: float | None = None,
    ) -> None:
        """Classify a failed (re-)submit: poll to reconcile, or fail fast.

        A transient transport failure (``A2AClientError`` that is not permanent)
        keeps the run ``awaiting_remote`` and schedules a poll: the request may
        have reached the remote (response lost), so a poll re-attaches, or — if
        the task was never created — finds it missing and re-submits. A permanent
        error (bad auth / protocol) or an unexpected non-A2A error (e.g. a code or
        conversion bug) fails the run fast with the real error rather than waiting
        out the cap. ``poll_delay_seconds`` overrides the transient-retry poll
        delay (e.g. to preserve a not-found re-submit's backoff).
        """
        delegation_id = run["delegation_id"]
        if isinstance(exc, A2AClientError) and not isinstance(exc, A2APermanentError):
            logger.warning(
                "Submit for delegation %s failed transiently; scheduling a poll "
                "to reconcile.",
                delegation_id,
                exc_info=exc,
            )
            await self._enqueue_delegation_poll(
                exec_context,
                run,
                delay_seconds=poll_delay_seconds
                if poll_delay_seconds is not None
                else _poll_interval_for(target_service),
            )
            return
        logger.error(
            "Submit for delegation %s failed permanently.",
            delegation_id,
            exc_info=exc,
        )
        await self._fail_delegation_run(
            exec_context,
            delegation_id=delegation_id,
            error="".join(traceback.format_exception(exc)),
        )

    async def _resubmit_with_backoff(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        target_service: PollableDelegationService,
        clock: Clock,
        reason: str,
    ) -> None:
        """Re-submit an ``awaiting_remote`` run, paced with poll backoff.

        Shared by the poll handler's lost-response (NULL id) and not-found
        branches: bump the attempt counter so repeated recoveries back off
        instead of spinning at the flat base interval, then re-submit (which
        reconciles the new remote id and reschedules a poll).
        """
        delegation_id = run["delegation_id"]
        # Bump in its own committed transaction so the delegation_runs row lock is
        # released before the re-submit's reconcile (update_remote_task) touches
        # the same row from a separate isolated context — otherwise the two
        # contend on the row and block on PostgreSQL.
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            attempts = await isolated_db.delegation_runs.bump_poll_attempt(
                delegation_id, clock.now()
            )
        logger.warning("Re-submitting remote delegation %s: %s.", delegation_id, reason)
        await self._resubmit_awaiting_remote(
            exec_context,
            run,
            target_service,
            poll_delay_seconds=_delegation_poll_backoff(
                attempts or 1, _poll_interval_for(target_service)
            ),
        )

    async def _resubmit_awaiting_remote(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        target_service: PollableDelegationService,
        *,
        poll_delay_seconds: float | None = None,
    ) -> None:
        """Re-submit an ``awaiting_remote`` run, creating a fresh remote task.

        Recovers a run whose submit response was lost (NULL id) or whose remote
        task the remote no longer has (poll returned not-found). Per A2A spec
        §3.4.2 the re-submit carries no client task id, so the remote assigns a
        new id which is reconciled in. This abandons any orphaned earlier task
        and is the accepted narrow duplicate-on-recovery window (a remote that
        actually created a task from a lost-response submit). ``poll_delay_seconds``
        overrides the next poll's delay (used to apply backoff on a not-found
        re-submit).
        """
        content_parts = cast("list[ContentPartDict]", run["content_parts_json"])
        try:
            submission = await target_service.submit_async(
                content_parts,
                conversation_id=run["conversation_id"],
                subconversation_id=run["subconversation_id"],
            )
        except Exception as exc:
            await self._handle_submit_failure(
                exec_context,
                run,
                target_service,
                exc,
                poll_delay_seconds=poll_delay_seconds,
            )
            return
        await self._after_submission(
            exec_context,
            run,
            target_service,
            submission,
            poll_delay_seconds=poll_delay_seconds,
        )

    async def _after_submission(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        target_service: PollableDelegationService,
        submission: RemoteSubmission,
        *,
        poll_delay_seconds: float | None = None,
    ) -> None:
        """Record the remote-assigned id, then finalize inline or enqueue a poll.

        ``poll_delay_seconds`` overrides the next poll's delay; it lets a
        not-found re-submit reschedule with backoff instead of the flat base
        interval used after a first submit.
        """
        delegation_id = run["delegation_id"]
        # Persist the remote-assigned id (the run was claimed with a NULL id, or
        # a re-submit produced a new one) so polling/cancel target the real task.
        if submission.remote_task_id != run["remote_task_id"]:
            async with exec_context.db_context.create_isolated_context() as isolated_db:
                await isolated_db.delegation_runs.update_remote_task(
                    delegation_id,
                    remote_task_id=submission.remote_task_id,
                    remote_context_id=submission.remote_context_id,
                )

        if submission.terminal_result is not None:
            # The remote returned a terminal task on submit; no polling needed.
            await self._finalize_delegation_run(
                exec_context, delegation_id, submission.terminal_result
            )
            return

        await self._enqueue_delegation_poll(
            exec_context,
            run,
            delay_seconds=poll_delay_seconds
            if poll_delay_seconds is not None
            else _poll_interval_for(target_service),
        )

    async def _enqueue_delegation_poll(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        *,
        delay_seconds: float = DELEGATION_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Enqueue a single delegation_poll task for an awaiting_remote run."""
        clock = exec_context.clock or self.clock
        task_id = f"{DELEGATION_POLL_TASK_TYPE}_{uuid.uuid4().hex}"
        payload: DelegationPollPayload = {
            "delegation_id": run["delegation_id"],
            "interface_type": run["interface_type"],
            "conversation_id": run["conversation_id"],
            "user_name": run["user_name"] or exec_context.user_name,
        }
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            await isolated_db.tasks.enqueue(
                task_id=task_id,
                task_type=DELEGATION_POLL_TASK_TYPE,
                payload=payload,
                scheduled_at=clock.now() + timedelta(seconds=delay_seconds),
                max_retries_override=3,
            )

    async def handle_delegation_poll(
        self,
        exec_context: ToolExecutionContext,
        payload: DelegationPollPayload,
    ) -> None:
        """Poll an awaiting_remote delegation once; reschedule or finalize.

        One ``tasks/get`` against the remote: terminal -> finalize and notify;
        still pending -> reschedule with backoff, or give up + cancel + fail once
        past the wall-clock cap. The cap is enforced only AFTER polling and only
        for a still-pending task, so a remote that finished just before a late
        poll (backoff/scheduler delay can push a poll past the cap) still
        delivers its result instead of being failed as a timeout. Holds a worker
        only for the single poll, never for the whole remote run.
        """
        delegation_id = payload.get("delegation_id")
        if not delegation_id:
            raise ValueError("delegation_poll payload missing delegation_id")

        clock = exec_context.clock or self.clock
        run = await exec_context.db_context.delegation_runs.get_by_delegation_id(
            delegation_id
        )
        if run is None or run["status"] in TERMINAL_DELEGATION_STATUSES:
            return
        if run["status"] != "awaiting_remote":
            logger.warning(
                "delegation_poll for %s in unexpected status %s; skipping.",
                delegation_id,
                run["status"],
            )
            return

        processing_service = exec_context.processing_service
        registry = (
            processing_service.processing_services_registry
            if processing_service is not None
            else None
        )
        target_service = registry.get(run["target_service_id"]) if registry else None
        if not isinstance(target_service, PollableDelegationService):
            await self._fail_delegation_run(
                exec_context,
                delegation_id=delegation_id,
                error=(
                    f"Target service '{run['target_service_id']}' is no longer a "
                    "pollable remote profile."
                ),
            )
            return

        max_async_seconds = _max_async_for(target_service)
        started_at = _as_aware_utc(run["started_at"] or run["created_at"])
        past_cap = clock.now() - started_at > timedelta(seconds=max_async_seconds)
        timed_out_error = (
            "The remote profile did not finish within the allowed time "
            f"({max_async_seconds:.0f}s)."
        )

        remote_task_id = run["remote_task_id"]
        if not remote_task_id:
            # No remote id yet: the first submit's response was lost (we never
            # learned the remote-assigned id). Within the cap, re-submit to
            # (re)create the task and learn its id; past the cap there is nothing
            # to poll, so give up.
            if past_cap:
                await self._fail_delegation_run(
                    exec_context, delegation_id=delegation_id, error=timed_out_error
                )
                return
            await self._resubmit_with_backoff(
                exec_context,
                run,
                target_service,
                clock,
                "its submit response was lost (no remote task id)",
            )
            return

        # Poll once even when past the cap: the remote may have finished just
        # before this (late) poll fired, and a completed result must be delivered
        # rather than discarded as a timeout. The cap is enforced below, only for
        # a still-pending result.
        try:
            result = await target_service.poll_async(
                remote_task_id, run["remote_context_id"]
            )
        except A2ATaskNotFoundError:
            # The remote has no such task — it lost the task (e.g. a restart).
            # Within the cap, re-submit to recreate it (a new remote-assigned id
            # is reconciled in); past the cap, give up rather than starting fresh
            # remote work.
            if past_cap:
                await self._fail_delegation_run(
                    exec_context, delegation_id=delegation_id, error=timed_out_error
                )
                return
            await self._resubmit_with_backoff(
                exec_context,
                run,
                target_service,
                clock,
                "the remote reports its task is not found",
            )
            return
        except A2APermanentError:
            # Definitive negative (bad auth / protocol error): the task will
            # never complete. Fail with the real error.
            error = traceback.format_exc()
            logger.error(
                "Polling remote delegation %s hit a permanent error.",
                delegation_id,
                exc_info=True,
            )
            await self._fail_delegation_run(
                exec_context, delegation_id=delegation_id, error=error
            )
            return
        except A2AClientError:
            # Transient transport error; reschedule and let the wall-clock cap
            # eventually give up if it persists.
            logger.warning(
                "Polling remote delegation %s failed transiently; rescheduling.",
                delegation_id,
                exc_info=True,
            )
            result = PENDING
        except Exception:
            # A non-transport error (e.g. result conversion or a code bug) will
            # not resolve by retrying; fail the run with the actual error instead
            # of masking it as a timeout once the cap expires.
            error = traceback.format_exc()
            logger.error(
                "Polling remote delegation %s raised a non-transient error.",
                delegation_id,
                exc_info=True,
            )
            await self._fail_delegation_run(
                exec_context, delegation_id=delegation_id, error=error
            )
            return

        if result is PENDING:
            # Still not terminal: now enforce the wall-clock cap — only a pending
            # task is cancelled and failed, never one that just finished above.
            if past_cap:
                await target_service.cancel_async(remote_task_id)
                await self._fail_delegation_run(
                    exec_context,
                    delegation_id=delegation_id,
                    error=(
                        "The remote profile did not finish within the allowed "
                        f"time ({max_async_seconds:.0f}s) and was cancelled."
                    ),
                )
                return
            attempts = await exec_context.db_context.delegation_runs.bump_poll_attempt(
                delegation_id, clock.now()
            )
            await self._enqueue_delegation_poll(
                exec_context,
                run,
                delay_seconds=_delegation_poll_backoff(
                    attempts or 1, _poll_interval_for(target_service)
                ),
            )
            return

        await self._finalize_delegation_run(exec_context, delegation_id, result)

    async def handle_delegation_run_cleanup(
        self,
        exec_context: ToolExecutionContext,
        payload: DelegationRunCleanupPayload,
    ) -> None:
        """Fail and notify delegation runs stranded ``queued`` or ``running``.

        Backstop for the rare case where a run's owning task never retries (e.g.
        the process was killed): the retry "running" entry guard normally fails
        an interrupted run quickly, but a run with no further attempts — or one
        lost while still ``queued`` — would otherwise stay non-terminal forever.
        """
        clock = exec_context.clock or self.clock
        now = clock.now()
        running_timeout_seconds = payload.get(
            "running_timeout_seconds", DELEGATION_RUN_STALE_SECONDS
        )
        created_before = now - timedelta(seconds=running_timeout_seconds)
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            reaped = await isolated_db.delegation_runs.reap_stale(
                now=now,
                created_before=created_before,
                error=(
                    "The delegated run did not complete within the allowed time "
                    "and was marked failed."
                ),
            )
        if reaped:
            logger.warning(
                "Reaped %d stale delegation run(s) older than %.0fs.",
                len(reaped),
                running_timeout_seconds,
            )
        # A reaped run has no live caller waiting to deliver inline, so notify
        # unconditionally even if it was never handed off.
        for run in reaped:
            await self._force_notify_delegation(exec_context, run)

        # Awaiting-remote runs whose poll task was lost (so the per-poll
        # wall-clock cap never fires) are given up here once past the cap:
        # cancel the remote, fail, and notify. Younger ones are left for their
        # own poll task to advance.
        await self._reap_stale_awaiting_remote(exec_context, now=now)

        # Recover terminal runs whose completion notification was never
        # delivered. Two cases reap_stale (queued/running only) cannot reach:
        # a caller that crashed after the run finished but before delivering
        # inline or claiming the handoff leaves a terminal run with
        # handed_off_at NULL that the worker's gated notify skipped; and a
        # force-notify whose delivery failed leaves a terminal run notified_at
        # NULL with no owning task left to retry it. The completed_at gate keeps
        # this from racing a live inline caller within its short handoff window.
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            unnotified = await isolated_db.delegation_runs.find_terminal_unnotified(
                completed_before=created_before
            )
        if unnotified:
            logger.warning(
                "Recovering %d terminal delegation run(s) left unnotified.",
                len(unnotified),
            )
        for run in unnotified:
            await self._force_notify_delegation(exec_context, run)

    async def _reap_stale_awaiting_remote(
        self,
        exec_context: ToolExecutionContext,
        *,
        now: datetime,
    ) -> None:
        """Advance or give up on awaiting_remote runs.

        A run still within its cap whose poll task was lost (failed / exhausted
        retries) gets a fresh poll re-enqueued so it is not stuck until the cap.
        A run past its cap is failed (CAS) + the remote cancelled + notified.
        """
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            awaiting = await isolated_db.delegation_runs.list_awaiting_remote()
            # Delegation ids that already have a live (pending/processing) poll
            # task, so we re-enqueue only genuinely lost polls (no multiplication).
            live_poll_tasks = await isolated_db.tasks.get_all(
                task_type=DELEGATION_POLL_TASK_TYPE, status="pending", limit=500
            )
            live_poll_tasks += await isolated_db.tasks.get_all(
                task_type=DELEGATION_POLL_TASK_TYPE, status="processing", limit=500
            )
        polled_ids: set[str] = set()
        for task in live_poll_tasks:
            payload = task.get("payload")
            if payload and "delegation_id" in payload:
                polled_ids.add(payload["delegation_id"])
        processing_service = exec_context.processing_service
        registry = (
            processing_service.processing_services_registry
            if processing_service is not None
            else None
        )
        for run in awaiting:
            target_service = (
                registry.get(run["target_service_id"]) if registry else None
            )
            cap_seconds = (
                _max_async_for(target_service)
                if isinstance(target_service, PollableDelegationService)
                else DELEGATION_MAX_ASYNC_SECONDS
            )
            started_at = _as_aware_utc(run["started_at"] or run["created_at"])
            if now - started_at <= timedelta(seconds=cap_seconds):
                if run["delegation_id"] in polled_ids:
                    # A live poll task already owns this run.
                    continue
                grace_seconds = (
                    _submit_grace_for(target_service)
                    if isinstance(target_service, PollableDelegationService)
                    else DELEGATION_SUBMIT_GRACE_SECONDS
                )
                # Re-attach a run with a known remote id (a genuinely lost poll),
                # or a NULL-id run that is past the submit grace — i.e. its first
                # submit has definitely returned, so it is stuck (its only poll
                # was lost) rather than mid-submit, and re-submitting cannot race
                # an in-flight submit into a duplicate. A NULL-id run still within
                # the grace is likely mid-submit; leave it (its enqueued poll, or
                # the delegated_profile_run retry, recovers it).
                if run["remote_task_id"] or now - started_at > timedelta(
                    seconds=grace_seconds
                ):
                    logger.warning(
                        "Re-enqueuing a lost poll for awaiting_remote delegation %s.",
                        run["delegation_id"],
                    )
                    await self._enqueue_delegation_poll(exec_context, run)
                continue
            # Fail FIRST via the non-terminal CAS so we never clobber a terminal
            # result a live poll has just written; only if we won the transition
            # do we cancel the remote and notify.
            async with exec_context.db_context.create_isolated_context() as isolated_db:
                failed = await isolated_db.delegation_runs.mark_failed(
                    delegation_id=run["delegation_id"],
                    error=(
                        "The remote profile did not finish within the allowed "
                        "time and was cancelled."
                    ),
                    completed_at=now,
                )
            if failed is None:
                # A poll finalized this run between the snapshot and now.
                continue
            remote_task_id = run["remote_task_id"]
            if isinstance(target_service, PollableDelegationService) and remote_task_id:
                await target_service.cancel_async(remote_task_id)
            await self._force_notify_delegation(exec_context, failed)

    async def _force_notify_delegation(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
    ) -> None:
        """Force-notify a terminal run, isolating per-run delivery failures.

        A delivery failure for one run must not abort the rest of a cleanup
        batch. The run stays terminal with ``notified_at`` NULL, so the next
        cleanup pass re-attempts it via ``find_terminal_unnotified`` rather than
        stranding it (or its siblings).
        """
        try:
            await self._notify_delegation_if_needed(exec_context, run, force=True)
        except DelegationNotificationError:
            logger.warning(
                "Could not deliver delegation notification for %s; leaving it "
                "unnotified for a later retry.",
                run["delegation_id"],
                exc_info=True,
            )

    def _build_delegation_confirmation_callback(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
    ) -> RequestConfirmationCallback | None:
        """Build a confirmation callback for a delegated profile background run."""
        confirmation_ui_managers = (
            exec_context.confirmation_ui_managers or self.confirmation_ui_managers
        )
        if confirmation_ui_managers is None:
            return None

        confirmation_manager = confirmation_ui_managers.get(run["interface_type"])
        if confirmation_manager is None:
            logger.info(
                "Delegation run %s has no confirmation manager for interface %s.",
                run["delegation_id"],
                run["interface_type"],
            )
            return None

        async def request_confirmation(
            interface_type: str,
            conversation_id: str,
            turn_id: str | None,
            tool_name: str,
            call_id: str,
            # ast-grep-ignore: no-dict-any - confirmation callback protocol carries arbitrary tool arguments
            tool_args: dict[str, Any],
            timeout_seconds: float,
            context: ToolExecutionContext,
        ) -> ConfirmationOutcome:
            _ = interface_type
            _ = conversation_id
            renderer = TOOL_CONFIRMATION_RENDERERS.get(tool_name)
            if renderer:
                prompt_text = await renderer(tool_args, context)
            else:
                prompt_text = f"Confirm execution of tool: {tool_name}"

            display_turn_id = run["source_turn_id"] or turn_id
            execution_turn_id = turn_id
            source_message_internal_id = None
            if execution_turn_id is not None:
                source_row = (
                    await context.db_context.message_history.get_user_row_by_turn_id(
                        execution_turn_id
                    )
                )
                if source_row is not None:
                    source_message_internal_id = source_row["internal_id"]

            return await confirmation_manager.request_confirmation(
                conversation_id=run["conversation_id"],
                interface_type=run["interface_type"],
                turn_id=display_turn_id,
                prompt_text=prompt_text,
                tool_name=tool_name,
                tool_args=tool_args,
                timeout=timeout_seconds,
                target_user_id=run["user_id"],
                tool_call_id=call_id,
                source_message_internal_id=source_message_internal_id,
                wait_for_durable_execution=False,
            )

        return request_confirmation

    async def _fail_delegation_run(
        self,
        exec_context: ToolExecutionContext,
        *,
        delegation_id: str,
        error: str,
    ) -> None:
        """Mark a delegation run failed (committed immediately) and notify."""
        clock = exec_context.clock or self.clock
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            run = await isolated_db.delegation_runs.mark_failed(
                delegation_id=delegation_id,
                error=error,
                completed_at=clock.now(),
            )
        if run is not None:
            await self._notify_delegation_if_needed(exec_context, run)

    def _chat_interface_for_interface(
        self,
        exec_context: ToolExecutionContext,
        interface_type: str,
    ) -> ChatInterface | None:
        """Select the chat interface for a delegated run's original interface."""
        if (
            exec_context.chat_interfaces
            and interface_type in exec_context.chat_interfaces
        ):
            return exec_context.chat_interfaces[interface_type]
        return exec_context.chat_interface

    async def _notify_delegation_if_needed(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        *,
        force: bool = False,
    ) -> None:
        """Record and deliver a terminal delegation notification when needed.

        The worker delivers the terminal result only when the caller handed off
        (``handed_off_at`` set). If the caller is still — or was — waiting
        inline, it delivers the result itself, so the worker must not also
        notify. The notification (history row + delivery + ``mark_notified``) is
        committed in its own transaction so it is durable and idempotent: a
        delivery that fails leaves ``notified_at`` NULL and is retried.
        """
        if run["notified_at"] is not None:
            return
        # Normally the worker only notifies once the caller handed off (otherwise
        # the caller delivers inline). The reaper sets force=True because a
        # reaped run has no live caller waiting to deliver the result.
        if not force and run["handed_off_at"] is None:
            return

        clock = exec_context.clock or self.clock
        source_service = self._source_service_for_delegation(exec_context, run)
        if source_service is not None:
            try:
                await self._wake_source_profile_for_delegation(
                    exec_context,
                    run,
                    source_service,
                    clock,
                )
                return
            except Exception:
                logger.error(
                    "Failed to wake source profile '%s' for completed delegation %s; "
                    "falling back to direct completion notification.",
                    run["source_profile_id"],
                    run["delegation_id"],
                    exc_info=True,
                )

        message_text = self._delegation_notification_text(run)
        attachments = self._delegation_notification_attachments(run)
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            message_internal_id = await isolated_db.message_history.add_message(
                AssistantMessage(content=message_text),
                interface_type=run["interface_type"],
                conversation_id=run["conversation_id"],
                timestamp=clock.now(),
                attachments=attachments,
            )

            if run["interface_type"] in _HISTORY_NOTIFICATION_INTERFACES:
                await self._push_notify_delegation_completion(
                    isolated_db,
                    run,
                    message_text,
                )
                self._tickle_stream_hub_on_commit(
                    isolated_db,
                    run["conversation_id"],
                    user_id=run["user_id"],
                )
            else:
                chat_interface = self._chat_interface_for_interface(
                    exec_context,
                    run["interface_type"],
                )
                if chat_interface is None:
                    raise RuntimeError(
                        f"No chat interface available for {run['interface_type']}"
                    )
                sent_message_id = await chat_interface.send_message(
                    conversation_id=run["conversation_id"],
                    text=message_text,
                    parse_mode=None,
                    attachment_ids=run["result_attachment_ids_json"] or None,
                )
                if sent_message_id is None:
                    # Delivery failed (invalid chat, Bot API error, ...). Roll back
                    # this isolated transaction so the message-history row is undone
                    # and notified_at stays NULL; the run is retried via the
                    # terminal-on-entry re-notification path rather than being
                    # recorded as delivered.
                    raise DelegationNotificationError(
                        f"Failed to deliver delegation notification for "
                        f"{run['delegation_id']} via {run['interface_type']}."
                    )
                if message_internal_id is not None:
                    await isolated_db.message_history.update_interface_id(
                        internal_id=message_internal_id,
                        interface_message_id=sent_message_id,
                    )

            await isolated_db.delegation_runs.mark_notified(
                delegation_id=run["delegation_id"],
                result_message_internal_id=message_internal_id,
                notified_at=clock.now(),
            )

    def _source_service_for_delegation(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
    ) -> ProcessingService | None:
        """Return the local profile that initiated a delegated run, if available."""
        processing_service = exec_context.processing_service
        if processing_service is None:
            return None
        if processing_service.service_config.id == run["source_profile_id"]:
            return processing_service
        registry = processing_service.processing_services_registry
        if registry is None:
            return None
        source_service = registry.get(run["source_profile_id"])
        return source_service if isinstance(source_service, ProcessingService) else None

    async def _wake_source_profile_for_delegation(
        self,
        exec_context: ToolExecutionContext,
        run: DelegationRunDict,
        source_service: ProcessingService,
        clock: Clock,
    ) -> int | None:
        """Wake the delegating profile with a terminal delegation result."""
        chat_interface = self._chat_interface_for_interface(
            exec_context,
            run["interface_type"],
        )
        trigger_text = self._delegation_wakeup_text(run)
        source_subconversation_id = run["source_subconversation_id"]
        async with exec_context.db_context.create_isolated_context() as wake_db:
            wake_turn_id = str(uuid.uuid4())
            data_message_internal_id = await wake_db.message_history.add_message(
                UserMessage(content=self._delegation_wakeup_data_text(run)),
                interface_type=run["interface_type"],
                conversation_id=run["conversation_id"],
                turn_id=wake_turn_id,
                timestamp=clock.now(),
                processing_profile_id=source_service.service_config.id,
                user_id=run["user_id"],
                attachments=self._delegation_notification_attachments(run),
                subconversation_id=source_subconversation_id,
                is_internal=True,
            )
            if data_message_internal_id is None:
                raise DelegationNotificationError(
                    f"Failed to persist wakeup data for delegation "
                    f"{run['delegation_id']}."
                )
            result = await source_service.handle_chat_interaction(
                db_context=wake_db,
                interface_type=run["interface_type"],
                conversation_id=run["conversation_id"],
                trigger_content_parts=[{"type": "text", "text": trigger_text}],
                trigger_interface_message_id=None,
                user_name=run["user_name"] or exec_context.user_name,
                user_id=run["user_id"],
                replied_to_interface_id=None,
                chat_interface=chat_interface,
                chat_interfaces=exec_context.chat_interfaces,
                confirmation_ui_managers=exec_context.confirmation_ui_managers,
                request_confirmation_callback=build_deferred_confirmation_callback(
                    target_user_id=run["user_id"],
                    source_prefix=("From a completed delegated task — approve to run:"),
                    missing_owner_message=lambda tool_name: (
                        "This delegated task has no recorded owner, so the "
                        f"confirm-gated tool '{tool_name}' cannot be approved and "
                        "was not run."
                    ),
                ),
                trigger_attachments=self._delegation_notification_attachments(run),
                subconversation_id=source_subconversation_id,
                thread_root_id=data_message_internal_id,
                trigger_is_internal=True,
                pinned_history_message_ids=[data_message_internal_id],
                trigger_role="system",
                save_history_with_isolated_context=False,
                turn_id=wake_turn_id,
            )
            message_internal_id = (
                await self._deliver_source_profile_delegation_response(
                    wake_db,
                    run,
                    result,
                    chat_interface,
                    clock,
                    wake_turn_id,
                    data_message_internal_id,
                )
            )
            await wake_db.delegation_runs.mark_notified(
                delegation_id=run["delegation_id"],
                result_message_internal_id=message_internal_id,
                notified_at=clock.now(),
            )
            return message_internal_id

    async def _deliver_source_profile_delegation_response(
        self,
        db_context: DatabaseContext,
        run: DelegationRunDict,
        result: ChatInteractionResult,
        chat_interface: ChatInterface | None,
        clock: Clock,
        wake_turn_id: str,
        thread_root_id: int,
    ) -> int | None:
        """Deliver the source profile's response to a terminal delegation wakeup."""
        if result.has_error:
            raise DelegationNotificationError(
                f"Source profile '{run['source_profile_id']}' failed while handling "
                f"delegation {run['delegation_id']} wakeup."
            )

        delivery_attachment_ids = self._source_delivery_attachment_ids(run, result)
        has_response = bool(result.text_reply) or bool(delivery_attachment_ids)
        if not has_response:
            raise DelegationNotificationError(
                f"Source profile '{run['source_profile_id']}' produced no response "
                f"for delegation {run['delegation_id']}."
            )

        delivery_attachments = self._delegation_attachment_metadata(
            delivery_attachment_ids
        )
        message_internal_id = result.assistant_message_internal_id
        delivery_text = result.text_reply or "Delegated task finished."
        visible_message_internal_id = message_internal_id

        if message_internal_id is None:
            message_internal_id = await db_context.message_history.add_message(
                AssistantMessage(content=delivery_text),
                interface_type=run["interface_type"],
                conversation_id=run["conversation_id"],
                timestamp=clock.now(),
                turn_id=wake_turn_id,
                thread_root_id=thread_root_id,
                processing_profile_id=run["source_profile_id"],
                user_id=run["user_id"],
                attachments=delivery_attachments,
                subconversation_id=run["source_subconversation_id"],
            )
            if message_internal_id is None:
                raise DelegationNotificationError(
                    f"Failed to persist source profile response for delegation "
                    f"{run['delegation_id']}."
                )
            visible_message_internal_id = message_internal_id
        elif message_internal_id is not None and delivery_attachments is not None:
            await db_context.message_history.update_attachments(
                internal_id=message_internal_id,
                attachments=delivery_attachments,
            )

        if run["source_subconversation_id"] is not None:
            visible_message_internal_id = await db_context.message_history.add_message(
                AssistantMessage(content=delivery_text),
                interface_type=run["interface_type"],
                conversation_id=run["conversation_id"],
                timestamp=clock.now(),
                turn_id=wake_turn_id,
                processing_profile_id=run["source_profile_id"],
                user_id=run["user_id"],
                attachments=delivery_attachments,
                subconversation_id=None,
            )
            if visible_message_internal_id is None:
                raise DelegationNotificationError(
                    f"Failed to persist visible source profile response for "
                    f"delegation {run['delegation_id']}."
                )

        if visible_message_internal_id is None:
            raise DelegationNotificationError(
                f"Failed to identify source profile response row for delegation "
                f"{run['delegation_id']}."
            )

        if run["interface_type"] in _HISTORY_NOTIFICATION_INTERFACES:
            await self._push_notify_delegation_completion(
                db_context,
                run,
                delivery_text,
            )
            self._tickle_stream_hub_on_commit(
                db_context,
                run["conversation_id"],
                user_id=run["user_id"],
            )
            return visible_message_internal_id

        if chat_interface is None:
            raise RuntimeError(
                f"No chat interface available for {run['interface_type']}"
            )
        sent_message_id = await chat_interface.send_message(
            conversation_id=run["conversation_id"],
            text=delivery_text,
            parse_mode=None,
            attachment_ids=delivery_attachment_ids,
        )
        if sent_message_id is None:
            raise DelegationNotificationError(
                f"Failed to deliver source profile response for delegation "
                f"{run['delegation_id']} via {run['interface_type']}."
            )
        await db_context.message_history.update_interface_id(
            internal_id=visible_message_internal_id,
            interface_message_id=sent_message_id,
        )
        return visible_message_internal_id

    @staticmethod
    def _source_delivery_attachment_ids(
        run: DelegationRunDict,
        result: ChatInteractionResult,
    ) -> list[str] | None:
        """Return source-response plus delegated-result attachment IDs."""
        attachment_ids: list[str] = []
        for attachment_id in [
            *(result.attachment_ids or []),
            *(run["result_attachment_ids_json"] or []),
        ]:
            if attachment_id not in attachment_ids:
                attachment_ids.append(attachment_id)
        return attachment_ids or None

    @staticmethod
    def _delegation_attachment_metadata(
        attachment_ids: list[str] | None,
    ) -> list[MessageAttachmentMetadata] | None:
        """Build history metadata for attachment references."""
        if not attachment_ids:
            return None
        return [
            {
                "type": "attachment_reference",
                "attachment_id": attachment_id,
            }
            for attachment_id in attachment_ids
        ]

    def _delegation_wakeup_text(self, run: DelegationRunDict) -> str:
        """Build the internal system trigger for a completed delegation."""
        if run["status"] == "completed":
            return (
                "System: Delegated profile task completed.\n\n"
                f"Delegation reference: {run['delegation_id']}\n"
                "The delegated result is provided as lower-priority data in the "
                "message history for this turn. Use it to respond to the user. Do not mention the internal "
                "delegation reference unless it is useful for troubleshooting."
            )
        return (
            "System: Delegated profile task failed.\n\n"
            f"Delegation reference: {run['delegation_id']}\n"
            "The failure detail is provided as lower-priority data in this turn's "
            "message history. Tell the user that the delegated work failed and summarize "
            "the useful details. Do not expose tracebacks unless the user is debugging."
        )

    def _delegation_wakeup_data_text(self, run: DelegationRunDict) -> str:
        """Build lower-priority data for a completed delegation wakeup."""
        if run["status"] == "completed":
            result_text = (
                run["result_text"]
                or "The delegated profile completed without a textual response."
            )
            return (
                "Delegated profile task completed data.\n\n"
                f"Delegation reference: {run['delegation_id']}\n"
                f"Target profile: {run['target_service_id']}\n"
                f"Original request: {run['request_text']}\n\n"
                "Delegated result:\n"
                f"{result_text}"
            )
        error_summary = (
            short_error_summary(run["error"])
            or "The delegated profile failed during processing."
        )
        return (
            "Delegated profile task failed data.\n\n"
            f"Delegation reference: {run['delegation_id']}\n"
            f"Target profile: {run['target_service_id']}\n"
            f"Original request: {run['request_text']}\n\n"
            "Failure detail:\n"
            f"{error_summary}"
        )

    def _delegation_notification_text(self, run: DelegationRunDict) -> str:
        """Build concise terminal notification text for a delegation run."""
        if run["status"] == "completed":
            result_text = (
                run["result_text"]
                or "The delegated profile completed without a textual response."
            )
            return (
                f"Delegated task {run['delegation_id']} completed via "
                f"{run['target_service_id']}.\n\n{result_text}"
            )
        error_summary = (
            short_error_summary(run["error"])
            or "The delegated profile failed during processing."
        )
        return (
            f"Delegated task {run['delegation_id']} failed via "
            f"{run['target_service_id']}.\n\n{error_summary}"
        )

    def _delegation_notification_attachments(
        self,
        run: DelegationRunDict,
    ) -> list[MessageAttachmentMetadata] | None:
        """Return message attachment references for a completed delegation result."""
        return self._delegation_attachment_metadata(run["result_attachment_ids_json"])

    async def _push_notify_delegation_completion(
        self,
        db_context: DatabaseContext,
        run: DelegationRunDict,
        message_text: str,
    ) -> None:
        """Send push notification for a web delegation completion."""
        if self.notification_dispatcher is None:
            return
        try:
            await notify_conversation(
                self.notification_dispatcher,
                db_context,
                interface_type=run["interface_type"],
                conversation_id=run["conversation_id"],
                title="Delegated task finished",
                body=message_text[:100],
                metadata=NotificationMetadata(
                    category=MESSAGE_CATEGORY,
                    conversation_id=run["conversation_id"],
                ),
            )
        except Exception:
            logger.warning(
                "Failed to send delegation completion push notification",
                exc_info=True,
            )

    def _tickle_stream_hub_on_commit(
        self,
        db_context: DatabaseContext,
        conversation_id: str,
        user_id: str | None = None,
    ) -> None:
        """Nudge open web streams to reload once the message commits.

        Two nudges fire, both scheduled from an ``on_commit`` hook so they only
        run after the surrounding transaction commits (the new row must be
        visible to a refetch). Strong references are held until they complete.

        * A content-free ``message`` event on the conversation's own stream, so
          a client with that thread open refetches its history. This mirrors
          WebChatInterface's post-commit tickle for messages written outside the
          streaming turn path.
        * A ``conversation_activity`` ping on the account-global activity stream
          (when ``user_id`` is known), so a client looking at a *different*
          thread — or the conversation list — refreshes the list and sees the
          delegated/scheduled reply land without a manual pull-to-refresh.
        """
        if self.stream_hub is None:
            return
        hub = self.stream_hub
        loop = asyncio.get_running_loop()

        def _schedule_publish() -> None:
            task = loop.create_task(
                hub.publish(
                    conversation_id,
                    "message",
                    turn_id=None,
                    payload={
                        "conversation_id": conversation_id,
                        "new_messages": True,
                    },
                )
            )
            self._hub_publish_tasks.add(task)
            task.add_done_callback(self._hub_publish_tasks.discard)

            if user_id:
                activity_task = loop.create_task(
                    hub.publish_activity(
                        conversation_id,
                        user_id=user_id,
                        reason="delegation",
                    )
                )
                self._hub_publish_tasks.add(activity_task)
                activity_task.add_done_callback(self._hub_publish_tasks.discard)

        db_context.on_commit(_schedule_publish)

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
            user_tz = self.timezone
            last_scheduled_in_user_tz = last_scheduled_at.astimezone(user_tz)
            logger.debug(
                f"RECURRENCE DEBUG: Converting scheduled time from {last_scheduled_at} UTC to {last_scheduled_in_user_tz} {self.timezone} for recurrence calculation"
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

    def _schedule_automation_advance_request_for_task(
        self, task: TaskDict
    ) -> ScheduleAutomationAdvanceRequest | None:
        """Build schedule automation advancement work for a terminal source task."""
        payload = task.get("payload") or {}
        automation_id = payload.get("automation_id")
        automation_type = payload.get("automation_type")
        if not automation_id or automation_type != "schedule":
            return None

        return ScheduleAutomationAdvanceRequest(
            automation_id=str(automation_id),
            source_task_id=task["task_id"],
            execution_time=self.clock.now(),
        )

    def _payload_with_schedule_automation_advance_outbox(
        self,
        task: TaskDict,
        request: ScheduleAutomationAdvanceRequest | None,
    ) -> dict[str, object] | None:
        """Return task payload with durable schedule advancement outbox attached."""
        payload = dict(task.get("payload") or {})
        if request is None:
            return task.get("payload")

        payload[SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY] = {
            "automation_id": request.automation_id,
            "source_task_id": request.source_task_id,
            "execution_time": request.execution_time.isoformat(),
            "schedule_next": request.schedule_next,
        }
        return payload

    def _advance_request_from_outbox_payload(
        self,
        payload: Mapping[str, object] | None,
    ) -> ScheduleAutomationAdvanceRequest | None:
        """Parse a persisted schedule advancement outbox payload."""
        if not payload:
            return None
        outbox = payload.get(SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY)
        if not isinstance(outbox, dict):
            return None
        automation_id = outbox.get("automation_id")
        source_task_id = outbox.get("source_task_id")
        execution_time = outbox.get("execution_time")
        schedule_next = outbox.get("schedule_next", True)
        if (
            not isinstance(automation_id, str)
            or not isinstance(source_task_id, str)
            or not isinstance(execution_time, str)
            or not isinstance(schedule_next, bool)
        ):
            raise ValueError("Invalid persisted schedule automation advance outbox")
        return ScheduleAutomationAdvanceRequest(
            automation_id=automation_id,
            source_task_id=source_task_id,
            execution_time=_parse_payload_datetime(
                execution_time,
                "execution_time",
            ),
            schedule_next=schedule_next,
        )

    async def _enqueue_schedule_automation_advance(
        self,
        db_context: DatabaseContext,
        request: ScheduleAutomationAdvanceRequest,
    ) -> None:
        """Persist retryable work to advance a terminal schedule automation task."""
        await db_context.tasks.enqueue(
            task_id=f"sched_auto_advance_{request.source_task_id}",
            task_type=SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
            payload=ScheduleAutomationAdvancePayload(
                automation_id=request.automation_id,
                source_task_id=request.source_task_id,
                execution_time=request.execution_time.isoformat(),
                schedule_next=request.schedule_next,
            ),
            max_retries_override=5,
        )
        logger.info(
            f"Enqueued schedule automation advancement for automation "
            f"{request.automation_id} after terminal task {request.source_task_id}"
        )

    async def _flush_schedule_automation_advance_outbox(
        self,
        db_context: DatabaseContext,
        source_task_id: str,
    ) -> bool:
        """Drain one durable schedule advancement outbox into an advance task."""
        row = await db_context.fetch_one(
            select(tasks_table).where(tasks_table.c.task_id == source_task_id)
        )
        if row is None:
            logger.warning(
                f"Cannot flush schedule automation advancement for missing source task {source_task_id}"
            )
            return False

        task = cast("TaskDict", dict(row))
        payload = task.get("payload") or {}
        request = self._advance_request_from_outbox_payload(payload)
        if request is None:
            return False

        await self._enqueue_schedule_automation_advance(db_context, request)
        updated_payload = dict(payload)
        updated_payload.pop(SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY, None)
        await db_context.execute_with_retry(
            update(tasks_table)
            .where(tasks_table.c.task_id == source_task_id)
            .values(payload=updated_payload)
        )
        return True

    async def _drain_schedule_automation_advance_outbox(
        self,
        db_context: DatabaseContext,
    ) -> int:
        """Flush persisted schedule advancement outbox entries from terminal source tasks."""
        outbox_exists = (
            func.json_type(
                tasks_table.c.payload,
                f"$.{SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY}",
            ).is_not(None)
            if db_context.engine.dialect.name == "sqlite"
            else tasks_table.c.payload[SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY].is_not(
                None
            )
        )
        rows = await db_context.fetch_all(
            select(tasks_table)
            .where(tasks_table.c.status.in_(["done", "failed"]))
            .where(
                tasks_table.c.task_type.in_(
                    ["llm_callback", "script_execution"],
                )
            )
            .where(outbox_exists)
            .order_by(tasks_table.c.created_at.asc())
            .limit(20)
        )
        flushed_count = 0
        for row in rows:
            task = cast("TaskDict", dict(row))
            payload = task.get("payload") or {}
            if SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY not in payload:
                continue
            if await self._flush_schedule_automation_advance_outbox(
                db_context,
                task["task_id"],
            ):
                flushed_count += 1
        return flushed_count

    async def _handle_schedule_automation_task_terminal(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
    ) -> None:
        """Persist retryable work to advance a terminal schedule automation task."""
        request = self._schedule_automation_advance_request_for_task(task)
        if request is None:
            return
        await self._enqueue_schedule_automation_advance(db_context, request)

    async def handle_schedule_automation_advance(
        self,
        exec_context: ToolExecutionContext,
        payload: ScheduleAutomationAdvancePayload,
    ) -> None:
        """Advance a schedule automation after one run reaches terminal state."""
        automation_id = payload.get("automation_id")
        if not automation_id:
            raise ValueError(
                "schedule_automation_advance payload missing automation_id"
            )
        execution_time = payload.get("execution_time")
        if not execution_time:
            raise ValueError(
                "schedule_automation_advance payload missing execution_time"
            )
        schedule_next = payload.get("schedule_next", True)
        if not isinstance(schedule_next, bool):
            raise ValueError("schedule_automation_advance schedule_next must be bool")

        await exec_context.db_context.schedule_automations.after_task_execution(
            automation_id=int(automation_id),
            execution_time=_parse_payload_datetime(execution_time, "execution_time"),
            timezone=exec_context.timezone,
            schedule_next=schedule_next,
        )
        logger.info(
            f"Advanced schedule automation {automation_id} "
            f"after terminal task {payload.get('source_task_id', 'unknown')}"
        )

    async def _process_task(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
        wake_up_event: asyncio.Event,
    ) -> ScheduleAutomationAdvanceRequest | None:
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
            return None  # Stop processing this task

        with tracer.start_as_current_span(
            f"task.process.{task['task_type']}",
            attributes={
                "task.type": task["task_type"],
                "task.id": str(task["task_id"]),
            },
        ) as span:
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
                        return None  # Stop processing
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
                    chat_interface=(
                        self.chat_interfaces.get(
                            final_interface_type, self.chat_interface
                        )
                        if self.chat_interfaces
                        else self.chat_interface
                    ),
                    chat_interfaces=self.chat_interfaces,
                    timezone=self.timezone,
                    processing_profile_id=(
                        self.processing_service.service_config.id
                        if self.processing_service
                        else None
                    ),
                    update_activity_callback=self._update_last_activity,  # Pass activity callback
                    embedding_generator=self.embedding_generator,
                    indexing_source=self.indexing_source,  # Pass the indexing source
                    visibility_grants=(
                        set(self.processing_service.service_config.visibility_grants)
                        if self.processing_service
                        and self.processing_service.service_config.visibility_grants
                        else None
                    ),
                    default_note_visibility_labels=(
                        self.processing_service.service_config.default_note_visibility_labels
                        if self.processing_service
                        else None
                    ),
                    confirmation_result_waiters=self.confirmation_result_waiters,
                    confirmation_ui_managers=getattr(
                        self,
                        "confirmation_ui_managers",
                        None,
                    ),
                )
                # --- Execute Handler with Context ---
                logger.debug(
                    f"HANDLER START: Worker {self.worker_id} executing handler for task {task['task_id']} with context."
                )
                # Pass the context and the original payload with timeout. The
                # timeout may be overridden per task type (e.g. delegated runs that
                # park on a human confirmation get a longer budget).
                effective_timeout = self._timeout_for_task_type(task["task_type"])
                try:
                    await asyncio.wait_for(
                        handler(exec_context, task["payload"]),
                        timeout=effective_timeout,
                    )
                    logger.debug(
                        f"HANDLER SUCCESS: Worker {self.worker_id} completed handler for task {task['task_id']}"
                    )
                except TimeoutError:
                    logger.error(
                        f"HANDLER TIMEOUT: Task {task['task_id']} (type: {task['task_type']}) timed out after {effective_timeout} seconds"
                    )
                    # Re-raise to trigger retry logic in _handle_task_failure
                    raise

                # Task details for logging
                task_id = task["task_id"]
                original_task_id = task.get(
                    "original_task_id", task_id
                )  # Use task_id if original is missing (first run)
                advance_request = self._schedule_automation_advance_request_for_task(
                    task
                )

                # Mark task as done
                await db_context.tasks.update_status(
                    task_id=task_id,
                    status="done",
                    payload=self._payload_with_schedule_automation_advance_outbox(
                        task,
                        advance_request,
                    ),
                )
                span.set_attribute("task.status", "success")
                logger.info(
                    f"PROCESS SUCCESS: Worker {self.worker_id} completed task {task_id} (Original: {original_task_id})"
                )

                # --- Handle Recurrence ---
                await self._handle_recurrence(db_context, task)
                return advance_request

            except Exception as handler_exc:
                span.set_status(StatusCode.ERROR, str(handler_exc))
                span.record_exception(handler_exc)
                span.set_attribute("task.status", "error")
                return await self._handle_task_failure(db_context, task, handler_exc)

    async def _handle_task_failure(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
        handler_exc: Exception,
    ) -> ScheduleAutomationAdvanceRequest | None:
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

        can_retry = current_retry < max_retries and not isinstance(
            handler_exc, NonRetryableTaskError
        )

        if can_retry:
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
                advance_request = self._schedule_automation_advance_request_for_task(
                    task
                )
                await db_context.tasks.update_status(
                    task_id=task["task_id"],
                    status="failed",
                    error=f"Handler Error: {error_str}. Reschedule Failed: {reschedule_err}",
                    payload=self._payload_with_schedule_automation_advance_outbox(
                        task,
                        advance_request,
                    ),
                )
                return advance_request
            return None
        else:
            if isinstance(handler_exc, NonRetryableTaskError):
                logger.warning(
                    f"Task {task['task_id']} failed with a non-retryable error. Marking as failed."
                )
            else:
                # Handle case where the turn completed but the final assistant message had no content
                logger.warning(
                    f"Task {task['task_id']} reached max retries ({max_retries}). Marking as failed."
                )
            advance_request = self._schedule_automation_advance_request_for_task(task)
            await db_context.tasks.update_status(
                task_id=task["task_id"],
                status="failed",
                error=error_str,
                payload=self._payload_with_schedule_automation_advance_outbox(
                    task,
                    advance_request,
                ),
            )
            # Handle recurrence even if task failed (after max retries)
            await self._handle_recurrence(db_context, task)
            # Notify user about script execution failures
            if task["task_type"] == "script_execution":
                await self._enqueue_script_error_notification(
                    db_context, task, error_str
                )
            # Push-notify the conversation owner that a background task failed.
            await self._notify_task_failure(db_context, task)
            return advance_request

    async def _notify_task_failure(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
    ) -> None:
        """Push-notify the conversation owner that a task failed after its retries."""
        if self.notification_dispatcher is None:
            return
        payload = task.get("payload") or {}
        conversation_id = payload.get("conversation_id")
        try:
            await notify_conversation(
                self.notification_dispatcher,
                db_context,
                interface_type=payload.get("interface_type"),
                conversation_id=conversation_id,
                title="Task failed",
                body=f"A background task ({task['task_type']}) failed.",
                metadata=NotificationMetadata(
                    category=MESSAGE_CATEGORY,
                    conversation_id=conversation_id,
                ),
            )
        except Exception:
            logger.warning("Failed to send task failure notification", exc_info=True)

    async def _enqueue_script_error_notification(
        self,
        db_context: DatabaseContext,
        task: TaskDict,
        error_str: str,
    ) -> None:
        """Enqueue an llm_callback task to notify the user about a script failure.

        Only called after a script_execution task exhausts its retries.
        The notification task uses max_retries=1 and its own failure will NOT
        trigger another notification (loop prevention: only script_execution
        failures trigger this).
        """
        payload_dict = task.get("payload") or {}
        config = payload_dict.get("config") or {}

        if config.get("notify_on_failure") is False:
            logger.info(
                f"Skipping error notification for task {task['task_id']} "
                "(notify_on_failure=False)"
            )
            return

        conversation_id = payload_dict.get("conversation_id", "")
        interface_type = payload_dict.get("interface_type", "telegram")

        # Build informative error context for the LLM
        script_code = payload_dict.get("script_code") or ""
        script_name = payload_dict.get("script_name") or ""
        if not script_code and script_name:
            script_code = f"(stored script: {script_name})"
        script_lines = script_code.strip().splitlines()
        if len(script_lines) > 100:
            script_code = "\n".join(script_lines[:100]) + "\n... (truncated)"

        event_data = payload_dict.get("event_data")
        event_data_str = str(event_data) if event_data else ""
        if len(event_data_str) > 2000:
            event_data_str = event_data_str[:2000] + "... (truncated)"

        automation_id = payload_dict.get("automation_id", "unknown")
        task_name = payload_dict.get("task_name", task["task_id"])
        listener_id = payload_dict.get("listener_id", "")
        retry_count = task.get("retry_count", 0)

        source_label = (
            f"event listener {listener_id}"
            if listener_id
            else f"automation {automation_id}"
        )

        error_lines = error_str.strip().splitlines()
        if len(error_lines) > 50:
            error_str = "\n".join(error_lines[-50:])

        callback_context = (
            f"An automation script has failed after exhausting all retries.\n\n"
            f"**Task:** {task_name}\n"
            f"**Source:** {source_label}\n"
            f"**Retries exhausted:** {retry_count}\n\n"
            f"**Error:**\n```\n{error_str}\n```\n\n"
            f"**Script code:**\n```python\n{script_code}\n```\n\n"
        )
        if event_data_str and event_data_str != "{}":
            callback_context += (
                f"**Triggering event data:**\n```\n{event_data_str}\n```\n\n"
            )
        callback_context += (
            "Please summarize this error for the user and suggest possible fixes. "
            "Do NOT re-run the script."
        )

        notification_task_id = f"script_error_notify_{uuid.uuid4().hex[:8]}"

        notification_payload = LlmCallbackPayload(
            conversation_id=conversation_id,
            interface_type=interface_type,
            callback_context=callback_context,
            scheduling_timestamp=datetime.now(UTC).isoformat(),
        )
        created_by_user_id = payload_dict.get("created_by_user_id")
        if created_by_user_id is not None:
            notification_payload["created_by_user_id"] = created_by_user_id

        try:
            await enqueue_task(
                db_context=db_context,
                task_id=notification_task_id,
                task_type="llm_callback",
                payload=notification_payload,
                max_retries_override=1,
            )
            logger.info(
                f"Enqueued error notification {notification_task_id} "
                f"for failed script task {task['task_id']}"
            )
        except Exception:
            logger.exception(
                f"Failed to enqueue error notification for task {task['task_id']}"
            )

    async def _wait_for_next_poll(self, wake_up_event: asyncio.Event) -> None:
        """Waits for the polling interval or a wake-up event.

        The event is cleared at the top of each loop iteration (before the
        dequeue attempt), NOT here. That ordering avoids a lost-wakeup race: a
        notification that arrives *during* a dequeue that returned nothing (e.g.
        a sibling that just claimed a task and woke us to drain the rest of the
        queue) leaves the event set, so this wait returns immediately instead of
        blocking for the full poll interval.
        """
        try:
            logger.debug(
                f"Worker {self.worker_id}: No tasks found, waiting for event or timeout ({TASK_POLLING_INTERVAL}s)..."
            )

            await asyncio.wait_for(wake_up_event.wait(), timeout=TASK_POLLING_INTERVAL)
            # If wait_for completes without timeout, the event was set
            logger.debug(f"Worker {self.worker_id}: Woken up by event.")
        except TimeoutError:
            # Event didn't fire, timeout reached, proceed to next polling cycle
            logger.debug(
                f"Worker {self.worker_id}: Wait timed out, continuing poll cycle."
            )
            # Continue the loop normally after timeout

    async def run(self, wake_up_event: asyncio.Event | None = None) -> None:
        """Continuously polls for and processes tasks.

        Args:
            wake_up_event: Optional override event (used by tests). If not
                provided, each worker creates its OWN per-instance wake event so
                that one worker clearing its event after a wake does not swallow
                the notification for sibling workers in the pool. The event is
                registered with the storage layer so an enqueued task fans out to
                every worker's event; it is unregistered when the loop exits.
        """
        # Each worker waits on its OWN event. Tests may pass an explicit event;
        # registering it too means enqueue notifications still reach it.
        if wake_up_event is None:
            wake_up_event = asyncio.Event()
        register_worker_wake_event(wake_up_event)
        try:
            await self._run_loop(wake_up_event)
        finally:
            unregister_worker_wake_event(wake_up_event)

    async def _run_loop(self, wake_up_event: asyncio.Event) -> None:
        """The task-processing loop body, run with a registered wake event."""
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
                # Clear the wake event BEFORE attempting a dequeue. Any
                # notification that arrives after this point (including while the
                # dequeue runs) survives to the next _wait_for_next_poll, so a
                # sibling waking us to drain remaining queued work is never lost.
                wake_up_event.clear()
                # Database context per iteration (starts a transaction)
                if not self.engine:
                    raise RuntimeError("Database engine not initialized")
                # Split task processing into separate transactions for better isolation
                async with get_db_context(
                    engine=self.engine,
                ) as outbox_context:
                    drained_count = (
                        await self._drain_schedule_automation_advance_outbox(
                            outbox_context
                        )
                    )
                if drained_count > 0:
                    self._update_last_activity()
                    continue

                # Transaction 1: Dequeue task (commits immediately)
                task = None
                async with get_db_context(
                    engine=self.engine,
                ) as dequeue_context:
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
                    # Claiming a task does not drain the queue: this worker only
                    # claims one task per dequeue, and a sibling that lost a claim
                    # race may be parked. Wake siblings so they re-poll for any
                    # remaining work instead of waiting out the poll interval.
                    notify_other_workers(wake_up_event)
                    self._update_last_activity()  # Update activity when starting task processing
                    try:  # Inner try for task processing
                        # Transaction 2: Process task and update status (commits immediately)
                        async with get_db_context(
                            engine=self.engine,
                        ) as process_context:
                            advance_request = await self._process_task(
                                process_context, task, wake_up_event
                            )
                        if advance_request is not None:
                            async with get_db_context(
                                engine=self.engine,
                            ) as advance_context:
                                await self._flush_schedule_automation_advance_outbox(
                                    advance_context, advance_request.source_task_id
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
    payload: SystemEventCleanupPayload,
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
    payload: SystemErrorLogCleanupPayload,
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
    payload: WorkerTaskCleanupPayload,
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
    stale_marked = 0

    try:
        # Step 0: Mark stale tasks as failed before cleanup
        stale_marked = await exec_context.db_context.worker_tasks.mark_stale_tasks()
        if stale_marked:
            logger.info(f"Marked {stale_marked} stale worker tasks as failed")

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
            f"Marked {stale_marked} stale tasks, "
            f"deleted {db_deleted} database records, {dirs_deleted} task directories "
            f"older than {retention_hours} hours."
        )
    except Exception as e:
        logger.error(f"Error during worker task cleanup: {e}", exc_info=True)
        raise


async def handle_completed_automation_cleanup(
    exec_context: ToolExecutionContext,
    payload: CompletedAutomationCleanupPayload,
) -> None:
    """Task handler for cleaning up completed one-time automations.

    Deletes one-time event listeners that have been disabled (after firing)
    and are older than the retention period.

    Payload can include:
        retention_hours: Override the default 24-hour retention period
    """
    retention_hours = int(payload.get("retention_hours", 24))

    logger.info(
        f"Starting completed automation cleanup (retention: {retention_hours} hours)"
    )

    try:
        deleted_count = (
            await exec_context.db_context.events.cleanup_completed_one_time_listeners(
                retention_hours
            )
        )

        logger.info(
            f"Completed automation cleanup finished. "
            f"Deleted {deleted_count} completed one-time listeners "
            f"older than {retention_hours} hours."
        )
    except Exception as e:
        logger.error(f"Error during completed automation cleanup: {e}", exc_info=True)
        raise


async def _process_script_wake_llm(
    exec_context: ToolExecutionContext,
    wake_contexts: list[WakeRequest],
    # ast-grep-ignore: no-dict-any - Event data from external sources (Home Assistant, webhooks) with arbitrary structure
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
    trigger_attachments: list[MessageAttachmentMetadata] | None = None
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
                        trigger_attachments.append(
                            MessageAttachmentMetadata(
                                type=attachment_type,
                                attachment_id=attachment_metadata.attachment_id,
                                url=attachment_metadata.content_url,
                                content_url=attachment_metadata.content_url,
                                mime_type=attachment_metadata.mime_type,
                                description=attachment_metadata.description,
                                filename=attachment_metadata.metadata.get(
                                    "original_filename", "attachment"
                                ),
                                size=attachment_metadata.size,
                            )
                        )
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
    payload: LlmCallbackPayload = {
        "interface_type": exec_context.interface_type,
        "conversation_id": exec_context.conversation_id,
        "user_name": exec_context.user_name,  # Preserve user_name
        "callback_context": wake_message,
        "scheduling_timestamp": scheduling_timestamp,
        "metadata": combined_context,
    }
    if exec_context.user_id is not None:
        payload["created_by_user_id"] = exec_context.user_id

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


def _resolve_script_execution_service(
    exec_context: ToolExecutionContext,
    processing_profile_id: str | None,
) -> ProcessingService | None:
    """Resolve the processing service a script should execute under.

    Scripts run under the profile that created the automation so that the tool
    set used at validation time matches the tool set available at execution
    time. Legacy automations created before provenance was tracked have no
    recorded profile and fall back to the default processing service.

    An automation explicitly stamped with a non-default profile that can no
    longer be resolved (e.g. the profile was renamed/removed) is **not**
    downgraded to the default: the script was validated and stamped for specific
    tools/visibility, so running it under a different policy could change its
    capabilities or data access. Such cases raise so the task fails loudly
    rather than executing under the wrong profile.
    """
    default_service = exec_context.processing_service
    if (
        default_service is None
        or processing_profile_id is None
        or processing_profile_id == default_service.service_config.id
    ):
        return default_service

    registry = default_service.processing_services_registry
    unresolvable_reason: str | None = None
    candidate: object | None = None
    if registry is None:
        unresolvable_reason = "no processing services registry is available"
    else:
        candidate = registry.get(processing_profile_id)
        if candidate is None:
            unresolvable_reason = "the profile is no longer registered"
        elif getattr(candidate, "kind", None) != "local" or not hasattr(
            candidate, "tools_provider"
        ):
            unresolvable_reason = "the profile is not a local profile"

    if unresolvable_reason is not None:
        raise RuntimeError(
            f"Automation script is stamped with profile '{processing_profile_id}' "
            f"but it cannot be resolved ({unresolvable_reason}); refusing to "
            "downgrade to the default profile and run with different tools or "
            "visibility than the script was validated against."
        )

    return cast("ProcessingService", candidate)


def build_script_confirmation_callback(
    created_by_user_id: str | None,
) -> RequestConfirmationCallback:
    """Build the confirmation callback used while executing an automation script.

    Scripts run in the task worker with no interactive channel, so a confirm-gated
    tool call is deferred to a durable confirmation addressed to the automation's
    owner. The tool runs later via the confirmation_tool_execution task once the
    user approves. Legacy automations with no recorded owner cannot be approved,
    so the tool is reported as not run.
    """

    return build_deferred_confirmation_callback(
        target_user_id=created_by_user_id,
        source_prefix="From an automation — approve to run:",
        missing_owner_message=lambda tool_name: (
            "This automation has no recorded owner, so the confirm-gated "
            f"tool '{tool_name}' cannot be approved and was not run."
        ),
    )


async def handle_script_execution(
    exec_context: ToolExecutionContext,
    payload: ScriptExecutionPayload,
) -> None:
    """
    Task handler for executing scripts triggered by events.

    Executes user-defined scripts in response to events from Home Assistant,
    document indexing, and other sources. Scripts run under the processing
    profile that created the automation (see
    docs/design/automation_provenance.md), so the tools available at execution
    time match those used to validate the script at creation time. Legacy
    automations without a recorded profile fall back to the task worker's
    default profile.

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
    script_name = payload.get("script_name")
    event_data = payload.get("event_data", {})
    config = payload.get("config", {})
    listener_id = payload.get("listener_id")
    conversation_id = payload.get("conversation_id")

    # Resolve script_name to script_code from the scripts repository
    if not script_code and script_name:
        db = exec_context.db_context
        stored_script = await db.scripts.get_by_name(script_name)
        if stored_script is None:
            raise ValueError(f"Stored script '{script_name}' not found")
        script_code = stored_script.script_code

        # Validate parameters against stored script schema.
        # Runtime globals (event, conversation_id, etc.) are injected below into
        # script_globals, so they count as satisfied even if not in script_parameters.
        script_parameters = payload.get("script_parameters")
        if stored_script.parameters_schema:
            params = script_parameters or {}
            required = stored_script.parameters_schema.get("required", [])
            if not isinstance(required, list):
                required = []
            for req in required:
                if req in AUTOMATION_RUNTIME_GLOBALS:
                    continue
                if req not in params:
                    raise ValueError(
                        f"Stored script '{script_name}' requires parameter '{req}'"
                    )

        logger.info(f"Resolved script_name '{script_name}' to stored script code")

    # Validate required fields
    if not script_code:
        logger.error(
            f"Invalid payload for script_execution task (missing script_code and script_name): {payload}"
        )
        raise ValueError(
            "Missing required field in payload: script_code or script_name"
        )

    if listener_id:
        logger.info(
            f"Starting script execution for listener {listener_id} in conversation {conversation_id}"
        )
    else:
        logger.info(
            f"Starting scheduled script execution in conversation {conversation_id}"
        )

    # Resolve the processing profile the script was created under so that the
    # tools available here match those used to validate the script at creation
    # time. Legacy automations (no recorded profile) fall back to the default.
    processing_service = _resolve_script_execution_service(
        exec_context, payload.get("processing_profile_id")
    )
    tools_provider = None

    if processing_service and hasattr(processing_service, "tools_provider"):
        tools_provider = processing_service.tools_provider
        # Re-point the execution context at the resolved profile so tool policy,
        # visibility grants and note labels all reflect the creating profile.
        # The owner also becomes the acting user, so anything the script itself
        # creates (e.g. via create_automation) inherits the same owner.
        exec_context = replace(
            exec_context,
            processing_service=processing_service,
            processing_profile_id=processing_service.service_config.id,
            user_id=payload.get("created_by_user_id") or exec_context.user_id,
            tools_provider=tools_provider,
            # Carry the resolved profile's infrastructure backends so tools that
            # read them use the creating profile's clients, not the worker
            # default's (mirrors _build_confirmation_execution_context).
            home_assistant_client=processing_service.home_assistant_client,
            attachment_registry=processing_service.attachment_registry,
            camera_backend=processing_service.camera_backend,
            visibility_grants=(
                set(processing_service.service_config.visibility_grants)
                if processing_service.service_config.visibility_grants
                else None
            ),
            default_note_visibility_labels=(
                processing_service.service_config.default_note_visibility_labels
            ),
            request_confirmation_callback=build_script_confirmation_callback(
                payload.get("created_by_user_id")
            ),
        )
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
        default_timezone=exec_context.timezone,
    )

    # Prepare global variables for the script
    script_globals = {
        "event": event_data,
        "conversation_id": conversation_id,
        "listener_id": listener_id,
        "listener_name": config.get("listener_name", ""),
    }
    # Merge script_parameters, but don't overwrite built-in context variables
    script_parameters = payload.get("script_parameters")
    if isinstance(script_parameters, dict):
        for k, v in script_parameters.items():
            if k not in script_globals:
                script_globals[k] = v

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


def _tool_result_text(result: str | ToolResult) -> str:
    """Return user-facing text for a confirmed tool execution result."""
    if isinstance(result, ToolResult):
        return result.get_text()
    return str(result)


async def _register_confirmation_result_attachments(
    context: ToolExecutionContext,
    request: ConfirmationRequestRow,
    result: str | ToolResult,
) -> list[str] | None:
    """Register tool result attachments for fallback chat delivery."""
    if not isinstance(result, ToolResult) or not result.attachments:
        return None

    attachment_ids: list[str] = []
    for attachment in result.attachments:
        if attachment.attachment_id is not None:
            attachment_ids.append(attachment.attachment_id)
            continue

        if attachment.content is None:
            continue

        if context.attachment_registry is None:
            raise ConfirmationNotificationError(
                f"Confirmation {request['id']} result has attachments but no "
                "attachment registry is available"
            )

        file_extension = get_file_extension_from_mime_type(attachment.mime_type)
        registered_metadata = (
            await context.attachment_registry.store_and_register_tool_attachment(
                file_content=attachment.content,
                filename=f"confirmation_result_{uuid.uuid4()}{file_extension}",
                content_type=attachment.mime_type,
                tool_name=request["tool_name"],
                description=attachment.description
                or f"Output from {request['tool_name']}",
                conversation_id=context.conversation_id,
                metadata={
                    "tool_call_id": request["tool_call_id"] or request["id"],
                    "confirmation_request_id": request["id"],
                    "auto_display": True,
                },
                db_context=context.db_context,
            )
        )
        attachment.attachment_id = registered_metadata.attachment_id
        attachment_ids.append(registered_metadata.attachment_id)

    return attachment_ids or None


async def _build_confirmation_execution_context(
    exec_context: ToolExecutionContext,
    request: ConfirmationRequestRow,
    source_row: MessageHistoryRow | None,
    processing_service: ProcessingService,
) -> ToolExecutionContext:
    """Reconstruct the best available context for deferred tool execution.

    Prefers the source message's interface/conversation; confirmations created
    without one (automation scripts) instead carry the origin recorded on the
    request itself, so approved tools act in the requesting conversation rather
    than the worker's placeholder context.
    """
    interface_type = (
        str(source_row["interface_type"])
        if source_row is not None
        else (
            request["origin_interface_type"]
            or request["resolved_via_interface"]
            or exec_context.interface_type
        )
    )
    conversation_id = (
        str(source_row["conversation_id"])
        if source_row is not None
        else request["origin_conversation_id"] or exec_context.conversation_id
    )
    turn_id = (
        str(source_row["turn_id"])
        if source_row is not None and source_row.get("turn_id") is not None
        else exec_context.turn_id
    )
    user_name = exec_context.user_name or str(request["target_user_id"])
    # The profile id must match the service actually used (already resolved from
    # the confirmation's recorded profile or the source message), so tools that
    # read processing_profile_id directly (history filtering, automation
    # stamping) see the same profile their provider belongs to.
    processing_profile_id = processing_service.service_config.id
    subconversation_id = (
        str(source_row["subconversation_id"])
        if source_row is not None and source_row.get("subconversation_id") is not None
        else exec_context.subconversation_id
    )

    chat_interface = exec_context.chat_interface
    if exec_context.chat_interfaces is not None:
        chat_interface = exec_context.chat_interfaces.get(
            interface_type, chat_interface
        )

    tools_provider = processing_service.tools_provider

    async def approved_confirmation_callback(
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        # ast-grep-ignore: no-dict-any - confirmation callback protocol carries arbitrary tool arguments
        tool_args: dict[str, Any],
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        _ = interface_type
        _ = conversation_id
        _ = turn_id
        _ = timeout_seconds
        _ = context

        expected_call_id = request["tool_call_id"] or request["id"]
        if tool_name == request["tool_name"] and tool_args == request["tool_args_json"]:
            if call_id != expected_call_id:
                logger.info(
                    "Approved confirmation %s accepted nested confirmation "
                    "callback %s for tool %s",
                    request["id"],
                    call_id,
                    tool_name,
                )
            return ConfirmationOutcome(kind="approved")

        logger.warning(
            "Approved confirmation %s did not satisfy nested or mismatched "
            "confirmation request for tool %s",
            request["id"],
            tool_name,
        )
        return ConfirmationOutcome(kind="rejected")

    return ToolExecutionContext(
        interface_type=interface_type,
        conversation_id=conversation_id,
        user_name=user_name,
        user_id=request["target_user_id"],
        turn_id=turn_id,
        db_context=exec_context.db_context,
        processing_service=processing_service,
        clock=exec_context.clock,
        home_assistant_client=processing_service.home_assistant_client,
        event_sources=exec_context.event_sources,
        attachment_registry=processing_service.attachment_registry,
        camera_backend=processing_service.camera_backend,
        chat_interface=chat_interface,
        chat_interfaces=exec_context.chat_interfaces,
        confirmation_ui_managers=exec_context.confirmation_ui_managers,
        timezone=processing_service.service_config.timezone,
        processing_profile_id=processing_profile_id,
        subconversation_id=subconversation_id,
        request_confirmation_callback=approved_confirmation_callback,
        update_activity_callback=exec_context.update_activity_callback,
        embedding_generator=exec_context.embedding_generator,
        indexing_source=exec_context.indexing_source,
        tools_provider=tools_provider,
        visibility_grants=processing_service.service_config.visibility_grants,
        default_note_visibility_labels=(
            processing_service.service_config.default_note_visibility_labels
        ),
        note_registry=processing_service.service_config.note_registry,
        confirmation_result_waiters=exec_context.confirmation_result_waiters,
    )


def _build_confirmation_notification_context(
    exec_context: ToolExecutionContext,
    request: ConfirmationRequestRow,
    source_row: MessageHistoryRow | None,
) -> ToolExecutionContext:
    """Reconstruct enough context to notify the original conversation."""
    if source_row is None:
        return exec_context

    interface_type = str(source_row["interface_type"])
    chat_interface = exec_context.chat_interface
    if exec_context.chat_interfaces is not None:
        chat_interface = exec_context.chat_interfaces.get(
            interface_type,
            chat_interface,
        )

    return replace(
        exec_context,
        interface_type=interface_type,
        conversation_id=str(source_row["conversation_id"]),
        turn_id=(
            str(source_row["turn_id"])
            if source_row.get("turn_id") is not None
            else exec_context.turn_id
        ),
        user_id=request["target_user_id"],
        chat_interface=chat_interface,
        processing_profile_id=(
            str(source_row["processing_profile_id"])
            if source_row.get("processing_profile_id") is not None
            else exec_context.processing_profile_id
        ),
        subconversation_id=(
            str(source_row["subconversation_id"])
            if source_row.get("subconversation_id") is not None
            else exec_context.subconversation_id
        ),
    )


def _get_processing_tools_provider(exec_context: ToolExecutionContext) -> ToolsProvider:
    """Return the current tool provider from the processing service."""
    processing_service = exec_context.processing_service
    if processing_service is None:
        raise RuntimeError("Confirmation execution requires a processing service")
    return processing_service.tools_provider


def _resolve_confirmation_processing_service(
    exec_context: ToolExecutionContext,
    source_row: MessageHistoryRow | None,
    recorded_profile_id: str | None = None,
) -> ProcessingService:
    """Resolve the local processing service that originally created the confirmation.

    Prefers the profile recorded on the confirmation request itself, falling back
    to the source message's profile. Script-originated confirmations have no
    source message row, so the recorded profile is what keeps the deferred
    execution on the automation's creating profile.
    """
    default_processing_service = exec_context.processing_service
    if default_processing_service is None:
        raise RuntimeError("Confirmation execution requires a processing service")

    profile_id = recorded_profile_id
    if profile_id is None and source_row is not None:
        source_profile = source_row.get("processing_profile_id")
        if source_profile is not None:
            profile_id = str(source_profile)
    if profile_id is None or profile_id == default_processing_service.service_config.id:
        return default_processing_service

    registry = default_processing_service.processing_services_registry
    if registry is None:
        raise RuntimeError(
            "Confirmation execution cannot resolve non-default profile "
            f"'{profile_id}' without a processing service registry"
        )

    candidate = registry.get(profile_id)
    if candidate is None:
        raise RuntimeError(
            f"Confirmation execution profile '{profile_id}' is no longer available"
        )
    if getattr(candidate, "kind", None) != "local":
        raise RuntimeError(
            "Confirmation execution requires a local processing profile, got "
            f"'{profile_id}' of kind '{getattr(candidate, 'kind', None)}'"
        )
    if not hasattr(candidate, "tools_provider") or not hasattr(
        candidate, "service_config"
    ):
        raise RuntimeError(
            "Confirmation execution resolved an unsupported local profile object "
            f"for '{profile_id}'"
        )
    return cast("ProcessingService", candidate)


def _resolve_confirmation_result_delivery(
    context: ToolExecutionContext,
    request: ConfirmationRequestRow,
    source_row: MessageHistoryRow | None,
) -> tuple[ChatInterface, str, str | None] | None:
    """Pick where to deliver a confirmation result message.

    Chat/email confirmations thread the result back to the originating
    conversation (replying to the source message). Automation-originated
    confirmations have no source message, so the result is delivered to the
    target user's primary Telegram chat instead (mirroring how the pending
    confirmation was delivered). Returns ``(chat_interface, conversation_id,
    reply_to_interface_id)`` or None when no deliverable target exists.
    """
    if source_row is not None:
        if context.chat_interface is None:
            return None
        reply_to_interface_id = (
            str(source_row["interface_message_id"])
            if source_row.get("interface_message_id") is not None
            else None
        )
        return context.chat_interface, context.conversation_id, reply_to_interface_id

    processing_service = context.processing_service
    if processing_service is None or context.chat_interfaces is None:
        return None
    telegram_interface = context.chat_interfaces.get("telegram")
    if telegram_interface is None:
        return None
    telegram_user_id = UserIdentityResolver(
        processing_service.app_config
    ).get_primary_telegram_user_id(request["target_user_id"])
    if telegram_user_id is None:
        return None
    return telegram_interface, str(telegram_user_id), None


async def _notify_confirmation_execution_result(
    context: ToolExecutionContext,
    request: ConfirmationRequestRow,
    result: str | ToolResult,
    source_row: MessageHistoryRow | None,
    *,
    succeeded: bool = True,
) -> None:
    """Send a deterministic result notification when no live waiter consumes it."""
    delivery = _resolve_confirmation_result_delivery(context, request, source_row)
    if delivery is None:
        logger.info(
            "Confirmation %s completed without a deliverable notification target; "
            "skipping result notification",
            request["id"],
        )
        return

    chat_interface, delivery_conversation_id, reply_to_interface_id = delivery

    try:
        result_text = _tool_result_text(result)
        attachment_ids = await _register_confirmation_result_attachments(
            context,
            request,
            result,
        )

        if succeeded:
            message = (
                "Approved action completed.\n\n"
                f"Tool: {request['tool_name']}\n\n"
                f"Result:\n{result_text}"
            )
        else:
            message = (
                "Approved action failed.\n\n"
                f"Tool: {request['tool_name']}\n\n"
                f"Error:\n{result_text}"
            )
        sent_message_id = await chat_interface.send_message(
            conversation_id=delivery_conversation_id,
            text=message,
            reply_to_interface_id=reply_to_interface_id,
            attachment_ids=attachment_ids,
        )
        if sent_message_id is None:
            raise ConfirmationNotificationError(
                f"Confirmation {request['id']} result notification was not delivered"
            )
    except ConfirmationNotificationError:
        raise
    except Exception as exc:
        raise ConfirmationNotificationError(
            f"Confirmation {request['id']} result notification failed"
        ) from exc


async def handle_confirmation_tool_execution(
    exec_context: ToolExecutionContext,
    payload: ConfirmationToolExecutionPayload,
) -> None:
    """Execute the exact tool invocation stored on an approved confirmation."""
    request_id = payload.get("confirmation_request_id")
    if not request_id:
        raise ValueError("Missing confirmation_request_id in task payload")

    request = await exec_context.db_context.confirmation_requests.get(request_id)
    if request is None:
        raise ValueError(f"Confirmation request {request_id} not found")

    if request["status"] != "approved":
        logger.info(
            "Skipping confirmation execution for request %s with status %s",
            request_id,
            request["status"],
        )
        return

    source_row = None
    source_message_internal_id = request["source_message_internal_id"]
    if source_message_internal_id is not None:
        source_row = (
            await exec_context.db_context.message_history.get_row_by_internal_id(
                source_message_internal_id,
            )
        )

    execution_context = _build_confirmation_notification_context(
        exec_context,
        request,
        source_row,
    )

    async def deliver_execution_failure(
        context: ToolExecutionContext,
        error_result: str,
    ) -> None:
        delivered_to_waiter = False
        if context.confirmation_result_waiters is not None:
            delivered_to_waiter = context.confirmation_result_waiters.resolve_failed(
                request_id,
                error_result,
            )
        if not delivered_to_waiter:
            try:
                await _notify_confirmation_execution_result(
                    context,
                    request,
                    error_result,
                    source_row,
                    succeeded=False,
                )
            except ConfirmationNotificationError as notification_exc:
                logger.exception(
                    "Confirmation %s failed and failure notification could not be sent: %s",
                    request_id,
                    notification_exc,
                )

    try:
        processing_service = _resolve_confirmation_processing_service(
            exec_context,
            source_row,
            request.get("processing_profile_id"),
        )
        execution_context = await _build_confirmation_execution_context(
            exec_context,
            request,
            source_row,
            processing_service,
        )
        tools_provider = _get_processing_tools_provider(execution_context)
        call_id = request["tool_call_id"] or request["id"]

        logger.info(
            "Executing approved confirmation %s as tool %s",
            request_id,
            request["tool_name"],
        )
        result = await tools_provider.execute_tool(
            request["tool_name"],
            request["tool_args_json"],
            execution_context,
            call_id,
        )
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None:
            # wait_for() cancels the handler coroutine on timeout. Clear that
            # cancellation while delivering the deterministic failure outcome,
            # then re-raise so the worker still records the task timeout.
            current_task.uncancel()
        try:
            async with asyncio.timeout(CONFIRMATION_CANCELLATION_CLEANUP_TIMEOUT):
                await deliver_execution_failure(
                    execution_context,
                    f"Error executing approved tool '{request['tool_name']}': "
                    "execution was cancelled",
                )
        except TimeoutError:
            logger.warning(
                "Timed out while delivering failure outcome for cancelled "
                "confirmation %s",
                request_id,
            )
        raise
    except Exception as exc:
        error_result = f"Error executing approved tool '{request['tool_name']}': {exc}"
        await deliver_execution_failure(execution_context, error_result)
        raise

    if (
        execution_context.confirmation_result_waiters is not None
        and execution_context.confirmation_result_waiters.resolve_completed(
            request_id,
            result,
        )
    ):
        logger.info(
            "Delivered confirmation %s result to live waiter",
            request_id,
        )
        return

    try:
        await _notify_confirmation_execution_result(
            execution_context,
            request,
            result,
            source_row,
        )
    except ConfirmationNotificationError as notification_exc:
        logger.exception(
            "Confirmation %s executed successfully but result notification failed: %s",
            request_id,
            notification_exc,
        )
        raise


async def handle_reindex_document(
    exec_context: ToolExecutionContext,
    payload: ReindexDocumentPayload,
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
    "SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE",
    "TaskWorker",
    "handle_log_message",
    "handle_llm_callback",
    "handle_system_event_cleanup",
    "handle_system_error_log_cleanup",
    "handle_script_execution",
    "handle_confirmation_tool_execution",
    "handle_reindex_document",
    "build_script_confirmation_callback",
]  # Export class and relevant handlers
