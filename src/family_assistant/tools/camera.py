"""Camera tools for investigating security camera footage.

This module provides tools for the LLM to query camera history, search for events,
and extract frames for visual analysis using binary search methodology.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
# ast-grep-ignore: no-dict-any - Tool definitions require dict[str, Any] for JSON schema
CAMERA_TOOLS_DEFINITION: list[dict[str, Any]] = [
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
                        "description": "Start of time range (ISO 8601 format, e.g., '2024-01-15T08:00:00Z')",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range (ISO 8601 format, e.g., '2024-01-15T18:00:00Z')",
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
                        "description": "Exact timestamp to extract frame (ISO 8601 format)",
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
                "Get multiple frames at regular intervals for BINARY SEARCH investigation.\n\n"
                "This is the KEY TOOL for answering 'when did X happen' questions:\n"
                "1. Start with a wide time range (e.g., 6 hours)\n"
                "2. Review the batch frames to identify when the change occurred\n"
                "3. Narrow down to a smaller range and repeat\n"
                "4. Use get_camera_frame for the exact moment once identified\n\n"
                "Default: 15-minute intervals, max 10 frames. Adjust interval_minutes and max_frames as needed."
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
                        "description": "Start of time range (ISO 8601 format)",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range (ISO 8601 format)",
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Minutes between each frame (default: 15)",
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
                        "description": "Start of time range (ISO 8601 format)",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range (ISO 8601 format)",
                    },
                },
                "required": ["camera_id", "start_time", "end_time"],
            },
        },
    },
]


async def list_cameras_tool(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """List all configured cameras with status."""
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        cameras = await exec_context.camera_backend.list_cameras()
        camera_list = [
            {
                "id": cam.id,
                "name": cam.name,
                "status": cam.status,
                "backend": cam.backend,
            }
            for cam in cameras
        ]
        # Include camera IDs in text so LLM knows what IDs to use
        if cameras:
            camera_summary = ", ".join(
                f"'{cam.id}' ({cam.name}, {cam.status})" for cam in cameras
            )
            text = f"Found {len(cameras)} camera(s): {camera_summary}"
        else:
            text = "No cameras configured"
        return ToolResult(
            text=text,
            data={"cameras": camera_list, "count": len(cameras)},
        )
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
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        events = await exec_context.camera_backend.search_events(
            camera_id=camera_id,
            start_time=start_dt,
            end_time=end_dt,
            event_types=event_types,
        )
        event_list = [
            {
                "camera_id": evt.camera_id,
                "start_time": evt.start_time.isoformat(),
                "end_time": evt.end_time.isoformat() if evt.end_time else None,
                "event_type": evt.event_type,
                "confidence": evt.confidence,
                "metadata": evt.metadata,
            }
            for evt in events
        ]
        return ToolResult(
            text=f"Found {len(events)} event(s) on camera '{camera_id}' between {start_time} and {end_time}",
            data={"events": event_list, "count": len(events)},
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
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        frame_bytes = await exec_context.camera_backend.get_frame(
            camera_id=camera_id,
            timestamp=ts,
        )
        return ToolResult(
            text=f"Frame from camera '{camera_id}' at {timestamp}",
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
    """Get multiple frames at regular intervals for binary search."""
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        frames = await exec_context.camera_backend.get_frames_batch(
            camera_id=camera_id,
            start_time=start_dt,
            end_time=end_dt,
            interval_seconds=interval_minutes * 60,
            max_frames=max_frames,
        )

        if not frames:
            return ToolResult(
                text=f"No frames available for camera '{camera_id}' in the specified time range",
                data={"frames": [], "count": 0},
            )

        attachments = [
            ToolAttachment(
                mime_type="image/jpeg",
                content=frame.jpeg_bytes,
                description=f"Frame {i + 1}/{len(frames)} at {frame.timestamp.isoformat()}",
            )
            for i, frame in enumerate(frames)
        ]

        return ToolResult(
            text=f"Retrieved {len(frames)} frame(s) from camera '{camera_id}' ({interval_minutes}min intervals)",
            attachments=attachments,
            data={
                "timestamps": [f.timestamp.isoformat() for f in frames],
                "count": len(frames),
            },
        )
    except ValueError as e:
        # Expected errors (invalid camera ID, etc.) - return cleanly
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error getting camera frames batch")
        return ToolResult(data={"error": f"Failed to get frames: {e}"})


async def get_camera_recordings_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
) -> ToolResult:
    """List available recording segments in a time range."""
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        recordings = await exec_context.camera_backend.get_recordings(
            camera_id=camera_id,
            start_time=start_dt,
            end_time=end_dt,
        )
        recording_list = [
            {
                "camera_id": rec.camera_id,
                "start_time": rec.start_time.isoformat(),
                "end_time": rec.end_time.isoformat(),
                "filename": rec.filename,
                "size_bytes": rec.size_bytes,
                "duration_seconds": (rec.end_time - rec.start_time).total_seconds(),
            }
            for rec in recordings
        ]
        total_duration = sum(r["duration_seconds"] for r in recording_list)
        return ToolResult(
            text=f"Found {len(recordings)} recording segment(s) on camera '{camera_id}' ({total_duration / 3600:.1f} hours total)",
            data={"recordings": recording_list, "count": len(recordings)},
        )
    except ValueError as e:
        # Expected errors (invalid camera ID, etc.) - return cleanly
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error getting camera recordings")
        return ToolResult(data={"error": f"Failed to get recordings: {e}"})
