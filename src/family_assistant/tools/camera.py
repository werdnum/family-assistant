"""Camera tools for investigating security camera footage.

This module provides tools for the LLM to query camera history, search for events,
and extract frames for visual analysis using binary search methodology.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

# Threshold for warning about old dates (likely model confusion about current date)
OLD_DATE_THRESHOLD = timedelta(days=30)

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.content_parts import (
        ImageUrlContentPartDict,
        TextContentPartDict,
    )
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


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
                        "description": "Start of time range in LOCAL TIME (e.g., '2024-01-15T08:00:00'). Use local time, not UTC.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in LOCAL TIME (e.g., '2024-01-15T18:00:00'). Use local time, not UTC.",
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
                "Scan camera frames in parallel with AI-powered per-frame analysis.\n\n"
                "This is the MOST EFFICIENT tool for scanning large time ranges:\n"
                "- Extracts frames at regular intervals\n"
                "- Analyzes EACH frame in PARALLEL with focused AI analysis\n"
                "- Returns ONLY frames matching your query (unless filter_matching=false)\n\n"
                "USE THIS TOOL when:\n"
                "1. Scanning a large time range (hours) for a specific event\n"
                "2. You want to quickly identify WHEN something happened\n"
                "3. You need efficient analysis without manually reviewing all frames\n\n"
                "The tool returns matching frames with descriptions like:\n"
                "- '14:12: Delivery truck visible in driveway'\n"
                "- '14:14: Person walking to door carrying brown package'\n\n"
                "After finding matches, use get_camera_frame for detailed examination."
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
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look for in each frame. Be specific about what constitutes a match. "
                            "Examples: 'person entering the yard', 'package being delivered', "
                            "'car in driveway', 'animal near the fence'"
                        ),
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Minutes between each frame (default: 5). Use smaller intervals for precise timing.",
                    },
                    "max_frames": {
                        "type": "integer",
                        "description": "Maximum number of frames to scan (default: 20). Higher values scan more thoroughly.",
                    },
                    "filter_matching": {
                        "type": "boolean",
                        "description": "If true (default), only return frames that match the query. If false, return all frames with their analysis.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to use for frame analysis (e.g., 'gemini-2.0-flash', 'gpt-4o-mini'). Defaults to the profile's configured model.",
                    },
                },
                "required": ["camera_id", "start_time", "end_time", "query"],
            },
        },
    },
]


def _parse_local_time(time_str: str, timezone_str: str) -> datetime:
    """Parse a time string, assuming local timezone if not specified.

    Args:
        time_str: ISO 8601 formatted time string, with or without timezone.
        timezone_str: The local timezone to use if not specified in time_str.

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
        local_tz = ZoneInfo(timezone_str)
        dt = dt.replace(tzinfo=local_tz)
        logger.debug(f"Time '{time_str}' lacks timezone, assuming {timezone_str}")

    return dt


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
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone_str)
        end_dt = _parse_local_time(end_time, exec_context.timezone_str)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    # Warn if dates are suspiciously old (likely model confused about current date)
    now = datetime.now(UTC)
    date_warning = ""
    if now - end_dt > OLD_DATE_THRESHOLD:
        date_warning = (
            f" WARNING: These dates are more than {OLD_DATE_THRESHOLD.days} days in the past. "
            f"Current time is {now.strftime('%Y-%m-%d %H:%M UTC')}. "
            "Did you mean to search a more recent time range?"
        )

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
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        ts = _parse_local_time(timestamp, exec_context.timezone_str)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    try:
        frame_bytes = await exec_context.camera_backend.get_frame(
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
    """Get multiple frames at regular intervals for binary search."""
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone_str)
        end_dt = _parse_local_time(end_time, exec_context.timezone_str)
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
                data={"camera_id": camera_id, "timestamps": [], "count": 0}
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
            data={
                "camera_id": camera_id,
                "timestamps": [f.timestamp.isoformat() for f in frames],
                "count": len(frames),
            },
            attachments=attachments,
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
        start_dt = _parse_local_time(start_time, exec_context.timezone_str)
        end_dt = _parse_local_time(end_time, exec_context.timezone_str)
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
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    try:
        snapshot_bytes = await exec_context.camera_backend.get_live_snapshot(
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


# --- Parallel Frame Scanning ---

# System prompt for per-frame analysis
_FRAME_ANALYSIS_SYSTEM_PROMPT = """You are analyzing a single frame from a security camera recording.
Your task is to determine if this frame matches the user's query and provide a brief description.

Respond with a JSON object containing:
- "matches_query": true if the frame shows what the user is looking for, false otherwise
- "description": A brief (1-2 sentence) description of what you see in the frame
- "confidence": A number from 0.0 to 1.0 indicating how confident you are in the match
- "detected_objects": A list of key objects/entities visible in the frame

Be specific about what you observe. If the query asks for "person entering yard" and you see a person
already in the yard (not entering), that may or may not match depending on context.

Respond ONLY with valid JSON, no other text."""


@dataclass
class FrameAnalysisResult:
    """Result of analyzing a single frame."""

    timestamp: datetime
    matches_query: bool
    description: str
    confidence: float
    detected_objects: list[str]
    jpeg_bytes: bytes
    error: str | None = None


async def _analyze_single_frame(
    llm_client: LLMInterface,
    jpeg_bytes: bytes,
    timestamp: datetime,
    query: str,
) -> FrameAnalysisResult:
    """Analyze a single frame with the LLM.

    Args:
        llm_client: The LLM client to use for analysis
        jpeg_bytes: The frame image bytes
        timestamp: The timestamp of this frame
        query: What to look for in the frame

    Returns:
        FrameAnalysisResult with the analysis
    """
    try:
        # Encode image as base64 data URL
        b64_image = base64.b64encode(jpeg_bytes).decode("utf-8")
        image_url = f"data:image/jpeg;base64,{b64_image}"

        # Import messages here to avoid circular import
        from family_assistant.llm.messages import (  # noqa: PLC0415
            SystemMessage,
            UserMessage,
        )

        # Create content parts using TypedDicts
        image_part: ImageUrlContentPartDict = {
            "type": "image_url",
            "image_url": {"url": image_url},
        }
        text_part: TextContentPartDict = {
            "type": "text",
            "text": f"Query: {query}\n\nAnalyze this frame and respond with JSON.",
        }

        # Create messages for the LLM
        messages = [
            SystemMessage(content=_FRAME_ANALYSIS_SYSTEM_PROMPT),
            UserMessage(content=[image_part, text_part]),  # type: ignore[list-item]
        ]

        # Call the LLM
        response = await llm_client.generate_response(
            messages=messages,
            tools=None,
            tool_choice=None,
        )

        # Parse the response
        response_text = response.content.strip() if response.content else ""

        # Try to extract JSON from the response
        # Handle case where LLM wraps JSON in markdown code blocks
        if response_text.startswith("```"):
            # Extract content between code fences
            lines = response_text.split("\n")
            json_lines = []
            in_code = False
            for line in lines:
                if line.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    json_lines.append(line)
            response_text = "\n".join(json_lines)

        try:
            # ast-grep-ignore: no-dict-any - JSON parsing produces dynamic types
            result_data: dict[str, Any] = json.loads(response_text)
            return FrameAnalysisResult(
                timestamp=timestamp,
                matches_query=bool(result_data.get("matches_query", False)),
                description=str(result_data.get("description", "No description")),
                confidence=float(result_data.get("confidence", 0.5)),
                detected_objects=list(result_data.get("detected_objects", [])),
                jpeg_bytes=jpeg_bytes,
            )
        except json.JSONDecodeError:
            # If JSON parsing fails, try to infer from text
            matches = "yes" in response_text.lower() or "true" in response_text.lower()
            return FrameAnalysisResult(
                timestamp=timestamp,
                matches_query=matches,
                description=response_text[:200] if response_text else "Analysis failed",
                confidence=0.3,  # Low confidence since we couldn't parse properly
                detected_objects=[],
                jpeg_bytes=jpeg_bytes,
            )

    except Exception as e:
        logger.warning(f"Error analyzing frame at {timestamp}: {e}")
        return FrameAnalysisResult(
            timestamp=timestamp,
            matches_query=False,
            description="",
            confidence=0.0,
            detected_objects=[],
            jpeg_bytes=jpeg_bytes,
            error=str(e),
        )


async def scan_camera_frames_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
    query: str,
    interval_minutes: int = 5,
    max_frames: int = 20,
    filter_matching: bool = True,
    model: str | None = None,
) -> ToolResult:
    """Scan camera frames in parallel with per-frame LLM analysis.

    This tool extracts frames at regular intervals and analyzes each one
    in parallel to efficiently identify frames matching the query.

    Args:
        exec_context: The tool execution context
        camera_id: Camera to scan
        start_time: Start of time range in local time
        end_time: End of time range in local time
        query: What to look for in each frame
        interval_minutes: Minutes between each frame
        max_frames: Maximum number of frames to scan
        filter_matching: If True, only return frames that match the query
        model: Model to use for frame analysis. Defaults to profile's model.

    Returns:
        ToolResult with analysis summary and matching frame attachments
    """
    # Check camera backend
    if not exec_context.camera_backend:
        return ToolResult(
            data={
                "error": "Camera backend not configured. Check camera_config in profile."
            }
        )

    # Check for LLM client (needed for per-frame analysis)
    if not exec_context.processing_service:
        return ToolResult(
            data={"error": "Processing service not available for frame analysis."}
        )

    # Create LLM client - use custom model if specified, otherwise use profile's model
    if model:
        from family_assistant.llm import LLMClientFactory  # noqa: PLC0415

        llm_client = LLMClientFactory.create_client({"model": model})
        logger.info(f"Using custom model for frame analysis: {model}")
    else:
        llm_client = exec_context.processing_service.llm_client

    # Parse time range
    try:
        start_dt = _parse_local_time(start_time, exec_context.timezone_str)
        end_dt = _parse_local_time(end_time, exec_context.timezone_str)
    except ValueError as e:
        return ToolResult(data={"error": f"Invalid timestamp format: {e}"})

    # Validate parameters
    interval_minutes = max(interval_minutes, 1)
    max_frames = max(max_frames, 1)
    max_frames = min(max_frames, 50)  # Cap at reasonable limit

    try:
        # Get frames from camera backend (already parallelized internally)
        logger.info(
            f"Scanning {camera_id} from {start_time} to {end_time} "
            f"(interval={interval_minutes}m, max_frames={max_frames})"
        )

        frames = await exec_context.camera_backend.get_frames_batch(
            camera_id=camera_id,
            start_time=start_dt,
            end_time=end_dt,
            interval_seconds=interval_minutes * 60,
            max_frames=max_frames,
        )

        if not frames:
            return ToolResult(
                data={
                    "camera_id": camera_id,
                    "query": query,
                    "frames_scanned": 0,
                    "matches_found": 0,
                    "message": "No frames found in the specified time range.",
                }
            )

        logger.info(f"Got {len(frames)} frames, starting parallel analysis")

        # Analyze all frames in parallel (provider handles rate limiting)
        analysis_tasks = [
            _analyze_single_frame(
                llm_client=llm_client,
                jpeg_bytes=frame.jpeg_bytes,
                timestamp=frame.timestamp,
                query=query,
            )
            for frame in frames
        ]

        results = await asyncio.gather(*analysis_tasks)

        # Separate successful results and errors
        successful_results = [r for r in results if r.error is None]
        error_count = len(results) - len(successful_results)

        # Filter to matching frames if requested
        if filter_matching:
            matching_results = [r for r in successful_results if r.matches_query]
        else:
            matching_results = successful_results

        # Sort by timestamp
        matching_results.sort(key=lambda r: r.timestamp)

        # Build response data
        # ast-grep-ignore: no-dict-any - Building dynamic result dict
        analysis_summaries: list[dict[str, Any]] = []
        attachments: list[ToolAttachment] = []

        for result in matching_results:
            ts_str = result.timestamp.isoformat()
            analysis_summaries.append({
                "timestamp": ts_str,
                "matches_query": result.matches_query,
                "description": result.description,
                "confidence": result.confidence,
                "detected_objects": result.detected_objects,
            })

            # Include attachments for matching frames (or all if not filtering)
            if filter_matching and result.matches_query:
                attachments.append(
                    ToolAttachment(
                        mime_type="image/jpeg",
                        content=result.jpeg_bytes,
                        description=f"[{ts_str}] {result.description}",
                    )
                )
            elif not filter_matching:
                match_label = "MATCH" if result.matches_query else "no match"
                attachments.append(
                    ToolAttachment(
                        mime_type="image/jpeg",
                        content=result.jpeg_bytes,
                        description=f"[{ts_str}] ({match_label}) {result.description}",
                    )
                )

        # Build summary
        match_count = len([r for r in successful_results if r.matches_query])

        # ast-grep-ignore: no-dict-any - Building dynamic result dict
        result_data: dict[str, Any] = {
            "camera_id": camera_id,
            "query": query,
            "time_range": {
                "start": start_time,
                "end": end_time,
                "interval_minutes": interval_minutes,
            },
            "frames_scanned": len(frames),
            "frames_analyzed": len(successful_results),
            "matches_found": match_count,
            "analysis_results": analysis_summaries,
        }

        if error_count > 0:
            result_data["analysis_errors"] = error_count
            result_data["warning"] = f"{error_count} frame(s) could not be analyzed"

        # Generate text summary
        if match_count == 0:
            text_summary = (
                f"Scanned {len(frames)} frames from {camera_id} "
                f"({start_time} to {end_time}). "
                f"No frames matched the query: '{query}'"
            )
        else:
            match_times = [
                r.timestamp.strftime("%H:%M:%S")
                for r in matching_results
                if r.matches_query
            ]
            text_summary = (
                f"Found {match_count} matching frame(s) from {len(frames)} scanned. "
                f"Matches at: {', '.join(match_times)}. "
                f"Query: '{query}'"
            )

        return ToolResult(
            text=text_summary,
            data=result_data,
            attachments=attachments if attachments else None,
        )

    except ValueError as e:
        # Expected errors (invalid camera ID, etc.) - return cleanly
        return ToolResult(data={"error": str(e)})
    except Exception as e:
        logger.exception("Error scanning camera frames")
        return ToolResult(data={"error": f"Failed to scan frames: {e}"})
