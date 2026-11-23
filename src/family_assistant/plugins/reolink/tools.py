"""Camera tools for investigating security camera footage.

This module provides tools for the LLM to query camera history, search for events,
and extract frames for visual analysis using binary search methodology.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from family_assistant.plugins.reolink.instance import ReolinkInstance
from family_assistant.plugins.reolink.judge import LlmFrameJudge, jev_frame_judge
from family_assistant.plugins.reolink.search import (
    find_changes,
    judge_frames,
    plan_sample_times,
    sample_frames,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

# Threshold for warning about old dates (likely model confusion about current date)
OLD_DATE_THRESHOLD = timedelta(days=30)

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from family_assistant.plugins.reolink.judge import FrameJudge
    from family_assistant.plugins.reolink.protocol import CameraBackend
    from family_assistant.plugins.reolink.search import Change, JudgedFrame
    from family_assistant.tools.metadata import ToolImplementation
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Attaching every matching frame would put a scan's worth of images back into
# the turn's context, which is the cost the judge exists to avoid.
MAX_SCAN_ATTACHMENTS = 6
MAX_SCAN_FRAMES = 50
MAX_CHANGE_FRAMES = 40
_CHANGE_COARSE_FRAMES = 12
_MAX_CHANGES = 3
# The bar a frame judged by the turn's model must clear. Jev's comes from
# ``typesafe.camera_frames``, since it is tuned against Jev's probabilities.
_LLM_JUDGE_THRESHOLD = 0.5

# Tool Definitions
CAMERA_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "list_cameras",
            "description": (
                "List all configured security cameras with their status. "
                "Returns camera IDs, names, status (online/offline), and backend type. "
                "Use this first to discover available cameras before querying events or frames."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_camera_events",
            "description": (
                "Search for AI detection events (person, vehicle, pet, motion) on a camera "
                "within a time range. Returns events with timestamps, types, and confidence levels.\n\n"
                "This is typically the FIRST STEP in investigating 'what happened' questions. "
                "Use the results to identify time periods of interest, then use get_camera_frames_batch "
                "or get_camera_frame to visually examine those periods.\n\n"
                "Example: To find when chickens escaped, search for 'pet' events on the coop camera."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start of time range in LOCAL TIME (e.g., '2024-01-15T08:00:00' or '2024-01-15T08:00'). Use local time, not UTC.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in LOCAL TIME (e.g., '2024-01-15T18:00:00'). Use local time, not UTC.",
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional filter for event types. Valid types: 'person', 'vehicle', 'pet', 'motion'. "
                            "If not specified, returns all event types."
                        ),
                    },
                },
                "required": ["camera_id", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_frame",
            "description": (
                "Get a single frame/thumbnail from a camera at a specific timestamp. "
                "Returns a JPEG image attachment.\n\n"
                "Use this to examine a SPECIFIC moment identified from events or batch frame analysis. "
                "The timestamp should be within a recorded period (use get_camera_recordings to check)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                    "timestamp": {
                        "type": "string",
                        "description": "Exact timestamp in LOCAL TIME (e.g., '2024-01-15T14:30:00'). Use local time, not UTC.",
                    },
                },
                "required": ["camera_id", "timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_frames_batch",
            "description": (
                "Get frames spread over the recorded footage in a range, every one "
                "attached as an image for you to look at. Frames come from inside "
                "recordings, so a camera that records on motion yields a frame per "
                "burst of activity rather than empty gaps.\n\n"
                "Every frame lands in your context, which is expensive: to find "
                "frames matching something use scan_camera_frames, and to find when "
                "something changed use find_camera_change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start of time range in LOCAL TIME (e.g., '2024-01-15T08:00:00'). Use local time, not UTC.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in LOCAL TIME (e.g., '2024-01-15T18:00:00'). Use local time, not UTC.",
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Minutes between frames where the camera recorded continuously (default: 15)",
                    },
                    "max_frames": {
                        "type": "integer",
                        "description": "Maximum number of frames to return (default: 10)",
                    },
                },
                "required": ["camera_id", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_recordings",
            "description": (
                "List available recording segments in a time range. "
                "Returns recording start/end times, filenames, and sizes.\n\n"
                "Use this to verify footage availability before requesting frames, "
                "or to identify gaps in recording coverage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start of time range in LOCAL TIME (e.g., '2024-01-15T08:00:00'). Use local time, not UTC.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in LOCAL TIME (e.g., '2024-01-15T18:00:00'). Use local time, not UTC.",
                    },
                },
                "required": ["camera_id", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_camera_snapshot",
            "description": (
                "Get a LIVE snapshot showing the CURRENT state of the camera. "
                "Returns a real-time JPEG image of what the camera sees RIGHT NOW.\n\n"
                "Use this for:\n"
                "1. Checking the current state of a camera\n"
                "2. Testing camera connectivity\n"
                "3. Quick views without needing to search recordings\n\n"
                "NOTE: This shows what's happening NOW, not a historical recording."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                },
                "required": ["camera_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_camera_frames",
            "description": (
                "Judge frames across a time range against a condition, and return "
                "a timeline of how likely each frame is to satisfy it.\n\n"
                "Frames are sampled from inside the camera's recordings and each is "
                "judged by a fast classifier, so scanning hours of footage costs "
                "seconds. Only the frames most likely to satisfy the condition are "
                "attached as images; the rest appear as timestamps and "
                "probabilities in the timeline.\n\n"
                "Use this to find WHICH moments show something. To find WHEN "
                "something started or stopped being true, use find_camera_change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start of time range in LOCAL TIME (e.g., '2024-01-15T08:00:00'). Use local time, not UTC.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in LOCAL TIME (e.g., '2024-01-15T18:00:00'). Use local time, not UTC.",
                    },
                    "condition": {
                        "type": "string",
                        "description": (
                            "A statement about a single frame that is either true or "
                            "false, kept to one simple thing. Examples: 'A person is "
                            "visible.', 'A vehicle is in the driveway.', 'A parcel is "
                            "on the doorstep.' Ask about one thing at a time: split "
                            "'a person carrying a parcel' into separate scans."
                        ),
                    },
                    "interval_seconds": {
                        "type": "number",
                        "description": "Seconds between frames where the camera recorded continuously (default: 300). Motion-triggered recordings get a frame each regardless.",
                    },
                    "max_frames": {
                        "type": "integer",
                        "description": f"Maximum number of frames to judge (default: 20, max: {MAX_SCAN_FRAMES}).",
                    },
                },
                "required": ["camera_id", "start_time", "end_time", "condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_camera_change",
            "description": (
                "Find WHEN a condition about the scene started or stopped being "
                "true within a time range, narrowed to a precision you choose.\n\n"
                "The tool judges frames spread over the range, finds where the "
                "answer flips, and bisects each flip in recorded footage until the "
                "last frame before and the first frame after are within the "
                "precision. It returns those two frames as images.\n\n"
                "Phrase the condition as a state that holds for a while, not as "
                "the event itself: 'A parcel is on the doorstep.' finds when a "
                "parcel arrived or was taken; 'The gate is open.' finds when it was "
                "opened or closed. Changes during gaps in recording are reported at "
                "the gap's edges."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {
                        "type": "string",
                        "description": "Camera ID from list_cameras",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start of time range in LOCAL TIME (e.g., '2024-01-15T08:00:00'). Use local time, not UTC.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in LOCAL TIME (e.g., '2024-01-15T18:00:00'). Use local time, not UTC.",
                    },
                    "condition": {
                        "type": "string",
                        "description": "A statement about a single frame that is either true or false, describing a state rather than an event.",
                    },
                    "precision_seconds": {
                        "type": "number",
                        "description": "How close the before and after frames must be (default: 60).",
                    },
                },
                "required": ["camera_id", "start_time", "end_time", "condition"],
            },
        },
    },
]


def _parse_local_time(time_str: str, timezone: ZoneInfo) -> datetime:
    """Parse a time string, assuming local timezone if not specified.

    Args:
        time_str: ISO 8601 formatted time string, with or without timezone.
        timezone: The local timezone to use if not specified in time_str.

    Returns:
        A timezone-aware datetime object.

    Raises:
        ValueError: If the time string cannot be parsed.
    """
    # Handle 'Z' suffix (UTC)
    if time_str.endswith("Z"):
        time_str = time_str[:-1] + "+00:00"

    dt = datetime.fromisoformat(time_str)

    # If no timezone info, assume local timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone)
        logger.debug(f"Time '{time_str}' lacks timezone, assuming {timezone}")

    # Cameras are searched by wall clock, so a time given in another zone is
    # moved onto the household's before it reaches a backend.
    return dt.astimezone(timezone)


def _to_local_isoformat(dt: datetime, timezone: ZoneInfo) -> str:
    """Convert a datetime to ISO format in the user's local timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(timezone).isoformat()


