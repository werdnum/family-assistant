"""Unit tests for camera tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from family_assistant.camera.fake import FakeCameraBackend
from family_assistant.camera.protocol import (
    CameraEvent,
    Recording,
)
from family_assistant.tools.camera import (
    get_camera_frame_tool,
    get_camera_frames_batch_tool,
    get_camera_recordings_tool,
    list_cameras_tool,
    search_camera_events_tool,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult


@pytest.fixture
def fake_camera_backend() -> FakeCameraBackend:
    """Create a FakeCameraBackend with test data."""
    backend = FakeCameraBackend()

    # Add test cameras
    backend.add_camera("cam_front", "Front Door", "online")
    backend.add_camera("cam_back", "Back Yard", "online")
    backend.add_camera("cam_garage", "Garage", "offline")

    # Add test events
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    backend.add_event(
        CameraEvent(
            camera_id="cam_front",
            start_time=base_time,
            end_time=base_time + timedelta(seconds=30),
            event_type="person",
            confidence=0.95,
            metadata={"zone": "entry"},
        )
    )
    backend.add_event(
        CameraEvent(
            camera_id="cam_front",
            start_time=base_time + timedelta(hours=1),
            end_time=base_time + timedelta(hours=1, seconds=15),
            event_type="vehicle",
            confidence=0.88,
        )
    )
    backend.add_event(
        CameraEvent(
            camera_id="cam_back",
            start_time=base_time + timedelta(minutes=30),
            end_time=base_time + timedelta(minutes=30, seconds=45),
            event_type="pet",
            confidence=0.92,
        )
    )

    # Add test recordings
    backend.add_recording(
        Recording(
            camera_id="cam_front",
            start_time=base_time - timedelta(hours=1),
            end_time=base_time + timedelta(hours=2),
            filename="front_20240115_1100.mp4",
            size_bytes=1024 * 1024 * 100,  # 100 MB
        )
    )
    backend.add_recording(
        Recording(
            camera_id="cam_back",
            start_time=base_time,
            end_time=base_time + timedelta(hours=1),
            filename="back_20240115_1200.mp4",
            size_bytes=1024 * 1024 * 50,  # 50 MB
        )
    )

    # Add test frames
    frame_time = base_time
    for i in range(5):
        backend.set_frame(
            "cam_front",
            frame_time + timedelta(minutes=i * 15),
            f"frame_{i}".encode(),
        )

    return backend


@pytest.fixture
def exec_context(fake_camera_backend: FakeCameraBackend) -> ToolExecutionContext:
    """Create a ToolExecutionContext with fake camera backend."""
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
        camera_backend=fake_camera_backend,
    )


@pytest.mark.asyncio
async def test_list_cameras_success(exec_context: ToolExecutionContext) -> None:
    """Test listing cameras successfully returns all cameras."""
    result = await list_cameras_tool(exec_context)

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "cameras" in data
    assert data["count"] == 3

    cameras = data["cameras"]
    assert len(cameras) == 3

    # Verify camera data structure
    front_cam = next(c for c in cameras if c["id"] == "cam_front")
    assert front_cam["name"] == "Front Door"
    assert front_cam["status"] == "online"
    assert front_cam["backend"] == "fake"

    garage_cam = next(c for c in cameras if c["id"] == "cam_garage")
    assert garage_cam["status"] == "offline"


@pytest.mark.asyncio
async def test_list_cameras_no_backend() -> None:
    """Test listing cameras returns error when camera_backend is None."""
    exec_context = ToolExecutionContext(
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
        camera_backend=None,
    )

    result = await list_cameras_tool(exec_context)

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_search_camera_events_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test searching camera events finds events in time range."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=2)).isoformat()

    result = await search_camera_events_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "events" in data
    assert data["count"] == 2

    events = data["events"]
    assert len(events) == 2

    # Verify first event (person)
    person_event = events[0]
    assert person_event["camera_id"] == "cam_front"
    assert person_event["event_type"] == "person"
    assert person_event["confidence"] == 0.95
    assert person_event["metadata"]["zone"] == "entry"

    # Verify second event (vehicle)
    vehicle_event = events[1]
    assert vehicle_event["event_type"] == "vehicle"
    assert vehicle_event["confidence"] == 0.88


@pytest.mark.asyncio
async def test_search_camera_events_with_type_filter(
    exec_context: ToolExecutionContext,
) -> None:
    """Test searching camera events with event type filter."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=2)).isoformat()

    result = await search_camera_events_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        event_types=["person"],
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["count"] == 1

    events = data["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "person"


@pytest.mark.asyncio
async def test_search_camera_events_no_backend() -> None:
    """Test searching events returns error when camera_backend is None."""
    exec_context = ToolExecutionContext(
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
        camera_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await search_camera_events_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_frame_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting a single camera frame returns frame attachment."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    timestamp = base_time.isoformat()

    result = await get_camera_frame_tool(
        exec_context,
        camera_id="cam_front",
        timestamp=timestamp,
    )

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1

    attachment = result.attachments[0]
    assert attachment.mime_type == "image/jpeg"
    assert attachment.content == b"frame_0"
    assert "Camera frame at" in attachment.description


@pytest.mark.asyncio
async def test_get_camera_frame_not_found(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting frame returns error when no frame exists at timestamp."""
    # Use a timestamp where no frame exists
    timestamp = datetime(2024, 1, 15, 20, 0, 0, tzinfo=UTC).isoformat()

    result = await get_camera_frame_tool(
        exec_context,
        camera_id="cam_front",
        timestamp=timestamp,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "No frame available" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_frame_no_backend() -> None:
    """Test getting frame returns error when camera_backend is None."""
    exec_context = ToolExecutionContext(
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
        camera_backend=None,
    )

    timestamp = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC).isoformat()
    result = await get_camera_frame_tool(
        exec_context,
        camera_id="cam_front",
        timestamp=timestamp,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_frames_batch_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting batch frames returns multiple frame attachments."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=1)).isoformat()

    result = await get_camera_frames_batch_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        interval_minutes=15,
        max_frames=10,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "timestamps" in data
    assert data["count"] == 5  # 5 frames at 15-minute intervals (0, 15, 30, 45, 60)

    # Verify attachments
    assert result.attachments is not None
    assert len(result.attachments) == 5

    for i, attachment in enumerate(result.attachments):
        assert attachment.mime_type == "image/jpeg"
        assert attachment.content == f"frame_{i}".encode()
        assert f"Frame {i + 1}/5" in attachment.description


@pytest.mark.asyncio
async def test_get_camera_frames_batch_no_frames(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting batch frames returns empty result when no frames available."""
    # Use a time range with no frames
    start_time = datetime(2024, 1, 15, 20, 0, 0, tzinfo=UTC).isoformat()
    end_time = datetime(2024, 1, 15, 21, 0, 0, tzinfo=UTC).isoformat()

    result = await get_camera_frames_batch_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        interval_minutes=15,
        max_frames=10,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["count"] == 0
    assert len(data["timestamps"]) == 0


@pytest.mark.asyncio
async def test_get_camera_frames_batch_no_backend() -> None:
    """Test getting batch frames returns error when camera_backend is None."""
    exec_context = ToolExecutionContext(
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
        camera_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await get_camera_frames_batch_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_recordings_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting camera recordings returns recording list."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = (base_time - timedelta(hours=2)).isoformat()
    end_time = (base_time + timedelta(hours=3)).isoformat()

    result = await get_camera_recordings_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "recordings" in data
    assert data["count"] == 1

    recordings = data["recordings"]
    assert len(recordings) == 1

    recording = recordings[0]
    assert recording["camera_id"] == "cam_front"
    assert recording["filename"] == "front_20240115_1100.mp4"
    assert recording["size_bytes"] == 1024 * 1024 * 100
    assert recording["duration_seconds"] == 3 * 3600  # 3 hours


@pytest.mark.asyncio
async def test_get_camera_recordings_no_backend() -> None:
    """Test getting recordings returns error when camera_backend is None."""
    exec_context = ToolExecutionContext(
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
        camera_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await get_camera_recordings_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]
