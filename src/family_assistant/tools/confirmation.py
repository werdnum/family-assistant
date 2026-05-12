"""Tool confirmation renderers.

This module contains functions for rendering confirmation prompts
for tools that require user confirmation before execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, cast

from family_assistant import calendar_integration

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.types import (
        CalendarConfig,
        CalendarEvent,
        ToolArgumentsView,
        ToolExecutionContext,
    )

logger = logging.getLogger(__name__)
CONFIRMATION_VALUE_MAX_CHARS = 1200


def _markdown_code_block(text: str) -> str:
    """Render text as inert markdown using a fence longer than any content fence."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def _confirmation_value(value: object, *, max_chars: int = 1200) -> str:
    """Return a bounded value for confirmation prompts."""
    text = "" if value is None else str(value)
    if len(text) > max_chars:
        text = text[:max_chars] + "... [truncated]"
    return text


def _confirmation_field(label: str, value: object) -> str:
    """Format a single confirmation field."""
    return (
        f"- {label}:\n"
        f"{_markdown_code_block(_confirmation_value(value, max_chars=CONFIRMATION_VALUE_MAX_CHARS))}"
    )


def _extract_calendar_config_from_provider(
    provider: ToolsProvider | None,
) -> CalendarConfig | None:
    """Extract calendar config from a tools provider.

    This helper avoids circular imports by using TYPE_CHECKING and runtime isinstance checks.
    """
    if provider is None:
        return None

    # Import here to avoid circular dependency at module load time
    from family_assistant.tools.infrastructure import (  # noqa: PLC0415
        CompositeToolsProvider,
        LocalToolsProvider,
    )

    # Direct LocalToolsProvider
    if isinstance(provider, LocalToolsProvider):
        config = provider.get_calendar_config()
        return cast("CalendarConfig", config) if config else None

    # ConfirmingToolsProvider or other wrapper
    if hasattr(provider, "wrapped_provider"):
        wrapped = provider.wrapped_provider  # type: ignore[attr-defined]
        if isinstance(wrapped, LocalToolsProvider):
            config = wrapped.get_calendar_config()
            return cast("CalendarConfig", config) if config else None
        elif isinstance(wrapped, CompositeToolsProvider):
            for p in wrapped.get_providers():
                if isinstance(p, LocalToolsProvider):
                    config = p.get_calendar_config()
                    return cast("CalendarConfig", config) if config else None

    return None


class ConfirmationRenderer(Protocol):
    """Protocol for confirmation prompt renderers.

    Confirmation renderers are responsible for fetching any necessary data
    and formatting a human-readable confirmation prompt. They receive the
    full ToolExecutionContext to access configuration, timezone, etc.
    """

    async def __call__(
        self,
        args: ToolArgumentsView,
        context: ToolExecutionContext,
    ) -> str:
        """Render a confirmation prompt from tool arguments.

        Args:
            args: Tool arguments (e.g., uid, calendar_url for calendar tools)
            context: Execution context with timezone, calendar config, etc.

        Returns:
            Formatted confirmation prompt string
        """
        ...


def _format_event_details_for_confirmation(
    details: CalendarEvent | None,
    timezone: ZoneInfo,
) -> str:
    """Formats fetched event details for inclusion in confirmation prompts."""
    if not details:
        return "Event details not found."
    summary = details.get("summary", "No Title")
    start_obj = details.get("start")
    end_obj = details.get("end")

    start_str = (
        calendar_integration.format_datetime_or_date(start_obj, timezone, is_end=False)
        if start_obj
        else "Unknown Start Time"
    )
    end_str = (
        calendar_integration.format_datetime_or_date(end_obj, timezone, is_end=True)
        if end_obj
        else "Unknown End Time"
    )
    all_day = details.get("all_day", False)
    if all_day:
        return f"'{summary}' (All Day: {start_str})"
    else:
        return f"'{summary}' ({start_str} - {end_str})"


