# Camera Integration Guide

This document explains how to use the camera integration features to view live camera feeds, review
historical footage, and investigate events.

## Overview

The assistant supports two types of camera integrations:

1. **Home Assistant Cameras** - Live snapshots from any camera integrated with Home Assistant
2. **Reolink Camera Backend** - Advanced investigation features for Reolink NVR/cameras including
   event search, historical footage review, and binary search through recordings

## Home Assistant Camera Integration

If you have cameras connected to Home Assistant, you can request live snapshots directly.

### Available Commands

**List Available Cameras**

Ask the assistant to show you what cameras are available:

- "What cameras do I have?"
- "List available cameras"
- "Show me all cameras in Home Assistant"

**Get Live Snapshots**

Request a current snapshot from any camera:

- "Show me the front door camera"
- "Take a snapshot from the backyard camera"
- "What's the current view from `camera.driveway`?"
- "Check the baby monitor camera"

### Example Conversations

**Checking a camera:**

> **You:** Show me the garage camera
>
> **Assistant:** _[displays live snapshot from camera.garage]_
>
> Here's the current view from the garage camera. I can see the garage door is closed and both cars
> are parked inside.

**Finding available cameras:**

> **You:** What cameras can I check?
>
> **Assistant:** Here are the available cameras in Home Assistant:
>
> - camera.front_door (Front Door)
> - camera.backyard (Backyard Camera)
> - camera.garage (Garage)
> - camera.driveway (Driveway Camera)

### Configuration

Home Assistant cameras are automatically available if:

1. Home Assistant integration is configured (via the `homeassistant` MCP server)
2. Camera entities exist in your Home Assistant setup (entities starting with `camera.`)

No additional configuration is required beyond the standard Home Assistant integration.

## Reolink Camera Investigation (Advanced)

For Reolink cameras, a specialized investigation profile provides powerful features for reviewing
historical footage and searching for events.

### Activating Camera Investigation Mode

Use the `/camera` or `/investigate` slash command to activate the camera investigation profile:

- `/camera What happened in the chicken coop this morning?`
- `/investigate When did the package get delivered?`
- `/camera Show me events from the driveway between 2pm and 4pm`

### Available Features

#### List Cameras

See all configured Reolink cameras and their status:

- "What cameras do I have?"
- "List all cameras"
- "Show camera status"

Returns camera IDs, names, online/offline status, and backend type.

#### Search Events

Search for AI-detected events (person, vehicle, pet, motion) in a time range:

- "Search for person events on the front door camera today"
- "Were there any vehicles in the driveway this morning?"
- "Find all pet detections on the coop camera between 6am and noon"
- "What motion events happened in the backyard yesterday?"

**Event types:**

- `person` - Human detection
- `vehicle` - Car, truck, or other vehicle
- `pet` - Animals (dogs, cats, etc.)
- `motion` - General motion detection

#### Get Camera Frames

**Single Frame:** Get a snapshot at a specific time:

- "Show me the garage camera at 3:15 PM"
- "Get a frame from the front door at 2024-01-15T14:30:00"

**Batch Frames (Binary Search):** Get multiple frames at intervals for investigating when something
happened:

- "Get frames from the backyard every 15 minutes between 8am and noon"
- "Show me hourly snapshots from the driveway today"

This is the key feature for answering "when did X happen?" questions - review batch frames to
identify the time period, then narrow down with more frequent intervals.

#### Check Recording Coverage

Verify what footage is available before requesting frames:

- "What recordings are available from the front door today?"
- "Check recording coverage for the garage between 10am and 2pm"
- "Are there any gaps in the driveway recordings?"

Returns recording segments with start/end times and file sizes.

#### Live Snapshots

Get real-time snapshots from Reolink cameras:

- "Show me a live view of the chicken coop"
- "What does the backyard camera show right now?"

### Investigation Workflow

The camera investigation profile is designed for answering questions like "when did this happen?"
using a binary search approach:

1. **Start with events** - Search for relevant AI detection events in your time range
2. **Get batch frames** - Review frames at 15-30 minute intervals to identify when the change
   occurred
3. **Narrow down** - Get more frequent frames in a smaller time range
4. **Pinpoint** - Use single frame requests to identify the exact moment

### Example Investigation

