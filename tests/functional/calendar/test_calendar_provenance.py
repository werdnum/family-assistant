"""Calendar search grades each event by its own authorship and version."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import caldav
import httpx
import pytest

from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
)
from family_assistant.storage.database import Database
from family_assistant.tools.calendar import (
    CalendarSearchResult,
    add_calendar_event_tool,
    search_calendar_events_tool,
)
from family_assistant.tools.calendar_provenance import (
    CALDAV_PROVENANCE_PROPERTY,
    grade_calendar_events,
    record_event_write,
)
from family_assistant.tools.types import CalendarConfig, ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _context(db: Database, tracker: InMemoryTurnTaintTracker) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="calendar-provenance-test",
        user_name="Member",
        user_id="member-1",
        turn_id="calendar-provenance-turn",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        taint_tracker=tracker,
    )


def _event(
    *, marker: str | None, version: str | None, user_vetted: bool
) -> CalendarSearchResult:
    return {
        "summary": "Appointment",
        "uid": "event-1",
        "start": "2026-10-01 10:00 UTC",
        "end": "2026-10-01 11:00 UTC",
        "calendar_url": None,
        "source_id": "google:primary",
        "source_kind": "google",
        "provenance_marker": marker,
        "event_version": version,
        "user_vetted": user_vetted,
    }


@pytest.mark.asyncio
async def test_assistant_event_inherits_its_write_turn_and_changed_versions_fail_closed(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    writer_tracker = InMemoryTurnTaintTracker()
    writer_tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="hotel-listing",
            tier=SourceTrustTier.RECOGNIZED_MACHINE,
            labels=frozenset(),
            reason="Structured hotel search result.",
        )
    )
    await record_event_write(
        _context(db, writer_tracker),
        marker="marker-1",
        source_key="google:primary",
        event_uid="event-1",
        event_version="v1",
    )

    read_tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(db, read_tracker),
        [_event(marker="marker-1", version="v1", user_vetted=True)],
    )
    assert read_tracker.snapshot().max_tier is SourceTrustTier.RECOGNIZED_MACHINE

    changed_tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(db, changed_tracker),
        [_event(marker="marker-1", version="v2", user_vetted=True)],
    )
    assert changed_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL

    missing_tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(db, missing_tracker),
        [_event(marker="missing", version="v1", user_vetted=True)],
    )
    assert missing_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL

    stripped_tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(db, stripped_tracker),
        [_event(marker=None, version="v2", user_vetted=True)],
    )
    assert stripped_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_manual_own_event_is_trusted_but_external_event_is_not(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(db, tracker),
        [_event(marker=None, version="v1", user_vetted=True)],
    )
    assert tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER

    await grade_calendar_events(
        _context(db, tracker),
        [_event(marker=None, version="v1", user_vetted=False)],
    )
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_caldav_write_search_and_out_of_band_change(
    db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    base_url, username, password, calendar_url = radicale_server
    db = Database(db_engine)
    config: CalendarConfig = {
        "caldav": {
            "base_url": base_url,
            "username": username,
            "password": password,
            "calendar_urls": [calendar_url],
        }
    }
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    write_context = _context(db, InMemoryTurnTaintTracker())
    result = await add_calendar_event_tool(
        write_context,
        config,
        summary="Dental checkup",
        start_time=tomorrow.isoformat(),
        end_time=(tomorrow + timedelta(hours=1)).isoformat(),
        bypass_duplicate_check=True,
    )
    assert result.startswith("OK.")

    with caldav.DAVClient(url=base_url, username=username, password=password) as client:
        stored = client.calendar(url=calendar_url).events()[0]
        marker = str(stored.icalendar_component.get(CALDAV_PROVENANCE_PROPERTY))
        record = (await db.calendar_provenance.get_many([marker]))[marker]
        assert (
            record["event_version"]
            == hashlib.sha256(stored.icalendar_component.to_ical()).hexdigest()
        )

    read_tracker = InMemoryTurnTaintTracker()
    result = await search_calendar_events_tool(
        _context(db, read_tracker), config, start_date=tomorrow.date().isoformat()
    )
    assert "Dental checkup" in result
    assert read_tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER

    with caldav.DAVClient(url=base_url, username=username, password=password) as client:
        event = client.calendar(url=calendar_url).events()[0]
        event.data = event.data.replace("Dental checkup", "Injected instruction")
        event.save()

    changed_tracker = InMemoryTurnTaintTracker()
    result = await search_calendar_events_tool(
        _context(db, changed_tracker), config, start_date=tomorrow.date().isoformat()
    )
    assert "Injected instruction" in result
    assert changed_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL

    with caldav.DAVClient(url=base_url, username=username, password=password) as client:
        event = client.calendar(url=calendar_url).events()[0]
        event.data = re.sub(r"(?m)^X-FA-PROVENANCE-ID:[^\r\n]*\r?\n", "", event.data)
        event.save()

    stripped_tracker = InMemoryTurnTaintTracker()
    await search_calendar_events_tool(
        _context(db, stripped_tracker),
        config,
        start_date=tomorrow.date().isoformat(),
    )
    assert stripped_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_mixed_household_and_subscribed_calendar_grades_returned_events(
    db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, username, password, calendar_url = radicale_server
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    feed = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:school-1",
        "SUMMARY:School notice",
        f"DTSTART:{tomorrow.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{(tomorrow + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        del self, url, kwargs
        return httpx.Response(200, text=feed)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    config: CalendarConfig = {
        "caldav": {
            "base_url": base_url,
            "username": username,
            "password": password,
            "calendar_urls": [{"id": "family", "url": calendar_url}],
        },
        "ical": {"urls": [{"id": "school", "url": "https://school.test/events.ics"}]},
    }
    db = Database(db_engine)
    with caldav.DAVClient(url=base_url, username=username, password=password) as client:
        client.calendar(url=calendar_url).save_event(
            "\r\n".join([
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "BEGIN:VEVENT",
                "UID:family-1",
                "SUMMARY:Family dinner",
                f"DTSTART:{tomorrow.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{(tomorrow + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}",
                "END:VEVENT",
                "END:VCALENDAR",
                "",
            ])
        )

    household_tracker = InMemoryTurnTaintTracker()
    household_result = await search_calendar_events_tool(
        _context(db, household_tracker),
        config,
        start_date=tomorrow.date().isoformat(),
        source_ids=["family"],
    )
    assert "Family dinner" in household_result
    assert household_tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER

    mixed_tracker = InMemoryTurnTaintTracker()
    mixed_result = await search_calendar_events_tool(
        _context(db, mixed_tracker), config, start_date=tomorrow.date().isoformat()
    )
    assert "Family dinner" in mixed_result
    assert "School notice" in mixed_result
    assert mixed_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
