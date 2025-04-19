import asyncio # Import asyncio for run_in_executor
import logging
import os
from datetime import datetime, date, timedelta, time # Added time
# Consolidated imports including Any
from typing import List, Dict, Optional, Tuple, Any
import vobject
import caldav
from caldav.lib.error import DAVError, NotFoundError

logger = logging.getLogger(__name__)

# --- Configuration (loaded from main.py environment) ---
# Base connection details
CALDAV_URL = os.getenv("CALDAV_URL")
CALDAV_USERNAME = os.getenv("CALDAV_USERNAME")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD")
# Specific calendar URLs (preferred method)
CALDAV_CALENDAR_URLS_STR = os.getenv("CALDAV_CALENDAR_URLS")
CALDAV_CALENDAR_URLS = [url.strip() for url in CALDAV_CALENDAR_URLS_STR.split(',')] if CALDAV_CALENDAR_URLS_STR else []
# Fallback: Calendar names (less reliable, kept for potential backward compatibility)
CALDAV_CALENDAR_NAMES_STR = os.getenv("CALDAV_CALENDAR_NAMES")
CALDAV_CALENDAR_NAMES = [name.strip() for name in CALDAV_CALENDAR_NAMES_STR.split(',')] if CALDAV_CALENDAR_NAMES_STR else []

# --- Helper Functions ---

def format_datetime_or_date(dt_obj: datetime | date, is_end: bool = False) -> str:
    """Formats datetime or date object into a user-friendly string."""
    if isinstance(dt_obj, datetime):
        # Format datetime, potentially adjust for end time display
        # Example: "Today 14:30", "Tomorrow 09:00", "Apr 21 10:00"
        now = datetime.now(dt_obj.tzinfo) # Use event's timezone if available
        today = now.date()
        tomorrow = today + timedelta(days=1)

        if dt_obj.date() == today:
            day_str = "Today"
        elif dt_obj.date() == tomorrow:
            day_str = "Tomorrow"
        else:
            day_str = dt_obj.strftime("%b %d") # e.g., Apr 21

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
            return dt_obj.strftime("%b %d") # e.g., Apr 21
    else:
        return str(dt_obj) # Fallback

def parse_event(event_data: str) -> Optional[Dict[str, Any]]:
    """Parses VCALENDAR data into a dictionary."""
    try:
        cal = vobject.readComponents(event_data)
        # Assuming the first component is the VEVENT
        vevent = next(cal).vevent
        summary = vevent.summary.value if hasattr(vevent, 'summary') else "No Title"
        dtstart = vevent.dtstart.value if hasattr(vevent, 'dtstart') else None
        dtend = vevent.dtend.value if hasattr(vevent, 'dtend') else None

        # Basic check for valid event data
        if not summary or not dtstart:
            return None

        is_all_day = not isinstance(dtstart, datetime)

        # If dtend is missing, assume duration based on type (e.g., 1 hour for timed, 1 day for all-day)
        if dtend is None:
            if is_all_day:
                dtend = dtstart + timedelta(days=1)
            else:
                dtend = dtstart + timedelta(hours=1) # Default duration assumption

        return {
            "summary": summary,
            "start": dtstart,
            "end": dtend,
            "all_day": is_all_day,
        }
    except Exception as e:
        logger.error(f"Failed to parse VCALENDAR data: {e}\nData: {event_data[:200]}...", exc_info=True)
        return None

# --- Core Synchronous Function (to be run in executor) ---

def _fetch_events_sync(
    # base_url is no longer needed if using direct URLs
    username: str,
    password: str,
    calendar_urls: List[str],
    # calendar_names: List[str] # Removed as we prioritize URLs
) -> List[Dict[str, Any]]:
    """Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."""
    logger.debug("Executing synchronous CalDAV fetch using direct calendar URLs.")
    all_events = []

    if not calendar_urls:
        logger.error("No calendar URLs provided to _fetch_events_sync.")
        return []

    # Define date range once
    start_date = date.today()
    end_date = start_date + timedelta(days=16) # Search up to 16 days out

    # Iterate through each specific calendar URL
    for calendar_url in calendar_urls:
        logger.info(f"Attempting to connect and fetch from calendar: {calendar_url}")
        try:
            # Create a *new* client instance for *each* calendar URL
            # This avoids the URL joining issue by using the specific URL for initialization.
            with caldav.DAVClient(
                url=calendar_url, # Use the specific calendar URL here
                username=username,
                password=password,
            ) as client:
                # The client is now connected directly to the calendar URL's host.
                # Get the calendar object associated with this specific URL.
                # The `calendar()` method on the client, when given the *same URL* the client
                # was initialized with, should return the calendar object itself.
                target_calendar = client.calendar(url=calendar_url)

                if not target_calendar:
                    logger.error(f"Failed to obtain calendar object for URL: {calendar_url}")
                    continue # Skip to the next URL

                logger.info(f"Searching for events between {start_date} and {end_date} in calendar {calendar_url}")
                # Use search method (synchronous) on the identified calendar object
                results = target_calendar.search(start=start_date, end=end_date, event=True, expand=False)
                logger.debug(f"Found {len(results)} potential events in calendar {calendar_url}")

                # Process fetched events
                for event in results:
                    try:
                        event_url_attr = getattr(event, 'url', 'N/A')
                        event_data = event.data # Access data synchronously
                        parsed = parse_event(event_data)
                        if parsed:
                            all_events.append(parsed)
                        else:
                            logger.warning(f"Failed to parse event data for event {event_url_attr} in {calendar_url}. Skipping.")
                    except (DAVError, NotFoundError, Exception) as event_err:
                         logger.error(f"Error processing individual event {getattr(event, 'url', 'N/A')} in {calendar_url}: {event_err}", exc_info=True)

        except DAVError as e:
            logger.error(f"CalDAV connection or authentication error for URL {calendar_url}: {e}", exc_info=True)
            # Continue to the next URL if one fails
        except Exception as e:
            logger.error(f"Unexpected error during CalDAV fetch for URL {calendar_url}: {e}", exc_info=True)
            # Continue to the next URL

    # Sort events by start time
    try:
        all_events.sort(key=lambda x: x["start"])
    except TypeError as sort_err:
        logger.error(f"Error sorting events, possibly due to mixed date/datetime types without tzinfo: {sort_err}")

    logger.info(f"Synchronously fetched and parsed {len(all_events)} events.")
    return all_events


