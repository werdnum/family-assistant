"""Camera backend protocol and data types.

This module defines the abstract interface for camera backends (Reolink, Frigate, etc.)
enabling backend-agnostic tools and easy testing with fake implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


@dataclass
class CameraInfo:
    """Camera metadata."""

    id: str
    name: str
    status: str  # "online", "offline", "unknown"
    backend: str  # "reolink", "frigate", "fake"


@dataclass
class CameraEvent:
    """Detection event from camera."""

    camera_id: str
    start_time: datetime
    event_type: str  # "person", "vehicle", "pet", "motion"
    end_time: datetime | None = None
    confidence: float | None = None
    # ast-grep-ignore: no-dict-any - Backend-specific metadata is arbitrary
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Recording:
    """Video recording segment."""

    camera_id: str
    start_time: datetime
    end_time: datetime
    filename: str | None = None
    size_bytes: int | None = None


@dataclass
class FrameWithTimestamp:
    """A frame with its timestamp."""

    timestamp: datetime
    jpeg_bytes: bytes
    camera_id: str


class CameraBackend(Protocol):
    """Protocol for camera system backends (Reolink, Frigate, etc.).

    Implementations must provide async methods for:
    - Listing available cameras
    - Searching for detection events
    - Retrieving recordings metadata
    - Extracting frames at specific timestamps
    - Batch frame extraction for binary search workflows
    """

    async def list_cameras(self) -> list[CameraInfo]:
        """List available cameras.

        Returns:
            List of CameraInfo objects with camera metadata.
        """
        ...

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
        ...

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
        ...

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
            ValueError: If no recording exists at the timestamp.
            RuntimeError: If frame extraction fails.
        """
        ...

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
        ...

    async def close(self) -> None:
        """Cleanup connections and resources."""
        ...
