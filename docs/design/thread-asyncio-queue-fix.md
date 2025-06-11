# Thread-to-Asyncio Queue Fix

## Problem
The Home Assistant event source was experiencing "Event queue full" errors even when the queue wasn't actually full. This was caused by using `asyncio.Queue` from a thread, which is not thread-safe.

## Root Cause
The WebSocket listener runs in a thread (via `asyncio.to_thread`) and was using `asyncio.Queue.put_nowait()` to pass events to the asyncio event processor. Since `asyncio.Queue` is not thread-safe, this caused race conditions and incorrect queue state.

## Solution Implemented
Changed from `asyncio.Queue` to `queue.Queue` (thread-safe):
- Producer thread uses `queue.Queue.put_nowait()`
- Consumer asyncio task uses `await run_in_executor(None, queue.get, True, 1.0)`

## Future Recommendation
Consider using the `janus` library, which provides a cleaner dual-interface queue:
```python
import janus

# In __init__
self._event_queue: janus.Queue[dict[str, Any]] | None = None

# In start()
self._event_queue = janus.Queue(maxsize=1000)

# Producer (thread)
self._event_queue.sync_q.put_nowait(event)

# Consumer (asyncio)
event = await self._event_queue.async_q.get()

# In stop()
await self._event_queue.aclose()
```

This eliminates the need for `run_in_executor` and provides a more idiomatic solution for thread-to-asyncio communication.