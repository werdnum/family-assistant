"""Tool confirmation renderers.

This module contains functions for rendering confirmation prompts
for tools that require user confirmation before execution.
"""

from __future__ import annotations

import logging
from typing import Any

import telegramify_markdown

from family_assistant import calendar_integration

logger = logging.getLogger(__name__)


def _format_event_details_for_confirmation(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    details: dict[str, Any] | None,
    timezone_str: str,
) -> str:
    """Formats fetched event details for inclusion in confirmation prompts."""
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
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    args: dict[str, Any],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    event_details: dict[str, Any] | None = None,
    timezone_str: str = "UTC",
) -> str:
    """Renders the confirmation message for deleting a calendar event.

    Args:
        args: Tool arguments containing uid and calendar_url
        event_details: Optional event details for richer display
        timezone_str: Timezone for formatting times (default: UTC)
    """
    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(event_details, timezone_str)

    # Use MarkdownV2 compatible formatting
    return (
        f"Please confirm you want to *delete* the event:\n"
        f"Event: {telegramify_markdown.escape_markdown(event_desc)}"
    )


def render_modify_calendar_event_confirmation(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    args: dict[str, Any],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    event_details: dict[str, Any] | None = None,
    timezone_str: str = "UTC",
) -> str:
    """Renders the confirmation message for modifying a calendar event.

    Args:
        args: Tool arguments containing uid, calendar_url, and modification fields
        event_details: Optional event details for richer display
        timezone_str: Timezone for formatting times (default: UTC)
    """
    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(event_details, timezone_str)

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
        f"With the following changes:\n" + "\n".join(changes)
    )


# Mapping of tool names to their confirmation renderers
# Signature: (args: dict, event_details: dict | None = None, timezone_str: str = "UTC") -> str
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
TOOL_CONFIRMATION_RENDERERS: dict[str, Any] = {
    "delete_calendar_event": render_delete_calendar_event_confirmation,
    "modify_calendar_event": render_modify_calendar_event_confirmation,
}
