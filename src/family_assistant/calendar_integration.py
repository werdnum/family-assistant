import asyncio  # Import asyncio for run_in_executor
import logging
import os
from datetime import datetime, date, timedelta, time  # Added time

# Consolidated imports including Any
from typing import List, Dict, Optional, Tuple, Any
import vobject
import caldav
from caldav.lib.error import DAVError, NotFoundError
import httpx  # Import httpx

logger = logging.getLogger(__name__)

# --- Configuration (Now passed via function arguments) ---
# Environment variables are still read here for the standalone test section (__main__)

# --- Helper Functions ---


def format_datetime_or_date(dt_obj: datetime | date, is_end: bool = False) -> str:
    """Formats datetime or date object into a user-friendly string."""
    if isinstance(dt_obj, datetime):
        # Format datetime, potentially adjust for end time display
        # Example: "Today 14:30", "Tomorrow 09:00", "Apr 21 10:00"
        now = datetime.now(dt_obj.tzinfo)  # Use event's timezone if available
        today = now.date()
        tomorrow = today + timedelta(days=1)

        if dt_obj.date() == today:
            day_str = "Today"
        elif dt_obj.date() == tomorrow:
            day_str = "Tomorrow"
        else:
            day_str = dt_obj.strftime("%b %d")  # e.g., Apr 21

        # For end times exactly at midnight, display as end of previous day if makes sense
        if is_end and dt_obj.time() == time(0, 0):
            # Check if it's the day after the start date (common for multi-day events ending at midnight)
            # This logic might need refinement based on how start_date is passed or stored
            # For simplicity now, just format as is.
            pass

        return f"{day_str} {dt_obj.strftime('%H:%M')}"

    elif isinstance(dt_obj, date):
        # Format date (all-day event)
        # Example: "Today", "Tomorrow", "Apr 21"
        today = date.today()
        tomorrow = today + timedelta(days=1)

        # Adjust end date for display: CalDAV often stores end date as the day *after*
        if is_end:
            dt_obj -= timedelta(days=1)

        if dt_obj == today:
            return "Today"
        elif dt_obj == tomorrow:
            return "Tomorrow"
        else:
            return dt_obj.strftime("%b %d")  # e.g., Apr 21
    else:
        return str(dt_obj)  # Fallback


def parse_event(event_data: str) -> Optional[Dict[str, Any]]:
    """Parses VCALENDAR data into a dictionary."""
    try:
        cal = vobject.readComponents(event_data)
        # Assuming the first component is the VEVENT
        vevent = next(cal).vevent
        summary = vevent.summary.value if hasattr(vevent, "summary") else "No Title"
        dtstart = vevent.dtstart.value if hasattr(vevent, "dtstart") else None
        dtend = vevent.dtend.value if hasattr(vevent, "dtend") else None

        # Basic check for valid event data
        if not summary or not dtstart:
            return None

        is_all_day = not isinstance(dtstart, datetime)

        # If dtend is missing, assume duration based on type (e.g., 1 hour for timed, 1 day for all-day)
        if dtend is None:
            if is_all_day:
                dtend = dtstart + timedelta(days=1)
            else:
                dtend = dtstart + timedelta(hours=1)  # Default duration assumption

        return {
            "summary": summary,
            "start": dtstart,
            "end": dtend,
            "all_day": is_all_day,
        }
    except Exception as e:
        logger.error(
            f"Failed to parse VCALENDAR data: {e}\nData: {event_data[:200]}...",
            exc_info=True,
        )
        return None


# --- Core Fetching Functions ---


async def _fetch_ical_events_async(ical_urls: List[str]) -> List[Dict[str, Any]]:
    """Asynchronously fetches and parses events from a list of iCal URLs."""
    all_events = []
    async with httpx.AsyncClient(timeout=30.0) as client:  # Increased timeout
        fetch_tasks = []
        for url in ical_urls:
            logger.info(f"Fetching iCal data from: {url}")
            fetch_tasks.append(client.get(url))

        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for i, result in enumerate(results):
            url = ical_urls[i]
            if isinstance(result, Exception):
                logger.error(
                    f"Error fetching iCal URL {url}: {result}", exc_info=result
                )
                continue
            elif result.status_code != 200:
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
                cal = vobject.readComponents(ical_data)
                count = 0
                for component in cal:
                    if component.name.upper() == "VEVENT":
                        parsed = parse_event(
                            component.serialize()
                        )  # Reuse existing parser
                        if parsed:
                            all_events.append(parsed)
                            count += 1
                logger.info(f"Parsed {count} events from iCal URL: {url}")
            except Exception as e:
                logger.error(f"Error parsing iCal data from {url}: {e}", exc_info=True)

    logger.info(
        f"Fetched and parsed {len(all_events)} total events from {len(ical_urls)} iCal URL(s)."
    )
    return all_events


