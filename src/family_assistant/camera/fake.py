"""Fake camera backend for testing."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from family_assistant.camera.protocol import (
    CameraEvent,
    CameraInfo,
    FrameWithTimestamp,
    Recording,
)

logger = logging.getLogger(__name__)


class FakeCameraBackend:
    """In-memory fake camera backend for testing.

    Stores cameras, events, recordings, and frames in memory. Provides test setup
    methods to populate data and implements the CameraBackend protocol for testing
    camera-related functionality without real hardware.
    """

    def __init__(self) -> None:
        """Initialize fake camera backend with empty data structures."""
        self._cameras: dict[str, CameraInfo] = {}
        self._events: list[CameraEvent] = []
        self._recordings: list[Recording] = []
        # Maps (camera_id, timestamp) -> jpeg_bytes
        self._frames: dict[tuple[str, datetime], bytes] = {}

    # Test setup methods

    def add_camera(
        self,
        camera_id: str,
        name: str,
        status: str = "online",
    ) -> None:
        """Add a camera to the fake backend.

        Args:
            camera_id: Unique camera identifier.
            name: Human-readable camera name.
            status: Camera status (online, offline, unknown).
        """
        self._cameras[camera_id] = CameraInfo(
            id=camera_id,
            name=name,
            status=status,
            backend="fake",
        )
        logger.debug("Added camera: %s (%s)", camera_id, name)

    def add_event(self, event: CameraEvent) -> None:
        """Add a detection event to the fake backend.

        Args:
            event: CameraEvent to add.
        """
        self._events.append(event)
        logger.debug(
            "Added event: %s at %s on camera %s",
            event.event_type,
            event.start_time,
            event.camera_id,
        )

    def add_recording(self, recording: Recording) -> None:
        """Add a recording segment to the fake backend.

        Args:
            recording: Recording to add.
        """
        self._recordings.append(recording)
        logger.debug(
            "Added recording: %s from %s to %s",
            recording.camera_id,
            recording.start_time,
            recording.end_time,
        )

    def set_frame(
        self,
        camera_id: str,
        timestamp: datetime,
        jpeg_bytes: bytes,
    ) -> None:
        """Set a frame at a specific timestamp.

        Args:
            camera_id: Camera identifier.
            timestamp: Timestamp for the frame.
            jpeg_bytes: JPEG image data.
        """
        self._frames[(camera_id, timestamp)] = jpeg_bytes
        logger.debug(
            "Set frame for camera %s at %s (%d bytes)",
            camera_id,
            timestamp,
            len(jpeg_bytes),
        )

    # CameraBackend protocol implementation

    async def list_cameras(self) -> list[CameraInfo]:
        """List available cameras.

        Returns:
            List of CameraInfo objects with camera metadata.
        """
        return list(self._cameras.values())

    async def search_events(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
        event_types: list[str] | None = None,
    ) -> list[CameraEvent]:
        """Search for detection events in time range.

        Args:
            camera_id: ID of the camera to search.
            start_time: Start of the time range (UTC).
            end_time: End of the time range (UTC).
            event_types: Optional filter for event types (person, vehicle, pet, motion).

        Returns:
            List of CameraEvent objects matching the criteria.
        """
        results = []
        for event in self._events:
            # Filter by camera
            if event.camera_id != camera_id:
                continue

            # Filter by time range (event starts before end_time and ends after start_time)
            event_end = event.end_time or event.start_time
            if event.start_time > end_time or event_end < start_time:
                continue

            # Filter by event type if specified
            if event_types and event.event_type not in event_types:
                continue

            results.append(event)

        logger.debug(
            "Found %d events for camera %s between %s and %s",
            len(results),
            camera_id,
            start_time,
            end_time,
        )
        return results

    async def get_recordings(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Recording]:
        """List recording segments in time range.

        Args:
            camera_id: ID of the camera.
            start_time: Start of the time range (UTC).
            end_time: End of the time range (UTC).

        Returns:
            List of Recording objects representing available footage.
        """
        results = []
        for recording in self._recordings:
            # Filter by camera
            if recording.camera_id != camera_id:
                continue

            # Filter by time range (recording starts before end_time and ends after start_time)
            if recording.start_time > end_time or recording.end_time < start_time:
                continue

            results.append(recording)

        logger.debug(
            "Found %d recordings for camera %s between %s and %s",
            len(results),
            camera_id,
            start_time,
            end_time,
        )
        return results

    async def get_frame(
        self,
        camera_id: str,
        timestamp: datetime,
    ) -> bytes:
        """Extract single frame at timestamp.

        Args:
            camera_id: ID of the camera.
            timestamp: Exact time to extract frame (UTC).

        Returns:
            JPEG bytes of the frame.

        Raises:
            ValueError: If no frame exists at the timestamp.
        """
        key = (camera_id, timestamp)
        if key not in self._frames:
            raise ValueError(
                f"No frame available for camera {camera_id} at {timestamp}"
            )
        return self._frames[key]

    async def get_frames_batch(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
        interval_seconds: int = 900,
        max_frames: int = 10,
    ) -> list[FrameWithTimestamp]:
        """Extract frames at regular intervals for binary search.

        Args:
            camera_id: ID of the camera.
            start_time: Start of the time range (UTC).
            end_time: End of the time range (UTC).
            interval_seconds: Seconds between each frame (default 15 minutes).
            max_frames: Maximum number of frames to return.

        Returns:
            List of FrameWithTimestamp objects with timestamps and JPEG bytes.
        """
        results = []
        current_time = start_time

        while current_time <= end_time and len(results) < max_frames:
            key = (camera_id, current_time)
            if key in self._frames:
                results.append(
                    FrameWithTimestamp(
                        timestamp=current_time,
                        jpeg_bytes=self._frames[key],
                        camera_id=camera_id,
                    )
                )
            current_time += timedelta(seconds=interval_seconds)

        logger.debug(
            "Extracted %d frames for camera %s between %s and %s (interval: %ds)",
            len(results),
            camera_id,
            start_time,
            end_time,
            interval_seconds,
        )
        return results

    async def get_live_snapshot(self, camera_id: str) -> bytes:
        """Get a live snapshot (current frame) from the camera.

        For the fake backend, this returns a predefined test image.

        Args:
            camera_id: ID of the camera.

        Returns:
            JPEG bytes of a test frame.

        Raises:
            ValueError: If camera_id is not configured.
        """
        if camera_id not in self._cameras:
            msg = f"Unknown camera: {camera_id}"
            raise ValueError(msg)
        # Return minimal JPEG bytes for testing (start + end markers)
        # Real cameras return full images, this is just for protocol compliance
        return b"\xff\xd8\xff\xd9"

    async def close(self) -> None:
        """Cleanup connections and resources."""
        logger.debug("Closing fake camera backend")
        # Nothing to cleanup for fake backend
