# Camera Integration Guide

This guide covers viewing live camera snapshots and investigating historical Reolink footage.

## Overview

1. **Home Assistant Cameras** — Live snapshots from any `camera.*` entity in Home Assistant
   (available automatically when the `homeassistant` MCP server is configured).
2. **Reolink Camera Backend** — Historical footage investigation for configured Reolink
   NVRs/cameras: AI event search, binary search for when a state changed, visual frame search across
   a time range, recording coverage checks, and live snapshots. See the
   [configuration reference](../operations/CONFIGURATION_REFERENCE.md#reolink-cameras) for operator
   setup.

All timestamps are interpreted in the household's local time. Images sent to the model are capped at
20 MB.

## Home Assistant Cameras

Ask to list available cameras or grab a live snapshot:

- "What cameras do I have?"
- "Show me the front door camera"
- "What's the current view from `camera.driveway`?"

## Reolink Camera Investigation

- **List cameras and status:** "What cameras do I have?" / "Show camera status" (returns camera IDs,
  names, online/offline state, and backend type).
- **Search AI events (`person`, `vehicle`, `pet`, `motion`):**
  - "Search for person events on the front door camera today"
  - "Find all pet detections on the coop camera between 6am and noon"
- **Find when something changed (binary search over recordings):** Ask when a visual state started
  or stopped being true. The assistant narrows down the transition and shows the frames just before
  and just after (or the bounding time range if there was a recording gap):
  - "When did the parcel arrive on the doorstep?"
  - "When did the chickens go back into the coop this evening?"
- **Search footage for something:** Scans frames across a time window and inspects the top matches
  before answering:
  - "Was anyone in the backyard this afternoon?"
  - "Show me when the dog was on the deck today"
- **Get frames (single timestamp or spread across a range):**
  - "Show me the garage camera at 3:15 PM"
  - "Show me what the backyard looked like through the morning"
- **Check recording coverage:** Verify available segments (start/end times and sizes) and identify
  gaps before requesting frames:
  - "Check recording coverage for the garage between 10am and 2pm"
- **Live snapshots:** "Show me a live view of the chicken coop"

## Tips and Troubleshooting

- **Ask about a visible state for change detection:** "When was the parcel on the doorstep?" finds
  both arrival and removal; ask about one clear condition at a time.
- **Check events or recording coverage first:** AI detection events and recording coverage quickly
  narrow down long time windows before pulling frames.
- **No recordings or events found:** Check that the camera was online, the time window is in local
  time and within the NVR's retention period, or fall back from specific AI types (`person`, `pet`)
  to `"motion"` or frame search.