def _camera_backend(exec_context: ToolExecutionContext) -> CameraBackend | None:
    """The camera backend of the Reolink instance the turn's profile selected."""
    if exec_context.plugins is None:
        return None
    instance = exec_context.plugins.get(ReolinkInstance)
    return None if instance is None else instance.backend


def _not_configured() -> ToolResult:
    return ToolResult(
        data={"error": "Camera backend not configured: no cameras for this profile."}
    )


async def list_cameras_tool(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """List all configured cameras with status."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()

    try:
        cameras = await camera_backend.list_cameras()
        camera_list = [
            {
                "id": cam.id,
                "name": cam.name,
                "status": cam.status,
                "backend": cam.backend,
            }
            for cam in cameras
        ]
        return ToolResult(data={"cameras": camera_list, "count": len(cameras)})
    except Exception as e:
        logger.exception("Error listing cameras")
        return ToolResult(data={"error": f"Failed to list cameras: {e}"})


async def search_camera_events_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
    event_types: list[str] | None = None,
) -> ToolResult:
    """Search for AI detection events in a time range."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()

    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone)
        end_dt = _parse_local_time(end_time, exec_context.timezone)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    # Warn if dates are suspiciously old (likely model confused about current date)
    now = datetime.now(UTC)
    date_warning = ""
    if now - end_dt > OLD_DATE_THRESHOLD:
        local_now = now.astimezone(exec_context.timezone)
        date_warning = (
            f" WARNING: These dates are more than {OLD_DATE_THRESHOLD.days} days in the past. "
            f"Current time is {local_now.strftime('%Y-%m-%d %H:%M %Z')}. "
            "Did you mean to search a more recent time range?"
        )

    try:
        events = await camera_backend.search_events(
            camera_id=camera_id,
            start_time=start_dt,
            end_time=end_dt,
            event_types=event_types,
        )
        event_list = [
            {
                "camera_id": evt.camera_id,
                "start_time": _to_local_isoformat(
                    evt.start_time, exec_context.timezone
                ),
                "end_time": _to_local_isoformat(evt.end_time, exec_context.timezone)
                if evt.end_time
                else None,
                "event_type": evt.event_type,
                "confidence": evt.confidence,
                "metadata": evt.metadata,
            }
            for evt in events
        ]
        return ToolResult(
            data={
                "events": event_list,
                "count": len(events),
                "warning": date_warning.strip() if date_warning else None,
            }
        )
    except ValueError as e:
        # Expected errors (invalid camera ID, etc.) - return cleanly without traceback
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error searching camera events")
        return ToolResult(data={"error": f"Failed to search events: {e}"})


