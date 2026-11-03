# Camera Integration Guide

This document explains how to use the camera integration features to view live camera feeds, review
historical footage, and investigate events.

## Overview

The assistant supports two types of camera integrations:

1. **Home Assistant Cameras** - Live snapshots from any camera integrated with Home Assistant
2. **Reolink Camera Backend** - Advanced investigation features for Reolink NVR/cameras including
   event search, searching hours of footage for something, and finding when something changed

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

For Reolink cameras, the assistant can review historical footage and search for events. Just ask
about a camera in your own words:

- "What happened in the chicken coop this morning?"
- "When did the package get delivered?"
- "Show me events from the driveway between 2pm and 4pm"

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

#### Find When Something Changed

Ask when something started or stopped being true, and the assistant searches the recordings for the
moment it changed, then shows you the frame just before and just after:

- "When did the parcel arrive on the doorstep?"
- "When did the chickens go back into the coop this evening?"
- "When was the side gate left open?"

If the camera wasn't recording at the moment of the change, you get the time range it happened in
instead of an exact moment.

#### Search Footage for Something

Ask which moments show something, and the assistant checks frames across the time range and shows
you the ones most likely to match:

- "Was anyone in the backyard this afternoon?"
- "Show me when the dog was on the deck today"
- "Were there any cars in the driveway this morning?"

The checks are fast but can be wrong about small or distant things, so the assistant looks at the
matching frames itself before telling you what it found.

#### Get Camera Frames

**Single Frame:** Get a snapshot at a specific time:

- "Show me the garage camera at 3:15 PM"
- "Get a frame from the front door at 2024-01-15T14:30:00"

**A spread of frames:** See frames from across a time range:

- "Show me what the backyard looked like through the morning"
- "Show me snapshots from the driveway today"

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

### Example Investigation

> **You:** When did the chickens get out of the coop today?
>
> **Assistant:** _[searches the coop camera's recordings for when the coop door opened]_
>
> The coop door was closed at 9:24:10 and open at 9:24:52 — here are the frames either side. The
> first chicken is outside by 9:25.

### Configuration

Your administrator sets up which Reolink cameras the assistant can reach; see the Reolink section of
the [configuration reference](../operations/CONFIGURATION_REFERENCE.md#reolink-cameras).

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

| What You Want                 | What to Say                                                     |
| ----------------------------- | --------------------------------------------------------------- |
| Find when something happened  | "When did the package get delivered?"                           |
| Search for people             | "Any visitors at the front door today?"                         |
| Check for vehicles            | "Were there any cars in the driveway this morning?"             |
| Review animal activity        | "What time did the chickens come out of the coop?"              |
| Get footage at specific time  | "Show me the garage at 3:15 PM"                                 |
| Verify recording availability | "Do we have recordings from the backyard between 10am and 2pm?" |
| Real-time view                | "What does the chicken coop look like right now?"               |

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

2. **Ask about a state, not an event:** "When was the parcel on the doorstep?" finds both when it
   arrived and when it was taken; asking about one simple thing at a time works best

3. **Use event search first:** AI detection events can quickly identify times of interest before
   reviewing footage

4. **Combine with Home Assistant:** Use location tracking and sensor data alongside camera footage
   for complete context

5. **Be specific about cameras:** If you have multiple cameras, specify which one you want to check
   to avoid confusion
