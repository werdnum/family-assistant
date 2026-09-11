"""Calendar tools for the Family Assistant.

This module contains all calendar-related tool implementations that can be
used by the LLM to manage calendar events via CalDAV.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo

import caldav
import httpx
import vobject
from caldav.lib.error import DAVError, NotFoundError
from dateutil.parser import isoparse

from family_assistant.calendar_integration import (
    CalendarSource,
    fetch_ical_events_async,
    resolve_calendar_sources,
)
from family_assistant.similarity import create_similarity_strategy_from_config

if TYPE_CHECKING:
    from family_assistant.tools.types import (
        CalendarConfig,
        ToolDefinition,
        ToolExecutionContext,
    )

logger = logging.getLogger(__name__)


class CalendarSearchResult(TypedDict):
    """Structured calendar event result for search and duplicate detection."""

    summary: str
    uid: str
    start: str
    end: str
    calendar_url: str | None
    similarity: NotRequired[float | None]
    source_id: NotRequired[str | None]
    source_name: NotRequired[str | None]
    source_kind: NotRequired[Literal["caldav", "ical"] | None]
    writable: NotRequired[bool | None]
    start_dt: NotRequired[datetime | date | None]


def _event_sort_key(event: CalendarSearchResult, local_tz: ZoneInfo) -> datetime:
    s_dt = event.get("start_dt")
    if isinstance(s_dt, datetime):
        if s_dt.tzinfo is None:
            return s_dt.replace(tzinfo=local_tz)
        return s_dt.astimezone(local_tz)
    if isinstance(s_dt, date):
        return datetime.combine(s_dt, time.min, tzinfo=local_tz)
    s_str = event.get("start", "")
    try:
        parsed = isoparse(s_str)
        if isinstance(parsed, datetime):
            return (
                parsed.replace(tzinfo=local_tz)
                if parsed.tzinfo is None
                else parsed.astimezone(local_tz)
            )
        if isinstance(parsed, date):
            return datetime.combine(parsed, time.min, tzinfo=local_tz)
    except Exception:
        pass
    return datetime.min.replace(tzinfo=local_tz)


def _format_event_time_for_display(
    dt_or_date: datetime | date | str,
    local_tz: ZoneInfo,
) -> str:
    """Formats a datetime, date, or date string into a user-friendly display string."""
    if isinstance(dt_or_date, str):
        return dt_or_date
    if isinstance(dt_or_date, datetime):
        if dt_or_date.tzinfo is None:
            dt_or_date = dt_or_date.replace(tzinfo=local_tz)
        return dt_or_date.astimezone(local_tz).strftime("%Y-%m-%d %H:%M %Z")
    return str(dt_or_date)


def _parse_caldav_event_component(
    event: caldav.objects.Event,
    src: CalendarSource,
    local_tz: ZoneInfo,
) -> CalendarSearchResult | None:
    try:
        vevent = event.icalendar_component
        summary = str(vevent.get("summary", ""))
        uid = str(vevent.get("uid", ""))
        dtstart = vevent.get("dtstart")
        dtend = vevent.get("dtend")
    except Exception as e:
        logger.warning(f"Error reading CalDAV event component: {e}")
        return None

    start_str = (
        _format_event_time_for_display(dtstart.dt, local_tz)
        if dtstart
        else "Unknown time"
    )
    end_str = (
        _format_event_time_for_display(dtend.dt, local_tz) if dtend else "No end time"
    )

    return {
        "summary": summary,
        "uid": uid,
        "start": start_str,
        "end": end_str,
        "calendar_url": src.url,
        "source_id": src.source_id,
        "source_name": src.name,
        "source_kind": "caldav",
        "writable": True,
        "start_dt": dtstart.dt if dtstart else None,
    }


def _search_single_caldav_source(
    client: caldav.DAVClient,
    src: CalendarSource,
    search_start: datetime,
    search_end: datetime,
    local_tz: ZoneInfo,
    use_naive_datetimes: bool,
) -> list[CalendarSearchResult]:
    start_arg = (
        datetime.combine(search_start.date(), time.min)
        if use_naive_datetimes
        else search_start
    )
    end_arg = (
        datetime.combine(search_end.date(), time.max)
        if use_naive_datetimes
        else search_end
    )
    try:
        calendar_obj = client.calendar(url=src.url)
        if not calendar_obj:
            logger.warning(f"Could not access calendar at {src.url}")
            return []
        events = calendar_obj.search(
            start=start_arg,
            end=end_arg,
            event=True,
            expand=True,
        )
    except Exception as e:
        logger.error(f"Error searching calendar {src.url}: {e}")
        return []

    results: list[CalendarSearchResult] = []
    for event in events:
        parsed = _parse_caldav_event_component(event, src, local_tz)
        if parsed is not None:
            results.append(parsed)
    return results


def _search_caldav_sources_sync(
    client_url: str,
    username: str,
    password: str,
    sources: list[CalendarSource],
    search_start: datetime,
    search_end: datetime,
    local_tz: ZoneInfo,
    use_naive_datetimes: bool = False,
) -> list[CalendarSearchResult]:
    """Synchronously queries CalDAV collections for events in the specified range."""
    logger.debug(f"Connecting to CalDAV server: {client_url}")
    all_events: list[CalendarSearchResult] = []
    with caldav.DAVClient(
        url=client_url,
        username=username,
        password=password,
        timeout=30,
    ) as client:
        for src in sources:
            cal_events = _search_single_caldav_source(
                client=client,
                src=src,
                search_start=search_start,
                search_end=search_end,
                local_tz=local_tz,
                use_naive_datetimes=use_naive_datetimes,
            )
            all_events.extend(cal_events)
    return all_events


async def _search_events_in_range(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
    search_start: datetime,
    search_end: datetime,
    sources: list[CalendarSource] | None = None,
) -> list[CalendarSearchResult]:
    """Queries both CalDAV and iCal sources for events within [search_start, search_end]."""
    target_sources = (
        sources if sources is not None else resolve_calendar_sources(calendar_config)
    )
    if not target_sources:
        return []

    local_tz = exec_context.timezone
    caldav_sources = [s for s in target_sources if s.kind == "caldav"]
    ical_sources = [s for s in target_sources if s.kind == "ical"]

    all_events: list[CalendarSearchResult] = []

    # CalDAV fetch
    caldav_config = calendar_config.get("caldav")
    if caldav_config and caldav_sources:
        username = caldav_config.get("username")
        password = caldav_config.get("password")
        base_url = caldav_config.get("base_url")

        client_url_to_use = base_url
        if not client_url_to_use and caldav_sources:
            try:
                parsed_first = httpx.URL(caldav_sources[0].url)
                client_url_to_use = f"{parsed_first.scheme}://{parsed_first.host}"
                if parsed_first.port is not None:
                    client_url_to_use += f":{parsed_first.port}"
            except Exception as e:
                logger.error(f"Could not infer CalDAV base_url: {e}")

        if username and password and client_url_to_use:
            use_naive = caldav_config.get("_use_naive_datetimes_for_search", False)
            loop = asyncio.get_running_loop()
            try:
                caldav_events = await loop.run_in_executor(
                    None,
                    _search_caldav_sources_sync,
                    client_url_to_use,
                    username,
                    password,
                    caldav_sources,
                    search_start,
                    search_end,
                    local_tz,
                    use_naive,
                )
                all_events.extend(caldav_events)
            except Exception as e:
                logger.exception(f"Error executing CalDAV search: {e}")

    # iCal fetch
    if ical_sources:
        try:
            raw_ical_events = await fetch_ical_events_async(
                ical_sources,
                timezone=local_tz,
                clock=exec_context.clock,
                start_date=search_start,
                end_date=search_end,
            )
            for evt in raw_ical_events:
                start_str = _format_event_time_for_display(evt["start"], local_tz)
                end_str = _format_event_time_for_display(evt["end"], local_tz)
                all_events.append({
                    "summary": evt["summary"],
                    "uid": evt["uid"],
                    "start": start_str,
                    "end": end_str,
                    "calendar_url": None,
                    "source_id": evt.get("source_id", ""),
                    "source_name": evt.get("source_name", "iCal feed"),
                    "source_kind": "ical",
                    "writable": False,
                    "start_dt": evt["start"],
                })
        except Exception as e:
            logger.exception(f"Error fetching iCal events for search: {e}")

    all_events.sort(key=lambda e: _event_sort_key(e, local_tz))
    return all_events


# Calendar Tool Definitions
async def check_for_duplicate_events(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
    summary: str,
    start_time: str,
    end_time: str,
    all_day: bool,
) -> str | None:
    """
    Check for similar events in a time window around the newly created event across all sources.

    Returns a warning message if similar events are found, None otherwise.

    Time windows:
    - Timed events: ±2 hours from start time
    - All-day events: same date

    Args:
        exec_context: Tool execution context
        calendar_config: Calendar configuration
        summary: Summary of the newly created event
        start_time: Start time in ISO format
        end_time: End time in ISO format
        all_day: Whether this is an all-day event

    Returns:
        Warning message string if duplicates found, None otherwise
    """

    async def check_for_duplicates() -> str | None:
        local_tz = exec_context.timezone
        if all_day:
            event_date = isoparse(start_time).date()
            search_start = datetime.combine(event_date, time.min, tzinfo=local_tz)
            search_end = datetime.combine(event_date, time(23, 59, 59), tzinfo=local_tz)
        else:
            event_dt = isoparse(start_time)
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=local_tz)

            dup_detection = calendar_config.get("duplicate_detection") or {}
            time_window_hours = dup_detection.get("time_window_hours", 2)

            search_start = event_dt - timedelta(hours=time_window_hours)
            search_end = event_dt + timedelta(hours=time_window_hours)

        events_in_window = await _search_events_in_range(
            exec_context=exec_context,
            calendar_config=calendar_config,
            search_start=search_start,
            search_end=search_end,
        )

        if not events_in_window:
            return None

        # Apply similarity filtering to find similar events
        dup_detection = calendar_config.get("duplicate_detection") or {}
        similarity_threshold = dup_detection.get("similarity_threshold", 0.30)

        try:
            similarity_strategy = create_similarity_strategy_from_config(
                calendar_config
            )
        except Exception as e:
            logger.warning(
                f"Failed to create similarity strategy for duplicate detection: {e}"
            )
            return None

        # Compute similarity for each event
        similar_events = []
        for event in events_in_window:
            similarity = await similarity_strategy.compute_similarity(
                summary, event["summary"]
            )
            if similarity >= similarity_threshold:
                event["similarity"] = similarity
                similar_events.append(event)

        if not similar_events:
            return None

        # Sort by similarity (highest first)
        similar_events.sort(key=lambda e: e.get("similarity", 0.0), reverse=True)

        # Format error message with bypass instructions
        error_lines = [
            f"Error: Cannot create event '{summary}' - found {len(similar_events)} similar event(s) at nearby times:",
            "",
        ]

        for idx, event in enumerate(similar_events, 1):
            error_lines.append(
                f"{idx}. '{event['summary']}' at {event['start']} (similarity: {event['similarity']:.2f})"
            )
            error_lines.append(f"   UID: {event['uid']}")
            if idx < len(similar_events):
                error_lines.append("")

        error_lines.append("")
        error_lines.append(
            "If you believe this is NOT a duplicate, retry with bypass_duplicate_check=true."
        )

        return "\n".join(error_lines)

    try:
        return await check_for_duplicates()
    except Exception as e:
        logger.warning(f"Error checking for duplicate events: {e}", exc_info=True)
        return None


_check_for_duplicate_events = check_for_duplicate_events


CALENDAR_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "list_calendars",
            "description": (
                "Lists all configured calendars and event sources, including their IDs, friendly names, source type (CalDAV or iCal feed), and whether they are writable or read-only. Use this to discover available calendars before searching with source filters or adding events to a specific calendar."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": (
                "Adds a new event to the primary family calendar (requires CalDAV configuration). Can create single or recurring events. Use this to schedule appointments, reminders with duration, or block out time. IMPORTANT: Always use search_calendar_events first to check for existing similar events and avoid creating duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "The title or brief summary of the event.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "The start date or datetime of the event in ISO 8601 format. MUST include timezone offset (e.g., '2025-05-20T09:00:00+02:00' for timed event, '2025-05-21' for all-day)."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "The end date or datetime of the event in ISO 8601 format. MUST include timezone offset. For all-day events ending on May 21, use '2025-05-22' (one day after the last included day)."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional detailed description of the event.",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": "True for all-day events, False for timed events (default: False)",
                    },
                    "recurrence_rule": {
                        "type": "string",
                        "description": (
                            "Optional iCalendar RRULE string for recurring events. Examples: 'FREQ=WEEKLY;BYDAY=MO,WE,FR' for every Mon/Wed/Fri, 'FREQ=MONTHLY;BYMONTHDAY=15' for 15th of each month."
                        ),
                    },
                    "bypass_duplicate_check": {
                        "type": "boolean",
                        "description": (
                            "Set to true to bypass duplicate detection and create the event anyway. Use this if you've reviewed the similar events and determined this is NOT a duplicate (e.g., different doctors, different purposes). Default: false."
                        ),
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": (
                            "Optional ID of the calendar to add the event to (from list_calendars). If omitted, adds to the default calendar."
                        ),
                    },
                },
                "required": ["summary", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_events",
            "description": (
                "Searches for calendar events by summary text or within a date range. Uses semantic similarity to find related events, not just exact matches. Each result includes a similarity score. Use this to check for conflicts before adding new events, find existing events to modify/delete, or list upcoming events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_text": {
                        "type": "string",
                        "description": "Optional text to search for in event summaries. Uses similarity matching to find semantically related events (e.g., searching 'doctor' finds 'Doctor appointment', 'Dr. Smith checkup', 'Medical visit'). Results include similarity scores (0.0-1.0).",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional start date for the search range in ISO 8601 format (e.g., '2025-05-20'). If not provided, searches from today.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional end date for the search range in ISO 8601 format. If not provided, searches up to 3 months from start date.",
                    },
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of calendar source IDs to limit the search to (obtained from list_calendars). If omitted, searches all configured calendars and feeds.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_calendar_event",
            "description": (
                "Modifies an existing calendar event. You must first use search_calendar_events to find the event's UID and calendar_url or calendar_id. Leave fields as None to keep existing values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "The unique identifier of the event to modify (obtained from search_calendar_events).",
                    },
                    "calendar_url": {
                        "type": "string",
                        "description": "The calendar URL where the event is stored (obtained from search_calendar_events).",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional ID of the calendar where the event is stored (from list_calendars). Can be provided instead of calendar_url.",
                    },
                    "new_summary": {
                        "type": "string",
                        "description": "New title for the event (optional).",
                    },
                    "new_start_time": {
                        "type": "string",
                        "description": "New start time in ISO 8601 format (optional).",
                    },
                    "new_end_time": {
                        "type": "string",
                        "description": "New end time in ISO 8601 format (optional).",
                    },
                    "new_description": {
                        "type": "string",
                        "description": "New description for the event (optional).",
                    },
                    "recurrence_rule": {
                        "type": "string",
                        "description": (
                            "Optional iCalendar RRULE string to set or update recurring pattern. Pass empty string to remove recurrence."
                        ),
                    },
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": (
                "Deletes a calendar event. You must first use search_calendar_events to find the event's UID and calendar_url or calendar_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "The unique identifier of the event to delete (obtained from search_calendar_events).",
                    },
                    "calendar_url": {
                        "type": "string",
                        "description": "The calendar URL where the event is stored (obtained from search_calendar_events).",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional ID of the calendar where the event is stored (from list_calendars). Can be provided instead of calendar_url.",
                    },
                },
                "required": ["uid"],
            },
        },
    },
]


async def list_calendars_tool(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
) -> str:
    """Lists all configured calendars and event sources."""
    logger.info("Executing list_calendars_tool")
    sources = resolve_calendar_sources(calendar_config)
    if not sources:
        return "No calendars configured."

    lines = ["Available calendars:"]
    for src in sources:
        kind_label = "CalDAV" if src.kind == "caldav" else "iCal feed"
        writable_label = "writable" if src.writable else "read-only"
        default_label = " [default for new events]" if src.is_default else ""
        lines.append(
            f"- {src.source_id}: {src.name} ({kind_label}, {writable_label}){default_label}"
        )

    return "\n".join(lines)


async def add_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
    summary: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    all_day: bool = False,
    recurrence_rule: str | None = None,
    bypass_duplicate_check: bool = False,
    calendar_id: str | None = None,
) -> str:
    """
    Adds an event to a configured CalDAV calendar.
    Can create recurring events if an RRULE string is provided.

    Args:
        bypass_duplicate_check: If True, skip duplicate detection and create the event anyway.
                                Use this if you've determined the event is not actually a duplicate.
        calendar_id: Optional ID of target calendar (from list_calendars). Defaults to primary calendar.
    """
    logger.info(
        f"Executing add_calendar_event_tool: {summary}, RRULE: {recurrence_rule}"
    )
    # calendar_config is now a direct parameter
    caldav_config = calendar_config.get("caldav")

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot add calendar event."

    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    if not username or not password:
        return "Error: CalDAV configuration is incomplete (missing user or pass). Cannot add event."

    all_sources = resolve_calendar_sources(calendar_config)
    caldav_sources = [s for s in all_sources if s.kind == "caldav"]

    target_source: CalendarSource | None = None
    if calendar_id:
        matching = [s for s in all_sources if s.source_id == calendar_id]
        if not matching:
            writable_ids = ", ".join(s.source_id for s in caldav_sources if s.writable)
            return (
                f"Error: Calendar '{calendar_id}' not found. "
                f"Available writable calendars: {writable_ids or 'none'}."
            )
        matched = matching[0]
        if not matched.writable or matched.kind == "ical":
            return (
                f"Error: Calendar '{matched.name}' ({calendar_id}) is read-only (iCal subscription). "
                f"Events cannot be added to it. Use a writable CalDAV calendar instead."
            )
        target_source = matched
    else:
        # Default calendar: look for is_default CalDAV source, or first writable CalDAV source
        default_sources = [s for s in caldav_sources if s.is_default and s.writable]
        if default_sources:
            target_source = default_sources[0]
        elif caldav_sources:
            target_source = caldav_sources[0]

    if not target_source:
        return "Error: CalDAV configuration is incomplete (missing calendar_urls). Cannot add event."

    target_calendar_url = target_source.url
    base_url: str | None = caldav_config.get("base_url")

    # Determine client_url and target_calendar_url
    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            parsed_first_cal_url = httpx.URL(target_calendar_url)
            client_url_to_use = f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}:{parsed_first_cal_url.port}"
            if parsed_first_cal_url.port is None:
                client_url_to_use = (
                    f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}"
                )
            logger.warning(
                f"CalDAV base_url not provided for add_calendar_event_tool, inferred '{client_url_to_use}'. "
                "Explicit 'base_url' in config is recommended."
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for add_calendar_event_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred. Cannot add event."

    if not client_url_to_use:  # Should be caught above, but defensive
        return "Error: CalDAV client URL could not be determined."

    logger.info(
        f"Targeting CalDAV server '{client_url_to_use}' and calendar collection '{target_calendar_url}'"
    )

    async def add_event() -> str:
        # Parse start and end times
        if all_day:
            # For all-day events, parse as date objects
            dtstart = isoparse(start_time).date()
            dtend = isoparse(end_time).date()
            # Basic validation: end date must be after start date for all-day
            if dtend <= dtstart:
                raise ValueError(
                    "End date must be after start date for all-day events."
                )
        else:
            # For timed events, parse as datetime objects, require timezone
            # For timed events, parse as datetime objects
            dtstart = isoparse(start_time)
            dtend = isoparse(end_time)
            # Assume configured timezone if none is provided in the input string
            local_tz = exec_context.timezone
            if dtstart.tzinfo is None:
                logger.warning(
                    f"Start time '{start_time}' lacks timezone. Assuming {exec_context.timezone}."
                )
                dtstart = dtstart.replace(tzinfo=local_tz)
            if dtend.tzinfo is None:
                logger.warning(
                    f"End time '{end_time}' lacks timezone. Assuming {exec_context.timezone}."
                )
                dtend = dtend.replace(tzinfo=local_tz)

            # Basic validation: end time must be after start time
            if dtend <= dtstart:
                raise ValueError("End time must be after start time for timed events.")

        # Create VEVENT component using vobject
        cal = vobject.iCalendar()  # cal is vobject.base.Component
        vevent = cal.add(
            "vevent"
        )  # add returns the new component, vevent is vobject.base.Component
        # Attributes like summary, dtstart are ContentLine objects after being added.
        vevent.add("uid").value = str(uuid.uuid4())  # type: ignore[union-attr]
        vevent.add("summary").value = summary  # type: ignore[union-attr]
        vevent.add("dtstart").value = dtstart  # vobject handles date vs datetime # type: ignore[union-attr]
        vevent.add("dtend").value = dtend  # vobject handles date vs datetime # type: ignore[union-attr]
        vevent.add("dtstamp").value = datetime.now(  # type: ignore[union-attr]
            ZoneInfo("UTC")
        )  # Use ZoneInfo for UTC
        if description:
            vevent.add("description").value = description  # type: ignore[union-attr]
        if recurrence_rule:
            vevent.add("rrule").value = recurrence_rule  # type: ignore[union-attr]
            logger.info(f"Adding recurrence rule to event: {recurrence_rule}")

        event_data: str = cal.serialize()  # type: ignore[union-attr]
        logger.debug(f"Generated VEVENT data:\n{event_data}")

        # Connect to CalDAV server and save event (synchronous, run in executor)
        def save_event_sync() -> str:
            logger.debug(f"Connecting to CalDAV server: {client_url_to_use}")
            with caldav.DAVClient(
                url=client_url_to_use,  # Use base_url for client
                username=username,
                password=password,
                timeout=30,
            ) as client:
                # Get the specific calendar object using its full URL
                target_calendar_obj: caldav.objects.Calendar = client.calendar(
                    url=target_calendar_url  # Use full collection URL here
                )
                if not target_calendar_obj:
                    raise ConnectionError(
                        f"Failed to obtain calendar object for URL: {target_calendar_url} on server {client_url_to_use}"
                    )

                logger.info(f"Saving event to calendar: {target_calendar_obj.url}")
                # Save event with no_overwrite=True to use If-None-Match:* for creation
                new_event_resource: caldav.objects.Event = (
                    target_calendar_obj.save_event(event_data, no_overwrite=True)
                )
                logger.info(
                    f"Event saved successfully. URL: {getattr(new_event_resource, 'url', 'N/A')}, ETag: {getattr(new_event_resource, 'etag', 'N/A')}"
                )
                return f"OK. Event '{summary}' added to the calendar."

        async def save_event() -> str:
            # Check for duplicate events BEFORE creation (if duplicate detection is enabled and not bypassed)
            dup_detection = calendar_config.get("duplicate_detection") or {}
            if dup_detection.get("enabled", True) and not bypass_duplicate_check:
                try:
                    error_message = await _check_for_duplicate_events(
                        exec_context=exec_context,
                        calendar_config=calendar_config,
                        summary=summary,
                        start_time=start_time,
                        end_time=end_time,
                        all_day=all_day,
                    )
                    if error_message:
                        # Return error - do not create the event
                        return error_message
                except Exception as dup_check_err:
                    # Don't fail the whole operation if duplicate detection fails
                    logger.warning(
                        f"Failed to check for duplicate events: {dup_check_err}",
                        exc_info=True,
                    )

            # Create the event (either no duplicates found, or bypass flag is set)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, save_event_sync)

            # If bypass was used, note it in the response
            if bypass_duplicate_check:
                result = f"{result} (duplicate check bypassed)"

            return result

        try:
            return await save_event()
        except (DAVError, ConnectionError, Exception) as sync_err:
            logger.exception(
                f"Error during synchronous CalDAV save operation: {sync_err}"
            )
            # Provide a more specific error if possible
            if "authentication" in str(sync_err).lower():
                return (
                    "Error: Failed to add event due to CalDAV authentication failure."
                )
            elif "not found" in str(sync_err).lower():  # type: ignore[operator]
                return f"Error: Failed to add event. Calendar not found at URL: {target_calendar_url}"
            else:
                return f"Error: Failed to add event to CalDAV calendar. {sync_err}"

    try:
        return await add_event()
    except ValueError as ve:
        logger.error(f"Invalid arguments for adding calendar event: {ve}")
        return f"Error: Invalid arguments provided. {ve}"
    except Exception as e:
        logger.exception(f"Unexpected error adding calendar event: {e}")
        return f"Error: An unexpected error occurred while adding the event. {e}"


def _parse_search_date_range(
    start_date: str | None,
    end_date: str | None,
    local_tz: ZoneInfo,
    now: datetime,
) -> tuple[datetime, datetime]:
    if start_date:
        search_start = isoparse(start_date)
        if isinstance(search_start, date) and not isinstance(search_start, datetime):
            search_start = datetime.combine(search_start, time.min, tzinfo=local_tz)
        elif search_start.tzinfo is None:
            search_start = search_start.replace(tzinfo=local_tz)
    else:
        search_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if end_date:
        search_end = isoparse(end_date)
        if isinstance(search_end, date) and not isinstance(search_end, datetime):
            search_end = datetime.combine(search_end, time(23, 59, 59), tzinfo=local_tz)
        elif search_end.tzinfo is None:
            search_end = search_end.replace(tzinfo=local_tz)
    else:
        search_end = search_start + timedelta(days=90)

    return search_start, search_end


async def _filter_events_by_similarity(
    events: list[CalendarSearchResult],
    search_text: str,
    calendar_config: CalendarConfig,
) -> tuple[list[CalendarSearchResult], float]:
    dup_detection = calendar_config.get("duplicate_detection") or {}
    similarity_threshold = dup_detection.get("similarity_threshold", 0.30)

    try:
        similarity_strategy = create_similarity_strategy_from_config(calendar_config)
        logger.info(
            f"Using similarity strategy: {similarity_strategy.name} with threshold {similarity_threshold}"
        )
    except Exception as e:
        logger.warning(
            f"Failed to create similarity strategy: {e}. Falling back to substring matching."
        )
        filtered = [
            event for event in events if search_text.lower() in event["summary"].lower()
        ]
        return filtered, similarity_threshold

    events_with_similarity: list[CalendarSearchResult] = []
    for event in events:
        similarity = await similarity_strategy.compute_similarity(
            search_text, event["summary"]
        )
        if similarity >= similarity_threshold:
            event_with_sim: CalendarSearchResult = dict(event)  # type: ignore[assignment]
            event_with_sim["similarity"] = similarity
            events_with_similarity.append(event_with_sim)

    events_with_similarity.sort(
        key=lambda e: e.get("similarity", 0.0) or 0.0, reverse=True
    )
    return events_with_similarity, similarity_threshold


def _format_search_results(events: list[CalendarSearchResult]) -> str:
    result_lines = [f"Found {len(events)} event(s):"]
    for idx, event in enumerate(events, 1):
        similarity = event.get("similarity")
        similarity_str = (
            f" (similarity: {similarity:.2f})" if similarity is not None else ""
        )
        result_lines.append(f"\n{idx}. {event['summary']}{similarity_str}")
        result_lines.append(f"   Start: {event['start']}")
        result_lines.append(f"   End: {event['end']}")
        result_lines.append(f"   UID: {event['uid']}")
        if event.get("calendar_url"):
            result_lines.append(f"   Calendar: {event['calendar_url']}")
        else:
            result_lines.append(
                f"   Calendar: {event.get('source_name', 'iCal feed')} (read-only)"
            )
        source_id = event.get("source_id")
        if source_id:
            result_lines.append(f"   Source ID: {source_id}")

    return "\n".join(result_lines)


async def search_calendar_events_tool(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
    search_text: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    source_ids: list[str] | None = None,
) -> str:
    """
    Searches for calendar events across configured CalDAV calendars and iCal feeds.
    Returns a list of events with their UIDs, calendar locations, and similarity scores.

    When search_text is provided, uses similarity strategy from config to find
    semantically similar events, not just exact substring matches.
    """
    logger.info(
        f"Executing search_calendar_events_tool: text='{search_text}', start={start_date}, end={end_date}, source_ids={source_ids}"
    )

    all_sources = resolve_calendar_sources(calendar_config)
    if not all_sources:
        return "Error: No calendars configured. Cannot search calendar events."

    target_sources: list[CalendarSource]
    if source_ids:
        sources_by_id = {s.source_id: s for s in all_sources}
        matching = [sources_by_id[sid] for sid in source_ids if sid in sources_by_id]
        if not matching:
            available = ", ".join(s.source_id for s in all_sources)
            return (
                f"Error: None of the requested calendar source IDs ({', '.join(source_ids)}) were found. "
                f"Available sources: {available}."
            )
        target_sources = matching
    else:
        target_sources = all_sources

    # Check for CalDAV configuration if CalDAV sources are targeted without any iCal sources
    caldav_sources = [s for s in target_sources if s.kind == "caldav"]
    ical_sources = [s for s in target_sources if s.kind == "ical"]
    if caldav_sources and not ical_sources:
        caldav_config = calendar_config.get("caldav")
        if not caldav_config:
            return "Error: CalDAV is not configured. Cannot search calendar events."
        username = caldav_config.get("username")
        password = caldav_config.get("password")
        if not username or not password:
            return "Error: CalDAV configuration is incomplete. Cannot search events."

    local_tz = exec_context.timezone
    now = (
        exec_context.clock.now().astimezone(local_tz)
        if exec_context.clock
        else datetime.now(local_tz)
    )

    try:
        search_start, search_end = _parse_search_date_range(
            start_date, end_date, local_tz, now
        )
    except ValueError as ve:
        logger.error(f"Invalid search parameters: {ve}")
        return f"Error: Invalid search parameters. {ve}"

    try:
        all_events = await _search_events_in_range(
            exec_context=exec_context,
            calendar_config=calendar_config,
            search_start=search_start,
            search_end=search_end,
            sources=target_sources,
        )
    except Exception as e:
        logger.exception(f"Unexpected error searching calendar events: {e}")
        return f"Error: An unexpected error occurred while searching events. {e}"

    if not all_events:
        if search_text:
            dup_detection = calendar_config.get("duplicate_detection") or {}
            similarity_threshold = dup_detection.get("similarity_threshold", 0.30)
            return f"No events found matching '{search_text}' (threshold: {similarity_threshold})."
        return "No events found matching the search criteria."

    # Apply similarity-based filtering if search_text is provided
    if search_text:
        all_events, similarity_threshold = await _filter_events_by_similarity(
            all_events, search_text, calendar_config
        )
        if not all_events:
            return f"No events found matching '{search_text}' (threshold: {similarity_threshold})."
    else:
        all_events.sort(key=lambda e: _event_sort_key(e, local_tz))

    return _format_search_results(all_events)


def resolve_target_caldav_url(
    calendar_config: CalendarConfig,
    calendar_url: str | None,
    calendar_id: str | None,
    operation_verb: str = "modify",
) -> tuple[str | None, str | None]:
    """Resolves target CalDAV calendar URL for write operations (modify/delete).

    Returns (resolved_url, error_message). Exactly one will be non-None.
    """
    all_sources = resolve_calendar_sources(calendar_config)
    operation_past = "modified" if operation_verb == "modify" else "deleted"

    if calendar_id and calendar_url:
        matching = [s for s in all_sources if s.source_id == calendar_id]
        if matching:
            matched = matching[0]
            if matched.url != calendar_url:
                return None, (
                    f"Error: Conflicting calendar targets provided: calendar_id '{calendar_id}' "
                    f"resolves to '{matched.url}', but calendar_url '{calendar_url}' was also specified."
                )

    if calendar_id:
        matching = [s for s in all_sources if s.source_id == calendar_id]
        if matching:
            matched = matching[0]
            if not matched.writable or matched.kind == "ical":
                return None, (
                    f"Error: Calendar '{matched.name}' ({calendar_id}) is a read-only subscription. "
                    f"Events cannot be {operation_past}."
                )
            return matched.url, None

        writable_ids = ", ".join(
            s.source_id for s in all_sources if s.writable and s.kind == "caldav"
        )
        return None, (
            f"Error: Calendar '{calendar_id}' not found. "
            f"Available writable calendars: {writable_ids or 'none'}."
        )

    if calendar_url:
        for s in all_sources:
            if s.url == calendar_url and (s.kind == "ical" or not s.writable):
                return None, (
                    f"Error: Calendar '{s.name}' is a read-only subscription. "
                    f"Events cannot be {operation_past}."
                )
        return calendar_url, None

    return (
        None,
        f"Error: Either calendar_id or calendar_url must be provided to {operation_verb} an event.",
    )


_resolve_target_caldav_url = resolve_target_caldav_url


async def modify_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
    uid: str,
    calendar_url: str | None = None,
    calendar_id: str | None = None,
    new_summary: str | None = None,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
    new_description: str | None = None,
    recurrence_rule: str | None = None,
) -> str:
    """Modifies an existing calendar event identified by UID.

    Leave parameters as None to keep existing values.
    """
    logger.info(f"Executing modify_calendar_event_tool for UID: {uid}")

    target_cal_url, err = _resolve_target_caldav_url(
        calendar_config=calendar_config,
        calendar_url=calendar_url,
        calendar_id=calendar_id,
        operation_verb="modify",
    )
    if err:
        return err
    assert target_cal_url is not None

    caldav_config = calendar_config.get("caldav")

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot modify calendar event."

    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    base_url: str | None = caldav_config.get("base_url")

    if not username or not password:
        return "Error: CalDAV configuration is incomplete. Cannot modify event."

    # Determine client_url
    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            parsed_cal_url = httpx.URL(target_cal_url)
            client_url_to_use = (
                f"{parsed_cal_url.scheme}://{parsed_cal_url.host}:{parsed_cal_url.port}"
            )
            if parsed_cal_url.port is None:
                client_url_to_use = f"{parsed_cal_url.scheme}://{parsed_cal_url.host}"
            logger.warning(
                f"CalDAV base_url not provided for modify_calendar_event_tool, inferred '{client_url_to_use}'"
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for modify_calendar_event_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred."

    if not client_url_to_use:
        return "Error: CalDAV client URL could not be determined."

    async def modify_event() -> str:
        # Modify event (synchronous, run in executor)
        def modify_event_sync() -> str:
            logger.debug(f"Connecting to CalDAV server: {client_url_to_use}")
            with caldav.DAVClient(
                url=client_url_to_use,
                username=username,
                password=password,
                timeout=30,
            ) as client:
                # Get the specific calendar
                calendar_obj = client.calendar(url=target_cal_url)
                if not calendar_obj:
                    raise ConnectionError(
                        f"Failed to obtain calendar object for URL: {target_cal_url}"
                    )

                # Search for the event by UID
                # Note: calendar.search(uid=uid) doesn't work reliably with all CalDAV servers
                # So we fetch all events and search manually
                def update_event() -> str:
                    all_events = calendar_obj.events()
                    event = None

                    for evt in all_events:
                        try:
                            # Use vobject_instance to get vobject representation
                            evt_vobj = evt.vobject_instance
                            evt_vevent = (
                                evt_vobj.vevent
                                if hasattr(evt_vobj, "vevent")
                                else evt_vobj
                            )
                            evt_uid = str(
                                evt_vevent.uid.value
                                if hasattr(evt_vevent, "uid")
                                else ""
                            )
                        except Exception as e:
                            logger.warning(f"Error checking event UID: {e}")
                            continue

                        if evt_uid == uid:
                            event = evt
                            break

                    if not event:
                        return f"Error: Event with UID '{uid}' not found in calendar."

                    # Get the existing event data using vobject_instance (not icalendar_component)
                    vobj = event.vobject_instance
                    old_vevent = vobj.vevent if hasattr(vobj, "vevent") else vobj

                    # Store original values for the result message
                    original_summary = str(
                        old_vevent.summary.value
                        if hasattr(old_vevent, "summary")
                        else ""
                    )

                    # Extract current values from the existing event
                    current_summary = (
                        new_summary
                        if new_summary is not None
                        else str(
                            old_vevent.summary.value
                            if hasattr(old_vevent, "summary")
                            else ""
                        )
                    )
                    current_description = (
                        old_vevent.description.value
                        if hasattr(old_vevent, "description")
                        else None
                    )
                    if new_description is not None:
                        current_description = (
                            new_description if new_description else None
                        )

                    # Extract existing times
                    current_start = (
                        old_vevent.dtstart.value
                        if hasattr(old_vevent, "dtstart")
                        else None
                    )
                    current_end = (
                        old_vevent.dtend.value if hasattr(old_vevent, "dtend") else None
                    )

                    local_tz = exec_context.timezone

                    # Parse new times if provided
                    if new_start_time:
                        current_start = isoparse(new_start_time)
                        if (
                            isinstance(current_start, datetime)
                            and current_start.tzinfo is None
                        ):
                            current_start = current_start.replace(tzinfo=local_tz)

                    if new_end_time:
                        current_end = isoparse(new_end_time)
                        if (
                            isinstance(current_end, datetime)
                            and current_end.tzinfo is None
                        ):
                            current_end = current_end.replace(tzinfo=local_tz)

                    # Get existing or new recurrence rule
                    current_rrule = None
                    if hasattr(old_vevent, "rrule"):
                        current_rrule = old_vevent.rrule.value
                    if recurrence_rule is not None:
                        current_rrule = recurrence_rule if recurrence_rule else None

                    # Create a fresh vobject calendar with updated values (like in add_calendar_event_tool)
                    new_cal = vobject.iCalendar()
                    new_vevent = new_cal.add("vevent")
                    new_vevent.add("uid").value = uid  # Keep the same UID
                    new_vevent.add("summary").value = current_summary
                    new_vevent.add("dtstart").value = current_start  # type: ignore[union-attr]
                    new_vevent.add("dtend").value = current_end  # type: ignore[union-attr]
                    new_vevent.add("dtstamp").value = datetime.now(  # type: ignore[union-attr]
                        ZoneInfo("UTC")
                    )
                    new_vevent.add("last-modified").value = datetime.now(  # type: ignore[union-attr]
                        ZoneInfo("UTC")
                    )

                    if current_description:
                        new_vevent.add("description").value = current_description

                    if current_rrule:
                        new_vevent.add("rrule").value = current_rrule
                        logger.info(f"Updated recurrence rule to: {current_rrule}")

                    # Serialize and save the new calendar data
                    event_data = new_cal.serialize()
                    event.data = event_data
                    event.save()
                    logger.info(f"Event '{original_summary}' modified successfully")

                    # Build result message
                    changes = []
                    if new_summary:
                        changes.append(f"title to '{new_summary}'")
                    if new_start_time:
                        changes.append(f"start time to {new_start_time}")
                    if new_end_time:
                        changes.append(f"end time to {new_end_time}")
                    if new_description is not None:
                        changes.append("description")
                    if recurrence_rule is not None:
                        if recurrence_rule:
                            changes.append("recurrence rule")
                        else:
                            changes.append("removed recurrence")

                    if changes:
                        return f"OK. Event '{original_summary}' updated: {', '.join(changes)}."
                    else:
                        return (
                            f"OK. Event '{original_summary}' checked (no changes made)."
                        )

                try:
                    return update_event()
                except NotFoundError:
                    return f"Error: Event with UID '{uid}' not found in calendar."
                except Exception as e:
                    logger.exception(f"Error modifying event: {e}")
                    return f"Error: Failed to modify event. {e}"

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, modify_event_sync)
            return result
        except Exception as sync_err:
            logger.exception(f"Error during calendar modification: {sync_err}")
            return f"Error: Failed to modify calendar event. {sync_err}"

    try:
        return await modify_event()
    except ValueError as ve:
        logger.error(f"Invalid modification parameters: {ve}")
        return f"Error: Invalid modification parameters. {ve}"
    except Exception as e:
        logger.exception(f"Unexpected error modifying calendar event: {e}")
        return f"Error: An unexpected error occurred while modifying the event. {e}"


async def delete_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: CalendarConfig,
    uid: str,
    calendar_url: str | None = None,
    calendar_id: str | None = None,
) -> str:
    """Deletes a calendar event identified by UID."""
    logger.info(f"Executing delete_calendar_event_tool for UID: {uid}")

    target_cal_url, err = _resolve_target_caldav_url(
        calendar_config=calendar_config,
        calendar_url=calendar_url,
        calendar_id=calendar_id,
        operation_verb="delete",
    )
    if err:
        return err
    assert target_cal_url is not None

    caldav_config = calendar_config.get("caldav")

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot delete calendar event."

    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    base_url: str | None = caldav_config.get("base_url")

    if not username or not password:
        return "Error: CalDAV configuration is incomplete. Cannot delete event."

    # Determine client_url
    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            parsed_cal_url = httpx.URL(target_cal_url)
            client_url_to_use = (
                f"{parsed_cal_url.scheme}://{parsed_cal_url.host}:{parsed_cal_url.port}"
            )
            if parsed_cal_url.port is None:
                client_url_to_use = f"{parsed_cal_url.scheme}://{parsed_cal_url.host}"
            logger.warning(
                f"CalDAV base_url not provided for delete_calendar_event_tool, inferred '{client_url_to_use}'"
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for delete_calendar_event_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred."

    if not client_url_to_use:
        return "Error: CalDAV client URL could not be determined."

    async def delete_event() -> str:
        # Delete event (synchronous, run in executor)
        def delete_event_sync() -> str:
            logger.debug(f"Connecting to CalDAV server: {client_url_to_use}")
            with caldav.DAVClient(
                url=client_url_to_use,
                username=username,
                password=password,
                timeout=30,
            ) as client:
                # Get the specific calendar
                calendar_obj = client.calendar(url=target_cal_url)
                if not calendar_obj:
                    raise ConnectionError(
                        f"Failed to obtain calendar object for URL: {target_cal_url}"
                    )

                # Search for the event by UID
                # Note: calendar.search(uid=uid) doesn't work reliably with all CalDAV servers
                # So we fetch all events and search manually
                def remove_event() -> str:
                    all_events = calendar_obj.events()
                    event = None

                    for evt in all_events:
                        try:
                            # Use vobject_instance to get vobject representation
                            evt_vobj = evt.vobject_instance
                            evt_vevent = (
                                evt_vobj.vevent
                                if hasattr(evt_vobj, "vevent")
                                else evt_vobj
                            )
                            evt_uid = str(
                                evt_vevent.uid.value
                                if hasattr(evt_vevent, "uid")
                                else ""
                            )
                        except Exception as e:
                            logger.warning(f"Error checking event UID: {e}")
                            continue

                        if evt_uid == uid:
                            event = evt
                            break

                    if not event:
                        return f"Error: Event with UID '{uid}' not found in calendar."
                    vevent = event.icalendar_component
                    summary = str(vevent.get("summary", "Untitled"))

                    # Delete the event
                    event.delete()
                    logger.info(f"Event '{summary}' deleted successfully")
                    return f"OK. Event '{summary}' deleted from calendar."

                try:
                    return remove_event()
                except NotFoundError:
                    return f"Error: Event with UID '{uid}' not found in calendar."
                except Exception as e:
                    logger.exception(f"Error deleting event: {e}")
                    return f"Error: Failed to delete event. {e}"

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, delete_event_sync)
            return result
        except Exception as sync_err:
            logger.exception(f"Error during calendar deletion: {sync_err}")
            return f"Error: Failed to delete calendar event. {sync_err}"

    try:
        return await delete_event()
    except Exception as e:
        logger.exception(f"Unexpected error deleting calendar event: {e}")
        return f"Error: An unexpected error occurred while deleting the event. {e}"
