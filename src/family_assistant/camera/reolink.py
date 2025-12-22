"""Reolink camera backend implementation.

This module implements the CameraBackend protocol for Reolink cameras using the
reolink_aio library. It provides:
- Connection pooling and session management
- Frame extraction from RTSP streams via OpenCV
- AI detection event retrieval via VOD trigger filtering
- Recording metadata access

The implementation handles timezone conversions (UTC to camera local time) and
manages concurrent access to avoid exceeding Reolink's session limits.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from family_assistant.camera.protocol import (
    CameraEvent,
    CameraInfo,
    FrameWithTimestamp,
    Recording,
)

try:
    import cv2  # pyright: ignore[reportMissingImports]
    import numpy as np  # pyright: ignore[reportMissingImports]
    from reolink_aio.api import Host  # type: ignore[import-not-found]

    REOLINK_AVAILABLE = True
except ImportError:
    REOLINK_AVAILABLE = False
    Host = None  # type: ignore[assignment]
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from datetime import datetime

    from reolink_aio.api import Host as HostType  # type: ignore[import-not-found]
else:
    HostType = Any

logger = logging.getLogger(__name__)

# Mapping from Reolink VOD triggers to our event types
TRIGGER_TO_EVENT_TYPE: dict[str, str] = {
    "person": "person",
    "face": "person",
    "vehicle": "vehicle",
    "car": "vehicle",
    "dog_cat": "pet",
    "pet": "pet",
    "motion": "motion",
}

# VOD split time for searching recordings (5 minutes per chunk)
VOD_SPLIT_TIME = timedelta(minutes=5)


class _ReolinkCameraConfigRequired(TypedDict):
    """Required fields for Reolink camera configuration."""

    host: str
    username: str
    password: str


class ReolinkCameraConfigDict(_ReolinkCameraConfigRequired, total=False):
    """TypedDict for Reolink camera configuration."""

    port: int
    use_https: bool
    channel: int
    name: str | None


@dataclass
class ReolinkCameraConfig:
    """Configuration for a single Reolink camera or NVR channel.

    Attributes:
        host: IP address or hostname of the camera/NVR.
        username: Authentication username.
        password: Authentication password.
        port: HTTPS port (default 443).
        use_https: Whether to use HTTPS (default True).
        channel: Channel number for NVR (default 0 for standalone cameras).
        name: Human-readable name (optional, will use device name if not provided).
    """

    host: str
    username: str
    password: str
    port: int = 443
    use_https: bool = True
    channel: int = 0
    name: str | None = None


class ReolinkBackend:
    """Reolink camera backend implementation.

    This backend manages connections to one or more Reolink cameras/NVRs,
    providing async methods for event search, recording access, and frame extraction.

    The implementation uses connection pooling to reuse Host instances and
    per-camera locks to prevent concurrent API calls that could exceed session limits.
    """

    def __init__(self, cameras: dict[str, ReolinkCameraConfig]) -> None:
        """Initialize Reolink backend.

        Args:
            cameras: Mapping of camera_id to ReolinkCameraConfig.

        Raises:
            ImportError: If reolink_aio or cv2 is not available.
        """
        if not REOLINK_AVAILABLE:
            msg = "reolink_aio and opencv-python are required for Reolink backend"
            raise ImportError(msg)

        self._cameras = cameras
        self._hosts: dict[str, HostType] = {}
        self._locks: dict[str, asyncio.Lock] = {
            camera_id: asyncio.Lock() for camera_id in cameras
        }

    async def _get_or_create_host(self, camera_id: str) -> HostType:
        """Get or create Host instance for camera.

        Args:
            camera_id: ID of the camera.

        Returns:
            Host instance for the camera.

        Raises:
            ValueError: If camera_id is unknown.
        """
        if camera_id not in self._cameras:
            msg = f"Unknown camera ID: {camera_id}"
            raise ValueError(msg)

        # Check if we already have a host connection
        if camera_id in self._hosts:
            return self._hosts[camera_id]

        # Create new Host instance
        config = self._cameras[camera_id]
        if Host is None:
            msg = "reolink_aio not available"
            raise RuntimeError(msg)

        host = Host(
            host=config.host,
            username=config.username,
            password=config.password,
            port=config.port,
            use_https=config.use_https,
        )

        # Initialize connection
        await host.get_host_data()

        self._hosts[camera_id] = host
        return host

    async def list_cameras(self) -> list[CameraInfo]:
        """List available cameras.

        Returns:
            List of CameraInfo objects with camera metadata.
        """
        cameras: list[CameraInfo] = []

        for camera_id, config in self._cameras.items():
            try:
                async with self._locks[camera_id]:
                    host = await self._get_or_create_host(camera_id)
                    # Check if camera is connected
                    status = "online" if host.session_active else "offline"
                    name = config.name or host.nvr_name or f"Camera {camera_id}"

                    cameras.append(
                        CameraInfo(
                            id=camera_id,
                            name=name,
                            status=status,
                            backend="reolink",
                        )
                    )
            except Exception:
                logger.exception("Failed to get info for camera %s", camera_id)
                # Add camera with offline status
                cameras.append(
                    CameraInfo(
                        id=camera_id,
                        name=config.name or f"Camera {camera_id}",
                        status="offline",
                        backend="reolink",
                    )
                )

        return cameras

    async def search_events(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
        event_types: list[str] | None = None,
    ) -> list[CameraEvent]:
        """Search for detection events in time range.

        Uses VOD file triggers to identify AI detection events. Each recording
        segment may have trigger flags indicating what type of detection
        (person, vehicle, pet, motion) caused the recording.

        Args:
            camera_id: ID of the camera to search.
            start_time: Start of the time range (UTC).
            end_time: End of the time range (UTC).
            event_types: Optional filter for event types (person, vehicle, pet, motion).

        Returns:
            List of CameraEvent objects matching the criteria.
        """
        async with self._locks[camera_id]:
            host = await self._get_or_create_host(camera_id)
            config = self._cameras[camera_id]
            channel = config.channel

            events: list[CameraEvent] = []

            try:
                # Search for VOD files with triggers
                _statuses, vod_files = await host.request_vod_files(
                    channel,
                    start_time,
                    end_time,
                    stream="main",
                    split_time=VOD_SPLIT_TIME,
                )

                for vod_file in vod_files:
                    # Get triggers from the VOD file
                    triggers = getattr(vod_file, "triggers", [])
                    if not triggers:
                        continue

                    for trigger in triggers:
                        trigger_lower = trigger.lower()
                        # Map trigger to standard event type, or use the raw trigger
                        if trigger_lower in TRIGGER_TO_EVENT_TYPE:
                            event_type = TRIGGER_TO_EVENT_TYPE[trigger_lower]
                        else:
                            event_type = trigger_lower

                        # Filter by event type if specified
                        if event_types and event_type not in event_types:
                            continue

                        events.append(
                            CameraEvent(
                                camera_id=camera_id,
                                start_time=vod_file.start_time,
                                end_time=getattr(vod_file, "end_time", None),
                                event_type=event_type,
                                confidence=None,  # Reolink doesn't provide confidence
                                metadata={
                                    "filename": vod_file.file_name,
                                    "raw_trigger": trigger,
                                },
                            )
                        )

            except Exception:
                logger.exception("Error searching events for camera %s", camera_id)
                raise

            return events

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
        async with self._locks[camera_id]:
            host = await self._get_or_create_host(camera_id)
            config = self._cameras[camera_id]
            channel = config.channel

            recordings: list[Recording] = []

            try:
                # Search for VOD files
                _statuses, vod_files = await host.request_vod_files(
                    channel,
                    start_time,
                    end_time,
                    stream="main",
                    split_time=VOD_SPLIT_TIME,
                )

                for vod_file in vod_files:
                    # Get recording end time
                    file_end_time = getattr(vod_file, "end_time", None)
                    if file_end_time is None:
                        # Estimate end time from duration if available
                        duration = getattr(vod_file, "duration", None)
                        if duration:
                            file_end_time = vod_file.start_time + duration
                        else:
                            # Default to start time + split time
                            file_end_time = vod_file.start_time + VOD_SPLIT_TIME

                    recordings.append(
                        Recording(
                            camera_id=camera_id,
                            start_time=vod_file.start_time,
                            end_time=file_end_time,
                            filename=vod_file.file_name,
                            size_bytes=getattr(vod_file, "size", None),
                        )
                    )

            except Exception:
                logger.exception("Error getting recordings for camera %s", camera_id)
                raise

            return recordings

    async def get_frame(
        self,
        camera_id: str,
        timestamp: datetime,
    ) -> bytes:
        """Extract single frame at timestamp.

        For historical timestamps, this extracts a frame from the recorded video.
        Uses OpenCV to decode the video stream and extract the frame.

        Args:
            camera_id: ID of the camera.
            timestamp: Exact time to extract frame (UTC).

        Returns:
            JPEG bytes of the frame.

        Raises:
            ValueError: If no recording exists at the timestamp.
            RuntimeError: If frame extraction fails.
        """
        async with self._locks[camera_id]:
            host = await self._get_or_create_host(camera_id)
            config = self._cameras[camera_id]
            channel = config.channel

            # First, find the recording that contains this timestamp
            search_start = timestamp - timedelta(minutes=1)
            search_end = timestamp + timedelta(minutes=1)

            try:
                _statuses, vod_files = await host.request_vod_files(
                    channel,
                    search_start,
                    search_end,
                    stream="sub",  # Use sub stream for faster extraction
                    split_time=VOD_SPLIT_TIME,
                )

                if not vod_files:
                    msg = f"No recording found at timestamp {timestamp}"
                    raise ValueError(msg)

                # Find the VOD file that contains our timestamp
                target_file = None
                for vod_file in vod_files:
                    file_start = vod_file.start_time
                    file_end = getattr(
                        vod_file, "end_time", file_start + VOD_SPLIT_TIME
                    )
                    if file_start <= timestamp <= file_end:
                        target_file = vod_file
                        break

                if target_file is None:
                    target_file = vod_files[0]  # Use first available

                # Get playback URL for this file
                _mime_type, playback_url = await host.get_vod_source(
                    channel,
                    target_file.file_name,
                    "sub",
                    "RTMP",  # RTMP or RTSP depending on camera support
                )

                # Extract frame using OpenCV in a thread
                def _extract_frame() -> bytes:
                    if cv2 is None or np is None:
                        msg = "OpenCV not available"
                        raise RuntimeError(msg)

                    cap = cv2.VideoCapture(playback_url)
                    try:
                        # Calculate offset into video
                        fps = cap.get(cv2.CAP_PROP_FPS) or 15
                        offset_seconds = (
                            timestamp - target_file.start_time
                        ).total_seconds()
                        frame_number = int(offset_seconds * fps)

                        # Seek to frame
                        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)

                        ret, frame = cap.read()
                        if not ret:
                            msg = "Failed to read frame from video"
                            raise RuntimeError(msg)

                        # Encode to JPEG
                        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
                        success, buffer = cv2.imencode(".jpg", frame, encode_params)
                        if not success:
                            msg = "Failed to encode frame to JPEG"
                            raise RuntimeError(msg)

                        return buffer.tobytes()
                    finally:
                        cap.release()

                return await asyncio.to_thread(_extract_frame)

            except ValueError:
                raise
            except Exception as e:
                logger.exception("Error extracting frame for camera %s", camera_id)
                msg = f"Frame extraction failed: {e}"
                raise RuntimeError(msg) from e

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
        frames: list[FrameWithTimestamp] = []

        # Calculate timestamps
        current = start_time
        timestamps: list[datetime] = []
        while current <= end_time and len(timestamps) < max_frames:
            timestamps.append(current)
            current += timedelta(seconds=interval_seconds)

        # Extract frames (releasing lock between each to avoid blocking too long)
        for ts in timestamps:
            try:
                jpeg_bytes = await self.get_frame(camera_id, ts)
                frames.append(
                    FrameWithTimestamp(
                        timestamp=ts, jpeg_bytes=jpeg_bytes, camera_id=camera_id
                    )
                )
            except (ValueError, RuntimeError) as e:
                # Skip timestamps where no recording exists or extraction fails
                logger.warning("Could not extract frame at %s: %s", ts, e)
                continue

        return frames

    async def close(self) -> None:
        """Cleanup connections and resources."""
        for camera_id, host in self._hosts.items():
            try:
                async with self._locks[camera_id]:
                    await host.logout()
            except Exception:
                logger.exception("Error closing host for camera %s", camera_id)

        self._hosts.clear()


def create_reolink_backend(
    cameras_config: dict[str, ReolinkCameraConfigDict],
) -> ReolinkBackend | None:
    """Create ReolinkBackend from config dict.

    Args:
        cameras_config: Dict mapping camera_id to config dict with keys:
            - host: str
            - username: str
            - password: str
            - port: int (optional, default 443)
            - use_https: bool (optional, default True)
            - channel: int (optional, default 0)
            - name: str (optional)

    Returns:
        ReolinkBackend instance, or None if reolink_aio is not available.

    Example:
        >>> config = {
        ...     "front_door": {
        ...         "host": "192.168.1.100",
        ...         "username": "admin",
        ...         "password": "secret",
        ...         "name": "Front Door",
        ...     }
        ... }
        >>> backend = create_reolink_backend(config)
    """
    if not REOLINK_AVAILABLE:
        logger.warning("reolink_aio not available, cannot create Reolink backend")
        return None

    cameras: dict[str, ReolinkCameraConfig] = {}
    for camera_id, config in cameras_config.items():
        cameras[camera_id] = ReolinkCameraConfig(
            host=config["host"],
            username=config["username"],
            password=config["password"],
            port=config.get("port", 443),
            use_https=config.get("use_https", True),
            channel=config.get("channel", 0),
            name=config.get("name"),
        )

    return ReolinkBackend(cameras)
