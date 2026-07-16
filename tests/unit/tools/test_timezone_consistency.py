"""Tests verifying that all tool outputs present dates/times in the user's local timezone.

The LLM should never see UTC timestamps - all dates/times must be formatted
in the configured timezone before being returned as tool results.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.camera.fake import FakeCameraBackend
from family_assistant.camera.protocol import CameraEvent, Recording
from family_assistant.tools.automations import (
    _to_isoformat,  # noqa: PLC2701
    format_automation_datetime,
)
from family_assistant.tools.camera import (
    get_camera_frames_batch_tool,
    get_camera_recordings_tool,
    search_camera_events_tool,
)
from family_assistant.tools.events import _format_event_timestamp  # noqa: PLC2701
from family_assistant.tools.types import ToolExecutionContext, ToolResult

SYDNEY = ZoneInfo("Australia/Sydney")
NEW_YORK = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

# --- Automations: format_automation_datetime should format in local timezone ---


class TestAutomationsFormatDatetime:
    """Test that format_automation_datetime outputs times in the user's timezone, not UTC."""

    def testformat_automation_datetime_in_sydney_timezone(self) -> None:
        """UTC midnight should display as 11:00 AEDT (UTC+11) in Sydney."""
        dt_utc = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = format_automation_datetime(dt_utc, SYDNEY)
        # UTC 00:00 = AEDT 11:00
        assert "11:00" in result
        assert "AEDT" in result
        assert "UTC" not in result

    def testformat_automation_datetime_in_new_york_timezone(self) -> None:
        """UTC midnight should display as 19:00 EST (UTC-5) in New York."""
        dt_utc = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = format_automation_datetime(dt_utc, NEW_YORK)
        # UTC 00:00 Jan 15 = EST 19:00 Jan 14
        assert "19:00" in result
        assert "EST" in result
        assert "UTC" not in result

    def testformat_automation_datetime_none_returns_never(self) -> None:
        """None input should still return 'Never'."""
        result = format_automation_datetime(None, SYDNEY)
        assert result == "Never"

    def testformat_automation_datetime_with_utc_timezone(self) -> None:
        """When user timezone is UTC, the format should include UTC."""
        dt_utc = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = format_automation_datetime(dt_utc, UTC_TZ)
        assert "12:00" in result
        assert "UTC" in result

    def testformat_automation_datetime_naive_assumed_utc(self) -> None:
        """A naive datetime (no tzinfo) should be assumed UTC before converting."""
        dt_naive = datetime(2025, 1, 15, 0, 0, 0)  # No tzinfo
        result = format_automation_datetime(dt_naive, SYDNEY)
        # Naive assumed UTC 00:00 -> AEDT 11:00
        assert "11:00" in result
        assert "AEDT" in result


# --- Camera: timestamps in tool results should be in local timezone ---


@pytest.fixture
def camera_backend_with_events() -> FakeCameraBackend:
    """Create a FakeCameraBackend with events at known UTC times."""
    backend = FakeCameraBackend()
    backend.add_camera("cam_front", "Front Door", "online")

    base_time = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)  # midnight UTC
    backend.add_event(
        CameraEvent(
            camera_id="cam_front",
            start_time=base_time,
            end_time=base_time + timedelta(seconds=30),
            event_type="person",
            confidence=0.95,
        )
    )
    backend.add_recording(
        Recording(
            camera_id="cam_front",
            start_time=base_time,
            end_time=base_time + timedelta(hours=1),
            filename="front_20250115.mp4",
            size_bytes=1024 * 1024,
        )
    )

    for i in range(3):
        backend.set_frame(
            "cam_front",
            base_time + timedelta(minutes=i * 15),
            f"frame_{i}".encode(),
        )

    return backend


