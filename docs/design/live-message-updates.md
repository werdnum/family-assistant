# Live Message Updates for Web Chat UI

**Issue:** #137 **Status:** In Progress **Author:** Claude Code **Date:** 2025-10-05

## Problem Statement

The current web chat UI operates in a request-response model where users must remain on the page
during the entire streaming response. This prevents several useful scenarios:

1. Send message → navigate elsewhere → return later for response
2. Background tabs receiving reminders or server-initiated notifications
3. Multiple browser tabs staying synchronized on the same conversation
4. Resilient message delivery with offline support

## Proposed Solution

Implement event-driven polling with tickle notifications using asyncio.Queue, following the same
pattern already proven successful in the task queue system.

### Architecture Overview

```
Message Created (any source)
    ↓
DatabaseContext.add_message()
    ↓
on_commit() → MessageNotifier.notify(conv_id, interface_type)
    ↓
Tickle all registered asyncio.Queues
    ↓
SSE connections wake up
    ↓
Query DB for messages after last_seen
    ↓
Push to client via Server-Sent Events
```

### Core Components

#### 1. MessageNotifier

In-memory notification dispatcher that manages asyncio.Queue per SSE listener:

```python
class MessageNotifier:
    """Manages notification queues for live message updates"""

    def __init__(self):
        # {(conversation_id, interface_type): [queue1, queue2, ...]}
        self._listeners: dict[tuple[str, str], list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def register(self, conv_id: str, interface_type: str) -> asyncio.Queue:
        """Register SSE listener, returns queue to wait on"""

    async def unregister(self, conv_id: str, interface_type: str, queue: asyncio.Queue):
        """Unregister on SSE disconnect"""

    def notify(self, conv_id: str, interface_type: str):
        """Tickle all listeners (non-blocking, from on_commit callback)"""
```

**Key Design Decisions:**

- **One queue per SSE connection** - Avoids Event.clear() race conditions
- **Tickle notifications** - Queue gets `True`, not message data (DB is truth)
- **Bounded queues** - `maxsize=10` prevents slow listener memory exhaustion
- **Conversation-scoped** - Granular to `(conversation_id, interface_type)`
- **No global state** - Instantiated in Assistant, injected via dependency injection

#### 2. SSE Events Endpoint

New endpoint: `GET /api/v1/chat/events?conversation_id=X&after=timestamp`

- Long-lived SSE connection
- Hybrid: `await queue.get()` with 5s timeout
- Query DB on wake (tickle or timeout)
- Push messages as SSE events
- Heartbeat every 5s when idle

#### 3. Enhanced Messages Endpoint

Existing endpoint enhanced with `after` parameter:
`GET /api/v1/chat/conversations/{id}/messages?after=timestamp`

- Supports incremental sync
- Used for initial load and reconnection catch-up
- Backward compatible (after is optional)

#### 4. Database Integration

New method: `MessageHistoryRepository.get_messages_after(conversation_id, after, interface_type)`

Modified: `add_message()` triggers `on_commit()` callback to notify

### Streaming vs Notification Interaction

**No conflict because messages only notify AFTER commit:**

**User-initiated streaming flow:**

1. POST `/api/v1/chat/send_message_stream`
2. SSE stream sends chunks as generated
3. Message NOT in database during streaming
4. Streaming completes → `add_message()` called
5. Message saved, `on_commit()` triggers notification
6. Events endpoint queries DB, gets complete message
7. Frontend: deduplicates by `internal_id`

**Background message flow:**

1. Task/reminder creates message
2. `add_message()` saves to DB
3. `on_commit()` triggers notification
4. Events endpoint gets tickle, queries DB
5. Frontend receives and displays

**Result:** Clean separation. Streaming provides real-time updates during generation, events provide
background delivery.

### Frontend Implementation

New hook: `useConversationSync(conversationId)`

1. **Initial load:** Fetch all messages on mount
2. **SSE connection:** Listen to `/api/v1/chat/events` for updates
3. **Visibility catch-up:** Query `?after=timestamp` when tab becomes active
4. **Deduplication:** Merge streaming + SSE by `internal_id`

```javascript
const ChatApp = () => {
  // Existing: User-initiated message streaming
  const { sendStreamingMessage } = useStreamingResponse({...});

  // New: Background message delivery
  const { messages: liveMessages } = useConversationSync(conversationId);

  // Merge and deduplicate
  const allMessages = useMemo(() => {
    /* Dedupe by internal_id, sort by timestamp */
  }, [messages, liveMessages]);
};
```

## Implementation Phases

### Phase 1: Backend Foundation

**Files to create:**

- `src/family_assistant/web/message_notifier.py`

**Files to modify:**

- `src/family_assistant/storage/repositories/message_history.py`
  - Add `get_messages_after()` method
  - Add notification hook in `add_message()`
- `src/family_assistant/assistant.py`
  - Instantiate `MessageNotifier`
  - Pass to web app via `app.state.message_notifier`

**Tests:**

- Unit tests for MessageNotifier (register, unregister, notify)
- Test `get_messages_after()` query correctness
- Test notification fires after transaction commit

### Phase 2: SSE Endpoint

**Files to modify:**

- `src/family_assistant/web/routers/chat_api.py`
  - Add `/v1/chat/events` endpoint
  - Enhance existing `/messages` endpoint with `after` parameter

**Tests:**

- SSE connection lifecycle
- Multiple listeners per conversation
- Message delivery via tickle
- Heartbeat delivery
- Reconnection with `after` timestamp

### Phase 3: Frontend Integration

**Files to create:**

