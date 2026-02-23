---
name: Camera Integration
description: Guide for viewing live camera feeds, searching events, and investigating historical footage via Home Assistant and Reolink cameras.
---

# Camera Integration

## Two Camera Backends

### 1. Home Assistant Cameras

Live snapshots from any camera integrated with Home Assistant. Available automatically if Home
Assistant is configured with camera entities.

- "What cameras do I have?" - List available cameras
- "Show me the front door camera" - Get live snapshot

### 2. Reolink Camera Backend (Advanced)

Activate with `/camera` or `/investigate` slash command for advanced investigation features:

- **Search Events**: AI-detected events (person, vehicle, pet, motion) in a time range
- **Get Frames**: Single frame at a specific time, or batch frames at intervals
- **Check Recordings**: Verify what footage is available
- **Live Snapshots**: Real-time views

## Investigation Workflow

For "when did X happen?" questions, use binary search:

1. **Search events** - Find AI detection events in the time range
2. **Batch frames** - Review frames at 15-30 min intervals
3. **Narrow down** - More frequent frames in smaller range
4. **Pinpoint** - Single frame requests for the exact moment

Example:

```
/camera When did the package get delivered?
```

## Event Types

- `person` - Human detection
- `vehicle` - Car, truck, etc.
- `pet` - Animals
- `motion` - General motion

## Tips

- Use local time for all timestamps
- Start broad, then narrow down
- Search events first before reviewing footage
- Combine with Home Assistant sensor data for context
