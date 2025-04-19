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

# --- Core Function ---

# ... (other imports and helper functions remain the same) ...

async def fetch_upcoming_events() -> List[Dict[str, Any]]:
    """Connects to CalDAV and fetches upcoming events using direct URLs or name discovery."""
    logger.debug(f"Attempting CalDAV fetch. URL: {CALDAV_URL}, User: {CALDAV_USERNAME}, URLs configured: {bool(CALDAV_CALENDAR_URLS)}, Names configured: {bool(CALDAV_CALENDAR_NAMES)}")
    # Check if base connection details are present
    if not all([CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD]):
        logger.warning("Core CalDAV connection details (URL, USERNAME, PASSWORD) missing. Skipping calendar fetch.")
        return []
    # Check if *either* URLs or Names are provided
    if not CALDAV_CALENDAR_URLS and not CALDAV_CALENDAR_NAMES:
        logger.warning("Neither CALDAV_CALENDAR_URLS nor CALDAV_CALENDAR_NAMES are configured. Skipping calendar fetch.")
        return []

    all_events = []
    target_calendars = []
    try:
        async with caldav.AsyncDAVClient(
            url=CALDAV_URL, # Use the base URL for the client connection
            username=CALDAV_USERNAME,
            password=CALDAV_PASSWORD,
        ) as client:
            # --- Determine target calendars ---
            if CALDAV_CALENDAR_URLS:
                logger.info(f"Using specified calendar URLs: {CALDAV_CALENDAR_URLS}")
                for url in CALDAV_CALENDAR_URLS:
                    try:
                        # Get calendar object directly using its URL
                        calendar = await client.calendar(url=url)
                        target_calendars.append(calendar)
                        logger.info(f"Successfully accessed calendar at URL: {url}")
                    except (DAVError, NotFoundError) as e:
                        logger.error(f"Failed to access calendar at URL '{url}': {e}")
                    except Exception as e:
                         logger.error(f"Unexpected error accessing calendar URL '{url}': {e}", exc_info=True)

            elif CALDAV_CALENDAR_NAMES:
                # Fallback to discovering calendars by name (original logic)
                logger.info(f"Using calendar names for discovery: {CALDAV_CALENDAR_NAMES}")
                principal = await client.principal()
                all_principal_calendars = await principal.calendars()
                target_calendar_names_set = set(CALDAV_CALENDAR_NAMES)

                for cal in all_principal_calendars:
                    try:
                        # Attempt to get display name, fallback to URL parsing
                        cal_name = await cal.get_property("displayname") if hasattr(cal, 'get_property') else str(cal.url).split('/')[-2]
                        if cal_name in target_calendar_names_set:
                            target_calendars.append(cal)
                            logger.info(f"Found target calendar by name: {cal_name} ({cal.url})")
                        else:
                            logger.debug(f"Skipping non-target calendar by name: {cal_name} ({cal.url})")
                    except (DAVError, NotFoundError) as e:
                        logger.warning(f"Could not get display name for calendar {cal.url}: {e}")
                    except Exception as e:
                        logger.error(f"Unexpected error processing calendar {cal.url} during name discovery: {e}", exc_info=True)
            else:
                # This case should be caught by the initial check, but added for safety
                logger.error("No calendar URLs or names provided.")
                return []


            if not target_calendars:
                logger.error("No target calendars could be accessed or found.")
                return []

            # --- Fetch events from target calendars ---
            # Define date range: Today to 16 days from now (covers today, tomorrow, and next 14 days)
            start_date = date.today()
            end_date = start_date + timedelta(days=16) # Search up to 16 days out

            logger.info(f"Searching for events between {start_date} and {end_date} in {len(target_calendars)} calendar(s).")
            logger.debug(f"Target calendar URLs/objects: {[getattr(c, 'url', str(c)) for c in target_calendars]}")

            fetch_tasks = []
            for calendar in target_calendars:
                # Use search method (date_search is deprecated) for fetching events in the range
                # Note: caldav library async support might need specific handling.
                # Assuming search method is awaitable or runs in executor.
                # The `caldav` library's async support might require careful handling.
                # Let's assume `calendar.search` works correctly with async client.
                fetch_tasks.append(calendar.search(start=start_date, end=end_date, event=True, expand=False)) # expand=False might be needed for recurring

            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Error fetching events from a calendar: {result}", exc_info=result)
                    continue # Skip this calendar's results

                # Process fetched events
                for event in result:
                    try:
                        # Event data might be fetched lazily, ensure it's loaded
                        # The structure might differ slightly based on library version/implementation
                        event_url = getattr(event, 'url', 'N/A')
                        logger.debug(f"Fetching data for event: {event_url}")
                        event_data = await event.data() # Assuming data needs await
                        logger.debug(f"Raw event data for {event_url}:\n{event_data[:500]}...") # Log first 500 chars
                        parsed = parse_event(event_data)
                        if parsed:
                            logger.debug(f"Successfully parsed event {event_url}: {parsed}")
                            all_events.append(parsed)
                        else:
                            logger.warning(f"Failed to parse event data for {event_url}. Skipping.")
                    except (DAVError, NotFoundError, Exception) as event_err:
                         logger.error(f"Error processing individual event {event_url}: {event_err}", exc_info=True)


    except DAVError as e:
        logger.error(f"CalDAV connection or authentication error: {e}", exc_info=True)
        return [] # Return empty list on connection failure
    except Exception as e:
        logger.error(f"Unexpected error during CalDAV fetch: {e}", exc_info=True)
        return []

    # Sort events by start time
    try:
        # Ensure start times are comparable (handle potential mix of date/datetime if necessary)
        all_events.sort(key=lambda x: x["start"])
        logger.debug("Sorted events by start time.")
    except TypeError as sort_err:
        logger.error(f"Error sorting events, possibly due to mixed date/datetime types without tzinfo: {sort_err}")
        # Proceed with unsorted events or handle differently if needed

    logger.info(f"Fetched and parsed {len(all_events)} events.")
    logger.debug(f"Final list of parsed events before formatting: {all_events}")
    return all_events

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

    logger.debug(f"Formatted 'Today & Tomorrow' events string:\n{today_tomorrow_str}")
    logger.debug(f"Formatted 'Next 2 Weeks' events string:\n{next_two_weeks_str}")

    return today_tomorrow_str, next_two_weeks_str

if __name__ == "__main__":
    # Example usage for testing
    logging.basicConfig(level=logging.INFO)
    # Load .env for testing standalone
    from dotenv import load_dotenv
    load_dotenv()
    # Re-assign config vars after loading .env
    CALDAV_URL = os.getenv("CALDAV_URL")
    CALDAV_USERNAME = os.getenv("CALDAV_USERNAME")
    CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD")
    CALDAV_CALENDAR_NAMES_STR = os.getenv("CALDAV_CALENDAR_NAMES")
    CALDAV_CALENDAR_NAMES = [name.strip() for name in CALDAV_CALENDAR_NAMES_STR.split(',')] if CALDAV_CALENDAR_NAMES_STR else []

    async def run_test():
        print("Fetching events...")
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
