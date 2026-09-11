# Calendar Sources and iCal Search Design

**Status:** Proposed  
**Date:** 2026-09-11  
**Author:** Pi (with user guidance)

## Problem Statement

Family Assistant currently exhibits an architectural mismatch between its CalDAV and iCal
integrations:

1. **iCal events are omitted from search entirely**:
   `search_calendar_events_tool` only connects to CalDAV collections. If a deployment only has
   iCal feeds configured (or when querying events that reside in subscribed feeds such as TripIt,
   school schedules, or holiday calendars), the search tool cannot find them. If CalDAV is not
   configured, `search_calendar_events` fails outright with an error.

2. **Duplicate detection misses iCal events**:
   When creating events with `add_calendar_event`, duplicate checking
   (`_check_for_duplicate_events`) only queries CalDAV. Existing events in iCal feeds (e.g. flight
   itineraries, school holidays) are ignored during duplicate checks.

3. **No calendar discovery or listing**:
   The assistant has no tool to discover what calendars exist, what they are named, which are
   writable (CalDAV) vs. read-only (iCal), or which calendar is the default for new events.

4. **No calendar targeting or filtering**:
   - `search_calendar_events` does not support filtering by calendar source.
   - `add_calendar_event` hardcodes targeting the first CalDAV URL, with no way to choose a named
     writable calendar (e.g. "Work" vs. "Family").

5. **No source attribution in prompt context**:
   The `<turn_context>` "Upcoming Events" block merges events into a flat chronological list
   without calendar identifiers or display names. The assistant cannot tell whether an event is
   from a personal calendar, a shared family calendar, or an external feed.

6. **Credential / URL exposure risk**:
   Subscribed iCal URLs often contain private bearer tokens in query strings or paths (e.g.
   TripIt private feeds, secret webcal links). Exposing raw URLs in tool outputs or prompt context
   risks leaking sensitive subscription tokens.

## Architecture and Data Models

### 1. `CalendarSource` Domain Model

We introduce a first-class `CalendarSource` representation:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class CalendarSource:
    source_id: str                          # Machine-friendly slug (e.g. 'family', 'tripit', 'work')
    name: str                               # Human-readable display name (e.g. 'Family', 'TripIt')
    kind: Literal["caldav", "ical"]         # Protocol type
    url: str                                # Internal URL (kept private, never sent to LLM)
    writable: bool                          # True for CalDAV, False for iCal
    is_default: bool = False                # True for the primary writable CalDAV calendar
```

### 2. Configuration Schema (100% Backward Compatible)

In `src/family_assistant/config_models.py`, we enrich `CalDAVConfig` and `ICalConfig` to accept
either legacy bare URL strings or rich configuration entries:

```python
class CalDAVCalendarConfig(BaseModel):
    """Configuration for an individual CalDAV calendar collection."""
    model_config = ConfigDict(extra="forbid")

    url: str
    id: str | None = None
    name: str | None = None

class ICalFeedConfig(BaseModel):
    """Configuration for an individual iCal feed."""
    model_config = ConfigDict(extra="forbid")

    url: str
    id: str | None = None
    name: str | None = None

class CalDAVConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    password: SecretStr | None = None
    calendar_urls: list[str | CalDAVCalendarConfig] = Field(default_factory=list)
    base_url: str | None = None

class ICalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str | ICalFeedConfig] = Field(default_factory=list)
```

Legacy configurations (`calendar_urls: ["https://..."]`, `urls: ["https://..."]`, and env vars
`CALDAV_CALENDAR_URLS`, `ICAL_URLS`) continue to work without modification.

### 3. Source Resolution & Slug/Name Derivation

`resolve_calendar_sources(calendar_config: CalendarConfig) -> list[CalendarSource]`:

- **For CalDAV collections**:
  - If a rich entry specifies `id` and `name`, use them.
  - If bare URL: derive `id` from the URL path slug (e.g. `.../calendars/user/family/` -> `family`)
    or fallback to `caldav_1`, `caldav_2`. Default `name` is slug title-cased.
  - First configured CalDAV source is marked `is_default=True`.
  - All CalDAV sources are `writable=True`.

- **For iCal feeds**:
  - If rich entry specifies `id` and `name`, use them.
  - If bare URL: derive `id` from URL filename/path (e.g. `tripit.ics` -> `tripit`) or fallback to
    `ical_1`, `ical_2`. Default `name` is slug title-cased.
  - At fetch time: if `X-WR-CALNAME` is present in the feed data and no explicit name was
    configured, the calendar name is dynamically enriched.
  - All iCal feeds are `writable=False`.

- **Collision handling**:
  All `source_id` values are normalized and checked for uniqueness. Duplicate IDs receive an
  incremented suffix (`family`, `family_2`).

### 4. Enriched `CalendarEvent`

In `src/family_assistant/tools/types.py`:

```python
class CalendarEvent(TypedDict):
    uid: str
    summary: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    calendar_url: str | None       # Collection URL for CalDAV; None for iCal (never leak token URL)
    similarity: float | None
    source_id: str | None          # e.g. "family", "tripit"
    source_name: str | None        # e.g. "Family", "TripIt"
    source_kind: Literal["caldav", "ical"] | None
    writable: bool | None
