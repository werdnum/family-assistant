# Parallel Image Scanning for Camera Analyst

## Problem Statement

When scanning large time ranges in the camera analyst profile, the current approach sends all images
to the LLM at once via `get_camera_frames_batch`. This has several limitations:

1. **Context/token usage**: All images consume context at once
2. **Latency**: Serial processing - all images must be processed together
3. **Limited filtering**: No way to pre-filter images before sending to main LLM
4. **Focus issues**: LLM must analyze many images simultaneously

## Key Insight

The LLM doesn't need all images at once. We can send each image independently to an LLM as parallel
requests with one or a few images each.

## Solution: New `scan_camera_frames` Tool

Create a new tool that performs parallel per-frame LLM analysis internally, returning only relevant
frames with structured analysis results.

### Tool Signature

```python
async def scan_camera_frames_tool(
    exec_context: ToolExecutionContext,
    camera_id: str,
    start_time: str,
    end_time: str,
    query: str,
    interval_seconds: float = 300,
    max_frames: int = 20,
    filter_matching: bool = True,
    model: str | None = None,
) -> ToolResult:
    """
    Scan camera frames in parallel with per-frame LLM analysis.

    This tool is optimized for scanning large time ranges efficiently:
    1. Extracts frames at regular intervals (parallelized frame extraction)
    2. Analyzes each frame with a focused LLM call (parallelized analysis)
    3. Returns filtered results with structured descriptions

    Args:
        camera_id: Camera to scan
        start_time: Start of time range in LOCAL TIME
        end_time: End of time range in LOCAL TIME
        query: What to look for (e.g., "person entering the yard", "package delivery")
        interval_seconds: Seconds between frames (default 300 = 5 minutes, min 1)
        max_frames: Maximum frames to scan (default 20, max 50)
        filter_matching: If True, only return frames that match the query (default True)
        model: Model to use for frame analysis. Defaults to profile's model.

    Returns:
        ToolResult with:
        - data: Summary of scan results (total scanned, matches found, timestamps)
        - attachments: Only the matching frames (if filter_matching=True) or all frames
    """
```

### How It Works

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         scan_camera_frames                               │
│                                                                          │
│  1. Extract frames (parallelized via existing get_frames_batch)          │
│     ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐                             │
│     │ t=0 │ │ t=5 │ │t=10 │ │t=15 │ │t=20 │  ...                        │
│     └──┬──┘ └──┬──┘ └──┬──┘ └──┬──┘ └──┬──┘                             │
│        │       │       │       │       │                                 │
│  2. Parallel LLM analysis (provider handles rate limiting)               │
│        │       │       │       │       │                                 │
│        ▼       ▼       ▼       ▼       ▼                                 │
│     ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐                             │
│     │ LLM │ │ LLM │ │ LLM │ │ LLM │ │ LLM │  (cheap/fast model)         │
│     └──┬──┘ └──┬──┘ └──┬──┘ └──┬──┘ └──┬──┘                             │
│        │       │       │       │       │                                 │
│  3. Structured output per frame:                                         │
│     {                                                                    │
│       "matches_query": true/false,                                       │
│       "description": "Person walking toward front door",                 │
│       "confidence": 0.85,                                                │
│       "details": {...}                                                   │
│     }                                                                    │
│        │       │       │       │       │                                 │
│  4. Filter & return matching frames with analysis                        │
│        ▼       ✗       ▼       ✗       ✗                                 │
│     ┌─────┐         ┌─────┐                                              │
│     │Match│         │Match│     (only 2 of 5 frames matched)            │
│     └─────┘         └─────┘                                              │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Structured Output Schema

Each frame is analyzed using LLM structured output with a Pydantic model:

```python
class FrameAnalysisLLMResponse(BaseModel):
    matches_query: bool = Field(
        description="Whether the frame shows what the user is looking for"
    )
    description: str = Field(
        description="Brief description of what is visible in the frame"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0"
    )
    detected_objects: list[str] = Field(
        default_factory=list, description="Key objects/entities visible in the frame"
    )
```

Example response:

```json
{
  "matches_query": true,
  "description": "Person walking up driveway toward front door carrying a package",
  "confidence": 0.85,
  "detected_objects": ["person", "package", "car"]
}
```

### Model Selection

The per-frame analysis model is configurable via the `model` parameter. If not specified, it uses
the camera_analyst profile's configured model.

Any multimodal-capable model supported by the system can be used. Choose a fast, cost-effective
model for frame analysis since many parallel calls will be made.

The provider handles rate limiting automatically with exponential backoff.

### Benefits

1. **Faster for large scans**: 20 frames analyzed in ~2-3 seconds vs ~10+ seconds serial
2. **Reduced main context**: Only matching frames returned to the calling LLM
3. **Structured filtering**: Boolean match + description enables smart filtering
4. **Focused analysis**: Each frame gets dedicated attention
5. **Progressive narrowing**: Find approximate time, then use existing tools to zoom in

### Example Usage

```
User: "When did the package get delivered today?"

Camera Analyst:
1. search_camera_events(camera_id="front_door", event_types=["person"], start/end=today)
   → Found person events at 10:30, 14:15, 16:45

2. scan_camera_frames(
     camera_id="front_door",
     start_time="14:00", end_time="14:30",
     query="person carrying or delivering a package",
     interval_minutes=2,
     filter_matching=True
   )
   → Returns 3 matching frames at 14:12, 14:14, 14:16 with descriptions:
     - 14:12: "Delivery truck visible in driveway"
     - 14:14: "Person walking to door carrying brown package"
     - 14:16: "Package visible on doorstep, person walking away"

3. Report: "Package delivered at approximately 14:14-14:16"
```

### Comparison with Existing Tools

| Tool                       | Use Case                      | Images to LLM  |
| -------------------------- | ----------------------------- | -------------- |
| `get_camera_frame`         | Single specific moment        | 1              |
| `get_camera_frames_batch`  | Manual review of intervals    | All (up to 10) |
| `scan_camera_frames` (NEW) | Smart scanning with filtering | Only matches   |

### Implementation Notes

1. Reuses existing `get_frames_batch` from camera backend (already parallelized)
2. Uses `asyncio.gather` for parallel LLM calls (provider handles rate limiting)
3. Uses `LLMClientFactory` to create custom LLM client when `model` parameter is specified
4. Uses LLM structured output via `generate_structured()` with Pydantic schema for type-safe
   responses
5. Error handling: Individual frame failures don't fail entire scan
6. Returns both summary data and filtered attachments

## Alternatives Considered

### Option 2: Modify `get_camera_frames_batch`

Add optional `analyze_query` parameter to existing tool. Rejected because:

- Changes existing behavior
- Makes the tool more complex
- Less clear separation of concerns

### Option 3: Script-based approach

Create a script template for parallelization. Rejected because:

- Puts more burden on the LLM
- Less structured
- Harder to maintain/test

## Implementation Plan

1. Add `scan_camera_frames_tool` function to `src/family_assistant/tools/camera.py`
2. Add tool definition to `CAMERA_TOOLS_DEFINITION`
3. Add tool registration in `src/family_assistant/tools/__init__.py`
4. Add to camera_analyst profile's `enable_local_tools` in `defaults.yaml`
5. Update camera_analyst system prompt to mention new tool
6. Write unit and functional tests
