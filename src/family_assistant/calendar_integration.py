import asyncio  # Import asyncio for run_in_executor
import logging
from datetime import date, datetime, time, timedelta  # Added time
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo  # Import ZoneInfo

import caldav
import httpx  # Import httpx
import vobject
from caldav.lib.error import (  # Reverted to original-like import path
    DAVError,
    NotFoundError,
)

from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig, CalendarEvent

logger = logging.getLogger(__name__)


# --- Configuration (Now passed via function arguments) ---
# Environment variables are still read here for the standalone test section (__main__)

# --- Helper Functions ---


def format_datetime_or_date(
    dt_obj: datetime | date,
    timezone_str: str,
    is_end: bool = False,
    clock: Clock | None = None,
) -> str:
    """Formats datetime or date object into a user-friendly string, relative to the specified timezone."""
    if clock is None:
        clock = SystemClock()
    try:
        local_tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(
            f"Invalid timezone string '{timezone_str}' in format_datetime_or_date. Falling back to UTC."
        )
        local_tz = ZoneInfo("UTC")

    now_local = clock.now().astimezone(local_tz)
    today_local = now_local.date()
    tomorrow_local = today_local + timedelta(days=1)

    if isinstance(dt_obj, datetime):
        # Check if the datetime is at midnight UTC, which indicates an all-day event
        if (
            dt_obj.time() == time(0, 0)
            and dt_obj.tzinfo
            and dt_obj.tzinfo.utcoffset(dt_obj) == timedelta(0)
        ):
            display_date = dt_obj.date()
            if is_end:
                display_date -= timedelta(days=1)

            if display_date == today_local:
                return "Today"
            if display_date == tomorrow_local:
                return "Tomorrow"
            return display_date.strftime("%b %d")

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
        # For all-day events, treat the date as starting at midnight in the local timezone
        # This ensures correct comparison against today_local and tomorrow_local

        # Adjust end date for display: CalDAV often stores end date as the day *after*
        display_date = dt_obj - timedelta(days=1) if is_end else dt_obj

        if display_date == today_local:
            return "Today"
        if display_date == tomorrow_local:
            return "Tomorrow"
        return display_date.strftime("%b %d")  # e.g., Apr 21
    # Fallback for other types, though dt_obj should only be datetime or date
    # Based on type hints, this path should not be reached if input is correct.
    # However, to satisfy linters about all paths returning, and for robustness:
    return str(dt_obj)


def parse_event(
    event_data: str,
    timezone_str: str | None = None,
) -> "CalendarEvent | None":
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

        return cast(
            "CalendarEvent",
            {
                "uid": uid,
                "summary": summary,
                "start": dtstart,
                "end": dtend,
                "all_day": is_all_day,
            },
        )
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
) -> list["CalendarEvent"]:
    """Asynchronously fetches and parses events from a list of iCal URLs."""
    all_events: list[CalendarEvent] = []
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
) -> list["CalendarEvent"]:
    """Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."""
    logger.debug("Executing synchronous CalDAV fetch using direct calendar URLs.")
    all_events: list[CalendarEvent] = []

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
    def get_sort_key_caldav(event: "CalendarEvent") -> datetime:
        """Converts date/datetime to timezone-aware datetime in the local timezone for sorting."""
        start_val = event["start"]
        try:
            local_tz = ZoneInfo(timezone_str)
        except Exception:
            logger.warning(
                f"Invalid timezone '{timezone_str}' in get_sort_key_caldav, falling back to UTC."
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
        all_events.sort(key=get_sort_key_caldav)
    except TypeError as sort_err:
        logger.error(
            f"Error sorting events, possibly due to mixed date/datetime types without tzinfo: {sort_err}"
        )

    logger.info(f"Synchronously fetched and parsed {len(all_events)} events.")
    return all_events


# --- Main Orchestration Function ---


async def fetch_upcoming_events(
    calendar_config: "CalendarConfig",
    timezone_str: str,  # Added timezone string
) -> list["CalendarEvent"]:
    """Fetches events from configured CalDAV and iCal sources and merges them."""
    logger.debug("Entering fetch_upcoming_events orchestrator.")
    all_events: list[CalendarEvent] = []
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
    def get_sort_key(event: "CalendarEvent") -> datetime:
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
    events: list["CalendarEvent"],
    prompts: dict[str, str],  # Prompts can have varied structure
    timezone_str: str,  # Added timezone string
    clock: Clock | None = None,
) -> tuple[str, str]:
    """Formats the fetched events into strings suitable for the prompt."""
    if clock is None:
        clock = SystemClock()
    try:
        local_tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(
            f"Invalid timezone string '{timezone_str}' in format_events_for_prompt. Falling back to UTC."
        )
        local_tz = ZoneInfo("UTC")

    today_local = clock.now().astimezone(local_tz).date()
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
        now_aware = clock.now().astimezone(local_tz)

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
        start_str = format_datetime_or_date(
            start_dt, timezone_str, is_end=False, clock=clock
        )
        end_str = format_datetime_or_date(
            end_dt, timezone_str, is_end=True, clock=clock
        )
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