> **You:** /camera When did the chickens get out of the coop today?
>
> **Assistant:** I'll investigate when the chickens escaped. Let me start by searching for pet
> events on the coop camera today.
>
> _[searches for pet events from 6am to now]_
>
> I found several pet detection events. The most interesting cluster is around 9:30 AM. Let me get
> batch frames from 9:00 AM to 10:00 AM at 5-minute intervals.
>
> _[displays batch frames]_
>
> Looking at these frames, I can see the coop door is closed at 9:20 and open at 9:25. The chickens
> appear to be outside the coop by 9:30 AM.

### Configuration

Reolink camera integration requires configuration in `config.yaml` or via environment variables:

```yaml
# In config.yaml under the camera_analyst profile:
processing_config:
  camera_config:
    backend: 'reolink'
    cameras_config:
      coop:
        host: '192.168.1.100'
        username: 'admin'
        password: 'your-password'
        name: 'Chicken Coop'
      driveway:
        host: '192.168.1.101'
        username: 'admin'
        password: 'your-password'
        name: 'Driveway Camera'
```

Or use the `REOLINK_CAMERAS` environment variable with JSON format:

```bash
REOLINK_CAMERAS='{"coop": {"host": "192.168.1.100", "username": "admin", "password": "secret", "name": "Chicken Coop"}}'
```

For detailed configuration options, see the configuration reference or contact your system
administrator.

## Natural Language Examples

### Home Assistant Cameras

| What You Want           | What to Say                                            |
| ----------------------- | ------------------------------------------------------ |
| See available cameras   | "What cameras do I have?"                              |
| Get a live snapshot     | "Show me the front door camera"                        |
| Check a specific camera | "What's happening in the backyard?"                    |
| Monitor baby/pet        | "Check the nursery camera" / "Show me the pet camera"  |
| Verify door/gate status | "Is the garage door closed? Show me the garage camera" |

### Reolink Camera Investigation

| What You Want                 | What to Say                                                             |
| ----------------------------- | ----------------------------------------------------------------------- |
| Find when something happened  | "/camera When did the package get delivered?"                           |
| Search for people             | "/camera Any visitors at the front door today?"                         |
| Check for vehicles            | "/camera Were there any cars in the driveway this morning?"             |
| Review animal activity        | "/camera What time did the chickens come out of the coop?"              |
| Get footage at specific time  | "/camera Show me the garage at 3:15 PM"                                 |
| Verify recording availability | "/camera Do we have recordings from the backyard between 10am and 2pm?" |
| Real-time view                | "/camera What does the chicken coop look like right now?"               |

## Troubleshooting

### Camera Not Found

If the assistant cannot find a camera:

1. **Home Assistant cameras:** Verify the camera entity exists in Home Assistant
   - Check the entity ID format: `camera.your_camera_name`
   - Ask the assistant to list available cameras
2. **Reolink cameras:** Verify the camera is configured and online
   - Check network connectivity to the camera
   - Verify credentials are correct

### No Recordings Available

If the assistant reports no recordings for a time period:

1. Check if the camera was online during that period
2. Verify recording settings on the camera/NVR
3. Check if the time range is within the camera's retention period
4. Use the "get recordings" feature to identify gaps

### Event Search Returns No Results

If event search returns no results:

1. The camera may not support AI detection for that event type
2. The sensitivity settings may need adjustment on the camera
3. Try searching for "motion" events instead of specific types
4. Verify the time range is correct (times are interpreted as local time)

### Image Too Large Error

If the assistant reports an image is too large:

1. This is typically a temporary issue with high-resolution cameras
2. Try requesting a different frame or waiting a moment
3. The system has a 20MB limit for images sent to the AI

### Connection Issues

If camera connections are failing:

1. Verify network connectivity between the assistant and cameras
2. Check that camera credentials haven't changed
3. Ensure the camera firmware is up to date
4. Contact your system administrator for configuration issues

## Tips for Best Results

1. **Use local time:** All timestamps are interpreted as local time (e.g., "3:15 PM" or
   "2024-01-15T14:30:00")

2. **Start broad, then narrow:** When investigating events, start with a wide time range and narrow
   down based on what you find

3. **Use event search first:** AI detection events can quickly identify times of interest before
   reviewing footage

4. **Combine with Home Assistant:** Use location tracking and sensor data alongside camera footage
   for complete context

5. **Be specific about cameras:** If you have multiple cameras, specify which one you want to check
   to avoid confusion
