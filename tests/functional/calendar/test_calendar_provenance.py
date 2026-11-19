"""Calendar search grades each event by its own authorship and version."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import pytest
from caldav.davclient import DAVClient

from family_assistant.caldav_sync import sync_result
from family_assistant.config_models import (
    AppConfig,
    EmailIntakeIdentityConfig,
    OIDCUserIdentityConfig,
    UserIdentityConfig,
)
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
    google_event_participants,
    grade_calendar_events,
    record_event_write,
)
from family_assistant.tools.types import CalendarConfig, ToolExecutionContext
from tests.mocks.idle_processing_service import make_idle_processing_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_HOUSEHOLD_CONFIG = AppConfig(
    users=[
        UserIdentityConfig(
            id="member-1",
            oidc=OIDCUserIdentityConfig(emails={"member@family.test"}),
        ),
        UserIdentityConfig(
            id="member-2",
            email_intake=EmailIntakeIdentityConfig(
                sender_addresses={"partner@family.test"}
            ),
        ),
    ]
)


def _context(
    db: Database,
    tracker: InMemoryTurnTaintTracker,
    *,
    app_config: AppConfig | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="calendar-provenance-test",
        user_name="Member",
        user_id="member-1",
        turn_id="calendar-provenance-turn",
        db_context=db,
        processing_service=(
            make_idle_processing_service(app_config=app_config)
            if app_config is not None
            else None
        ),
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        taint_tracker=tracker,
    )


def _event(
    *,
    marker: str | None,
    version: str | None,
    user_vetted: bool,
    household_participants: list[str] | None = None,
) -> CalendarSearchResult:
    return {
        "household_participants": household_participants,
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

    with DAVClient(url=base_url, username=username, password=password) as client:
        stored = sync_result(client.calendar(url=calendar_url).get_events())[0]
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

    with DAVClient(url=base_url, username=username, password=password) as client:
        event = sync_result(client.calendar(url=calendar_url).get_events())[0]
        event.data = event.data.replace("Dental checkup", "Injected instruction")
        event.save()

    changed_tracker = InMemoryTurnTaintTracker()
    result = await search_calendar_events_tool(
        _context(db, changed_tracker), config, start_date=tomorrow.date().isoformat()
    )
    assert "Injected instruction" in result
    assert changed_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL

    with DAVClient(url=base_url, username=username, password=password) as client:
        event = sync_result(client.calendar(url=calendar_url).get_events())[0]
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
    with DAVClient(url=base_url, username=username, password=password) as client:
        client.calendar(url=calendar_url).add_event(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("participants", "expected"),
    [
        (["member@family.test", "partner@family.test"], SourceTrustTier.TRUSTED_USER),
        (["partner@family.test"], SourceTrustTier.TRUSTED_USER),
        (
            ["outsider@else.test", "member@family.test"],
            SourceTrustTier.UNKNOWN_EXTERNAL,
        ),
        (
            ["member@family.test", "outsider@else.test"],
            SourceTrustTier.UNKNOWN_EXTERNAL,
        ),
        ([], SourceTrustTier.UNKNOWN_EXTERNAL),
        (None, SourceTrustTier.UNKNOWN_EXTERNAL),
    ],
)
async def test_event_with_only_household_participants_is_trusted(
    db_engine: AsyncEngine,
    participants: list[str] | None,
    expected: SourceTrustTier,
) -> None:
    tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(Database(db_engine), tracker, app_config=_HOUSEHOLD_CONFIG),
        [
            _event(
                marker=None,
                version="v1",
                user_vetted=False,
                household_participants=participants,
            )
        ],
    )
    assert tracker.snapshot().max_tier is expected


@pytest.mark.asyncio
async def test_household_participants_do_not_override_a_changed_or_missing_stamp(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await record_event_write(
        _context(db, InMemoryTurnTaintTracker()),
        marker="marker-1",
        source_key="google:primary",
        event_uid="event-1",
        event_version="v1",
    )
    household = ["partner@family.test"]
    for marker, version in (("marker-1", "v2"), ("missing", "v1"), (None, "v2")):
        tracker = InMemoryTurnTaintTracker()
        await grade_calendar_events(
            _context(db, tracker, app_config=_HOUSEHOLD_CONFIG),
            [
                _event(
                    marker=marker,
                    version=version,
                    user_vetted=False,
                    household_participants=household,
                )
            ],
        )
        assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_household_participants_need_configured_household(
    db_engine: AsyncEngine,
) -> None:
    tracker = InMemoryTurnTaintTracker()
    await grade_calendar_events(
        _context(Database(db_engine), tracker),
        [
            _event(
                marker=None,
                version="v1",
                user_vetted=False,
                household_participants=["partner@family.test"],
            )
        ],
    )
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


def test_google_event_participants() -> None:
    organized_by_partner: dict[str, object] = {
        "organizer": {"email": "Partner@Family.test"},
        "attendees": [
            {"email": "me@gmail.test", "self": True},
            {"email": "partner@family.test", "organizer": True},
        ],
    }
    assert google_event_participants(organized_by_partner) == [
        "partner@family.test",
        "partner@family.test",
    ]
    assert (
        google_event_participants({**organized_by_partner, "eventType": "fromGmail"})
        is None
    )
    assert (
        google_event_participants({
            "organizer": {"email": "partner@family.test"},
            "attendees": [{"displayName": "No address"}],
        })
        is None
    )


@pytest.mark.asyncio
async def test_caldav_event_grades_by_organizer_and_attendees(
    db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    base_url, username, password, calendar_url = radicale_server
    config: CalendarConfig = {
        "caldav": {
            "base_url": base_url,
            "username": username,
            "password": password,
            "calendar_urls": [calendar_url],
        }
    }
    db = Database(db_engine)
    start = datetime.now(UTC) + timedelta(days=1)

    def add_event(uid: str, organizer: str, attendees: list[str]) -> None:
        with DAVClient(url=base_url, username=username, password=password) as client:
            client.calendar(url=calendar_url).add_event(
                "\r\n".join([
                    "BEGIN:VCALENDAR",
                    "VERSION:2.0",
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"SUMMARY:{uid}",
                    f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
                    f"DTEND:{(start + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}",
                    f"ORGANIZER:mailto:{organizer}",
                    *(f"ATTENDEE:mailto:{attendee}" for attendee in attendees),
                    "END:VEVENT",
                    "END:VCALENDAR",
                    "",
                ])
            )

    add_event("family-dinner", "member@family.test", ["PARTNER@family.test"])
    household_tracker = InMemoryTurnTaintTracker()
    result = await search_calendar_events_tool(
        _context(db, household_tracker, app_config=_HOUSEHOLD_CONFIG),
        config,
        start_date=start.date().isoformat(),
    )
    assert "family-dinner" in result
    assert household_tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER

    add_event("outside-invite", "outsider@else.test", ["member@family.test"])
    outside_tracker = InMemoryTurnTaintTracker()
    result = await search_calendar_events_tool(
        _context(db, outside_tracker, app_config=_HOUSEHOLD_CONFIG),
        config,
        start_date=start.date().isoformat(),
    )
    assert "outside-invite" in result
    assert outside_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