# --- Async Wrapper Function ---

async def fetch_upcoming_events() -> List[Dict[str, Any]]:
    """Async wrapper to run the synchronous CalDAV fetch in an executor."""
    logger.debug("Entering async fetch_upcoming_events wrapper.")
    # Check if base connection details are present
    if not all([CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD]):
        logger.warning("Core CalDAV connection details (URL, USERNAME, PASSWORD) missing. Skipping calendar fetch.")
        return []
    # Check if *either* URLs or Names are provided
    if not CALDAV_CALENDAR_URLS and not CALDAV_CALENDAR_NAMES:
        logger.warning("Neither CALDAV_CALENDAR_URLS nor CALDAV_CALENDAR_NAMES are configured. Skipping calendar fetch.")
    # Check if *either* URLs or Names are provided
    if not CALDAV_CALENDAR_URLS and not CALDAV_CALENDAR_NAMES:
        logger.warning("Neither CALDAV_CALENDAR_URLS nor CALDAV_CALENDAR_NAMES are configured. Skipping calendar fetch.")
        return []

    loop = asyncio.get_running_loop()
    try:
        # Run the synchronous blocking code in the default executor
        logger.debug("Scheduling synchronous CalDAV fetch in executor.")
        all_events = await loop.run_in_executor(
            None, # Use default executor (ThreadPoolExecutor)
            _fetch_events_sync, # The function to run
            # Arguments for _fetch_events_sync (base_url and names removed):
            CALDAV_USERNAME,
            CALDAV_PASSWORD,
            CALDAV_CALENDAR_URLS,
            # CALDAV_CALENDAR_NAMES # Removed
        )
        logger.debug(f"Executor task finished, returned {len(all_events)} events.")
        return all_events
    except Exception as e:
        # Catch errors during scheduling or execution in executor
        logger.error(f"Error running CalDAV fetch in executor: {e}", exc_info=True)
        return []

# --- Formatting for Prompt ---

def format_events_for_prompt(events: List[Dict[str, Any]], prompts: Dict[str, str]) -> Tuple[str, str]:
    """Formats the fetched events into strings suitable for the prompt."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    two_weeks_later = today + timedelta(days=15) # End of the 14-day window after tomorrow

    today_tomorrow_events = []
    next_two_weeks_events = []

    event_fmt = prompts.get("event_item_format", "- {start_time} to {end_time}: {summary}")
    all_day_fmt = prompts.get("all_day_event_item_format", "- {start_time} (All Day): {summary}")

    for event in events:
        start_dt = event["start"]
        start_date_only = start_dt.date() if isinstance(start_dt, datetime) else start_dt

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

    today_tomorrow_str = "\n".join(today_tomorrow_events) if today_tomorrow_events else prompts.get("no_events_today_tomorrow", "None")
    next_two_weeks_str = "\n".join(limited_next_two_weeks) if limited_next_two_weeks else prompts.get("no_events_next_two_weeks", "None")

    return today_tomorrow_str, next_two_weeks_str

if __name__ == "__main__":
    # Example usage for testing
    logging.basicConfig(level=logging.INFO)
    # Load .env for testing standalone
    from dotenv import load_dotenv
    load_dotenv()
    # Re-assign config vars after loading .env
    # Re-assign config vars after loading .env for testing
    CALDAV_URL = os.getenv("CALDAV_URL")
    CALDAV_USERNAME = os.getenv("CALDAV_USERNAME")
    CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD")
    CALDAV_CALENDAR_URLS_STR = os.getenv("CALDAV_CALENDAR_URLS")
    CALDAV_CALENDAR_URLS = [url.strip() for url in CALDAV_CALENDAR_URLS_STR.split(',')] if CALDAV_CALENDAR_URLS_STR else []
    CALDAV_CALENDAR_NAMES_STR = os.getenv("CALDAV_CALENDAR_NAMES") # Keep for potential testing
    CALDAV_CALENDAR_NAMES = [name.strip() for name in CALDAV_CALENDAR_NAMES_STR.split(',')] if CALDAV_CALENDAR_NAMES_STR else []


    async def run_test():
        print("Fetching events (async wrapper)...")
        events = await fetch_upcoming_events()
        print(f"\nFetched {len(events)} events.")

        # Load dummy prompts for formatting test
        test_prompts = {
            "event_item_format": "- {start_time} to {end_time}: {summary}",
            "all_day_event_item_format": "- {start_time} (All Day): {summary}",
            "no_events_today_tomorrow": "No events scheduled for today or tomorrow.",
            "no_events_next_two_weeks": "No further events scheduled in the next two weeks."
        }
        today_str, future_str = format_events_for_prompt(events, test_prompts)

        print("\n--- Today & Tomorrow ---")
        print(today_str)
        print("\n--- Next 2 Weeks (Max 10) ---")
        print(future_str)

    import asyncio
    asyncio.run(run_test())
