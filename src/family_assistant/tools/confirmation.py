"""Tool confirmation renderers.

This module contains functions for rendering confirmation prompts
for tools that require user confirmation before execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

import telegramify_markdown

from family_assistant import calendar_integration

if TYPE_CHECKING:
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.types import CalendarConfig, ToolExecutionContext

logger = logging.getLogger(__name__)


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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        args: dict[str, Any],
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


async def render_delete_calendar_event_confirmation(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    args: dict[str, Any],
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
    uid = args.get("uid")
    calendar_url = args.get("calendar_url")

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
                )
            )

    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(
        event_details, context.timezone_str
    )

    # Use MarkdownV2 compatible formatting
    return (
        f"Please confirm you want to *delete* the event:\n"
        f"Event: {telegramify_markdown.escape_markdown(event_desc)}"
    )


async def render_modify_calendar_event_confirmation(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    args: dict[str, Any],
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
    uid = args.get("uid")
    calendar_url = args.get("calendar_url")

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
                )
            )

    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(
        event_details, context.timezone_str
    )

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
TOOL_CONFIRMATION_RENDERERS: dict[str, ConfirmationRenderer] = {
    "delete_calendar_event": render_delete_calendar_event_confirmation,
    "modify_calendar_event": render_modify_calendar_event_confirmation,
}
