"""Calendar tools for the Family Assistant.

This module contains all calendar-related tool implementations that can be
used by the LLM to manage calendar events via CalDAV.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import caldav
import httpx
import vobject
from caldav.lib.error import DAVError, NotFoundError
from dateutil.parser import isoparse

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Calendar Tool Definitions
CALENDAR_TOOLS_DEFINITION = [
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
                "Searches for calendar events by summary text or within a date range. Use this to check for conflicts before adding new events, find existing events to modify/delete, or list upcoming events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_text": {
                        "type": "string",
                        "description": "Optional text to search for in event summaries. Case-insensitive partial match.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional start date for the search range in ISO 8601 format (e.g., '2025-05-20'). If not provided, searches from today.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional end date for the search range in ISO 8601 format. If not provided, searches up to 3 months from start date.",
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
                "Modifies an existing calendar event. You must first use search_calendar_events to find the event's UID and calendar_url. Leave fields as None to keep existing values."
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
                "required": ["uid", "calendar_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": (
                "Deletes a calendar event. You must first use search_calendar_events to find the event's UID and calendar_url."
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
                },
                "required": ["uid", "calendar_url"],
            },
        },
    },
]


async def add_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: dict[str, Any],
    summary: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    all_day: bool = False,
    recurrence_rule: str | None = None,  # Added RRULE parameter
) -> str:
    """
    Adds an event to the first configured CalDAV calendar.
    Can create recurring events if an RRULE string is provided.
    """
    logger.info(
        f"Executing add_calendar_event_tool: {summary}, RRULE: {recurrence_rule}"
    )
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")  # type: ignore

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot add calendar event."

    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    calendar_urls_list: list[str] | None = caldav_config.get("calendar_urls", [])
    base_url: str | None = caldav_config.get("base_url")

    if not username or not password or not calendar_urls_list:
        return "Error: CalDAV configuration is incomplete (missing user, pass, or calendar_urls). Cannot add event."

    # Determine client_url and target_calendar_url
    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            parsed_first_cal_url = httpx.URL(calendar_urls_list[0])
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

    target_calendar_url: str = calendar_urls_list[
        0
    ]  # Use the first configured full calendar URL
    logger.info(
        f"Targeting CalDAV server '{client_url_to_use}' and calendar collection '{target_calendar_url}'"
    )

    try:
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
            local_tz = ZoneInfo(exec_context.timezone_str)
            if dtstart.tzinfo is None:
                logger.warning(
                    f"Start time '{start_time}' lacks timezone. Assuming {exec_context.timezone_str}."
                )
                dtstart = dtstart.replace(tzinfo=local_tz)
            if dtend.tzinfo is None:
                logger.warning(
                    f"End time '{end_time}' lacks timezone. Assuming {exec_context.timezone_str}."
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
        vevent.add(
            "dtstart"
        ).value = dtstart  # vobject handles date vs datetime # type: ignore[union-attr]
        vevent.add(
            "dtend"
        ).value = dtend  # vobject handles date vs datetime # type: ignore[union-attr]
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

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, save_event_sync)
            return result
        except (DAVError, ConnectionError, Exception) as sync_err:
            logger.error(
                f"Error during synchronous CalDAV save operation: {sync_err}",
                exc_info=True,
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

    except ValueError as ve:
        logger.error(f"Invalid arguments for adding calendar event: {ve}")
        return f"Error: Invalid arguments provided. {ve}"
    except Exception as e:
        logger.error(f"Unexpected error adding calendar event: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while adding the event. {e}"


async def search_calendar_events_tool(
    exec_context: ToolExecutionContext,
    calendar_config: dict[str, Any],
    search_text: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """
    Searches for calendar events by summary text or within a date range.
    Returns a list of events with their UIDs and calendar URLs.
    """
    logger.info(
        f"Executing search_calendar_events_tool: text='{search_text}', start={start_date}, end={end_date}"
    )
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot search calendar events."

    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    calendar_urls_list: list[str] | None = caldav_config.get("calendar_urls", [])
    base_url: str | None = caldav_config.get("base_url")

    if not username or not password or not calendar_urls_list:
        return "Error: CalDAV configuration is incomplete. Cannot search events."

    # Determine client_url
    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            parsed_first_cal_url = httpx.URL(calendar_urls_list[0])
            client_url_to_use = f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}:{parsed_first_cal_url.port}"
            if parsed_first_cal_url.port is None:
                client_url_to_use = (
                    f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}"
                )
            logger.warning(
                f"CalDAV base_url not provided for search_calendar_events_tool, inferred '{client_url_to_use}'"
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for search_calendar_events_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred."

    if not client_url_to_use:
        return "Error: CalDAV client URL could not be determined."

    try:
        # Parse search dates
        local_tz = ZoneInfo(exec_context.timezone_str)
        now = datetime.now(local_tz)

        if start_date:
            search_start = isoparse(start_date)
            if isinstance(search_start, date) and not isinstance(
                search_start, datetime
            ):
                search_start = datetime.combine(search_start, time.min, tzinfo=local_tz)
            elif search_start.tzinfo is None:
                search_start = search_start.replace(tzinfo=local_tz)
        else:
            search_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if end_date:
            search_end = isoparse(end_date)
            if isinstance(search_end, date) and not isinstance(search_end, datetime):
                # For end date, use end of day
                search_end = datetime.combine(
                    search_end, time(23, 59, 59), tzinfo=local_tz
                )
            elif search_end.tzinfo is None:
                search_end = search_end.replace(tzinfo=local_tz)
        else:
            # Default to 3 months from start
            search_end = search_start + timedelta(days=90)

        logger.info(f"Searching from {search_start} to {search_end}")

        # Search events (synchronous, run in executor)
        def search_events_sync() -> str:
            logger.debug(f"Connecting to CalDAV server: {client_url_to_use}")
            with caldav.DAVClient(
                url=client_url_to_use,
                username=username,
                password=password,
                timeout=30,
            ) as client:
                all_events = []
                for cal_url in calendar_urls_list:  # type: ignore
                    try:
                        calendar_obj = client.calendar(url=cal_url)
                        if not calendar_obj:
                            logger.warning(f"Could not access calendar at {cal_url}")
                            continue

                        # Search for events in the date range
                        events = calendar_obj.search(
                            start=search_start,
                            end=search_end,
                            event=True,
                            expand=True,  # Expand recurring events
                        )

                        for event in events:
                            try:
                                vevent = event.icalendar_component
                                summary = str(vevent.get("summary", ""))

                                # Apply text filter if provided
                                if (
                                    search_text
                                    and search_text.lower() not in summary.lower()
                                ):
                                    continue

                                uid = str(vevent.get("uid", ""))
                                dtstart = vevent.get("dtstart")
                                dtend = vevent.get("dtend")

                                # Format event info
                                if dtstart:
                                    start_val = dtstart.dt
                                    if isinstance(start_val, datetime):
                                        start_str = start_val.strftime(
                                            "%Y-%m-%d %H:%M %Z"
                                        )
                                    else:
                                        start_str = str(start_val)
                                else:
                                    start_str = "No start time"

                                if dtend:
                                    end_val = dtend.dt
                                    if isinstance(end_val, datetime):
                                        end_str = end_val.strftime("%Y-%m-%d %H:%M %Z")
                                    else:
                                        end_str = str(end_val)
                                else:
                                    end_str = "No end time"

                                all_events.append({
                                    "summary": summary,
                                    "uid": uid,
                                    "start": start_str,
                                    "end": end_str,
                                    "calendar_url": cal_url,
                                })
                            except Exception as e:
                                logger.warning(f"Error processing event: {e}")
                                continue

                    except Exception as e:
                        logger.error(f"Error searching calendar {cal_url}: {e}")
                        continue

                if not all_events:
                    return "No events found matching the search criteria."

                # Format results
                result_lines = [f"Found {len(all_events)} event(s):"]
                for idx, event in enumerate(all_events, 1):
                    result_lines.append(f"\n{idx}. {event['summary']}")
                    result_lines.append(f"   Start: {event['start']}")
                    result_lines.append(f"   End: {event['end']}")
                    result_lines.append(f"   UID: {event['uid']}")
                    result_lines.append(f"   Calendar: {event['calendar_url']}")

                return "\n".join(result_lines)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, search_events_sync)
            return result
        except Exception as sync_err:
            logger.error(f"Error during calendar search: {sync_err}", exc_info=True)
            return f"Error: Failed to search calendar events. {sync_err}"

    except ValueError as ve:
        logger.error(f"Invalid search parameters: {ve}")
        return f"Error: Invalid search parameters. {ve}"
    except Exception as e:
        logger.error(f"Unexpected error searching calendar events: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while searching events. {e}"


async def modify_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: dict[str, Any],
    uid: str,
    calendar_url: str,
    new_summary: str | None = None,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
    new_description: str | None = None,
    recurrence_rule: str | None = None,
) -> str:
    """
    Modifies an existing calendar event identified by UID.
    Leave parameters as None to keep existing values.
    """
    logger.info(f"Executing modify_calendar_event_tool for UID: {uid}")
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")

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
            parsed_cal_url = httpx.URL(calendar_url)
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

    try:
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
                calendar_obj = client.calendar(url=calendar_url)
                if not calendar_obj:
                    raise ConnectionError(
                        f"Failed to obtain calendar object for URL: {calendar_url}"
                    )

                # Search for the event by UID
                # Note: calendar.search(uid=uid) doesn't work reliably with all CalDAV servers
                # So we fetch all events and search manually
                try:
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
                            if evt_uid == uid:
                                event = evt
                                break
                        except Exception as e:
                            logger.warning(f"Error checking event UID: {e}")
                            continue

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

                    local_tz = ZoneInfo(exec_context.timezone_str)

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

                except NotFoundError:
                    return f"Error: Event with UID '{uid}' not found in calendar."
                except Exception as e:
                    logger.error(f"Error modifying event: {e}", exc_info=True)
                    return f"Error: Failed to modify event. {e}"

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, modify_event_sync)
            return result
        except Exception as sync_err:
            logger.error(
                f"Error during calendar modification: {sync_err}", exc_info=True
            )
            return f"Error: Failed to modify calendar event. {sync_err}"

    except ValueError as ve:
        logger.error(f"Invalid modification parameters: {ve}")
        return f"Error: Invalid modification parameters. {ve}"
    except Exception as e:
        logger.error(f"Unexpected error modifying calendar event: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while modifying the event. {e}"


async def delete_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: dict[str, Any],
    uid: str,
    calendar_url: str,
) -> str:
    """
    Deletes a calendar event identified by UID.
    """
    logger.info(f"Executing delete_calendar_event_tool for UID: {uid}")
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")

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
            parsed_cal_url = httpx.URL(calendar_url)
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

    try:
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
                calendar_obj = client.calendar(url=calendar_url)
                if not calendar_obj:
                    raise ConnectionError(
                        f"Failed to obtain calendar object for URL: {calendar_url}"
                    )

                # Search for the event by UID
                # Note: calendar.search(uid=uid) doesn't work reliably with all CalDAV servers
                # So we fetch all events and search manually
                try:
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
                            if evt_uid == uid:
                                event = evt
                                break
                        except Exception as e:
                            logger.warning(f"Error checking event UID: {e}")
                            continue

                    if not event:
                        return f"Error: Event with UID '{uid}' not found in calendar."
                    vevent = event.icalendar_component
                    summary = str(vevent.get("summary", "Untitled"))

                    # Delete the event
                    event.delete()
                    logger.info(f"Event '{summary}' deleted successfully")
                    return f"OK. Event '{summary}' deleted from calendar."

                except NotFoundError:
                    return f"Error: Event with UID '{uid}' not found in calendar."
                except Exception as e:
                    logger.error(f"Error deleting event: {e}", exc_info=True)
                    return f"Error: Failed to delete event. {e}"

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, delete_event_sync)
            return result
        except Exception as sync_err:
            logger.error(f"Error during calendar deletion: {sync_err}", exc_info=True)
            return f"Error: Failed to delete calendar event. {sync_err}"

    except Exception as e:
        logger.error(f"Unexpected error deleting calendar event: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while deleting the event. {e}"