async def render_delete_calendar_event_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Renders the confirmation message for deleting a calendar event.

    Fetches event details from the calendar to provide a meaningful prompt.

    Args:
        args: Tool arguments including uid and calendar_url
        context: Execution context with calendar config and timezone
    """
    # Fetch event details to show the user what they're deleting
    event_details = None
    raw_uid = args.get("uid")
    raw_calendar_url = args.get("calendar_url")
    uid = raw_uid if isinstance(raw_uid, str) else None
    calendar_url = raw_calendar_url if isinstance(raw_calendar_url, str) else None

    if uid and calendar_url:
        # Get calendar config from the tools provider
        calendar_config = _extract_calendar_config_from_provider(
            getattr(context, "tools_provider", None)
        )

        if calendar_config:
            # fetch_event_details_for_confirmation returns None on error
            event_details = (
                await calendar_integration.fetch_event_details_for_confirmation(
                    uid=uid,
                    calendar_url=calendar_url,
                    calendar_config=calendar_config,
                    timezone=context.timezone,
                )
            )

    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(event_details, context.timezone)

    return (
        "Please confirm you want to *delete* the event:\n"
        f"Event:\n{_markdown_code_block(event_desc)}"
    )


async def render_modify_calendar_event_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Renders the confirmation message for modifying a calendar event.

    Fetches event details from the calendar to provide a meaningful prompt.

    Args:
        args: Tool arguments including uid, calendar_url, and modification fields
        context: Execution context with calendar config and timezone
    """
    # Fetch event details to show the user what they're modifying
    event_details = None
    raw_uid = args.get("uid")
    raw_calendar_url = args.get("calendar_url")
    uid = raw_uid if isinstance(raw_uid, str) else None
    calendar_url = raw_calendar_url if isinstance(raw_calendar_url, str) else None

    if uid and calendar_url:
        # Get calendar config from the tools provider
        calendar_config = _extract_calendar_config_from_provider(
            getattr(context, "tools_provider", None)
        )

        if calendar_config:
            # fetch_event_details_for_confirmation returns None on error
            event_details = (
                await calendar_integration.fetch_event_details_for_confirmation(
                    uid=uid,
                    calendar_url=calendar_url,
                    calendar_config=calendar_config,
                    timezone=context.timezone,
                )
            )

    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(event_details, context.timezone)

    changes = []
    if args.get("new_summary") is not None:
        changes.append(
            "- Set summary to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_summary']))}"
        )
    if args.get("new_start_time") is not None:
        changes.append(
            "- Set start time to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_start_time']))}"
        )
    if args.get("new_end_time") is not None:
        changes.append(
            "- Set end time to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_end_time']))}"
        )
    if args.get("new_description") is not None:
        changes.append(
            "- Set description to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_description']))}"
        )
    if args.get("new_all_day") is not None:
        changes.append(
            "- Set all-day status to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_all_day']))}"
        )

    return (
        f"Please confirm you want to *modify* the event:\n"
        f"Event:\n{_markdown_code_block(event_desc)}\n"
        f"With the following changes:\n" + "\n".join(changes)
    )


async def render_add_calendar_event_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for creating a calendar event."""
    _ = context
    fields = [
        _confirmation_field("Title", args.get("summary")),
        _confirmation_field("Start", args.get("start_time")),
        _confirmation_field("End", args.get("end_time")),
        _confirmation_field("All day", args.get("all_day", False)),
    ]
    if args.get("location"):
        fields.append(_confirmation_field("Location", args.get("location")))
    if args.get("recurrence_rule"):
        fields.append(_confirmation_field("Recurrence", args.get("recurrence_rule")))
    if args.get("description"):
        fields.append(_confirmation_field("Description", args.get("description")))
    return "Please confirm you want to *create* this calendar event:\n" + "\n".join(
        fields
    )


async def render_add_or_update_note_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for creating or updating a note."""
    _ = context
    fields = [
        _confirmation_field("Title", args.get("title")),
        _confirmation_field("Append", args.get("append", False)),
        _confirmation_field("Include in prompt", args.get("include_in_prompt", False)),
        _confirmation_field("Content", args.get("content")),
    ]
    if args.get("visibility_labels"):
        fields.append(
            _confirmation_field("Visibility labels", args["visibility_labels"])
        )
    return "Please confirm you want to *save* this note:\n" + "\n".join(fields)


