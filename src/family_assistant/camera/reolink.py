"""Reolink camera backend implementation.

This module implements the CameraBackend protocol for Reolink cameras using the
reolink_aio library. It provides:
- Connection pooling and session management
- Frame extraction from RTSP streams via OpenCV
- AI detection event retrieval
- Recording metadata access

The implementation handles timezone conversions (UTC to camera local time) and
manages concurrent access to avoid exceeding Reolink's session limits.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from family_assistant.camera.protocol import (
    CameraEvent,
    CameraInfo,
    FrameWithTimestamp,
    Recording,
)

try:
    from reolink_aio.api import Host  # type: ignore[import-not-found]

    REOLINK_AVAILABLE = True
except ImportError:
    REOLINK_AVAILABLE = False
    Host = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from datetime import datetime

    from reolink_aio.api import Host as HostType  # type: ignore[import-not-found]
else:
    HostType = Any

logger = logging.getLogger(__name__)


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

    NOTE: This is currently a stub implementation. The infrastructure for connection
    management and camera listing is functional, but the core methods (search_events,
    get_recordings, get_frame, get_frames_batch) raise NotImplementedError and need
    to be implemented with access to real Reolink hardware for testing.

    The FakeCameraBackend can be used for testing the camera tools without real hardware.
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
                    _host = await self._get_or_create_host(camera_id)
                    # Get camera status
                    # TODO: Implement actual status check using _host API
                    status = "unknown"
                    name = config.name or f"Camera {camera_id}"

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

        Args:
            camera_id: ID of the camera to search.
            start_time: Start of the time range (UTC).
            end_time: End of the time range (UTC).
            event_types: Optional filter for event types (person, vehicle, pet, motion).

        Returns:
            List of CameraEvent objects matching the criteria.
        """
        async with self._locks[camera_id]:
            _host = await self._get_or_create_host(camera_id)
            _config = self._cameras[camera_id]

            # TODO: Convert UTC times to camera local time
            # TODO: Use _host API to search for AI events
            # TODO: Filter by event_types if specified
            # TODO: Map Reolink event types to our standard types

            raise NotImplementedError("search_events not yet implemented")

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
            _host = await self._get_or_create_host(camera_id)
            _config = self._cameras[camera_id]

            # TODO: Convert UTC times to camera local time
            # TODO: Use _host API to search for recordings
            # TODO: Map recording metadata to Recording objects

            raise NotImplementedError("get_recordings not yet implemented")

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
        async with self._locks[camera_id]:
            _host = await self._get_or_create_host(camera_id)
            _config = self._cameras[camera_id]

            # TODO: Get RTSP URL for main stream
            # TODO: Use OpenCV to extract frame at timestamp
            # TODO: Encode to JPEG and return bytes

            raise NotImplementedError("get_frame not yet implemented")

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
        async with self._locks[camera_id]:
            _host = await self._get_or_create_host(camera_id)
            _config = self._cameras[camera_id]

            # TODO: Calculate timestamps at interval_seconds apart
            # TODO: Extract frames using get_frame (or optimized batch method)
            # TODO: Return list of FrameWithTimestamp objects

            raise NotImplementedError("get_frames_batch not yet implemented")

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
