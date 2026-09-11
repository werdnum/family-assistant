import asyncio  # Import asyncio for run_in_executor
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta  # Added time
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo  # Import ZoneInfo

import caldav
import httpx  # Import httpx
import recurring_ical_events
import vobject
from caldav.lib.error import (  # Reverted to original-like import path
    DAVError,
    NotFoundError,
)
from icalendar import Calendar as ICalendar

from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig, CalendarEvent

logger = logging.getLogger(__name__)


# --- Configuration (Now passed via function arguments) ---
# Environment variables are still read here for the standalone test section (__main__)

# --- Calendar Sources ---


@dataclass(frozen=True)
class CalendarSource:
    """Represents a resolved calendar source (CalDAV collection or iCal feed)."""

    source_id: str
    name: str
    kind: Literal["caldav", "ical"]
    url: str
    writable: bool
    is_default: bool = False


def _derive_slug_from_url(url: str) -> str | None:
    """Extract a sanitized identifier slug from the terminal segment of a calendar URL."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return None
    path = parsed.path.rstrip("/")
    if not path:
        return None
    last_segment = path.split("/")[-1]
    if last_segment.lower().endswith(".ics"):
        last_segment = last_segment[:-4]
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", last_segment).strip("_")
    return cleaned.lower() if cleaned else None


def _format_slug_as_name(slug: str) -> str:
    """Convert an identifier slug into a title-cased display name."""
    words = slug.replace("-", "_").split("_")
    capitalized_words: list[str] = []
    for word in words:
        if not word:
            continue
        # Preserve acronyms like NSW, UK, US if uppercase in slug, else capitalize
        if word.isupper() and len(word) <= 4:
            capitalized_words.append(word)
        else:
            capitalized_words.append(word.capitalize())
    return " ".join(capitalized_words) if capitalized_words else "Calendar"


def resolve_calendar_sources(
    calendar_config: "CalendarConfig | None",
) -> list[CalendarSource]:
    """Resolve configured CalDAV collections and iCal feeds into CalendarSource instances.

    Generates stable, unique source_id slugs and friendly names if not explicitly configured.
    Identifies the default writable CalDAV calendar collection for new events.
    """
    if not calendar_config:
        return []

    sources: list[CalendarSource] = []
    seen_ids: set[str] = set()

    def _unique_id(base_id: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_-]", "_", base_id).strip("_").lower()
        if not clean:
            clean = "calendar"
        candidate = clean
        counter = 2
        while candidate in seen_ids:
            candidate = f"{clean}_{counter}"
            counter += 1
        seen_ids.add(candidate)
        return candidate

    # 1. CalDAV sources
    caldav_config = calendar_config.get("caldav")
    if caldav_config:
        calendar_urls = caldav_config.get("calendar_urls", [])
        for idx, entry in enumerate(calendar_urls):
            entry_url: str
            explicit_id: str | None = None
            explicit_name: str | None = None
            if isinstance(entry, dict):
                entry_url = entry.get("url", "")
                explicit_id = entry.get("id")
                explicit_name = entry.get("name")
            else:
                entry_url = str(entry)

            if not entry_url:
                continue

            if explicit_id:
                source_id = _unique_id(explicit_id)
            else:
                slug = _derive_slug_from_url(entry_url) or f"caldav_{idx + 1}"
                source_id = _unique_id(slug)

            source_name = explicit_name or _format_slug_as_name(source_id)
            is_default = len(sources) == 0  # First CalDAV source is default

            sources.append(
                CalendarSource(
                    source_id=source_id,
                    name=source_name,
                    kind="caldav",
                    url=entry_url,
                    writable=True,
                    is_default=is_default,
                )
            )

    # 2. iCal sources
    ical_config = calendar_config.get("ical")
    if ical_config:
        urls = ical_config.get("urls", [])
        for idx, entry in enumerate(urls):
            ical_url: str
            ical_explicit_id: str | None = None
            ical_explicit_name: str | None = None
            if isinstance(entry, dict):
                ical_url = entry.get("url", "")
                ical_explicit_id = entry.get("id")
                ical_explicit_name = entry.get("name")
            else:
                ical_url = str(entry)

            if not ical_url:
                continue

            if ical_explicit_id:
                source_id = _unique_id(ical_explicit_id)
            else:
                slug = _derive_slug_from_url(ical_url) or f"ical_{idx + 1}"
                source_id = _unique_id(slug)

            source_name = ical_explicit_name or _format_slug_as_name(source_id)

            sources.append(
                CalendarSource(
                    source_id=source_id,
                    name=source_name,
                    kind="ical",
                    url=ical_url,
                    writable=False,
                    is_default=False,
                )
            )

    return sources


# --- Helper Functions ---


def format_datetime_or_date(
    dt_obj: datetime | date,
    timezone: ZoneInfo,
    is_end: bool = False,
    clock: Clock | None = None,
) -> str:
    """Formats datetime or date object into a user-friendly string, relative to the specified timezone."""
    if clock is None:
        clock = SystemClock()
    local_tz = timezone

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

            date_str = display_date.strftime("%b %d")
            if display_date == today_local:
                return f"Today ({date_str})"
            if display_date == tomorrow_local:
                return f"Tomorrow ({date_str})"
            return date_str

        # Convert event time to local timezone for comparison and display
        dt_local = dt_obj.astimezone(local_tz)
        # Example: "Today (Apr 21) 14:30", "Tomorrow (Apr 22) 09:00", "Apr 23 10:00"

        date_str = dt_local.strftime("%b %d")
        if dt_local.date() == today_local:
            day_str = f"Today ({date_str})"
        elif dt_local.date() == tomorrow_local:
            day_str = f"Tomorrow ({date_str})"
        else:
            day_str = date_str  # e.g., Apr 21

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

        date_str = display_date.strftime("%b %d")
        if display_date == today_local:
            return f"Today ({date_str})"
        if display_date == tomorrow_local:
            return f"Tomorrow ({date_str})"
        return date_str  # e.g., Apr 21
    # Fallback for other types, though dt_obj should only be datetime or date
    # Based on type hints, this path should not be reached if input is correct.
    # However, to satisfy linters about all paths returning, and for robustness:
    return str(dt_obj)


def parse_event(
    event_data: str,
    timezone: ZoneInfo | None = None,
) -> "CalendarEvent | None":
    """
    Parses VCALENDAR data into a dictionary, including the UID.
    If timezone is provided, naive datetimes will be localized to that timezone.
    """
    try:
        return _parse_event(event_data, timezone)
    except StopIteration:
        logger.error(
            f"Failed to find VEVENT component in VCALENDAR data: {event_data[:200]}..."
        )
        return None
    except Exception as e:
        logger.exception(
            f"Failed to parse VCALENDAR data: {e}\nData: {event_data[:200]}..."
        )
        return None


def _parse_event(
    event_data: str,
    timezone: ZoneInfo | None,
) -> "CalendarEvent | None":
    components = vobject.readComponents(event_data)
    ical_component = next(components)
    vevent = (
        ical_component
        if getattr(ical_component, "name", "").upper() == "VEVENT"
        else ical_component.vevent
    )
    summary = vevent.summary.value if hasattr(vevent, "summary") else "No Title"  # type: ignore[union-attr]
    dtstart = vevent.dtstart.value if hasattr(vevent, "dtstart") else None  # type: ignore[union-attr]
    dtend = vevent.dtend.value if hasattr(vevent, "dtend") else None  # type: ignore[union-attr]
    uid = vevent.uid.value if hasattr(vevent, "uid") else None  # type: ignore[union-attr]
    if not summary or not dtstart or not uid:
        logger.warning(
            f"Parsed event missing essential fields (summary, dtstart, or uid). Summary='{summary}', Start='{dtstart}', UID='{uid}'"
        )
        return None

    is_all_day = not isinstance(dtstart, datetime)
    if timezone:
        if isinstance(dtstart, datetime):
            if dtstart.tzinfo is None:
                dtstart = dtstart.replace(tzinfo=timezone)
                logger.debug(f"Applied local timezone {timezone} to naive dtstart")
            else:
                dtstart = dtstart.astimezone(timezone)
                logger.debug(f"Converted aware dtstart to target timezone {timezone}")
        if isinstance(dtend, datetime):
            if dtend.tzinfo is None:
                dtend = dtend.replace(tzinfo=timezone)
                logger.debug(f"Applied local timezone {timezone} to naive dtend")
            else:
                dtend = dtend.astimezone(timezone)
                logger.debug(f"Converted aware dtend to target timezone {timezone}")
    if dtend is None:
        dtend = dtstart + timedelta(
            days=1 if is_all_day else 0,
            hours=0 if is_all_day else 1,
        )
    return cast(
        "CalendarEvent",
        {
            "uid": uid,
            "summary": summary,
            "start": dtstart,
            "end": dtend,
            "all_day": is_all_day,
            "calendar_url": None,
            "similarity": None,
        },
    )


def _parse_icalendar_event_component(
    event_component: object,
    timezone: ZoneInfo,
) -> "CalendarEvent | None":
    raw_event = cast("Any", event_component)
    summary_prop = raw_event.get("SUMMARY")
    summary = str(summary_prop) if summary_prop is not None else "No Title"
    uid_prop = raw_event.get("UID")
    uid = str(uid_prop) if uid_prop is not None else None

    if uid is None:
        logger.warning("Skipping iCal event without UID.")
        return None

    dtstart = raw_event.decoded("DTSTART", None)
    dtend = raw_event.decoded("DTEND", None)

    if dtstart is None:
        logger.warning("Skipping iCal event without DTSTART. UID='%s'", uid)
        return None

    is_all_day = not isinstance(dtstart, datetime)

    if isinstance(dtstart, datetime):
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=timezone)
        else:
            dtstart = dtstart.astimezone(timezone)

    if isinstance(dtend, datetime):
        if dtend.tzinfo is None:
            dtend = dtend.replace(tzinfo=timezone)
        else:
            dtend = dtend.astimezone(timezone)

    if dtend is None:
        if is_all_day:
            dtend = dtstart + timedelta(days=1)
        else:
            dtend = dtstart + timedelta(hours=1)

    return cast(
        "CalendarEvent",
        {
            "uid": uid,
            "summary": summary,
            "start": dtstart,
            "end": dtend,
            "all_day": is_all_day,
            "calendar_url": None,
            "similarity": None,
        },
    )


# --- Core Fetching Functions ---


async def _fetch_ical_events_async(
    ical_sources: Sequence[CalendarSource | str],
    timezone: ZoneInfo,
    clock: Clock | None = None,
) -> list["CalendarEvent"]:
    """Asynchronously fetches and parses events from a list of iCal sources or URLs."""
    if clock is None:
        clock = SystemClock()

    normalized_sources: list[CalendarSource] = []
    for item in ical_sources:
        if isinstance(item, CalendarSource):
            normalized_sources.append(item)
        else:
            url_str = str(item)
            slug = _derive_slug_from_url(url_str) or "ical"
            normalized_sources.append(
                CalendarSource(
                    source_id=slug,
                    name=_format_slug_as_name(slug),
                    kind="ical",
                    url=url_str,
                    writable=False,
                )
            )

    all_events: list[CalendarEvent] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        fetch_tasks: list[asyncio.Task[httpx.Response]] = []
        for src in normalized_sources:
            logger.info(f"Fetching iCal data from: {src.url}")
            fetch_tasks.append(
                asyncio.create_task(client.get(src.url, follow_redirects=True))
            )

        results: list[httpx.Response | BaseException] = await asyncio.gather(
            *fetch_tasks, return_exceptions=True
        )

        for i, result in enumerate(results):
            src = normalized_sources[i]
            if isinstance(result, httpx.Response):
                if result.status_code != 200:
                    logger.error(
                        f"Failed to fetch iCal URL {src.url}: Status {result.status_code}"
                    )
                    continue
                try:
                    all_events.extend(
                        _parse_ical_response(
                            result.text, src.url, timezone, clock, source=src
                        )
                    )
                except Exception as e:
                    logger.exception(f"Error parsing iCal data from {src.url}: {e}")
            elif isinstance(result, Exception):
                logger.error(
                    f"Error fetching iCal URL {src.url}: {result}", exc_info=result
                )
            else:
                logger.error(
                    f"Unexpected type in results for {src.url}: {type(result)}. Skipping."
                )

    logger.info(
        f"Fetched and parsed {len(all_events)} total events from {len(normalized_sources)} iCal source(s)."
    )
    return all_events


def _parse_ical_response(
    ical_data: str,
    url: str,
    timezone: ZoneInfo,
    clock: Clock,
    source: CalendarSource | None = None,
) -> list["CalendarEvent"]:
    logger.debug(
        f"Parsing iCal data from {url} (first 500 chars):\n{ical_data[:500]}..."
    )
    calendar = ICalendar.from_ical(ical_data)

    source_name = source.name if source else "Calendar"
    if source and source.name == _format_slug_as_name(source.source_id):
        raw_calname = calendar.get("X-WR-CALNAME")
        if raw_calname:
            source_name = str(raw_calname).strip()

    start_date = clock.now().astimezone(timezone)
    expanded_events = recurring_ical_events.of(calendar).between(
        start_date,
        start_date + timedelta(days=16),
    )
    parsed_events: list[CalendarEvent] = []
    for event_component in expanded_events:
        try:
            parsed = _parse_icalendar_event_component(
                event_component,
                timezone=timezone,
            )
        except Exception as event_parse_error:
            logger.exception(
                "Failed to parse individual event in iCal URL %s: %s",
                url,
                event_parse_error,
            )
            continue
        if parsed:
            parsed["calendar_url"] = None  # Withhold iCal URL to protect bearer tokens
            if source:
                parsed["source_id"] = source.source_id
                parsed["source_name"] = source_name
                parsed["source_kind"] = "ical"
                parsed["writable"] = False
            parsed_events.append(parsed)
    logger.info(f"Parsed {len(parsed_events)} events from iCal URL: {url}")
    return parsed_events


def _fetch_caldav_events_sync(
    username: str,
    password: str,
    caldav_sources: Sequence[CalendarSource | str],
    timezone: ZoneInfo,
    base_url: str | None = None,
) -> list["CalendarEvent"]:
    """Synchronous function to connect to CalDAV servers using specific calendar URLs and fetch events."""
    logger.debug("Executing synchronous CalDAV fetch using direct calendar URLs.")
    all_events: list[CalendarEvent] = []

    if not caldav_sources:
        logger.error("No calendar sources provided to _fetch_caldav_events_sync.")
        return []

    normalized_sources: list[CalendarSource] = []
    for item in caldav_sources:
        if isinstance(item, CalendarSource):
            normalized_sources.append(item)
        else:
            url_str = str(item)
            slug = _derive_slug_from_url(url_str) or "caldav"
            normalized_sources.append(
                CalendarSource(
                    source_id=slug,
                    name=_format_slug_as_name(slug),
                    kind="caldav",
                    url=url_str,
                    writable=True,
                    is_default=(len(normalized_sources) == 0),
                )
            )

    client_url = base_url
    if not client_url and normalized_sources:
        try:
            parsed_first_cal_url = httpx.URL(normalized_sources[0].url)
            client_url = f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}:{parsed_first_cal_url.port}"
            if parsed_first_cal_url.port is None:
                client_url = (
                    f"{parsed_first_cal_url.scheme}://{parsed_first_cal_url.host}"
                )
            logger.warning(
                f"CalDAV base_url not provided, inferred '{client_url}' from first calendar URL. "
                "It's recommended to configure 'base_url' explicitly in caldav_config."
            )
        except Exception as e:
            logger.error(
                f"Could not infer CalDAV base_url from '{normalized_sources[0].url}': {e}. Cannot proceed with CalDAV fetch."
            )
            return []
    elif not client_url and not normalized_sources:
        logger.error(
            "No CalDAV base_url provided and no calendar sources to infer from."
        )
        return []

    local_tz = timezone
    start_date = datetime.now(local_tz).date()
    end_date = start_date + timedelta(days=16)

    if not client_url:
        logger.error(
            "CalDAV client URL could not be determined. Cannot proceed with CalDAV fetch."
        )
        return []

    try:
        client = caldav.DAVClient(
            url=client_url,
            username=username,
            password=password,
            timeout=30,
        )
    except Exception as e_client:
        logger.error(
            f"Failed to initialize CalDAV client for server URL '{client_url}': {e_client}"
        )
        return []

    for src in normalized_sources:
        logger.info(f"Attempting to fetch from calendar collection: {src.url}")
        try:
            _fetch_caldav_calendar(
                client,
                src.url,
                start_date,
                end_date,
                timezone,
                all_events,
                source=src,
            )
        except NotFoundError:
            logger.error(f"Calendar collection not found at URL {src.url}. Skipping.")
        except DAVError as e:
            logger.exception(f"CalDAV error while processing calendar {src.url}: {e}")
        except Exception as e:
            logger.exception(
                f"Unexpected error during CalDAV fetch for calendar {src.url}: {e}"
            )

    # Sort events by start time
    def get_sort_key_caldav(event: "CalendarEvent") -> datetime:
        """Converts date/datetime to timezone-aware datetime in the local timezone for sorting."""
        start_val = event["start"]
        sort_tz = timezone

        if isinstance(start_val, date) and not isinstance(start_val, datetime):
            # Convert date to datetime at midnight *in the local timezone*
            return datetime.combine(start_val, time.min, tzinfo=sort_tz)
        elif isinstance(start_val, datetime):
            # If it's a datetime, ensure it's timezone-aware and in the correct local timezone
            if start_val.tzinfo is None:
                logger.warning(
                    f"Found naive datetime {start_val} during sorting for event '{event['summary']}'. Applying local timezone {timezone}."
                )
                return start_val.replace(tzinfo=sort_tz)  # Make aware assuming local TZ
            else:
                return start_val.astimezone(sort_tz)  # Convert to local TZ
        # Fallback for unexpected types (shouldn't happen with proper parsing)
        logger.error(
            f"Unexpected type for event start time: {type(start_val)}. Returning epoch."
        )
        return datetime.fromtimestamp(0, tz=sort_tz)

    try:
        all_events.sort(key=get_sort_key_caldav)
    except TypeError as sort_err:
        logger.error(
            f"Error sorting events, possibly due to mixed date/datetime types without tzinfo: {sort_err}"
        )

    logger.info(f"Synchronously fetched and parsed {len(all_events)} events.")
    return all_events


def _fetch_caldav_calendar(
    client: caldav.DAVClient,
    calendar_url: str,
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
    all_events: list["CalendarEvent"],
    source: CalendarSource | None = None,
) -> None:
    target_calendar = client.calendar(url=calendar_url)  # type: ignore[no-untyped-call]
    logger.info(
        f"Searching for events between {start_date} and {end_date} in calendar {target_calendar.url}"
    )
    caldav_results = target_calendar.search(
        start=start_date,
        end=end_date,
        event=True,
        expand=True,
    )
    logger.debug(
        f"Found {len(caldav_results)} potential events in calendar {target_calendar.url}"
    )
    for event_resource in caldav_results:
        try:
            _append_caldav_event(
                event_resource,
                calendar_url,
                timezone,
                all_events,
                source=source,
            )
        except (DAVError, NotFoundError, Exception) as event_error:
            logger.exception(
                f"Error processing individual event {getattr(event_resource, 'url', 'N/A')} in {calendar_url}: {event_error}"
            )


def _append_caldav_event(
    event_resource: object,
    calendar_url: str,
    timezone: ZoneInfo,
    all_events: list["CalendarEvent"],
    source: CalendarSource | None = None,
) -> None:
    raw_resource = cast("Any", event_resource)
    event_url = getattr(raw_resource, "url", "N/A")
    event_data: str = raw_resource.data
    parsed = parse_event(event_data, timezone=timezone)
    if parsed:
        parsed["calendar_url"] = calendar_url
        if source:
            parsed["source_id"] = source.source_id
            parsed["source_name"] = source.name
            parsed["source_kind"] = "caldav"
            parsed["writable"] = True
        all_events.append(parsed)
    else:
        logger.warning(
            f"Failed to parse event data for event {event_url} in {calendar_url}. Skipping."
        )


# --- Main Orchestration Function ---


async def fetch_upcoming_events(
    calendar_config: "CalendarConfig",
    timezone: ZoneInfo,
    clock: Clock | None = None,
) -> list["CalendarEvent"]:
    """Fetches events from configured CalDAV and iCal sources and merges them."""
    logger.debug("Entering fetch_upcoming_events orchestrator.")
    all_events: list[CalendarEvent] = []
    # Allow tasks list to hold both Futures (from run_in_executor) and Tasks
    tasks: list[asyncio.Future[Any] | asyncio.Task[Any]] = []

    resolved_sources = resolve_calendar_sources(calendar_config)
    caldav_sources = [s for s in resolved_sources if s.kind == "caldav"]
    ical_sources = [s for s in resolved_sources if s.kind == "ical"]

    # --- Schedule CalDAV Fetch (if configured) ---
    caldav_config = calendar_config.get("caldav")
    if caldav_config and caldav_sources:
        username = caldav_config.get("username")
        password = caldav_config.get("password")
        base_url = caldav_config.get("base_url")

        if username and password:
            loop = asyncio.get_running_loop()
            logger.debug("Scheduling synchronous CalDAV fetch in executor.")
            caldav_task = loop.run_in_executor(
                None,  # Use default executor
                _fetch_caldav_events_sync,
                username,
                password,
                caldav_sources,
                timezone,
                base_url,
            )
            tasks.append(caldav_task)
        else:
            logger.warning(
                "CalDAV config present (%r) but incomplete (missing username or password). Skipping CalDAV fetch.",
                caldav_config,
            )

    # --- Schedule iCal Fetch (if configured) ---
    if ical_sources:
        logger.debug("Scheduling asynchronous iCal fetch.")
        ical_task = asyncio.create_task(
            _fetch_ical_events_async(ical_sources, timezone, clock=clock)
        )
        tasks.append(ical_task)

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
        local_tz = timezone

        if isinstance(start_val, date) and not isinstance(start_val, datetime):
            # Convert date to datetime at midnight *in the local timezone*
            return datetime.combine(start_val, time.min, tzinfo=local_tz)
        elif isinstance(start_val, datetime):
            # If it's a datetime, ensure it's timezone-aware and in the correct local timezone
            if start_val.tzinfo is None:
                logger.warning(
                    f"Found naive datetime {start_val} during sorting for event '{event['summary']}'. Applying local timezone {timezone}."
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
    timezone: ZoneInfo,
    clock: Clock | None = None,
) -> tuple[str, str]:
    """Formats the fetched events into strings suitable for the prompt."""
    if clock is None:
        clock = SystemClock()
    local_tz = timezone

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
                f"Applying timezone {timezone} to naive start_dt {start_dt} in format_events_for_prompt"
            )
            start_dt = start_dt.replace(tzinfo=local_tz)

        end_dt = end_dt_orig
        if isinstance(end_dt, datetime) and end_dt.tzinfo is None:
            logger.debug(
                f"Applying timezone {timezone} to naive end_dt {end_dt} in format_events_for_prompt"
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
            start_dt, timezone, is_end=False, clock=clock
        )
        end_str = format_datetime_or_date(end_dt, timezone, is_end=True, clock=clock)
        summary = event["summary"]

        fmt = all_day_fmt if event["all_day"] else event_fmt
        source_name = event.get("source_name") or "Calendar"
        source_id = event.get("source_id") or ""
        source_kind = event.get("source_kind") or ""
        event_str = fmt.format(
            start_time=start_str,
            end_time=end_str,
            summary=summary,
            source_name=source_name,
            source_id=source_id,
            source_kind=source_kind,
        )

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
    timezone: ZoneInfo,
) -> "CalendarEvent | None":
    """Fetches calendar event details by UID for use in confirmation prompts.

    Args:
        uid: The UID of the calendar event to fetch
        calendar_url: The full URL of the calendar collection
        calendar_config: Calendar configuration containing CalDAV settings
        timezone: The user's configured timezone. Naive datetimes in the
            iCalendar data (e.g. floating all-day events) are localised to
            this zone so the confirmation prompt shown to the user never
            displays UTC wall-clock times.

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

    def fetch_sync_unchecked() -> "CalendarEvent | None":
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
            event_resource: caldav.objects.Event = target_calendar_obj.event_by_uid(uid)  # type: ignore
            event_data_str: str = event_resource.data  # type: ignore
            parsed_event = parse_event(event_data_str, timezone=timezone)
            if parsed_event:
                logger.info(
                    f"Successfully fetched event details for UID {uid}: {parsed_event.get('summary', 'No Title')}"
                )
                return parsed_event
            logger.warning(f"Failed to parse event data for UID {uid}")
            return None

    # Synchronous fetch function
    def fetch_sync() -> "CalendarEvent | None":
        try:
            return fetch_sync_unchecked()
        except NotFoundError:
            logger.warning(f"Event with UID {uid} not found in calendar {calendar_url}")
            return None
        except (DAVError, ConnectionError, Exception) as e:
            logger.exception(f"Error fetching event details for UID {uid}: {e}")
            return None

    # Execute in thread pool
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, fetch_sync)
        return result
    except Exception as e:
        logger.exception(
            f"Unexpected error in fetch_event_details_for_confirmation: {e}"
        )
        return None


# Removed unused function _fetch_event_details_sync
