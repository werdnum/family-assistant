import asyncio  # Import asyncio for run_in_executor
import logging
import uuid  # For generating event UIDs
from datetime import date, datetime, time, timedelta  # Added time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo  # Import ZoneInfo

import caldav
import httpx  # Import httpx
import vobject
from caldav.lib.error import (  # Reverted to original-like import path
    DAVError,
    NotFoundError,
)
from dateutil.parser import isoparse  # For parsing ISO strings in tools

if TYPE_CHECKING:
    # Import types needed by tools under TYPE_CHECKING to break circular import
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# --- Configuration (Now passed via function arguments) ---
# Environment variables are still read here for the standalone test section (__main__)

# --- Helper Functions ---


def format_datetime_or_date(
    dt_obj: datetime | date, timezone_str: str, is_end: bool = False
) -> str:
    """Formats datetime or date object into a user-friendly string, relative to the specified timezone."""
    try:
        local_tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(
            f"Invalid timezone string '{timezone_str}' in format_datetime_or_date. Falling back to UTC."
        )
        local_tz = ZoneInfo("UTC")

    now_local = datetime.now(local_tz)
    today_local = now_local.date()
    tomorrow_local = today_local + timedelta(days=1)

    if isinstance(dt_obj, datetime):
        # Convert event time to local timezone for comparison and display
        dt_local = dt_obj.astimezone(local_tz)
        # Example: "Today 14:30", "Tomorrow 09:00", "Apr 21 10:00"

        if dt_local.date() == today_local:
            day_str = "Today"
        elif dt_local.date() == tomorrow_local:
            day_str = "Tomorrow"
        else:
            day_str = dt_local.strftime("%b %d")  # e.g., Apr 21

        # For end times exactly at midnight, display as end of previous day if makes sense
        if is_end and dt_obj.time() == time(0, 0):
            # Check if it's the day after the start date (common for multi-day events ending at midnight)
            # This logic might need refinement based on how start_date is passed or stored
            # For simplicity now, just format as is.
            pass  # No specific end-of-day adjustment needed here currently

        return f"{day_str} {dt_local.strftime('%H:%M')}"

    else:  # dt_obj is a date
        # Format date (all-day event) - Dates don't have timezones inherently
        # Comparisons are relative to the local timezone's date
        # Example: "Today", "Tomorrow", "Apr 21"

        # Adjust end date for display: CalDAV often stores end date as the day *after*
        display_date = dt_obj
        if is_end:
            display_date = dt_obj - timedelta(days=1)

        if display_date == today_local:
            return "Today"
        elif display_date == tomorrow_local:
            return "Tomorrow"
        else:
            return display_date.strftime("%b %d")  # e.g., Apr 21
    # Fallback for other types, though dt_obj should only be datetime or date
    # Based on type hints, this path should not be reached if input is correct.
    # However, to satisfy linters about all paths returning, and for robustness:
    return str(dt_obj)


def parse_event(
    event_data: str, timezone_str: str | None = None
) -> dict[str, Any] | None:  # reportExplicitAny for Any is acceptable by style guide
    """
    Parses VCALENDAR data into a dictionary, including the UID.
    If timezone_str is provided, naive datetimes will be localized to that timezone.
    """
    local_tz: ZoneInfo | None = None
    if timezone_str:
        try:
            local_tz = ZoneInfo(timezone_str)
        except Exception:
            logger.warning(
                f"Invalid timezone string '{timezone_str}' provided to parse_event. Naive datetimes will not be localized."
            )

    try:
        # vobject.readComponents returns an iterator.
        # We expect a single VCALENDAR component, then access its VEVENT.
        components = vobject.readComponents(event_data)  # type: ignore[attr-defined]
        ical_component = next(components)  # Get the VCALENDAR component
        vevent = ical_component.vevent  # Access the VEVENT sub-component
        summary = vevent.summary.value if hasattr(vevent, "summary") else "No Title"  # type: ignore[union-attr]
        dtstart = vevent.dtstart.value if hasattr(vevent, "dtstart") else None  # type: ignore[union-attr]
        dtend = vevent.dtend.value if hasattr(vevent, "dtend") else None  # type: ignore[union-attr]
        uid = (
            vevent.uid.value if hasattr(vevent, "uid") else None
        )  # Extract UID # type: ignore[union-attr]

        # Basic check for valid event data (UID is mandatory in iCal standard)
        if not summary or not dtstart or not uid:  # `uid` can be str or None here
            logger.warning(
                f"Parsed event missing essential fields (summary, dtstart, or uid). Summary='{summary}', Start='{dtstart}', UID='{uid}'"
            )
            return None

        is_all_day = not isinstance(dtstart, datetime)

        # Ensure start/end datetimes are in the correct local timezone if provided
        if local_tz:
            if isinstance(dtstart, datetime):
                if dtstart.tzinfo is None:
                    # Naive datetime: Assume it's in the target local timezone
                    dtstart = dtstart.replace(tzinfo=local_tz)
                    logger.debug(
                        f"Applied local timezone {timezone_str} to naive dtstart"
                    )
                else:
                    # Aware datetime: Convert it to the target local timezone
                    dtstart = dtstart.astimezone(local_tz)
                    logger.debug(
                        f"Converted aware dtstart to target timezone {timezone_str}"
                    )
            # Repeat for dtend, checking if it exists first
            if isinstance(dtend, datetime):
                if dtend.tzinfo is None:
                    dtend = dtend.replace(tzinfo=local_tz)
                    logger.debug(
                        f"Applied local timezone {timezone_str} to naive dtend"
                    )
                else:
                    dtend = dtend.astimezone(local_tz)
                    logger.debug(
                        f"Converted aware dtend to target timezone {timezone_str}"
                    )

        # If dtend is missing, calculate it *after* ensuring dtstart is localized/converted
        if dtend is None and dtstart is not None:  # Check dtstart is not None
            if is_all_day:
                dtend = dtstart + timedelta(days=1)
            else:
                dtend = dtstart + timedelta(hours=1)  # Default duration assumption

        return {
            "uid": uid,  # Include UID in the returned dict
            "summary": summary,
            "start": dtstart,
            "end": dtend,
            "all_day": is_all_day,
        }
    except StopIteration:
        logger.error(
            f"Failed to find VEVENT component in VCALENDAR data: {event_data[:200]}..."
        )
        return None
    except Exception as e:
        logger.error(
            f"Failed to parse VCALENDAR data: {e}\nData: {event_data[:200]}...",
            exc_info=True,
        )
        return None