async def get_camera_frame_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    timestamp: str,
) -> ToolResult:
    """Get a single frame at a specific timestamp."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()

    try:
        ts = _parse_local_time(timestamp, exec_context.timezone)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        frame_bytes = await camera_backend.get_frame(
            camera_id=camera_id,
            timestamp=ts,
        )
        return ToolResult(
            data={"camera_id": camera_id, "timestamp": timestamp},
            attachments=[
                ToolAttachment(
                    mime_type="image/jpeg",
                    content=frame_bytes,
                    description=f"Camera frame at {timestamp}",
                )
            ],
        )
    except ValueError as e:
        # Expected errors (invalid camera ID, no recording, etc.) - return cleanly
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error getting camera frame")
        return ToolResult(data={"error": f"Failed to get frame: {e}"})


async def get_camera_frames_batch_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
    interval_minutes: int = 15,
    max_frames: int = 10,
) -> ToolResult:
    """Get frames spread over the recordings in a range, every one attached."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()

    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone)
        end_dt = _parse_local_time(end_time, exec_context.timezone)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        recordings = await camera_backend.get_recordings(camera_id, start_dt, end_dt)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})
    times = plan_sample_times(
        recordings,
        start_dt,
        end_dt,
        interval=timedelta(minutes=max(interval_minutes, 1)),
        max_frames=max(max_frames, 1),
    )
    frames = [
        frame
        for frame in await sample_frames(camera_backend, camera_id, times)
        if frame.jpeg_bytes is not None
    ]
    if not frames:
        return ToolResult(data={"camera_id": camera_id, "timestamps": [], "count": 0})
    timestamps = [
        _to_local_isoformat(frame.timestamp, exec_context.timezone) for frame in frames
    ]
    return ToolResult(
        data={"camera_id": camera_id, "timestamps": timestamps, "count": len(frames)},
        attachments=[
            ToolAttachment(
                mime_type="image/jpeg",
                content=frame.jpeg_bytes,
                description=f"Frame {i + 1}/{len(frames)} at {timestamp}",
            )
            for i, (frame, timestamp) in enumerate(zip(frames, timestamps, strict=True))
            if frame.jpeg_bytes is not None
        ],
    )