@pytest.fixture
def sydney_exec_context(
    camera_backend_with_events: FakeCameraBackend,
) -> ToolExecutionContext:
    """Create a ToolExecutionContext with Australia/Sydney timezone."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=camera_backend_with_events,
        timezone=SYDNEY,
        credential_resolvers=None,
        api_backend=None,
    )


class TestCameraTimezoneConsistency:
    """Test that camera tool outputs present timestamps in the user's timezone."""

    @pytest.mark.asyncio
    async def test_search_events_timestamps_in_local_timezone(
        self,
        sydney_exec_context: ToolExecutionContext,
    ) -> None:
        """Event timestamps should be in local timezone, not UTC."""
        base_time = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = await search_camera_events_tool(
            sydney_exec_context,
            camera_id="cam_front",
            start_time=base_time.isoformat(),
            end_time=(base_time + timedelta(hours=2)).isoformat(),
        )
        assert isinstance(result, ToolResult)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] >= 1

        # The event at UTC midnight should show as 11:00 AEDT
        event = data["events"][0]
        start_time_str = event["start_time"]
        # Should contain +11:00 offset (AEDT) not +00:00 (UTC)
        assert "+11:00" in start_time_str

    @pytest.mark.asyncio
    async def test_recordings_timestamps_in_local_timezone(
        self,
        sydney_exec_context: ToolExecutionContext,
    ) -> None:
        """Recording timestamps should be in local timezone, not UTC."""
        base_time = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = await get_camera_recordings_tool(
            sydney_exec_context,
            camera_id="cam_front",
            start_time=(base_time - timedelta(hours=1)).isoformat(),
            end_time=(base_time + timedelta(hours=2)).isoformat(),
        )
        assert isinstance(result, ToolResult)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] >= 1

        recording = data["recordings"][0]
        start_time_str = recording["start_time"]
        assert "+11:00" in start_time_str

    @pytest.mark.asyncio
    async def test_frames_batch_timestamps_in_local_timezone(
        self,
        sydney_exec_context: ToolExecutionContext,
    ) -> None:
        """Frame timestamps should be in local timezone, not UTC."""
        base_time = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = await get_camera_frames_batch_tool(
            sydney_exec_context,
            camera_id="cam_front",
            start_time=base_time.isoformat(),
            end_time=(base_time + timedelta(hours=1)).isoformat(),
            interval_minutes=15,
            max_frames=10,
        )
        assert isinstance(result, ToolResult)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] >= 1

        # All timestamps should be in AEDT
        for ts_str in data["timestamps"]:
            assert "+11:00" in ts_str

    @pytest.mark.asyncio
    async def test_camera_old_date_warning_uses_local_timezone(
        self,
        sydney_exec_context: ToolExecutionContext,
    ) -> None:
        """The old-date warning should show time in user's timezone, not UTC."""
        # Use dates well in the past to trigger the warning
        old_time = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
        result = await search_camera_events_tool(
            sydney_exec_context,
            camera_id="cam_front",
            start_time=old_time.isoformat(),
            end_time=(old_time + timedelta(hours=1)).isoformat(),
        )
        assert isinstance(result, ToolResult)
        data = result.get_data()
        assert isinstance(data, dict)
        warning = data.get("warning", "")
        # Warning should NOT contain "UTC"
        assert "UTC" not in warning


# --- Workspace files: timestamps should be timezone-aware ---


class TestWorkspaceFilesTimezone:
    """Test that workspace file timestamps are timezone-aware."""

    def test_fromtimestamp_with_timezone_produces_offset(self) -> None:
        """datetime.fromtimestamp with tz= should produce an offset in isoformat."""
        ts = time.time()
        dt = datetime.fromtimestamp(ts, tz=SYDNEY)
        iso_str = dt.isoformat()
        # Should contain timezone offset like +11:00 or +10:00
        assert "+" in iso_str


# --- Automations: _to_isoformat should convert to local timezone ---


class TestAutomationsToIsoformat:
    """Test that _to_isoformat converts to user's local timezone, not UTC."""

    def test_to_isoformat_converts_to_local_timezone(self) -> None:
        """UTC datetime should be converted to the specified local timezone."""
        dt_utc = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = _to_isoformat(dt_utc, SYDNEY)
        assert result is not None
        # UTC 00:00 = AEDT 11:00, should have +11:00 offset
        assert "+11:00" in result
        assert "11:00:00" in result

    def test_to_isoformat_none_returns_none(self) -> None:
        """None input should return None."""
        result = _to_isoformat(None, SYDNEY)
        assert result is None

    def test_to_isoformat_naive_assumed_utc(self) -> None:
        """Naive datetime should be assumed UTC before converting."""
        dt_naive = datetime(2025, 1, 15, 0, 0, 0)
        result = _to_isoformat(dt_naive, SYDNEY)
        assert result is not None
        assert "+11:00" in result

    def test_to_isoformat_utc_timezone_keeps_utc(self) -> None:
        """When user timezone is UTC, timestamps should stay UTC."""
        dt_utc = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = _to_isoformat(dt_utc, UTC_TZ)
        assert result is not None
        assert "+00:00" in result


# --- Events: _format_event_timestamp should convert to local timezone ---


class TestEventsFormatTimestamp:
    """Test that _format_event_timestamp converts to user's local timezone."""

    def test_format_datetime_to_local_timezone(self) -> None:
        """UTC datetime should be converted to the specified local timezone."""
        dt_utc = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        result = _format_event_timestamp(dt_utc, SYDNEY)
        assert "+11:00" in result

    def test_format_string_timestamp_to_local_timezone(self) -> None:
        """String timestamp should be parsed and converted to local timezone."""
        result = _format_event_timestamp("2025-01-15T00:00:00+00:00", SYDNEY)
        assert "+11:00" in result

    def test_format_naive_string_assumed_utc(self) -> None:
        """Naive string timestamp should be assumed UTC."""
        result = _format_event_timestamp("2025-01-15T00:00:00", SYDNEY)
        assert "+11:00" in result

    def test_format_naive_datetime_assumed_utc(self) -> None:
        """Naive datetime should be assumed UTC before converting."""
        dt_naive = datetime(2025, 1, 15, 0, 0, 0)
        result = _format_event_timestamp(dt_naive, SYDNEY)
        assert "+11:00" in result