# --- Core Fetching Functions ---


async def _fetch_ical_events_async(
    ical_urls: list[str],
    timezone_str: str,  # Added timezone string
) -> list[dict[str, Any]]:  # reportExplicitAny for Any is acceptable by style guide
    """Asynchronously fetches and parses events from a list of iCal URLs."""
    all_events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:  # Increased timeout
        fetch_tasks: list[asyncio.Task[httpx.Response]] = []
        for url_item in ical_urls:
            logger.info(f"Fetching iCal data from: {url_item}")
            # client.get returns a coroutine, ensure it's wrapped in a task for gather if not already
            fetch_tasks.append(asyncio.create_task(client.get(url_item)))

        # `results` will be a list of httpx.Response objects or exceptions
        results: list[httpx.Response | BaseException] = await asyncio.gather(
            *fetch_tasks, return_exceptions=True
        )

        for i, result in enumerate(results):
            url = ical_urls[i]  # Assuming ical_urls maps directly to fetch_tasks
            if isinstance(result, httpx.Response):
                if result.status_code != 200:
                    logger.error(
                        f"Failed to fetch iCal URL {url}: Status {result.status_code}"
                    )
                    continue
                try:
                    ical_data = result.text
                    logger.debug(
                        f"Parsing iCal data from {url} (first 500 chars):\n{ical_data[:500]}..."
                    )
                    # Use vobject to parse the fetched data
                    components = vobject.readComponents(ical_data)  # type: ignore[attr-defined]
                    count = 0
                    for component in components:  # component is vobject.base.Component
                        if component.name.upper() == "VEVENT":  # type: ignore[union-attr]
                            parsed = parse_event(
                                component.serialize(),  # type: ignore[union-attr]
                                timezone_str=timezone_str,  # Pass timezone here
                            )  # Reuse existing parser
                            if parsed:
                                all_events.append(parsed)
                                count += 1
                    logger.info(f"Parsed {count} events from iCal URL: {url}")
                except Exception as e:
                    logger.error(
                        f"Error parsing iCal data from {url}: {e}", exc_info=True
                    )
            elif isinstance(result, Exception):
                logger.error(
                    f"Error fetching iCal URL {url}: {result}", exc_info=result
                )
                # continue is implicit as this is an elif block
            else:
                # This case should ideally not be reached if gather behaves as expected
                logger.error(
                    f"Unexpected type in results for {url}: {type(result)}. Skipping."
                )

    logger.info(
        f"Fetched and parsed {len(all_events)} total events from {len(ical_urls)} iCal URL(s)."
    )
    return all_events