def _fetch_caldav_events_sync(
    username: str,
    password: str,
    calendar_urls: List[str],
) -> List[Dict[str, Any]]:
    """Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."""
    logger.debug("Executing synchronous CalDAV fetch using direct calendar URLs.")
    all_events = []

    if not calendar_urls:
        logger.error("No calendar URLs provided to _fetch_events_sync.")
        return []

    # Define date range once
    start_date = date.today()
    end_date = start_date + timedelta(days=16)  # Search up to 16 days out

    # Iterate through each specific calendar URL
    for calendar_url in calendar_urls:
        logger.info(f"Attempting to connect and fetch from calendar: {calendar_url}")
        try:
            # Create a *new* client instance for *each* calendar URL
            # This avoids the URL joining issue by using the specific URL for initialization.
            with caldav.DAVClient(
                url=calendar_url,  # Use the specific calendar URL here
                username=username,
                password=password,
            ) as client:
                # The client is now connected directly to the calendar URL's host.
                # Get the calendar object associated with this specific URL.
                # The `calendar()` method on the client, when given the *same URL* the client
                # was initialized with, should return the calendar object itself.
                target_calendar = client.calendar(url=calendar_url)

                if not target_calendar:
                    logger.error(
                        f"Failed to obtain calendar object for URL: {calendar_url}"
                    )
                    continue  # Skip to the next URL

                logger.info(
                    f"Searching for events between {start_date} and {end_date} in calendar {calendar_url}"
                )
                # Use search method (synchronous) on the identified calendar object
                results = target_calendar.search(
                    start=start_date, end=end_date, event=True, expand=False
                )
                logger.debug(
                    f"Found {len(results)} potential events in calendar {calendar_url}"
                )

                # Process fetched events
                for event in results:
                    try:
                        event_url_attr = getattr(event, "url", "N/A")
                        event_data = event.data  # Access data synchronously
                        parsed = parse_event(event_data)
                        if parsed:
                            all_events.append(parsed)
                        else:
                            logger.warning(
                                f"Failed to parse event data for event {event_url_attr} in {calendar_url}. Skipping."
                            )
                    except (DAVError, NotFoundError, Exception) as event_err:
                        logger.error(
                            f"Error processing individual event {getattr(event, 'url', 'N/A')} in {calendar_url}: {event_err}",
                            exc_info=True,
                        )

        except DAVError as e:
            logger.error(
                f"CalDAV connection or authentication error for URL {calendar_url}: {e}",
                exc_info=True,
            )
            # Continue to the next URL if one fails
        except Exception as e:
            logger.error(
                f"Unexpected error during CalDAV fetch for URL {calendar_url}: {e}",
                exc_info=True,
            )
            # Continue to the next URL

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
    calendar_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Fetches events from configured CalDAV and iCal sources and merges them."""
    logger.debug("Entering fetch_upcoming_events orchestrator.")
    all_events = []
    tasks = []

    # --- Schedule CalDAV Fetch (if configured) ---
    caldav_config = calendar_config.get("caldav")
    if caldav_config:
        username = caldav_config.get("username")
        password = caldav_config.get("password")
        calendar_urls = caldav_config.get("calendar_urls", [])
        if username and password and calendar_urls:
            loop = asyncio.get_running_loop()
            logger.debug("Scheduling synchronous CalDAV fetch in executor.")
            caldav_task = loop.run_in_executor(
                None,  # Use default executor
                _fetch_caldav_events_sync,  # Renamed function
                username,
                password,
                calendar_urls,
            )
            tasks.append(caldav_task)
        else:
            logger.warning(
                "CalDAV config present but incomplete (missing user/pass/urls). Skipping CalDAV fetch."
            )

    # --- Schedule iCal Fetch (if configured) ---
    ical_config = calendar_config.get("ical")
    if ical_config:
        ical_urls = ical_config.get("urls", [])
        if ical_urls:
            logger.debug("Scheduling asynchronous iCal fetch.")
            ical_task = asyncio.create_task(_fetch_ical_events_async(ical_urls))
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
    results = await asyncio.gather(*tasks, return_exceptions=True)

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
    def get_sort_key(event):
        """Converts date to datetime for sorting."""
        start_val = event["start"]
        if isinstance(start_val, date) and not isinstance(start_val, datetime):
            # Convert date to datetime at midnight for comparison
            return datetime.combine(start_val, time.min)
        # Assume datetime objects are directly comparable (might need tz handling if mixed)
        return start_val

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
    events: List[Dict[str, Any]], prompts: Dict[str, str]
) -> Tuple[str, str]:
    """Formats the fetched events into strings suitable for the prompt."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    two_weeks_later = today + timedelta(
        days=15
    )  # End of the 14-day window after tomorrow

    today_tomorrow_events = []
    next_two_weeks_events = []

    event_fmt = prompts.get(
        "event_item_format", "- {start_time} to {end_time}: {summary}"
    )
    all_day_fmt = prompts.get(
        "all_day_event_item_format", "- {start_time} (All Day): {summary}"
    )

    for event in events:
        start_dt = event["start"]
        start_date_only = (
            start_dt.date() if isinstance(start_dt, datetime) else start_dt
        )

        # Skip events that have already ended (useful if fetch range includes past)
        # This check needs refinement if end times aren't precise
        # end_dt = event["end"]
        # now_aware = datetime.now(getattr(end_dt, 'tzinfo', None)) # Match timezone if possible
        # if end_dt < now_aware:
        #    continue

        # Format start/end times
        start_str = format_datetime_or_date(event["start"], is_end=False)
        end_str = format_datetime_or_date(event["end"], is_end=True)
        summary = event["summary"]

        fmt = all_day_fmt if event["all_day"] else event_fmt
        event_str = fmt.format(start_time=start_str, end_time=end_str, summary=summary)

        # Categorize event
        if start_date_only <= tomorrow:
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