async def get_camera_recordings_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
) -> ToolResult:
    """List available recording segments in a time range."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()

    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone)
        end_dt = _parse_local_time(end_time, exec_context.timezone)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        recordings = await camera_backend.get_recordings(
            camera_id=camera_id,
            start_time=start_dt,
            end_time=end_dt,
        )
        recording_list = [
            {
                "camera_id": rec.camera_id,
                "start_time": _to_local_isoformat(
                    rec.start_time, exec_context.timezone
                ),
                "end_time": _to_local_isoformat(rec.end_time, exec_context.timezone),
                "filename": rec.filename,
                "size_bytes": rec.size_bytes,
                "duration_seconds": (rec.end_time - rec.start_time).total_seconds(),
            }
            for rec in recordings
        ]
        total_duration = sum(r["duration_seconds"] for r in recording_list)
        return ToolResult(
            data={
                "recordings": recording_list,
                "count": len(recordings),
                "total_duration_hours": round(total_duration / 3600, 1),
            }
        )
    except ValueError as e:
        # Expected errors (invalid camera ID, etc.) - return cleanly
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error getting camera recordings")
        return ToolResult(data={"error": f"Failed to get recordings: {e}"})


async def get_live_camera_snapshot_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
) -> ToolResult:
    """Get a live snapshot showing the current state of the camera."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()

    try:
        snapshot_bytes = await camera_backend.get_live_snapshot(
            camera_id=camera_id,
        )
        return ToolResult(
            data={"camera_id": camera_id, "type": "live_snapshot"},
            attachments=[
                ToolAttachment(
                    mime_type="image/jpeg",
                    content=snapshot_bytes,
                    description=f"Live snapshot from {camera_id}",
                )
            ],
        )
    except ValueError as e:
        # Expected errors (invalid camera ID, etc.) - return cleanly
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error getting live camera snapshot")
        return ToolResult(data={"error": f"Failed to get live snapshot: {e}"})


# --- Searching footage with a frame judge ---


@dataclass(frozen=True, slots=True)
class _Judging:
    judge: FrameJudge
    threshold: float


def _frame_judging(exec_context: ToolExecutionContext) -> _Judging | None:
    """Jev when the deployment has it for camera frames, else the turn's model."""
    service = exec_context.processing_service
    if service is not None:
        camera_frames = service.app_config.typesafe.camera_frames
        jev = jev_frame_judge(service.jev_client, camera_frames)
        if jev is not None:
            return _Judging(jev, camera_frames.threshold)
    if exec_context.llm_client is not None:
        return _Judging(
            LlmFrameJudge(exec_context.llm_client, name="turn model"),
            _LLM_JUDGE_THRESHOLD,
        )
    return None


def _no_judge() -> ToolResult:
    return ToolResult(data={"error": "No frame judge is available for this search."})