def _fetch_caldav_events_sync(
    username: str,
    password: str,
    calendar_urls: list[str],
    timezone_str: str,  # Added timezone string
    base_url: str | None = None,  # Added base_url parameter
) -> list[dict[str, Any]]:  # reportExplicitAny for Any is acceptable by style guide
    """Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."""
    logger.debug("Executing synchronous CalDAV fetch using direct calendar URLs.")
    all_events: list[dict[str, Any]] = []

    if not calendar_urls:
        logger.error("No calendar URLs provided to _fetch_caldav_events_sync.")
        return []

    # Determine the client URL: use provided base_url or infer from the first calendar_url
    client_url = base_url
    if not client_url and calendar_urls:
        # Basic inference: take the scheme and netloc from the first calendar URL.
        # This might not be robust for all CalDAV server setups.
        try:
            parsed_first_cal_url = httpx.URL(calendar_urls[0])
            client_url = f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}:{parsed_first_cal_url.port}"
            if parsed_first_cal_url.port is None:  # Handle default ports
                client_url = (
                    f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}"
                )
            logger.warning(
                f"CalDAV base_url not provided, inferred '{client_url}' from first calendar URL. "
                "It's recommended to configure 'base_url' explicitly in caldav_config."
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url from '{calendar_urls[0]}': {e}. Cannot proceed with CalDAV fetch."
            )
            return []
    elif (
        not client_url and not calendar_urls
    ):  # Should be caught by earlier check but defensive
        logger.error("No CalDAV base_url provided and no calendar_urls to infer from.")
        return []

    # Define date range based on the provided timezone
    try:
        local_tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(
            f"Invalid timezone string '{timezone_str}' in _fetch_caldav_events_sync. Falling back to UTC."
        )
        local_tz = ZoneInfo("UTC")
    start_date = datetime.now(local_tz).date()
    end_date = start_date + timedelta(
        days=16
    )  # Search up to 16 days out (exclusive end)

    if not client_url:  # Ensure client_url is not None before use
        logger.error(
            "CalDAV client URL could not be determined. Cannot proceed with CalDAV fetch."
        )
        return []

    # Initialize one client with the determined client_url (server base or inferred)
    try:
        client = caldav.DAVClient(
            url=client_url,  # Use the determined base URL for the client
            username=username,
            password=password,
            timeout=30,
        )
    except Exception as e_client:
        logger.error(
            f"Failed to initialize CalDAV client for server URL '{client_url}': {e_client}"
        )
        return []

    # Iterate through each specific calendar URL
    for calendar_url_item in calendar_urls:
        logger.info(
            f"Attempting to fetch from calendar collection: {calendar_url_item}"
        )
        try:
            # Get the Calendar object using the client and the specific calendar_url_item
            target_calendar = client.calendar(url=calendar_url_item)  # type: ignore[no-untyped-call]

            logger.info(
                f"Searching for events between {start_date} and {end_date} in calendar {target_calendar.url}"  # type: ignore[attr-defined]
            )

            caldav_results = target_calendar.search(
                start=start_date,
                end=end_date,
                event=True,
                expand=True,  # Fetches full data
            )
            logger.debug(
                f"Found {len(caldav_results)} potential events in calendar {target_calendar.url}"
            )

            # Process fetched events
            for (
                event_resource
            ) in caldav_results:  # event_resource is CalendarObjectResource
                try:
                    event_url_attr = getattr(event_resource, "url", "N/A")
                    event_data_str: str = (
                        event_resource.data
                    )  # Access data synchronously, it's a string
                    # Pass the timezone_str to parse_event for localization
                    parsed = parse_event(event_data_str, timezone_str=timezone_str)
                    if parsed:
                        all_events.append(parsed)
                    else:
                        logger.warning(
                            f"Failed to parse event data for event {event_url_attr} in {calendar_url_item}. Skipping."
                        )
                except (DAVError, NotFoundError, Exception) as event_err:
                    logger.error(
                        f"Error processing individual event {getattr(event_resource, 'url', 'N/A')} in {calendar_url_item}: {event_err}",
                        exc_info=True,
                    )
        except NotFoundError:
            logger.error(
                f"Calendar collection not found at URL {calendar_url_item}. Skipping."
            )
        except DAVError as e:
            logger.error(
                f"CalDAV error while processing calendar {calendar_url_item}: {e}",
                exc_info=True,
            )
        except Exception as e:
            logger.error(
                f"Unexpected error during CalDAV fetch for calendar {calendar_url_item}: {e}",
                exc_info=True,
            )
        # Continue to the next calendar URL if one fails

    # Sort events by start time
    try:
        all_events.sort(key=lambda x: x["start"])
    except TypeError as sort_err:
        logger.error(
            f"Error sorting events, possibly due to mixed date/datetime types without tzinfo: {sort_err}"
        )

    logger.info(f"Synchronously fetched and parsed {len(all_events)} events.")
    return all_events


# --- Main Orchestration Function ---


