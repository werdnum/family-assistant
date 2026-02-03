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
    FrameAnalysisLLMResponse,
    get_camera_frame_tool,
    get_camera_frames_batch_tool,
    get_camera_recordings_tool,
    list_cameras_tool,
    scan_camera_frames_tool,
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


# --- Tests for scan_camera_frames_tool ---


def create_mock_llm_client(
    responses: list[FrameAnalysisLLMResponse] | None = None,
) -> Mock:
    """Create a mock LLM client that returns specified responses.

    Args:
        responses: List of FrameAnalysisLLMResponse objects. If None, generates default responses.
    """
    if responses is None:
        # Default: first and third frames match
        responses = [
            FrameAnalysisLLMResponse(
                matches_query=True,
                description="Person visible",
                confidence=0.9,
                detected_objects=["person"],
            ),
            FrameAnalysisLLMResponse(
                matches_query=False,
                description="Empty yard",
                confidence=0.8,
                detected_objects=[],
            ),
            FrameAnalysisLLMResponse(
                matches_query=True,
                description="Person walking",
                confidence=0.85,
                detected_objects=["person"],
            ),
            FrameAnalysisLLMResponse(
                matches_query=False,
                description="Empty",
                confidence=0.7,
                detected_objects=[],
            ),
            FrameAnalysisLLMResponse(
                matches_query=False,
                description="Empty",
                confidence=0.7,
                detected_objects=[],
            ),
        ]

    call_count = 0

    async def mock_generate_structured(
        messages: object,
        response_model: type[FrameAnalysisLLMResponse],
        max_retries: int = 2,
    ) -> FrameAnalysisLLMResponse:
        nonlocal call_count
        response = responses[call_count % len(responses)]
        call_count += 1
        return response

    mock_client = Mock()
    mock_client.generate_structured = mock_generate_structured
    return mock_client


@pytest.fixture
def mock_processing_service() -> Mock:
    """Create a mock processing service with LLM client."""
    mock_service = Mock()
    mock_service.llm_client = create_mock_llm_client()
    return mock_service


@pytest.fixture
def exec_context_with_llm(
    fake_camera_backend: FakeCameraBackend,
    mock_processing_service: Mock,
) -> ToolExecutionContext:
    """Create a ToolExecutionContext with fake camera backend and LLM client."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=mock_processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=fake_camera_backend,
    )


@pytest.mark.asyncio
async def test_scan_camera_frames_success(
    exec_context_with_llm: ToolExecutionContext,
) -> None:
    """Test scanning camera frames with parallel analysis."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=1)).isoformat()

    result = await scan_camera_frames_tool(
        exec_context_with_llm,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        query="person in the yard",
        interval_minutes=15,
        max_frames=10,
        filter_matching=True,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)

    # Should have scanned frames
    assert "frames_scanned" in data
    assert data["frames_scanned"] == 5  # Based on fake backend setup

    # Should have found matches (based on mock responses)
    assert "matches_found" in data
    assert data["matches_found"] == 2  # First and third frames match

    # Should have analysis results
    assert "analysis_results" in data
    assert len(data["analysis_results"]) == 2  # Only matching frames

    # Should have attachments for matching frames
    assert result.attachments is not None
    assert len(result.attachments) == 2


@pytest.mark.asyncio
async def test_scan_camera_frames_no_filtering(
    exec_context_with_llm: ToolExecutionContext,
) -> None:
    """Test scanning camera frames without filtering returns all frames."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=1)).isoformat()

    result = await scan_camera_frames_tool(
        exec_context_with_llm,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        query="person in the yard",
        interval_minutes=15,
        max_frames=10,
        filter_matching=False,  # Return all frames
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)

    # Should have all frames in analysis results
    assert len(data["analysis_results"]) == 5

    # Should have attachments for all frames
    assert result.attachments is not None
    assert len(result.attachments) == 5


@pytest.mark.asyncio
async def test_scan_camera_frames_no_matches() -> None:
    """Test scanning camera frames when nothing matches."""
    fake_backend = FakeCameraBackend()
    fake_backend.add_camera("cam_test", "Test Camera", "online")

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        fake_backend.set_frame(
            "cam_test",
            base_time + timedelta(minutes=i * 15),
            f"frame_{i}".encode(),
        )

    # LLM client that always returns no match
    no_match_responses = [
        FrameAnalysisLLMResponse(
            matches_query=False,
            description="Nothing found",
            confidence=0.9,
            detected_objects=[],
        ),
    ] * 3

    mock_service = Mock()
    mock_service.llm_client = create_mock_llm_client(no_match_responses)

    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=mock_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=fake_backend,
    )

    result = await scan_camera_frames_tool(
        exec_context,
        camera_id="cam_test",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
        query="something that does not exist",
        interval_minutes=15,
        max_frames=10,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)

    assert data["matches_found"] == 0
    assert "No frames matched" in (result.text or "")
    assert result.attachments is None or len(result.attachments) == 0


@pytest.mark.asyncio
async def test_scan_camera_frames_no_backend() -> None:
    """Test scanning frames returns error when camera_backend is None."""
    mock_service = Mock()
    mock_service.llm_client = create_mock_llm_client()

    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=mock_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await scan_camera_frames_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
        query="test query",
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_scan_camera_frames_no_processing_service(
    fake_camera_backend: FakeCameraBackend,
) -> None:
    """Test scanning frames returns error when processing_service is None."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,  # No processing service
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=fake_camera_backend,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await scan_camera_frames_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
        query="test query",
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Processing service not available" in data["error"]


@pytest.mark.asyncio
async def test_scan_camera_frames_no_frames_in_range(
    exec_context_with_llm: ToolExecutionContext,
) -> None:
    """Test scanning frames returns empty result when no frames in range."""
    # Use a time range with no frames
    start_time = datetime(2024, 1, 15, 20, 0, 0, tzinfo=UTC).isoformat()
    end_time = datetime(2024, 1, 15, 21, 0, 0, tzinfo=UTC).isoformat()

    result = await scan_camera_frames_tool(
        exec_context_with_llm,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        query="person",
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["frames_scanned"] == 0
    assert data["matches_found"] == 0
    assert "No frames found" in data.get("message", "")


@pytest.mark.asyncio
async def test_scan_camera_frames_handles_llm_errors() -> None:
    """Test scanning frames handles LLM errors gracefully."""
    fake_backend = FakeCameraBackend()
    fake_backend.add_camera("cam_test", "Test Camera", "online")

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        fake_backend.set_frame(
            "cam_test",
            base_time + timedelta(minutes=i * 15),
            f"frame_{i}".encode(),
        )

    # Create mock that raises error on second call
    call_count = 0

    async def mock_generate_with_error(
        messages: object,
        response_model: type[FrameAnalysisLLMResponse],
        max_retries: int = 2,
    ) -> FrameAnalysisLLMResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("LLM error")
        return FrameAnalysisLLMResponse(
            matches_query=True,
            description="Found",
            confidence=0.9,
            detected_objects=[],
        )

    mock_service = Mock()
    mock_service.llm_client = Mock()
    mock_service.llm_client.generate_structured = mock_generate_with_error

    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=mock_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=fake_backend,
    )

    result = await scan_camera_frames_tool(
        exec_context,
        camera_id="cam_test",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
        query="test",
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)

    # Should still return results for successful frames
    assert data["frames_scanned"] == 3
    assert "analysis_errors" in data
    assert data["analysis_errors"] == 1  # One error
    assert data["frames_analyzed"] == 2  # Two successful