- `frontend/src/chat/useConversationSync.js`

**Files to modify:**

- `frontend/src/chat/ChatApp.tsx` - Integrate live updates
- `frontend/src/test/mocks/handlers.ts` - Add SSE mock handlers

**Tests:**

- SSE connection in React
- Message deduplication logic
- Visibility change catch-up
- Multi-tab synchronization

### Phase 4: Integration Testing

**New test file:**

- `tests/functional/web/test_chat_live_updates.py`

**Test scenarios:**

1. Send message → navigate away → return (response waiting)
2. Background tab receives reminder
3. Multiple tabs show same message simultaneously
4. Offline → reconnect → catch up on missed messages
5. All existing Playwright tests continue to pass

## Design Rationale

### Why asyncio.Queue Instead of Event?

**Event pattern (doesn't work for multiple listeners):**

```python
event = get_event(conv_id)
await event.wait()  # All listeners wake
event.clear()  # Race condition! Who clears?
```

**Queue pattern (works cleanly):**

```python
queue = await notifier.register(conv_id)
await queue.get()  # Consumes from own queue
# No clearing needed - each listener independent
```

### Why Tickle Instead of Message Data?

**Tickle (chosen):**

- DB remains single source of truth
- No message duplication in memory
- Natural batching (multiple messages → one query)
- Simpler consistency reasoning
- Handles reconnection cleanly

**Message data (rejected):**

- Messages duplicated (DB + N queues)
- Queue memory overhead grows with connections
- Backpressure management complexity
- Reconnection still needs DB query anyway

Extra ~1ms DB query is worth the simplicity.

### Why Conversation-Scoped Notifications?

**Conversation-level (chosen):**

- Right granularity for notification
- Web conversations already scoped per user (no sharing)
- Scales to ~100 active SSE per server

**User-level (rejected):**

- User with tabs on conv A and B → both wake for all messages
- Wasteful DB queries

**Global (rejected):**

- All connections wake for all messages
- Terrible scaling

## Edge Cases & Considerations

### Memory Management

- Bounded queues (`maxsize=10`) prevent slow listener DoS
- Explicit `unregister()` on SSE disconnect
- asyncio.Lock for thread-safety
- Queues cleaned up automatically when no listeners

### Message Deduplication

- Client uses `internal_id` as unique key
- Merge messages from streaming + SSE sources
- Sort by timestamp for consistent ordering
- Race conditions benign (duplicate fetched → dedupe → ignore)

### Scaling Path

**Current (single server):**

- In-memory MessageNotifier
- Supports ~100 SSE connections per server
- Sufficient for small-medium deployments

**Future (multi-server):**

- Replace `notify()` with Redis pub/sub
- Each server has own MessageNotifier + listeners
- Publish to Redis → all servers broadcast to their connections
- Architecture unchanged, just swap notification mechanism

### Connection Limits

- Browser: 6 connections per domain (HTTP/1.1)
- SSE uses 1 per tab per conversation
- Should be fine for normal usage

### Latency Characteristics

**Active connections:**

- Tickle latency: \<10ms (asyncio.Queue.put_nowait)
- Query latency: ~1-5ms (indexed DB query)
- **Total: ~10-15ms** notification to client

**Inactive connections:**

- Timeout poll interval: 5s
- **Total: up to 5s** for message delivery

Acceptable tradeoff: instant for active, 5s for background.

### Tool Confirmations

- Confirmation requests are messages → notified via SSE
- All tabs see confirmation request
- User approves in any tab
- Approval response via existing `/confirm_tool` endpoint
- Future: Could broadcast approval result as message

### Error Recovery

**SSE disconnect:**

- Browser EventSource auto-reconnects
- `after` parameter ensures no message loss
- Exponential backoff on repeated failures

**Database errors:**

- Query failure → skip this poll cycle
- Resume on next tickle or timeout
- No special handling needed

## Success Criteria

**Functionality:**

- ✅ User sends message, navigates away, sees response on return
- ✅ Background tab receives reminder without user action
- ✅ Multiple tabs stay synchronized
- ✅ Offline reconnection retrieves missed messages
- ✅ All existing Playwright tests pass

**Performance:**

- ✅ Message notification latency \<100ms (active connections)
- ✅ Supports 100+ concurrent SSE connections per server
- ✅ No memory leaks after 1 hour usage
- ✅ Message deduplication works correctly

**Reliability:**

- ✅ No lost messages
- ✅ Graceful degradation when SSE unavailable
- ✅ Recovery from network interruptions
- ✅ No duplicate messages displayed

## Migration & Compatibility

### Backward Compatibility

- Existing endpoints unchanged
- `/api/v1/chat/send_message_stream` - Works as before
- `/api/v1/chat/conversations/{id}/messages` - Enhanced with optional `after`

### Deployment Strategy

1. Deploy backend - SSE endpoint available but unused
2. Deploy frontend - Users get live updates
3. Monitor connection metrics and latency
4. Tune queue sizes and timeouts if needed

## Open Questions

1. **Queue timeout value?** Proposed 5s (balance latency vs overhead)
2. **Queue max size?** Proposed 10 (prevents slow listener DoS)
3. **Connection metrics?** Track count/latency/depth for debugging?
4. **Feature flag?** `web.live_updates_enabled` for A/B testing?

## References

- Issue #137: https://github.com/werdnum/family-assistant/issues/137
- Task queue pattern: `src/family_assistant/storage/tasks.py` (lines 37-53, 200-209)
- Existing SSE streaming: `src/family_assistant/web/routers/chat_api.py:690-1042`