async def fetch_upcoming_events(
    calendar_config: dict[str, Any],  # Config structure can be complex, Any is fine
    timezone_str: str,  # Added timezone string
) -> list[dict[str, Any]]:  # reportExplicitAny for Any is acceptable by style guide
    """Fetches events from configured CalDAV and iCal sources and merges them."""
    logger.debug("Entering fetch_upcoming_events orchestrator.")
    all_events: list[dict[str, Any]] = []
    # Allow tasks list to hold both Futures (from run_in_executor) and Tasks
    tasks: list[asyncio.Future[Any] | asyncio.Task[Any]] = []

    # --- Schedule CalDAV Fetch (if configured) ---
    caldav_config = calendar_config.get("caldav")
    if caldav_config:
        username = caldav_config.get("username")
        password = caldav_config.get("password")
        calendar_urls = caldav_config.get(
            "calendar_urls", []
        )  # These are full URLs to collections
        base_url = caldav_config.get("base_url")  # This is the server base URL

        if (
            username and password and calendar_urls
        ):  # base_url is optional but recommended
            loop = asyncio.get_running_loop()
            logger.debug("Scheduling synchronous CalDAV fetch in executor.")
            caldav_task = loop.run_in_executor(
                None,  # Use default executor
                _fetch_caldav_events_sync,
                username,
                password,
                calendar_urls,  # Pass list of full collection URLs
                timezone_str,
                base_url,  # Pass the server base_url
            )
            tasks.append(caldav_task)
        else:
            logger.warning(
                "CalDAV config present (%r) but incomplete (missing user/pass or calendar_urls). Skipping CalDAV fetch.",
                caldav_config,
            )

    # --- Schedule iCal Fetch (if configured) ---
    ical_config = calendar_config.get("ical")
    if ical_config:
        ical_urls = ical_config.get("urls", [])
        if ical_urls:
            logger.debug("Scheduling asynchronous iCal fetch.")
            # Pass timezone_str to the iCal fetcher
            ical_task = asyncio.create_task(
                _fetch_ical_events_async(ical_urls, timezone_str)
            )
            tasks.append(ical_task)
        else:
            logger.warning(
                "iCal config present but no URLs provided. Skipping iCal fetch."
            )

    # --- Gather Results ---
    if not tasks:
        logger.info("No calendar sources to fetch from.")
        return []

    logger.info(f"Fetching events from {len(tasks)} source(s) concurrently...")
    # Explicitly type results, as gather with return_exceptions=True can return exceptions
    results: list[Any | BaseException] = await asyncio.gather(
        *tasks, return_exceptions=True
    )

    # --- Process Results ---
    for result in results:
        if isinstance(result, Exception):
            # Errors within the fetch functions should already be logged
            logger.error(
                f"Caught exception during asyncio.gather: {result}", exc_info=result
            )
        elif isinstance(result, list):
            all_events.extend(result)
        else:
            logger.warning(f"Unexpected result type from gather: {type(result)}")

    logger.info(
        f"Total events fetched from all sources before sorting: {len(all_events)}"
    )

    # --- Sort Combined Events ---
    def get_sort_key(event: dict[str, Any]) -> datetime:
        """Converts date/datetime to timezone-aware datetime in the local timezone for sorting."""
        start_val = event["start"]
        try:
            local_tz = ZoneInfo(timezone_str)
        except Exception:
            logger.warning(
                f"Invalid timezone '{timezone_str}' in get_sort_key, falling back to UTC."
            )
            local_tz = ZoneInfo("UTC")  # Fallback

        if isinstance(start_val, date) and not isinstance(start_val, datetime):
            # Convert date to datetime at midnight *in the local timezone*
            return datetime.combine(start_val, time.min, tzinfo=local_tz)
        elif isinstance(start_val, datetime):
            # If it's a datetime, ensure it's timezone-aware and in the correct local timezone
            if start_val.tzinfo is None:
                logger.warning(
                    f"Found naive datetime {start_val} during sorting for event '{event['summary']}'. Applying local timezone {timezone_str}."
                )
                return start_val.replace(
                    tzinfo=local_tz
                )  # Make aware assuming local TZ
            else:
                return start_val.astimezone(local_tz)  # Convert to local TZ
        # Fallback for unexpected types (shouldn't happen with proper parsing)
        logger.error(
            f"Unexpected type for event start time: {type(start_val)}. Returning epoch."
        )
        return datetime.fromtimestamp(0, tz=local_tz)

    try:
        # Ensure start times are comparable using the helper function
        all_events.sort(key=get_sort_key)
        logger.debug("Sorted combined events by start time.")
    except TypeError as sort_err:
        # This error might still occur if there are timezone-aware and naive datetimes mixed
        logger.error(
            f"Error sorting combined events, possibly due to mixed date/datetime types without tzinfo: {sort_err}"
        )

    logger.info(f"Total unique events after potential sorting: {len(all_events)}")
    # Note: This doesn't explicitly handle duplicates between CalDAV and iCal sources
    # if they represent the same event. Sorting helps group them.
    return all_events


# --- Formatting for Prompt ---


def format_events_for_prompt(
    events: list[dict[str, Any]],
    prompts: dict[str, str],  # Prompts can have varied structure
    timezone_str: str,  # Added timezone string
) -> tuple[str, str]:
    """Formats the fetched events into strings suitable for the prompt."""
    try:
        local_tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(
            f"Invalid timezone string '{timezone_str}' in format_events_for_prompt. Falling back to UTC."
        )
        local_tz = ZoneInfo("UTC")

    today_local = datetime.now(local_tz).date()
    tomorrow_local = today_local + timedelta(days=1)
    two_weeks_later = today_local + timedelta(
        days=15
    )  # End of the 14-day window after tomorrow (inclusive end date for comparison)

    today_tomorrow_events = []
    next_two_weeks_events = []

    event_fmt = prompts.get(
        "event_item_format", "- {start_time} to {end_time}: {summary}"
    )
    all_day_fmt = prompts.get(
        "all_day_event_item_format", "- {start_time} (All Day): {summary}"
    )

    for event in events:
        start_dt_orig = event["start"]
        end_dt_orig = event["end"]

        # --- Ensure datetimes are timezone-aware before formatting ---
        # Apply local_tz to any naive datetime objects (likely from iCal)
        start_dt = start_dt_orig
        if isinstance(start_dt, datetime) and start_dt.tzinfo is None:
            logger.debug(
                f"Applying timezone {timezone_str} to naive start_dt {start_dt} in format_events_for_prompt"
            )
            start_dt = start_dt.replace(tzinfo=local_tz)

        end_dt = end_dt_orig
        if isinstance(end_dt, datetime) and end_dt.tzinfo is None:
            logger.debug(
                f"Applying timezone {timezone_str} to naive end_dt {end_dt} in format_events_for_prompt"
            )
            end_dt = end_dt.replace(tzinfo=local_tz)
        # --- End timezone awareness check ---

        start_date_only = (
            start_dt.date()
            if isinstance(start_dt, datetime)
            else start_dt_orig  # Use original if it was a date
        )

        # Skip events that have already ended (useful if fetch range includes past)
        # Compare end time (now guaranteed to be aware or a date) with current time
        now_aware = datetime.now(local_tz)

        # Convert end_dt (potentially localized datetime or original date) to aware datetime for comparison
        if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
            # All-day event ends at the start of the next day
            end_dt_aware = datetime.combine(end_dt, time.min, tzinfo=local_tz)
        elif isinstance(end_dt, datetime):
            # It's already aware (either originally or localized above)
            end_dt_aware = end_dt.astimezone(
                local_tz
            )  # Ensure it's in the *local* tz for comparison
        else:
            end_dt_aware = None  # Cannot compare if end time is invalid

        if end_dt_aware and end_dt_aware <= now_aware:
            logger.info(
                f"Skipping past event: '{event['summary']}' ended at {end_dt_aware}"
            )
            continue

        # Format start/end times (using potentially localized datetimes) using the timezone
        start_str = format_datetime_or_date(start_dt, timezone_str, is_end=False)
        end_str = format_datetime_or_date(end_dt, timezone_str, is_end=True)
        summary = event["summary"]

        fmt = all_day_fmt if event["all_day"] else event_fmt
        event_str = fmt.format(start_time=start_str, end_time=end_str, summary=summary)

        # Categorize event based on local date
        if start_date_only <= tomorrow_local:
            today_tomorrow_events.append(event_str)
        elif start_date_only <= two_weeks_later:
            next_two_weeks_events.append(event_str)
        # Ignore events further out than 2 weeks + today/tomorrow

    # Limit the "next two weeks" list
    limited_next_two_weeks = next_two_weeks_events[:10]

    today_tomorrow_str = (
        "\n".join(today_tomorrow_events)
        if today_tomorrow_events
        else prompts.get("no_events_today_tomorrow", "None")
    )
    next_two_weeks_str = (
        "\n".join(limited_next_two_weeks)
        if limited_next_two_weeks
        else prompts.get("no_events_next_two_weeks", "None")
    )

    return today_tomorrow_str, next_two_weeks_str


