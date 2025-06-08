"""Tool confirmation renderers.

This module contains functions for rendering confirmation prompts
for tools that require user confirmation before execution.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _format_event_details_for_confirmation(
    details: dict[str, Any] | None, timezone_str: str
) -> str:
    """Formats fetched event details for inclusion in confirmation prompts."""
    from family_assistant import calendar_integration

    if not details:
        return "Event details not found."
    summary = details.get("summary", "No Title")
    start_obj = details.get("start")
    end_obj = details.get("end")

    start_str = (
        calendar_integration.format_datetime_or_date(
            start_obj, timezone_str, is_end=False
        )
        if start_obj
        else "Unknown Start Time"
    )
    end_str = (
        calendar_integration.format_datetime_or_date(end_obj, timezone_str, is_end=True)
        if end_obj
        else "Unknown End Time"
    )
    all_day = details.get("all_day", False)
    if all_day:
        # All-day events typically don't need timezone formatting, but pass it anyway for consistency
        # Or adjust format_datetime_or_date to handle date objects without requiring timezone_str
        # Assuming format_datetime_or_date handles date objects gracefully.
        return f"'{summary}' (All Day: {start_str})"
    else:
        return f"'{summary}' ({start_str} - {end_str})"


def render_delete_calendar_event_confirmation(
    args: dict[str, Any], event_details: dict[str, Any] | None, timezone_str: str
) -> str:
    """Renders the confirmation message for deleting a calendar event."""
    from family_assistant.telegram_bot import telegramify_markdown

    event_desc = _format_event_details_for_confirmation(
        event_details, timezone_str
    )  # Pass timezone
    args.get("calendar_url", "Unknown Calendar")
    # Use MarkdownV2 compatible formatting
    return (
        f"Please confirm you want to *delete* the event:\n"
        f"Event: {telegramify_markdown.escape_markdown(event_desc)}"
        # Removed calendar URL line: f"From Calendar: `{telegramify_markdown.escape_markdown(cal_url)}`"
    )


def render_modify_calendar_event_confirmation(
    args: dict[str, Any], event_details: dict[str, Any] | None, timezone_str: str
) -> str:
    """Renders the confirmation message for modifying a calendar event."""
    from family_assistant.telegram_bot import telegramify_markdown

    event_desc = _format_event_details_for_confirmation(
        event_details, timezone_str
    )  # Pass timezone
    args.get("calendar_url", "Unknown Calendar")
    changes = []
    # Use MarkdownV2 compatible formatting for code blocks/inline code
    if args.get("new_summary"):
        changes.append(
            f"\\- Set summary to: `{telegramify_markdown.escape_markdown(args['new_summary'])}`"
        )
    if args.get("new_start_time"):
        changes.append(
            f"\\- Set start time to: `{telegramify_markdown.escape_markdown(args['new_start_time'])}`"
        )
    if args.get("new_end_time"):
        changes.append(
            f"\\- Set end time to: `{telegramify_markdown.escape_markdown(args['new_end_time'])}`"
        )
    if args.get("new_description"):
        changes.append(
            f"\\- Set description to: `{telegramify_markdown.escape_markdown(args['new_description'])}`"
        )
    if args.get("new_all_day") is not None:
        changes.append(f"\\- Set all\\-day status to: `{args['new_all_day']}`")

    return (
        f"Please confirm you want to *modify* the event:\n"
        f"Event: {telegramify_markdown.escape_markdown(event_desc)}\n"
        # Removed calendar URL line: f"From Calendar: `{telegramify_markdown.escape_markdown(cal_url)}`\n"
        f"With the following changes:\n" + "\n".join(changes)
    )


# Mapping of tool names to their confirmation renderers
TOOL_CONFIRMATION_RENDERERS: dict[
    str, Any  # Actually Callable[[dict[str, Any], dict[str, Any] | None, str], str]
] = {
    "delete_calendar_event": render_delete_calendar_event_confirmation,
    "modify_calendar_event": render_modify_calendar_event_confirmation,
}