```

## Tool Improvements

### 1. New Tool: `list_calendars`

A read-only tool that returns all configured calendar sources:

```
Available calendars:
- family: Family (CalDAV, writable) [default for new events]
- work: Work (CalDAV, writable)
- tripit: TripIt (iCal feed, read-only)
- nsw_schools: NSW School Terms (iCal feed, read-only)
```

- **Tool tags**: `READ_ONLY`, `SENSITIVE_DATA`, `CALENDAR`, `SCHEDULING`, `OUTPUT_TRUSTED`.
- **Secret protection**: Internal URLs are strictly withheld.
- Registered in `defaults.yaml` for default assistant, complex tasks, email assistant, etc.

### 2. Unified `search_calendar_events`

- **New parameter**: `source_ids: list[str] | None = None`
  - Optional list of calendar source IDs to search.
  - Validated against resolved sources; helpful error if an unknown ID is passed.
- **Search execution**:
  - Concurrently queries selected CalDAV collections (server-side date search) and fetches
    selected iCal feeds (expanding recurring events in the range via
    `recurring_ical_events.of(cal).between(start, end)`).
  - Handles single-source deployments (iCal only, CalDAV only, or mixed).
- **Ranking and sorting**:
  - If `search_text` is given: compute semantic/fuzzy similarity scores and rank by similarity.
  - If no `search_text`: sort chronologically by event start time.
- **Output formatting**:
  ```
  Found 2 event(s):

  1. Team Meeting (similarity: 0.85)
     Start: 2026-03-15 10:00 AEDT
     End: 2026-03-15 11:00 AEDT
     UID: evt-123
     Calendar: family (Family) [writable]
     Calendar URL: https://caldav.example.com/...

  2. Flight SYD -> MEL (similarity: 0.72)
     Start: 2026-03-16 08:30 AEDT
     End: 2026-03-16 10:05 AEDT
     UID: tripit-456
     Calendar: tripit (TripIt) [read-only]
  ```
  Note: For CalDAV, `Calendar URL:` is preserved for tool compatibility. For iCal, raw URLs are
  never exposed.

### 3. Duplicate Detection Across All Sources

`_check_for_duplicate_events` is refactored to use the unified multi-source search.
When creating an event in CalDAV, it checks for conflicting or duplicate events across **both
CalDAV and iCal feeds**, warning if an event already exists in a subscribed feed (e.g. TripIt or
school calendar).

### 4. Targetable Event Creation & Read-Only Protection

- `add_calendar_event`:
  - Accepts optional `calendar_id: str | None = None`.
  - If an iCal `calendar_id` is passed, fails immediately with:
    `Error: Calendar '{calendar_id}' is read-only (iCal feed). Events can only be added to writable calendars.`
  - If a valid CalDAV `calendar_id` is passed, targets that collection URL.
  - If omitted, targets the default CalDAV calendar (`calendar_urls[0]`).
- `modify_calendar_event` & `delete_calendar_event`:
  - `calendar_url` parameter accepts either the CalDAV collection URL or a `calendar_id`.
  - If an iCal `calendar_id` or iCal URL is passed, fails with:
    `Error: Calendar '{calendar_id}' is a read-only iCal feed and cannot be modified.`
  - If a CalDAV `calendar_id` is passed, resolves to the collection URL.

### 5. Source Attribution in `<turn_context>`

In `CalendarContextProvider` and `format_events_for_prompt`:
- Fetched events include `source_name` and `source_id`.
- `prompts.yaml` default item format is updated:
  `- {start_time} to {end_time}: {summary} [{source_name}]`
  `- {start_time} (All Day): {summary} [{source_name}]`
- The model now immediately sees the originating calendar for every event in its prompt context.

## Security Considerations

1. **Rule of Two compliance**:
   - `list_calendars` is read-only (`[B]` access to sensitive configuration metadata, no `[C]` external communication or state modification).
   - iCal feeds are strictly read-only; no write tool can be tricked into targeting them.
2. **Credential & Token Protection**:
   - Subscribed iCal URLs often contain private subscription tokens (e.g. TripIt, Google private iCal feeds).
   - iCal URLs are kept strictly internal: they are never sent to the LLM in prompt context, tool schemas, search results, or error messages. Only `source_id` and friendly `name` are exposed.

## Implementation Milestones

- **Milestone 1**: Data models, configuration schemas, and `resolve_calendar_sources` helper with unit tests.
- **Milestone 2**: Calendar integration updates, source tagging on events, dynamic `X-WR-CALNAME` extraction, and prompt context formatting with source attribution.
- **Milestone 3**: `list_calendars` tool implementation, tool registration, and default policies.
- **Milestone 4**: Unified multi-source search in `search_calendar_events` supporting iCal, `source_ids` filtering, and updated duplicate detection.
- **Milestone 5**: Calendar targeting in `add_calendar_event`, read-only enforcement in write tools, and `source_id` resolution.
- **Milestone 6**: Documentation updates (`docs/user/calendar.md`, prompts), linting, and full test suite verification.