# --- Tool Implementations (Moved from tools.py) ---


async def add_calendar_event_tool(
    exec_context: "ToolExecutionContext",
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
    exec_context: "ToolExecutionContext",
    calendar_config: dict[str, Any],
    query_text: str,
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    limit: int = 5,
) -> str:
    """Searches CalDAV events based on query text and date range."""
    logger.info(
        f"Executing search_calendar_events_tool: query='{query_text}', start='{start_date_str}', end='{end_date_str}'"
    )
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")  # type: ignore

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot search events."

    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    calendar_urls_list: list[str] | None = caldav_config.get("calendar_urls", [])
    base_url: str | None = caldav_config.get("base_url")

    if not username or not password or not calendar_urls_list:
        return "Error: CalDAV configuration is incomplete (missing user, pass, or calendar_urls). Cannot search events."

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
                f"CalDAV base_url not provided for search_calendar_events_tool, inferred '{client_url_to_use}'. "
                "Explicit 'base_url' in config is recommended."
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for search_calendar_events_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred. Cannot search events."

    if not client_url_to_use:
        return "Error: CalDAV client URL could not be determined for search."

    try:
        local_tz = ZoneInfo(exec_context.timezone_str)
        today_local = datetime.now(local_tz).date()

        start_date_obj = (
            isoparse(start_date_str).date() if start_date_str else today_local
        )
        if end_date_str:
            end_date_obj = isoparse(end_date_str).date()
        else:
            # Default end date: start_date + 3 days (inclusive start, exclusive end for search)
            # Default end date: start_date + 2 days to cover common requests like "tomorrow" or "day after"
            end_date_obj = start_date_obj + timedelta(
                days=3
            )  # Search up to end of day 2 days after start

        if end_date_obj <= start_date_obj:
            return "Error: End date must be after start date."

    except ValueError as ve:
        logger.error(f"Invalid date format for search: {ve}")
        return f"Error: Invalid date format provided. Use YYYY-MM-DD. {ve}"

    matching_events_details = []

    # --- Synchronous CalDAV Search Logic ---
    def search_sync() -> list[
        dict[str, Any]
    ]:  # reportExplicitAny for Any is acceptable
        found_details: list[dict[str, Any]] = []
        query_lower = query_text.lower()
        events_checked = 0
        events_matched = 0

        try:
            dav_client = caldav.DAVClient(
                url=client_url_to_use,  # Use base_url for client
                username=username,
                password=password,
                timeout=30,
            )
        except Exception as e_client_search:
            logger.error(
                f"Failed to initialize CalDAV client for server URL '{client_url_to_use}' in search: {e_client_search}"
            )
            return []  # Return empty list, error message will be generated by caller

        for cal_url_item in calendar_urls_list:
            logger.debug(
                f"Searching calendar collection: {cal_url_item} from {start_date_obj} to {end_date_obj} using server {client_url_to_use}"
            )
            try:
                target_calendar_obj: caldav.objects.Calendar = dav_client.calendar(
                    url=cal_url_item  # Get specific calendar using its full URL
                )
                if not target_calendar_obj:
                    logger.warning(
                        f"Could not get calendar object for {cal_url_item} on server {client_url_to_use}"
                    )
                    continue

                    # Fetch all events from the calendar and filter them in Python
                    # This is a workaround for potential issues with server-side time-range filtering on narrow ranges.
                    all_event_resources_in_calendar: list[
                        caldav.objects.CalendarObjectResource
                    ] = target_calendar_obj.events()  # type: ignore

                    logger.info(
                        f"Fetched {len(all_event_resources_in_calendar)} total events from {cal_url_item} for manual filtering."
                    )

                    for (
                        event_resource
                    ) in all_event_resources_in_calendar:  # event_resource is CalendarObjectResource
                        events_checked += 1
                        event_url_attr = getattr(
                            event_resource, "url", "N/A"
                        )  # Get URL for logging
                        logger.debug(
                            f"Processing event for manual filter: URL={event_url_attr}"
                        )

                        parsed: dict[str, Any] | None = (
                            None  # Initialize parsed to None
                        )
                        try:
                            event_data_str: str = event_resource.data  # type: ignore # It's a string
                            parsed = parse_event(
                                event_data_str, timezone_str=exec_context.timezone_str
                            )

                            if not parsed or not parsed.get("start"):
                                logger.debug(
                                    f"  -> Excluded (manual filter): Failed to parse event data or missing start time. URL={event_url_attr}"
                                )
                                continue

                            # Date filtering
                            event_start_val = parsed["start"]
                            event_start_date = (
                                event_start_val.date()
                                if isinstance(event_start_val, datetime)
                                else event_start_val
                            )

                            if not (
                                start_date_obj <= event_start_date < end_date_obj
                            ):
                                logger.debug(
                                    f"  -> Excluded (manual filter): Event UID {parsed.get('uid')} start date {event_start_date} outside range [{start_date_obj}, {end_date_obj})."
                                )
                                continue

                            # Text filtering
                            summary = parsed.get("summary", "")
                            summary_lower = summary.lower()
                            parsed_uid = parsed.get("uid", "UnknownUID")

                            if query_lower in summary_lower:
                                events_matched += 1
                                logger.info(
                                    f"  -> Matched (manual filter): Query '{query_lower}' in summary '{summary}'. UID={parsed_uid}"
                                )
                                found_details.append({
                                    "uid": str(parsed_uid),
                                    "summary": summary,
                                    "start": parsed.get("start"),
                                    "end": parsed.get("end"),
                                    "all_day": parsed.get("all_day"),
                                    "calendar_url": cal_url_item,
                                })
                                if len(found_details) >= limit:
                                    logger.info(
                                        f"Reached search limit ({limit}) during manual filter in calendar {cal_url_item}."
                                    )
                                    break
                            else:
                                logger.debug(
                                    f"  -> Excluded (manual filter): Query '{query_lower}' not in summary '{summary}'. UID={parsed_uid}"
                                )

                        except Exception as process_err:
                            logger.warning(
                                f"Error processing event {event_url_attr} during manual filter in {cal_url_item}: {process_err}",
                                exc_info=True,
                            )
                    if len(found_details) >= limit:
                        logger.info(
                            f"Reached search limit ({limit}) during manual filter. Stopping search across remaining calendars."
                        )
                        break

            except NotFoundError:
                logger.error(
                    f"Calendar collection not found at URL {cal_url_item} on server {client_url_to_use}. Skipping."
                )
            except (DAVError, ConnectionError, Exception) as sync_err:
                logger.error(
                    f"Error searching calendar collection {cal_url_item} on server {client_url_to_use}: {sync_err}",
                    exc_info=True,
                )
            # Continue to next calendar if one fails

        logger.info(
            f"Checked {events_checked} events across {len(calendar_urls_list)} calendar(s), found {events_matched} potential matches via text filter."
        )
        return found_details

    # --- End Synchronous Logic ---

    try:
        loop = asyncio.get_running_loop()
        matching_events_details = await loop.run_in_executor(None, search_sync)

        if not matching_events_details:
            return "No events found matching your query and date range."

        # Format for LLM (e.g., numbered list)
        response_lines = ["Found potential matches:"]
        for i, details in enumerate(matching_events_details):
            start_value = details.get("start")
            if start_value is None:
                start_str = "Unknown Start Time"
            else:
                # Format start/end for display here, passing the timezone
                start_str = format_datetime_or_date(
                    start_value, exec_context.timezone_str, is_end=False
                )
            response_lines.append(
                f"{i + 1}. Summary: '{details['summary']}', Start: {start_str}, UID: {details['uid']}, Calendar: {details['calendar_url']}"
            )
        return "\n".join(response_lines)

    except Exception as e:
        logger.error(f"Unexpected error searching calendar events: {e}", exc_info=True)
        return f"Error: An unexpected error occurred during event search. {e}"


