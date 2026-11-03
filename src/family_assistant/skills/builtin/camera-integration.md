---
name: Camera Integration
description: View cameras and investigate recorded footage — when something changed, which moments show something, what happened in a time range. Load before any camera question beyond a live Home Assistant snapshot. Activates the camera investigation tools.
activate_tools:
  - list_cameras
  - search_camera_events
  - get_camera_frame
  - get_camera_frames_batch
  - get_camera_recordings
  - get_live_camera_snapshot
  - scan_camera_frames
  - find_camera_change
---

# Camera Integration

## Two Camera Backends

1. **Home Assistant cameras**: `get_camera_snapshot` gives a live view of any `camera.` entity. It
   is available without this skill.
2. **Reolink cameras** (the investigation tools this skill activates): live snapshots, recorded
   footage, and AI-detected events. Camera ids come from `list_cameras`. If `list_cameras` reports
   no cameras, this backend is not configured; say so rather than guessing.

## Picking the Right Tool

- **"When did X start or stop?"** ("when did the parcel arrive?", "when was the gate left open?") →
  `find_camera_change` with a statement about a single frame that is true or false, describing a
  state rather than an event ("a parcel is on the doorstep"). It searches the recordings itself and
  returns the frames either side of the change, or the range it happened in when the camera was not
  recording at that moment.
- **"Which moments show X?"** ("was anyone in the backyard this afternoon?") → `scan_camera_frames`.
  Its frame judgements are fast but can miss small or distant things, so look at the matching frames
  yourself before telling the user what was found.
- **Detected events** (person, vehicle, pet, motion) in a range → `search_camera_events`.
- **A frame at a specific time** → `get_camera_frame`; **a spread across a range** →
  `get_camera_frames_batch`.
- **What footage exists** → `get_camera_recordings`, before requesting frames from a range that may
  have gaps.
- **Right now** → `get_live_camera_snapshot`.

The searching happens inside `find_camera_change` and `scan_camera_frames`, so an investigation
should be a handful of tool calls, not a loop of frame requests. Combine with Home Assistant sensor
data or the weather when it helps explain what the footage shows.

All timestamps are in the household's local time, not UTC.