def _timeline_entry(
    frame: JudgedFrame, timezone: ZoneInfo
) -> dict[str, str | float | None]:
    entry: dict[str, str | float | None] = {
        "timestamp": _to_local_isoformat(frame.timestamp, timezone),
        "probability": (
            round(frame.probability, 3) if frame.probability is not None else None
        ),
    }
    if frame.error is not None:
        entry["error"] = frame.error
    return entry


def _frame_attachment(
    frame: JudgedFrame, timezone: ZoneInfo, label: str
) -> ToolAttachment:
    assert frame.jpeg_bytes is not None
    probability = f"p={frame.probability:.2f}" if frame.probability is not None else ""
    return ToolAttachment(
        mime_type="image/jpeg",
        content=frame.jpeg_bytes,
        description=(
            f"[{_to_local_isoformat(frame.timestamp, timezone)}] {label} {probability}"
        ).strip(),
    )


async def scan_camera_frames_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
    condition: str,
    interval_seconds: float = 300,
    max_frames: int = 20,
) -> ToolResult:
    """Judge frames across a range against a condition, returning a timeline."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()
    judging = _frame_judging(exec_context)
    if judging is None:
        return _no_judge()
    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone)
        end_dt = _parse_local_time(end_time, exec_context.timezone)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})
    if end_dt < start_dt:
        return ToolResult(data={"error": "end_time must not be before start_time."})
    interval = timedelta(seconds=max(interval_seconds, 1.0))
    max_frames = min(max(max_frames, 1), MAX_SCAN_FRAMES)

    try:
        recordings = await camera_backend.get_recordings(camera_id, start_dt, end_dt)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})
    times = plan_sample_times(
        recordings, start_dt, end_dt, interval=interval, max_frames=max_frames
    )
    if not times:
        return ToolResult(
            data={
                "camera_id": camera_id,
                "condition": condition,
                "frames_scanned": 0,
                "matches_found": 0,
                "message": "The camera recorded nothing in this time range.",
            }
        )
    frames = await judge_frames(
        judging.judge,
        await sample_frames(camera_backend, camera_id, times),
        condition,
    )
    judged = [frame for frame in frames if frame.probability is not None]
    matches = sorted(
        (frame for frame in judged if (frame.probability or 0.0) >= judging.threshold),
        key=lambda frame: frame.probability or 0.0,
        reverse=True,
    )
    timezone = exec_context.timezone
    data: dict[str, object] = {
        "camera_id": camera_id,
        "condition": condition,
        "judge": judging.judge.name,
        "threshold": judging.threshold,
        "recordings_in_range": len(recordings),
        "frames_scanned": len(frames),
        "frames_judged": len(judged),
        "matches_found": len(matches),
        "timeline": [_timeline_entry(frame, timezone) for frame in frames],
    }
    unjudged = len(frames) - len(judged)
    if unjudged:
        data["warning"] = f"{unjudged} frame(s) could not be extracted or judged"

    if matches:
        shown = matches[:MAX_SCAN_ATTACHMENTS]
        text = (
            f"{len(matches)} of {len(judged)} judged frames satisfy '{condition}'. "
            f"Attached: the {len(shown)} most likely, highest first."
        )
        attachments = [_frame_attachment(frame, timezone, "match") for frame in shown]
    else:
        nearest = sorted(judged, key=lambda frame: frame.probability or 0.0)[-3:]
        text = (
            f"No frame of {len(judged)} judged satisfies '{condition}'. Closest: "
            + ", ".join(
                f"{_to_local_isoformat(frame.timestamp, timezone)} "
                f"(p={frame.probability:.2f})"
                for frame in reversed(nearest)
            )
            if nearest
            else f"No frame could be judged for '{condition}'."
        )
        attachments = []
    return ToolResult(
        text=text,
        data=data,
        attachments=attachments or None,
    )


async def find_camera_change_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
    condition: str,
    precision_seconds: float = 60,
) -> ToolResult:
    """Find when a condition's answer flips in a range, to within a precision."""
    camera_backend = _camera_backend(exec_context)
    if camera_backend is None:
        return _not_configured()
    judging = _frame_judging(exec_context)
    if judging is None:
        return _no_judge()
    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone)
        end_dt = _parse_local_time(end_time, exec_context.timezone)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})
    if end_dt <= start_dt:
        return ToolResult(data={"error": "end_time must be after start_time."})

    try:
        recordings = await camera_backend.get_recordings(camera_id, start_dt, end_dt)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})
    if not recordings:
        return ToolResult(
            data={
                "camera_id": camera_id,
                "condition": condition,
                "changes": [],
                "message": "The camera recorded nothing in this time range.",
            }
        )
    search = await find_changes(
        backend=camera_backend,
        judge=judging.judge,
        camera_id=camera_id,
        recordings=recordings,
        start_time=start_dt,
        end_time=end_dt,
        condition=condition,
        threshold=judging.threshold,
        precision=timedelta(seconds=max(precision_seconds, 1.0)),
        coarse_frames=_CHANGE_COARSE_FRAMES,
        max_changes=_MAX_CHANGES,
        frame_budget=MAX_CHANGE_FRAMES,
    )
    timezone = exec_context.timezone

    def describe(change: Change) -> dict[str, object]:
        assert change.after.probability is not None
        became = change.after.probability >= judging.threshold
        entry: dict[str, object] = {
            "became": "true" if became else "false",
            "last_before": _timeline_entry(change.before, timezone),
            "first_after": _timeline_entry(change.after, timezone),
            "resolved": change.resolved,
        }
        if change.reason is not None:
            entry["stopped_because"] = change.reason
        return entry

    changes = [describe(change) for change in search.changes]
    data: dict[str, object] = {
        "camera_id": camera_id,
        "condition": condition,
        "judge": judging.judge.name,
        "threshold": judging.threshold,
        "frames_judged": search.frames_judged,
        "coarse_timeline": [
            _timeline_entry(frame, timezone) for frame in search.coarse
        ],
        "changes": changes,
    }
    if not search.changes:
        judged = [frame for frame in search.coarse if frame.probability is not None]
        if not judged:
            text = f"No frame could be judged for '{condition}'."
        else:
            holds = sum(
                1 for frame in judged if (frame.probability or 0.0) >= judging.threshold
            )
            text = (
                f"No change found: '{condition}' was "
                + (
                    "true"
                    if holds == len(judged)
                    else "false"
                    if holds == 0
                    else "mixed but never flipped between adjacent frames"
                )
                + f" in all {len(judged)} frames judged across the range."
            )
        return ToolResult(text=text, data=data)

    lines = []
    attachments: list[ToolAttachment] = []
    for change, entry in zip(search.changes, changes, strict=True):
        before = _to_local_isoformat(change.before.timestamp, timezone)
        after = _to_local_isoformat(change.after.timestamp, timezone)
        lines.append(
            f"'{condition}' became {entry['became']} between {before} and {after}"
            + ("" if change.resolved else f" (not narrowed further: {change.reason})")
        )
        attachments.append(_frame_attachment(change.before, timezone, "before"))
        attachments.append(_frame_attachment(change.after, timezone, "after"))
    return ToolResult(text="\n".join(lines), data=data, attachments=attachments)


def _tool(
    name: str, implementation: ToolImplementation, *tags: ToolTag
) -> ToolRegistration:
    definitions = {
        definition["function"]["name"]: definition
        for definition in CAMERA_TOOLS_DEFINITION
    }
    return ToolRegistration(
        definition=definitions[name],
        implementation=implementation,
        metadata=make_local_tool_metadata(tags),
    )


CAMERA_TOOLS: tuple[ToolRegistration, ...] = (
    _tool(
        "list_cameras",
        list_cameras_tool,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    *(
        _tool(
            name,
            implementation,
            ToolTag.READ_ONLY,
            ToolTag.SENSITIVE_DATA,
            ToolTag.CAMERA,
            ToolTag.MEDIA,
            ToolTag.OUTPUT_TRUSTED,
        )
        for name, implementation in (
            ("search_camera_events", search_camera_events_tool),
            ("get_camera_frame", get_camera_frame_tool),
            ("get_camera_frames_batch", get_camera_frames_batch_tool),
            ("get_camera_recordings", get_camera_recordings_tool),
            ("get_live_camera_snapshot", get_live_camera_snapshot_tool),
            ("scan_camera_frames", scan_camera_frames_tool),
            ("find_camera_change", find_camera_change_tool),
        )
    ),
)