async def modify_calendar_event_tool(
    exec_context: "ToolExecutionContext",
    calendar_config: dict[str, Any],
    uid: str,
    calendar_url: str,  # Added calendar_url
    new_summary: str | None = None,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
    new_description: str | None = None,
    new_all_day: bool | None = None,
) -> str:
    """Modifies a specific CalDAV event by UID."""
    logger.info(
        f"Executing modify_calendar_event_tool for UID: {uid} in calendar: {calendar_url}"
    )
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")  # type: ignore

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot modify event."
    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    # base_url for the DAVClient should be part of caldav_config
    # The 'calendar_url' parameter for this tool is the specific collection URL
    base_url: str | None = caldav_config.get("base_url")

    if not username or not password:
        return "Error: CalDAV user/pass missing. Cannot modify event."

    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            # Infer from the specific calendar_url being modified
            parsed_cal_url = httpx.URL(calendar_url)
            client_url_to_use = (
                f"{parsed_cal_url.scheme}://{parsed_cal_url.host}:{parsed_cal_url.port}"
            )
            if parsed_cal_url.port is None:
                client_url_to_use = f"{parsed_cal_url.scheme}://{parsed_cal_url.host}"
            logger.warning(
                f"CalDAV base_url not provided for modify_calendar_event_tool, inferred '{client_url_to_use}' from target calendar URL. "
                "Explicit 'base_url' in config is recommended."
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for modify_calendar_event_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred. Cannot modify event."

    if not client_url_to_use:
        return "Error: CalDAV client URL could not be determined for modify."

    # Check if any modification was actually requested
    if all(
        arg is None
        for arg in [
            new_summary,
            new_start_time,
            new_end_time,
            new_description,
            new_all_day,
        ]
    ):
        return "Error: No changes specified. Please provide at least one field to modify (e.g., new_summary, new_start_time)."

    # --- Synchronous CalDAV Modify Logic ---
    def modify_sync() -> str | None:
        try:
            with caldav.DAVClient(
                url=client_url_to_use,  # Use base_url for client
                username=username,
                password=password,
                timeout=30,
            ) as client:
                # Get specific calendar object using its full URL
                target_calendar_obj: caldav.objects.Calendar = client.calendar(
                    url=calendar_url  # Full URL of the calendar collection to modify
                )
                if not target_calendar_obj:
                    raise ValueError(
                        f"Could not get calendar object for {calendar_url} on server {client_url_to_use}"
                    )

                logger.debug(
                    f"Fetching event with UID {uid} from {target_calendar_obj.url}"
                )
                event_resource: caldav.objects.Event = target_calendar_obj.event_by_uid(
                    uid
                )  # type: ignore
                logger.debug("Found event.")  # Removed ETag logging

                # Parse existing event data
                # readComponents yields components; we expect one top-level iCalendar component
                event_data_str: str = event_resource.data  # type: ignore
                ical_component_generator = vobject.readComponents(event_data_str)  # type: ignore[attr-defined]
                try:
                    ical_component: vobject.base.Component = next(
                        ical_component_generator
                    )  # type: ignore
                    # Assuming the main component contains a single VEVENT
                    vevent: vobject.base.Component = ical_component.vevent  # type: ignore
                except (StopIteration, AttributeError) as parse_err:
                    logger.error(
                        f"Failed to parse VEVENT from event data for UID {uid}: {parse_err}",
                        exc_info=True,
                    )
                    return f"Error: Could not parse existing event data for UID {uid}."

                # Apply modifications
                modified = False
                if new_summary is not None:
                    vevent.summary.value = new_summary  # type: ignore[union-attr]
                    modified = True
                if new_description is not None:
                    # Add or update description
                    if hasattr(vevent, "description"):
                        vevent.description.value = new_description  # type: ignore[union-attr]
                    else:
                        vevent.add("description").value = new_description  # type: ignore[union-attr]
                    modified = True

                # Handle time changes (more complex)
                current_is_all_day = not isinstance(vevent.dtstart.value, datetime)  # type: ignore[union-attr]
                target_all_day = (
                    new_all_day if new_all_day is not None else current_is_all_day
                )

                if new_start_time:
                    try:
                        if target_all_day:
                            vevent.dtstart.value = isoparse(new_start_time).date()  # type: ignore[union-attr]
                        else:
                            dtstart_val = isoparse(new_start_time)
                            local_tz = ZoneInfo(exec_context.timezone_str)
                            if dtstart_val.tzinfo is None:
                                logger.warning(
                                    f"New start time '{new_start_time}' lacks timezone. Assuming {exec_context.timezone_str}."
                                )
                                dtstart_val = dtstart_val.replace(tzinfo=local_tz)
                            vevent.dtstart.value = dtstart_val  # type: ignore[union-attr]
                        modified = True
                    except ValueError as ve_start:
                        return f"Error parsing new_start_time: {ve_start}"
                if new_end_time:
                    try:
                        if target_all_day:
                            vevent.dtend.value = isoparse(new_end_time).date()  # type: ignore[union-attr]
                        else:
                            dtend_val = isoparse(new_end_time)
                            local_tz = ZoneInfo(exec_context.timezone_str)
                            if dtend_val.tzinfo is None:
                                logger.warning(
                                    f"New end time '{new_end_time}' lacks timezone. Assuming {exec_context.timezone_str}."
                                )
                                dtend_val = dtend_val.replace(tzinfo=local_tz)
                            vevent.dtend.value = dtend_val  # type: ignore[union-attr]
                        modified = True
                    except ValueError as ve_end:
                        return f"Error parsing new_end_time: {ve_end}"

                # Basic validation after potential time changes
                if (
                    hasattr(vevent, "dtend")  # type: ignore[arg-type]
                    and vevent.dtend.value <= vevent.dtstart.value  # type: ignore[union-attr]
                ):
                    return (
                        "Error: Event end time cannot be before or same as start time."
                    )

                if modified:
                    # Update timestamp on the vevent itself
                    vevent.dtstamp.value = datetime.now(  # type: ignore[union-attr]
                        ZoneInfo("UTC")
                    )  # Use ZoneInfo for UTC
                    # Serialize the modified iCalendar component
                    updated_ical_data_str: str = ical_component.serialize()  # type: ignore[union-attr]
                    logger.debug(
                        "Attempting to save modified event."
                    )  # Removed ETag logging
                    # Set the updated data on the event object first
                    event_resource.data = updated_ical_data_str  # type: ignore
                    # Save the event, allowing overwrite since we are modifying an existing event
                    _ = event_resource.save(no_overwrite=False)  # type: ignore
                    logger.info(f"Successfully saved modified event UID {uid}")
                    summary_text_val = (
                        vevent.summary.value  # type: ignore[union-attr]
                        if hasattr(vevent, "summary")  # type: ignore[arg-type]
                        and hasattr(vevent.summary, "value")  # type: ignore[union-attr]
                        else "N/A"
                    )
                    return f"OK. Event '{summary_text_val}' updated."
                else:
                    # This case should be caught earlier, but handle defensively
                    return "No changes applied."

        except NotFoundError:
            logger.error(f"Event with UID {uid} not found in calendar {calendar_url}.")
            return f"Error: Event with UID {uid} not found."
        # Removed PreconditionFailed handler as ETag is not used
        except (
            DAVError,
            ConnectionError,
            ValueError,
            Exception,
        ) as sync_err:
            logger.error(f"Error modifying event UID {uid}: {sync_err}", exc_info=True)
            return f"Error: Failed to modify event. {sync_err}"

    # --- End Synchronous Logic ---

    try:
        loop = asyncio.get_running_loop()
        actual_result: str | None = await loop.run_in_executor(None, modify_sync)
        if actual_result is None:
            return "Error: Modify operation completed without a specific message."
        return actual_result
    except Exception as e:
        logger.error(f"Unexpected error modifying calendar event: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while modifying the event. {e}"


async def delete_calendar_event_tool(
    exec_context: "ToolExecutionContext",
    calendar_config: dict[str, Any],
    uid: str,
    calendar_url: str,  # Added calendar_url
) -> str:
    """Deletes a specific CalDAV event by UID."""
    logger.info(
        f"Executing delete_calendar_event_tool for UID: {uid} in calendar: {calendar_url}"
    )
    # calendar_config is now a direct parameter
    caldav_config: dict[str, Any] | None = calendar_config.get("caldav")  # type: ignore

    if not caldav_config:
        return "Error: CalDAV is not configured. Cannot delete event."
    username: str | None = caldav_config.get("username")
    password: str | None = caldav_config.get("password")
    base_url: str | None = caldav_config.get("base_url")  # Expect base_url in config

    if not username or not password:
        return "Error: CalDAV user/pass missing. Cannot delete event."

    client_url_to_use = base_url
    if not client_url_to_use:
        try:
            # Infer from the specific calendar_url being targeted
            parsed_cal_url = httpx.URL(calendar_url)
            client_url_to_use = (
                f"{parsed_cal_url.scheme}://{parsed_cal_url.host}:{parsed_cal_url.port}"
            )
            if parsed_cal_url.port is None:
                client_url_to_use = f"{parsed_cal_url.scheme}://{parsed_cal_url.host}"
            logger.warning(
                f"CalDAV base_url not provided for delete_calendar_event_tool, inferred '{client_url_to_use}' from target calendar URL. "
                "Explicit 'base_url' in config is recommended."
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for delete_calendar_event_tool: {e}"
            )
            return "Error: CalDAV base_url missing and could not be inferred. Cannot delete event."

    if not client_url_to_use:
        return "Error: CalDAV client URL could not be determined for delete."

    # --- Synchronous CalDAV Delete Logic ---
    def delete_sync() -> str | None:
        try:
            with caldav.DAVClient(
                url=client_url_to_use,  # Use base_url for client
                username=username,
                password=password,
                timeout=30,
            ) as client:
                # Get specific calendar object using its full URL
                target_calendar_obj: caldav.objects.Calendar = client.calendar(
                    url=calendar_url  # Full URL of the calendar collection
                )
                if not target_calendar_obj:
                    raise ValueError(
                        f"Could not get calendar object for {calendar_url} on server {client_url_to_use}"
                    )

                logger.debug(
                    f"Fetching event with UID {uid} for deletion from {target_calendar_obj.url}"
                )
                event_resource: caldav.objects.Event = target_calendar_obj.event_by_uid(
                    uid
                )  # type: ignore
                # Attempt to parse summary before deleting for better confirmation message
                summary_val = "Unknown Summary"
                try:
                    # Use the parse_event function defined within this module
                    event_data_str: str = event_resource.data  # type: ignore
                    parsed_event = parse_event(
                        event_data_str, timezone_str=exec_context.timezone_str
                    )  # Pass timezone for consistency
                    if parsed_event and parsed_event.get("summary"):
                        summary_val = parsed_event["summary"]
                except Exception:
                    logger.warning(
                        f"Could not parse event {uid} summary before deletion."
                    )

                logger.info(f"Found event '{summary_val}'. Deleting...")
                event_resource.delete()  # type: ignore
                logger.info(f"Successfully deleted event UID {uid}")
                return f"OK. Event '{summary_val}' deleted."

        except NotFoundError:
            logger.error(
                f"Event with UID {uid} not found in calendar {calendar_url} for deletion."
            )
            return f"Error: Event with UID {uid} not found."
        except (
            DAVError,
            ConnectionError,
            ValueError,
            Exception,
        ) as sync_err:
            logger.error(f"Error deleting event UID {uid}: {sync_err}", exc_info=True)
            return f"Error: Failed to delete event. {sync_err}"

    # --- End Synchronous Logic ---

    try:
        loop = asyncio.get_running_loop()
        actual_result: str | None = await loop.run_in_executor(None, delete_sync)
        if actual_result is None:
            return "Error: Delete operation completed without a specific message."
        return actual_result
    except Exception as e:
        logger.error(f"Unexpected error deleting calendar event: {e}", exc_info=True)
        return f"Error: An unexpected error occurred while deleting the event. {e}"


# Removed unused function _fetch_event_details_sync