async def render_schedule_reminder_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for scheduling a reminder."""
    _ = context
    fields = [
        _confirmation_field("Reminder time", args.get("reminder_time")),
        _confirmation_field("Message", args.get("message")),
    ]
    if args.get("follow_up"):
        fields.extend([
            _confirmation_field("Follow up", args.get("follow_up")),
            _confirmation_field("Follow-up interval", args.get("follow_up_interval")),
            _confirmation_field("Max follow-ups", args.get("max_follow_ups")),
        ])
    return "Please confirm you want to *schedule* this reminder:\n" + "\n".join(fields)


async def render_schedule_future_callback_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for scheduling a future assistant callback."""
    _ = context
    fields = [
        _confirmation_field("Callback time", args.get("callback_time")),
        _confirmation_field("Context", args.get("context")),
    ]
    return (
        "Please confirm you want to *schedule* this future assistant callback:\n"
        + "\n".join(fields)
    )


async def render_modify_pending_callback_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for modifying a pending callback."""
    _ = context
    fields = [_confirmation_field("Task ID", args.get("task_id"))]
    if args.get("new_callback_time") is not None:
        fields.append(_confirmation_field("New time", args.get("new_callback_time")))
    if args.get("new_context") is not None:
        fields.append(_confirmation_field("New context", args.get("new_context")))
    return "Please confirm you want to *modify* this callback:\n" + "\n".join(fields)


async def render_send_message_to_user_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for sending a message to a known user."""
    _ = context
    fields = [
        _confirmation_field("Target conversation", args.get("target_chat_id")),
        _confirmation_field("Message", args.get("message_content")),
    ]
    if args.get("attachment_ids"):
        fields.append(_confirmation_field("Attachments", args.get("attachment_ids")))
    return "Please confirm you want to *send* this message:\n" + "\n".join(fields)


async def render_ingest_document_from_url_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for ingesting a document from a URL."""
    _ = context
    fields = [
        _confirmation_field("URL", args.get("url_to_ingest")),
        _confirmation_field("Source type", args.get("source_type")),
        _confirmation_field("Source ID", args.get("source_id")),
    ]
    if args.get("title"):
        fields.append(_confirmation_field("Title", args.get("title")))
    if args.get("metadata_json"):
        fields.append(_confirmation_field("Metadata", args.get("metadata_json")))
    return (
        "Please confirm you want to *fetch and index* this document"
        " (the source type and ID determine which document record is created or overwritten):\n"
        + "\n".join(fields)
    )


async def render_delegate_to_service_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for delegating a task to another service."""
    target_service_id = str(args.get("target_service_id", "")).strip()
    user_request = str(args.get("user_request", "")).strip()

    prompt_target = target_service_id if target_service_id else "target service"
    request_preview = user_request[:100]
    if user_request and len(user_request) > 100:
        request_preview += "..."

    _ = context
    return (
        "Do you want to delegate this task:\n"
        f"{_markdown_code_block(request_preview)}\n"
        "to this profile?\n"
        f"{_markdown_code_block(prompt_target)}"
    )


# Mapping of tool names to their confirmation renderers
TOOL_CONFIRMATION_RENDERERS: dict[str, ConfirmationRenderer] = {
    "add_calendar_event": render_add_calendar_event_confirmation,
    "delete_calendar_event": render_delete_calendar_event_confirmation,
    "modify_calendar_event": render_modify_calendar_event_confirmation,
    "add_or_update_note": render_add_or_update_note_confirmation,
    "schedule_reminder": render_schedule_reminder_confirmation,
    "schedule_future_callback": render_schedule_future_callback_confirmation,
    "modify_pending_callback": render_modify_pending_callback_confirmation,
    "send_message_to_user": render_send_message_to_user_confirmation,
    "ingest_document_from_url": render_ingest_document_from_url_confirmation,
    "delegate_to_service": render_delegate_to_service_confirmation,
}