if __name__ == "__main__":
    # Example usage for testing
    logging.basicConfig(level=logging.INFO)
    # Load .env for testing standalone
    from dotenv import load_dotenv

    load_dotenv()

    # --- Build dummy config for testing ---
    test_calendar_config = {}
    caldav_user = os.getenv("CALDAV_USERNAME")
    caldav_pass = os.getenv("CALDAV_PASSWORD")
    caldav_urls_str = os.getenv("CALDAV_CALENDAR_URLS")
    caldav_urls = (
        [url.strip() for url in caldav_urls_str.split(",")] if caldav_urls_str else []
    )
    if caldav_user and caldav_pass and caldav_urls:
        test_calendar_config["caldav"] = {
            "username": caldav_user,
            "password": caldav_pass,
            "calendar_urls": caldav_urls,
        }

    ical_urls_str = os.getenv("ICAL_URLS")
    ical_urls = (
        [url.strip() for url in ical_urls_str.split(",")] if ical_urls_str else []
    )
    if ical_urls:
        test_calendar_config["ical"] = {"urls": ical_urls}
    # --- End dummy config ---

    async def run_test():
        print("Fetching events using orchestrator...")
        if not test_calendar_config:
            print("No calendar sources configured in .env for testing.")
            return
        events = await fetch_upcoming_events(test_calendar_config)  # Pass the config
        print(f"\nFetched {len(events)} events from all sources.")

        # Load dummy prompts for formatting test
        test_prompts = {
            "event_item_format": "- {start_time} to {end_time}: {summary}",
            "all_day_event_item_format": "- {start_time} (All Day): {summary}",
            "no_events_today_tomorrow": "No events scheduled for today or tomorrow.",
            "no_events_next_two_weeks": "No further events scheduled in the next two weeks.",
        }
        today_str, future_str = format_events_for_prompt(events, test_prompts)

        print("\n--- Today & Tomorrow ---")
        print(today_str)
        print("\n--- Next 2 Weeks (Max 10) ---")
        print(future_str)

    import asyncio

    asyncio.run(run_test())