# --- Event Detail Fetching for Confirmations ---


async def fetch_event_details_for_confirmation(
    uid: str,
    calendar_url: str,
    calendar_config: "CalendarConfig",
) -> "CalendarEvent | None":
    """Fetches calendar event details by UID for use in confirmation prompts.

    Args:
        uid: The UID of the calendar event to fetch
        calendar_url: The full URL of the calendar collection
        calendar_config: Calendar configuration containing CalDAV settings

    Returns:
        Dict containing event details (summary, start, end, all_day, uid) or None if not found
    """
    logger.info(
        f"Fetching event details for confirmation: UID={uid}, calendar={calendar_url}"
    )

    caldav_config = calendar_config.get("caldav")
    if not caldav_config:
        logger.error("CalDAV configuration not found for event details fetch")
        return None

    username = caldav_config.get("username")
    password = caldav_config.get("password")
    base_url = caldav_config.get("base_url")

    if not username or not password:
        logger.error("CalDAV credentials missing for event details fetch")
        return None

    # Determine client URL
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
                f"CalDAV base_url not provided for fetch_event_details_for_confirmation, inferred '{client_url_to_use}'"
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url for event details fetch: {e}"
            )
            return None

    if not client_url_to_use:
        logger.error(
            "CalDAV client URL could not be determined for event details fetch"
        )
        return None

    # Synchronous fetch function
    def fetch_sync() -> "CalendarEvent | None":
        try:
            with caldav.DAVClient(
                url=client_url_to_use,
                username=username,
                password=password,
                timeout=30,
            ) as client:
                target_calendar_obj: caldav.objects.Calendar = client.calendar(
                    url=calendar_url
                )
                if not target_calendar_obj:
                    logger.error(f"Could not get calendar object for {calendar_url}")
                    return None

                logger.debug(
                    f"Fetching event with UID {uid} from {target_calendar_obj.url}"
                )
                event_resource: caldav.objects.Event = target_calendar_obj.event_by_uid(
                    uid
                )  # type: ignore

                event_data_str: str = event_resource.data  # type: ignore
                # Use UTC as default timezone for confirmation display
                parsed_event = parse_event(event_data_str, timezone_str="UTC")

                if parsed_event:
                    logger.info(
                        f"Successfully fetched event details for UID {uid}: {parsed_event.get('summary', 'No Title')}"
                    )
                    return parsed_event
                else:
                    logger.warning(f"Failed to parse event data for UID {uid}")
                    return None

        except NotFoundError:
            logger.warning(f"Event with UID {uid} not found in calendar {calendar_url}")
            return None
        except (DAVError, ConnectionError, Exception) as e:
            logger.error(
                f"Error fetching event details for UID {uid}: {e}", exc_info=True
            )
            return None

    # Execute in thread pool
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, fetch_sync)
        return result
    except Exception as e:
        logger.error(
            f"Unexpected error in fetch_event_details_for_confirmation: {e}",
            exc_info=True,
        )
        return None


# Removed unused function _fetch_event_details_sync
