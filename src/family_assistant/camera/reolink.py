"""Reolink camera backend implementation.

This module implements the CameraBackend protocol for Reolink cameras using the
reolink_aio library. It provides:
- Connection pooling and session management
- Frame extraction from recorded video via FFmpeg
- AI detection event retrieval via VOD trigger filtering
- Recording metadata access

The implementation handles timezone conversions (UTC to camera local time) and
manages concurrent access to avoid exceeding Reolink's session limits.

Configuration can be provided via:
1. Environment variable REOLINK_CAMERAS (JSON format)
2. Direct config dict passed to create_reolink_backend()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, TypedDict

import aiohttp
from reolink_aio.api import Host
from reolink_aio.enums import VodRequestType
from reolink_aio.exceptions import ReolinkConnectionError
from reolink_aio.typings import VOD_file, VOD_trigger

from family_assistant.camera.protocol import (
    CameraEvent,
    CameraInfo,
    FrameWithTimestamp,
    Recording,
)

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.config_models import ReolinkCameraItemConfig

logger = logging.getLogger(__name__)

# Environment variable for camera configuration
REOLINK_CAMERAS_ENV = "REOLINK_CAMERAS"

# Mapping from VOD_trigger flag names to our event types
# VOD_trigger is an IntFlag with values: PERSON, VEHICLE, ANIMAL, MOTION, FACE, etc.
VOD_TRIGGER_TO_EVENT_TYPE: dict[str, str] = {
    "PERSON": "person",
    "FACE": "person",
    "VEHICLE": "vehicle",
    "ANIMAL": "pet",
    "MOTION": "motion",
    "DOORBELL": "doorbell",
    "PACKAGE": "package",
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
        port: Port number (default: 443 for HTTPS, 80 for HTTP).
        use_https: Whether to use HTTPS (default True).
        channel: Channel number for NVR (default 0 for standalone cameras).
        name: Human-readable name (optional, will use device name if not provided).
        prefer_download: Skip FLV streaming and use direct download (faster for cameras
            with TLS issues that cause FLV to fail).
    """

    host: str
    username: str
    password: str
    port: int | None = None  # None means auto-detect based on use_https
    use_https: bool = True
    channel: int = 0
    name: str | None = None
    prefer_download: bool = False

    @property
    def effective_port(self) -> int:
        """Get the effective port, defaulting based on use_https if not set."""
        if self.port is not None:
            return self.port
        return 443 if self.use_https else 80


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
        """
        self._cameras = cameras
        self._hosts: dict[str, Host] = {}
        self._locks: dict[str, asyncio.Lock] = {
            camera_id: asyncio.Lock() for camera_id in cameras
        }

    def _validate_camera_id(self, camera_id: str) -> None:
        """Validate that camera_id exists.

        Args:
            camera_id: ID of the camera.

        Raises:
            ValueError: If camera_id is unknown.
        """
        if camera_id not in self._cameras:
            available = ", ".join(self._cameras.keys())
            msg = f"Unknown camera ID: '{camera_id}'. Available cameras: {available}"
            raise ValueError(msg)

    async def _get_or_create_host(self, camera_id: str) -> Host:
        """Get or create Host instance for camera.

        Args:
            camera_id: ID of the camera.

        Returns:
            Host instance for the camera.

        Raises:
            ValueError: If camera_id is unknown.
        """
        self._validate_camera_id(camera_id)

        # Check if we already have a host connection with active session
        if camera_id in self._hosts:
            host = self._hosts[camera_id]
            if host.session_active:
                return host
            # Session expired, clean up and reconnect
            logger.debug("Session expired for camera %s, reconnecting", camera_id)
            try:
                await host.logout()
            except Exception:
                logger.debug(
                    "Error during logout for camera %s", camera_id, exc_info=True
                )
            del self._hosts[camera_id]

        # Create new Host instance
        config = self._cameras[camera_id]
        host = Host(
            host=config.host,
            username=config.username,
            password=config.password,
            port=config.effective_port,
            use_https=config.use_https,
            timeout=120,  # Increase timeout from 30s default for VOD operations
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
        self._validate_camera_id(camera_id)
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
                    # Get triggers from the VOD file (VOD_trigger is an IntFlag)
                    trigger_flags = vod_file.triggers
                    if not trigger_flags or trigger_flags == VOD_trigger.NONE:
                        continue

                    # Iterate over each flag that is set in the IntFlag
                    for trigger_flag in VOD_trigger:
                        if trigger_flag == VOD_trigger.NONE:
                            continue
                        if trigger_flag not in trigger_flags:
                            continue

                        # Map trigger flag to event type
                        trigger_name = trigger_flag.name or str(trigger_flag)
                        event_type = VOD_TRIGGER_TO_EVENT_TYPE.get(
                            trigger_name, trigger_name.lower()
                        )

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
                                    "raw_trigger": trigger_name,
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
        self._validate_camera_id(camera_id)
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

    async def _invalidate_host(self, camera_id: str) -> None:
        """Invalidate cached host connection for camera.

        Args:
            camera_id: ID of the camera.
        """
        if camera_id in self._hosts:
            try:
                await self._hosts[camera_id].logout()
            except Exception:
                logger.debug(
                    "Error during logout for camera %s", camera_id, exc_info=True
                )
            del self._hosts[camera_id]

    async def get_frame(
        self,
        camera_id: str,
        timestamp: datetime,
    ) -> bytes:
        """Extract single frame at timestamp.

        For historical timestamps, this extracts a frame from the recorded video.
        Uses FFmpeg with FLV streaming for efficient extraction, falling back to
        downloading the video file and using OpenCV if streaming fails.

        Args:
            camera_id: ID of the camera.
            timestamp: Exact time to extract frame (UTC).

        Returns:
            JPEG bytes of the frame.

        Raises:
            ValueError: If no recording exists at the timestamp.
            RuntimeError: If frame extraction fails.
        """
        self._validate_camera_id(camera_id)

        # Retry logic for handling session disconnections
        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(max_retries):
            async with self._locks[camera_id]:
                try:
                    return await self._get_frame_impl(camera_id, timestamp)
                except (aiohttp.ServerDisconnectedError, aiohttp.ClientError) as e:
                    last_error = e
                    logger.warning(
                        "Connection error on attempt %d/%d for camera %s: %s",
                        attempt + 1,
                        max_retries,
                        camera_id,
                        e,
                    )
                    # Invalidate the cached host so next attempt gets fresh session
                    await self._invalidate_host(camera_id)
                    if attempt < max_retries - 1:
                        # Exponential backoff: 1s, 2s, 4s...
                        await asyncio.sleep(2**attempt)
                except ValueError:
                    raise
                except Exception as e:
                    logger.exception("Error extracting frame for camera %s", camera_id)
                    msg = f"Frame extraction failed: {e}"
                    raise RuntimeError(msg) from e

        # All retries exhausted
        msg = f"Frame extraction failed after {max_retries} attempts: {last_error}"
        raise RuntimeError(msg) from last_error

    async def _get_frame_impl(
        self,
        camera_id: str,
        timestamp: datetime,
    ) -> bytes:
        """Internal implementation of frame extraction.

        Tries FFmpeg with PLAYBACK streaming first (faster for seeking), then falls
        back to HTTP download + FFmpeg if streaming fails.

        Args:
            camera_id: ID of the camera.
            timestamp: Exact time to extract frame (UTC).

        Returns:
            JPEG bytes of the frame.
        """
        host = await self._get_or_create_host(camera_id)
        config = self._cameras[camera_id]
        channel = config.channel

        # First, find the recording that contains this timestamp
        search_start = timestamp - timedelta(minutes=1)
        search_end = timestamp + timedelta(minutes=1)

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
        # Use naive timestamps to avoid DST timezone mismatches - the camera
        # may report times in a different timezone offset than the user's request
        ts_naive = timestamp.replace(tzinfo=None)
        target_file = None
        for vod_file in vod_files:
            file_start = vod_file.start_time.replace(tzinfo=None)
            file_end_raw = getattr(vod_file, "end_time", None)
            if file_end_raw:
                file_end = file_end_raw.replace(tzinfo=None)
            else:
                file_end = file_start + VOD_SPLIT_TIME
            if file_start <= ts_naive <= file_end:
                target_file = vod_file
                break

        if target_file is None:
            # If no exact match, find the closest file that starts before the timestamp
            closest_file = None
            closest_diff = None
            for vod_file in vod_files:
                file_start = vod_file.start_time.replace(tzinfo=None)
                if file_start <= ts_naive:
                    diff = ts_naive - file_start
                    if closest_diff is None or diff < closest_diff:
                        closest_diff = diff
                        closest_file = vod_file
            if closest_file:
                target_file = closest_file
                logger.debug(
                    "No exact file match, using closest file starting %s before timestamp",
                    closest_diff,
                )
            else:
                target_file = vod_files[0]  # Fallback to first available

        file_start = target_file.start_time

        # Skip PLAYBACK if prefer_download is enabled (for cameras with TLS issues)
        if not config.prefer_download:
            # Try FFmpeg with PLAYBACK streaming first (with reconnection for TLS issues)
            logger.info("FRAME_EXTRACT: Trying FFmpeg/PLAYBACK for %s", camera_id)
            try:
                return await self._extract_frame_ffmpeg(
                    host, channel, target_file, timestamp, file_start
                )
            except Exception as e:
                logger.warning("FFmpeg/PLAYBACK failed: %s, trying HTTP download", e)
                # Invalidate host after failure to get fresh connection
                await self._invalidate_host(camera_id)
                # Give camera time to recover
                await asyncio.sleep(2)

        # Use HTTP download approach (always used if prefer_download is True)
        logger.info("FRAME_EXTRACT: Using HTTP download for %s", camera_id)
        return await self._extract_frame_download(
            camera_id, channel, target_file, timestamp, file_start
        )

    async def _extract_frame_ffmpeg(
        self,
        host: Host,
        channel: int,
        target_file: VOD_file,
        timestamp: datetime,
        file_start: datetime,
    ) -> bytes:
        """Extract frame using FFmpeg with PLAYBACK streaming.

        Uses the Playback API which supports server-side seeking via the
        'start' parameter. The start parameter is a timestamp in YYYYMMDDHHmmss
        format that tells the camera where to begin streaming from.

        This is much faster than FFmpeg seeking because the camera handles it
        server-side, typically completing in 2-3 seconds.
        """
        # Calculate target time for camera-side seeking
        # Use naive timestamps to avoid DST timezone mismatches
        ts_naive = timestamp.replace(tzinfo=None)
        fs_naive = file_start.replace(tzinfo=None)
        offset_seconds = int(max(0, (ts_naive - fs_naive).total_seconds()))

        # Calculate the target timestamp for seeking
        target_time = fs_naive + timedelta(seconds=offset_seconds)

        # Get PLAYBACK streaming URL (uses token-based auth)
        _mime_type, stream_url = await host.get_vod_source(
            channel,
            target_file.file_name,
            "sub",
            VodRequestType.PLAYBACK,
        )

        # Update the 'start' parameter to seek to target time
        # The library generates: start=YYYYMMDDHHmmss (file start time)
        # We need to replace it with our target time
        target_start = target_time.strftime("%Y%m%d%H%M%S")
        stream_url = re.sub(r"start=\d+", f"start={target_start}", stream_url)

        # Log URL without token
        safe_url = re.sub(r"token=[^&]+", "token=REDACTED", stream_url)
        logger.info("FFmpeg extracting frame from PLAYBACK stream: %s", safe_url)

        def _run_ffmpeg() -> bytes:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                # Use FFmpeg to extract a single frame from PLAYBACK stream
                # The camera handles seeking via 'start' in the URL, so FFmpeg
                # just needs to grab the first frame from the stream.
                cmd = [
                    "ffmpeg",
                    "-y",  # Overwrite output
                    # Reconnection options for flaky connections
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "2",
                    # Timeout for initial connection
                    "-timeout",
                    "15000000",  # 15 seconds in microseconds
                    "-i",
                    stream_url,  # Input stream (already seeked by camera)
                    "-vframes",
                    "1",  # Extract one frame
                    "-q:v",
                    "2",  # JPEG quality (2 = high quality)
                    "-f",
                    "image2",  # Output format
                    "-update",
                    "1",  # Required for single image output
                    tmp_path,
                ]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=30,  # 30 second timeout
                    check=False,
                )

                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", errors="replace")
                    # Get error-related lines (filter out build config spam)
                    error_lines = [
                        line
                        for line in stderr.split("\n")
                        if any(
                            kw in line.lower()
                            for kw in [
                                "error",
                                "failed",
                                "cannot",
                                "no such",
                                "rtmp",
                                "tls",
                                "ssl",
                            ]
                        )
                    ]
                    if error_lines:
                        msg = f"FFmpeg failed: {'; '.join(error_lines[:5])}"
                    else:
                        # Fallback: show first 500 chars (actual error usually there)
                        msg = f"FFmpeg failed: {stderr[:500]}"
                    raise RuntimeError(msg)

                # Read the extracted frame
                with open(tmp_path, "rb") as f:
                    return f.read()
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        return await asyncio.to_thread(_run_ffmpeg)

    async def _extract_frame_download(
        self,
        camera_id: str,
        channel: int,
        target_file: VOD_file,
        timestamp: datetime,
        file_start: datetime,
    ) -> bytes:
        """Extract frame by downloading the video file and using FFmpeg.

        Uses the direct CGI Download API with curl, which is more reliable
        than the library's streaming approach that has TLS timeout issues.
        """
        file_name = target_file.file_name
        # Use naive timestamps to avoid DST timezone mismatches (same fix as FFmpeg)
        ts_naive = timestamp.replace(tzinfo=None)
        fs_naive = file_start.replace(tzinfo=None)
        offset_seconds = max(0, (ts_naive - fs_naive).total_seconds())
        config = self._cameras[camera_id]

        # Retry logic for downloading
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                logger.info(
                    "Attempt %d/3: Getting token for %s", attempt + 1, camera_id
                )

                # Get authentication token via Login API
                host = await self._get_or_create_host(camera_id)
                token = host._token  # noqa: SLF001 - accessing private for direct API

                if not token:
                    raise RuntimeError("Failed to get authentication token")

                # Build download URL
                protocol = "https" if config.use_https else "http"
                logger.debug(
                    "Config: use_https=%s, port=%s, effective_port=%s",
                    config.use_https,
                    config.port,
                    config.effective_port,
                )
                download_url = (
                    f"{protocol}://{config.host}:{config.effective_port}/cgi-bin/api.cgi"
                    f"?cmd=Download&source={file_name}&token={token}"
                )

                # Log the download URL (without token for security)
                safe_download_url = (
                    download_url.split("&token=", maxsplit=1)[0] + "&token=REDACTED"
                )
                logger.info(
                    "Attempt %d/3: Downloading VOD via CGI: %s",
                    attempt + 1,
                    safe_download_url,
                )

                # Download using subprocess curl (more reliable for camera's TLS)
                def _download_with_curl(url: str = download_url) -> bytes:
                    result = subprocess.run(
                        [
                            "curl",
                            "-sk",  # Silent, insecure (skip cert verify)
                            "--max-time",
                            "60",  # 60 second timeout (some cameras are slow)
                            url,
                        ],
                        capture_output=True,
                        timeout=90,
                        check=False,
                    )
                    if result.returncode != 0:
                        stderr = result.stderr.decode("utf-8", errors="replace")
                        raise RuntimeError(f"curl failed: {stderr[:500]}")
                    return result.stdout

                video_data = await asyncio.to_thread(_download_with_curl)
                logger.info("Downloaded video: %d bytes", len(video_data))
                # Debug: Check if we got actual video data or an error response
                if len(video_data) < 1000:
                    logger.warning(
                        "Downloaded data seems too small, first 200 bytes: %s",
                        video_data[:200],
                    )

                # Extract frame with FFmpeg from downloaded video
                def _extract_frame(data: bytes = video_data) -> bytes:
                    with tempfile.NamedTemporaryFile(
                        suffix=".mp4", delete=False
                    ) as video_tmp:
                        video_path = video_tmp.name
                        video_tmp.write(data)

                    with tempfile.NamedTemporaryFile(
                        suffix=".jpg", delete=False
                    ) as frame_tmp:
                        frame_path = frame_tmp.name

                    try:
                        ffmpeg_cmd = [
                            "ffmpeg",
                            "-y",
                            "-ss",
                            str(offset_seconds),
                            "-i",
                            video_path,
                            "-vframes",
                            "1",
                            "-q:v",
                            "2",
                            "-f",
                            "image2",
                            frame_path,
                        ]
                        ffmpeg_result = subprocess.run(
                            ffmpeg_cmd,
                            capture_output=True,
                            timeout=60,
                            check=False,
                        )
                        if ffmpeg_result.returncode != 0:
                            stderr = ffmpeg_result.stderr.decode(
                                "utf-8", errors="replace"
                            )
                            msg = f"FFmpeg failed: {stderr[-500:]}"
                            raise RuntimeError(msg)

                        # Read extracted frame
                        if (
                            not os.path.exists(frame_path)
                            or os.path.getsize(frame_path) == 0
                        ):
                            msg = "FFmpeg produced no output"
                            raise RuntimeError(msg)

                        with open(frame_path, "rb") as f:
                            return f.read()
                    finally:
                        if os.path.exists(video_path):
                            os.unlink(video_path)
                        if os.path.exists(frame_path):
                            os.unlink(frame_path)

                frame_data = await asyncio.to_thread(_extract_frame)
                logger.info("Successfully extracted frame: %d bytes", len(frame_data))
                return frame_data

            except (
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientError,
                ReolinkConnectionError,
                RuntimeError,
            ) as e:
                last_error = e
                logger.warning(
                    "Frame extraction failed on attempt %d/3: %s",
                    attempt + 1,
                    e,
                )
                await self._invalidate_host(camera_id)
                if attempt < 2:
                    await asyncio.sleep(5)  # Give camera more time to recover

        msg = f"Frame extraction failed after 3 download attempts: {last_error}"
        raise RuntimeError(msg) from last_error

    async def get_frames_batch(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
        interval_seconds: int = 900,
        max_frames: int = 10,
    ) -> list[FrameWithTimestamp]:
        """Extract frames at regular intervals for binary search.

        Frames are extracted in parallel for better performance, with a limit
        on concurrent extractions to avoid overwhelming the camera.

        Args:
            camera_id: ID of the camera.
            start_time: Start of the time range (UTC).
            end_time: End of the time range (UTC).
            interval_seconds: Seconds between each frame (default 15 minutes).
            max_frames: Maximum number of frames to return.

        Returns:
            List of FrameWithTimestamp objects with timestamps and JPEG bytes.
        """
        self._validate_camera_id(camera_id)

        # Calculate timestamps
        current = start_time
        timestamps: list[datetime] = []
        while current <= end_time and len(timestamps) < max_frames:
            timestamps.append(current)
            current += timedelta(seconds=interval_seconds)

        # Limit concurrent extractions to avoid overwhelming the camera
        # Using 3 concurrent downloads balances speed vs camera load
        semaphore = asyncio.Semaphore(3)

        async def extract_with_semaphore(
            ts: datetime,
        ) -> FrameWithTimestamp | None:
            async with semaphore:
                try:
                    jpeg_bytes = await self.get_frame(camera_id, ts)
                    return FrameWithTimestamp(
                        timestamp=ts, jpeg_bytes=jpeg_bytes, camera_id=camera_id
                    )
                except (ValueError, RuntimeError) as e:
                    # Skip timestamps where no recording exists or extraction fails
                    logger.warning("Could not extract frame at %s: %s", ts, e)
                    return None

        # Extract frames in parallel
        results = await asyncio.gather(
            *(extract_with_semaphore(ts) for ts in timestamps)
        )

        # Filter out failed extractions and maintain timestamp order
        return [frame for frame in results if frame is not None]

    async def get_live_snapshot(self, camera_id: str) -> bytes:
        """Get a live snapshot (current frame) from the camera.

        This uses a simpler HTTP endpoint than VOD playback and is useful for:
        1. Testing basic camera connectivity
        2. Getting current state of a camera
        3. Quick checks without needing to search recordings

        Args:
            camera_id: Configured camera identifier.

        Returns:
            JPEG image bytes.

        Raises:
            ValueError: If camera_id is not configured.
            RuntimeError: If snapshot could not be retrieved.
        """
        self._validate_camera_id(camera_id)
        config = self._cameras[camera_id]
        channel = config.channel

        host = await self._get_or_create_host(camera_id)
        logger.info(
            "Getting live snapshot from camera %s channel %d", camera_id, channel
        )

        try:
            snapshot = await host.get_snapshot(channel)
            if snapshot is None:
                msg = f"Camera {camera_id}: get_snapshot returned None"
                raise RuntimeError(msg)

            logger.info(
                "Got live snapshot from camera %s: %d bytes", camera_id, len(snapshot)
            )
            return snapshot

        except Exception as e:
            logger.exception("Error getting live snapshot from camera %s", camera_id)
            msg = f"Failed to get live snapshot: {e}"
            raise RuntimeError(msg) from e

    async def close(self) -> None:
        """Cleanup connections and resources."""
        for camera_id, host in self._hosts.items():
            try:
                async with self._locks[camera_id]:
                    await host.logout()
            except Exception:
                logger.exception("Error closing host for camera %s", camera_id)

        self._hosts.clear()


def get_cameras_from_env() -> dict[str, ReolinkCameraConfigDict] | None:
    """Read camera configuration from REOLINK_CAMERAS environment variable.

    Expected format (JSON):
    {
        "camera_id": {
            "host": "192.168.1.100",
            "username": "admin",
            "password": "secret",
            "name": "Front Door",
            "port": 443,
            "use_https": true,
            "channel": 0
        }
    }

    Returns:
        Dict of camera configs, or None if env var not set.

    Raises:
        ValueError: If env var contains invalid JSON.
    """
    env_value = os.environ.get(REOLINK_CAMERAS_ENV)
    if not env_value:
        return None

    try:
        config = json.loads(env_value)
        if not isinstance(config, dict):
            msg = f"{REOLINK_CAMERAS_ENV} must be a JSON object"
            raise ValueError(msg)
        return config
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in {REOLINK_CAMERAS_ENV}: {e}"
        raise ValueError(msg) from e


def create_reolink_backend(
    cameras_config: dict[str, ReolinkCameraItemConfig] | None = None,
) -> ReolinkBackend | None:
    """Create ReolinkBackend from typed config or environment variable.

    Configuration priority:
    1. cameras_config argument (if provided and non-empty)
    2. REOLINK_CAMERAS environment variable (JSON format)

    Args:
        cameras_config: Optional dict mapping camera_id to ReolinkCameraItemConfig
            Pydantic models with typed fields (host, username, password, etc.)

    Returns:
        ReolinkBackend instance, or None if:
        - reolink_aio is not available
        - No camera configuration provided (neither arg nor env var)

    Example:
        >>> # From typed config (normal usage from config.yaml)
        >>> # cameras_config comes from CameraConfig.cameras_config

        >>> # From environment variable (fallback)
        >>> # export REOLINK_CAMERAS='{"cam1": {"host": "...", ...}}'
        >>> backend = create_reolink_backend()
    """
    cameras: dict[str, ReolinkCameraConfig] = {}

    # Use provided typed config
    if cameras_config:
        for camera_id, config in cameras_config.items():
            cameras[camera_id] = ReolinkCameraConfig(
                host=config.host,
                username=config.username,
                password=config.password,
                port=config.effective_port,
                use_https=config.use_https,
                channel=config.channel,
                name=config.name,
                prefer_download=getattr(config, "prefer_download", False),
            )
    else:
        # Fall back to environment variable (returns untyped dicts)
        try:
            env_config = get_cameras_from_env()
        except ValueError:
            logger.exception("Failed to parse camera config from environment")
            return None

        if env_config:
            for camera_id, config in env_config.items():
                cameras[camera_id] = ReolinkCameraConfig(
                    host=config["host"],
                    username=config["username"],
                    password=config["password"],
                    port=config.get("port"),  # None = auto (443 for HTTPS, 80 for HTTP)
                    use_https=config.get("use_https", True),
                    channel=config.get("channel", 0),
                    name=config.get("name"),
                    prefer_download=config.get("prefer_download", False),
                )

    if not cameras:
        logger.debug(
            "No camera configuration provided (neither config dict nor %s env var)",
            REOLINK_CAMERAS_ENV,
        )
        return None

    logger.info(
        "Created Reolink backend with %d camera(s): %s",
        len(cameras),
        ", ".join(cameras.keys()),
    )
    return ReolinkBackend(cameras)
