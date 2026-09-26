"""Event-level trust grading for calendar search results."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_state_into_tracker,
)

if TYPE_CHECKING:
    from family_assistant.storage.calendar_provenance import CalendarProvenanceRecord
    from family_assistant.tools.calendar import CalendarSearchResult
    from family_assistant.tools.types import ToolExecutionContext

PROVENANCE_PROPERTY = "familyAssistantProvenanceId"
CALDAV_PROVENANCE_PROPERTY = "X-FA-PROVENANCE-ID"
logger = logging.getLogger(__name__)


def new_event_marker() -> str:
    """Give a remote event an unguessable link to its local provenance row."""
    return str(uuid.uuid4())


def google_event_marker(item: dict[str, object]) -> str | None:
    """Read the private provenance marker from a Google event, if present."""
    properties = item.get("extendedProperties")
    private = properties.get("private") if isinstance(properties, dict) else None
    marker = private.get(PROVENANCE_PROPERTY) if isinstance(private, dict) else None
    return marker if isinstance(marker, str) and marker else None


def is_household_authored_google_event(item: dict[str, object]) -> bool:
    """Only an event the connected user created or organized is their word."""
    if item.get("eventType") == "fromGmail":
        return False
    return any(
        isinstance(person := item.get(role), dict) and person.get("self") is True
        for role in ("creator", "organizer")
    )


def event_source_key(event: CalendarSearchResult) -> str:
    """Identify the calendar containing an event without using its display name."""
    if event.get("source_kind") == "caldav":
        return str(event.get("calendar_url") or "")
    return str(event.get("source_id") or "")


def _owner_for_source(source_key: str, user_id: str | None) -> str | None:
    """Google calendars are per-user; configured CalDAV calendars are household-wide."""
    return user_id if source_key.startswith("google:") else None


async def record_event_write(
    exec_context: ToolExecutionContext,
    *,
    marker: str,
    source_key: str,
    event_uid: str,
    event_version: str | None,
) -> bool:
    """Stamp a successful remote write; return whether the stamp is durable."""
    if not event_version:
        logger.error(
            "Calendar write returned no event version; provenance was not recorded"
        )
        return False
    state = (
        exec_context.taint_tracker.snapshot()
        if exec_context.taint_tracker is not None
        else TurnTaintState.empty()
    )
    try:
        await exec_context.db_context.calendar_provenance.record(
            marker=marker,
            owner_user_id=_owner_for_source(source_key, exec_context.user_id),
            source_key=source_key,
            event_uid=event_uid,
            event_version=event_version,
            taint_metadata=state.to_metadata(),
        )
    except SQLAlchemyError:
        logger.exception(
            "Calendar event was saved remotely but its provenance was not recorded"
        )
        return False
    return True


async def grade_calendar_events(
    exec_context: ToolExecutionContext,
    events: list[CalendarSearchResult],
) -> None:
    """Merge only provenance introduced by events actually returned to the model."""
    tracker = exec_context.taint_tracker
    if tracker is None:
        return
    markers = [marker for event in events if (marker := event.get("provenance_marker"))]
    records = await exec_context.db_context.calendar_provenance.get_many(markers)
    unmarked_uids = [
        event.get("recurring_event_id") or event["uid"]
        for event in events
        if not event.get("provenance_marker")
    ]
    previously_written = (
        await exec_context.db_context.calendar_provenance.known_event_uids(
            unmarked_uids
        )
    )
    for event in events:
        marker = event.get("provenance_marker")
        record = records.get(marker) if marker else None
        if record is not None and _record_matches(exec_context, event, record):
            merge_taint_state_into_tracker(
                tracker, TurnTaintState.from_metadata(record["taint_metadata"])
            )
        elif (
            marker
            or (
                event_source_key(event),
                event.get("recurring_event_id") or event["uid"],
                _owner_for_source(event_source_key(event), exec_context.user_id),
            )
            in previously_written
            or not event.get("user_vetted")
        ):
            tracker.add_source(
                TaintSource(
                    source_type=TaintSourceType.TOOL_OUTPUT,
                    source_id=f"calendar_event_{event['uid']}",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="Calendar event is external or its assistant-write stamp is missing or changed.",
                )
            )


def _record_matches(
    exec_context: ToolExecutionContext,
    event: CalendarSearchResult,
    record: CalendarProvenanceRecord,
) -> bool:
    """A stamp applies only to the same user, source, event, and version."""
    return (
        record["owner_user_id"]
        == _owner_for_source(event_source_key(event), exec_context.user_id)
        and record["source_key"] == event_source_key(event)
        and record["event_uid"] == event["uid"]
        and record["event_version"] == event.get("event_version")
    )
