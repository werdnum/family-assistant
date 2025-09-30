# Auto-Attachment Display in Web UI

## Problem Statement

When tools return attachments (e.g., camera snapshots, generated images), they are automatically
queued for display via the "auto-attachment" mechanism in `processing.py`. The backend correctly:

1. Auto-queues attachments when tools return them (`processing.py:519-526`)
2. Sends `attachment_ids` in the "done" SSE event (`processing.py:463-467`)

However, **these auto-attached attachments do not appear in the web UI**. The frontend never
processes the attachment data from the done event.

## Root Cause

The `useStreamingResponse` hook processes various SSE event types but does not handle the "done"
event's attachment metadata. Additionally, when loading conversation history, the frontend doesn't
synthesize tool calls for auto-attachments.

## Architecture Constraint

assistant-ui (v0.11.x) does **not support native attachments on assistant messages**. The
`MessagePrimitive.Attachments` component only works for user messages. We attempted to use
`AssistantMessageAttachments` but it did not function.

## Architecture Decision: Frontend Layer Synthesis

### Core Principle

**The workaround should live in the web UI layer, not pollute the backend or message history.**

### Why Frontend Synthesis?

✅ **Isolation**: Workaround contained to the layer with the limitation ✅ **Clean History**: No
synthetic tools in database or message history ✅ **Interface Independence**: Telegram and other
interfaces unaffected ✅ **Easy Migration**: When assistant-ui adds support, just remove synthesis
code ✅ **Architectural Purity**: Backend doesn't know about UI rendering quirks

### Rejected Alternatives

#### A) Backend Synthetic Tool Calls

**Problem**: Would pollute message history and affect non-web interfaces (Telegram).

#### B) Custom Attachment Component

**Problem**: Would require duplicate rendering logic and break from existing patterns.

## Solution: Frontend Tool Call Synthesis

### High-Level Approach

When the frontend receives auto-attachment data (either in streaming or history loading), it
synthesizes a fake `attach_to_response` tool call. This reuses the existing `AttachToResponseTool`
component for display.

```
Auto-attachment flow:
Backend: Tool returns image → Auto-queue → Done event with metadata
Frontend: Done event → Synthesize attach_to_response tool → Existing UI renders it
```

### Two Contexts to Handle

#### 1. Streaming Context (Live Chat)

**File**: `frontend/src/chat/useStreamingResponse.js`

When processing SSE events, detect done events with attachment metadata and synthesize a tool call:

```javascript
if (payload.attachment_ids && payload.attachments) {
    const syntheticToolCall = {
        id: `web_attach_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        name: 'attach_to_response',
        arguments: JSON.stringify({ attachment_ids: payload.attachment_ids }),
        result: JSON.stringify({
            status: 'attachments_queued',
            count: payload.attachments.length,
            attachments: payload.attachments
        }),
        attachments: payload.attachments,
        _synthetic: true
    };

    toolCalls.push(syntheticToolCall);
    onToolCall([...toolCalls]);
}
```

#### 2. History Loading Context

**File**: `frontend/src/chat/ChatApp.tsx`

When loading past conversations, check message metadata for auto-attachments:

```javascript
// In loadConversationMessages
if (msg.metadata?.attachment_ids && msg.metadata?.attachments) {
    content.push({
        type: 'tool-call',
        toolCallId: `history_attach_${msg.internal_id}`,
        toolName: 'attach_to_response',
        args: { attachment_ids: msg.metadata.attachment_ids },
        result: JSON.stringify({
            status: 'attachments_queued',
            attachments: msg.metadata.attachments
        }),
        attachments: msg.metadata.attachments
    });
}
```

## Implementation Details

### Backend Changes

#### 1. Enhance Done Event Metadata

**File**: `src/family_assistant/processing.py` ~line 463

**Before**:

```python
if pending_attachment_ids:
    done_metadata["attachment_ids"] = pending_attachment_ids
```

**After**:

```python
if pending_attachment_ids:
    # Fetch full metadata for each attachment
    attachment_details = []
    async with AttachmentRegistry() as registry:
        for att_id in pending_attachment_ids:
            try:
                metadata = await registry.get_metadata(att_id)
                attachment_details.append({
                    "id": att_id,
                    "type": "image",
                    "name": metadata.description or "Attachment",
                    "content": f"/api/attachments/{att_id}",
                    "mime_type": metadata.mime_type,
                    "size": metadata.size,
                })
            except Exception as e:
                logger.warning(f"Failed to fetch metadata for {att_id}: {e}")

    done_metadata["attachment_ids"] = pending_attachment_ids
    done_metadata["attachments"] = attachment_details
```

#### 2. Include Metadata in History API

**File**: `src/family_assistant/web/routers/chat_api.py`

In the GET `/conversations/{conversation_id}/messages` endpoint, include attachment metadata:

```python
for msg in messages:
    message_dict = {...}

    # Include attachment metadata if present
    if msg.metadata:
        try:
            metadata_dict = json.loads(msg.metadata) if isinstance(msg.metadata, str) else msg.metadata
            if metadata_dict and "attachment_ids" in metadata_dict:
                attachments = []
                async with AttachmentRegistry() as registry:
                    for att_id in metadata_dict["attachment_ids"]:
                        try:
                            metadata = await registry.get_metadata(att_id)
                            attachments.append({
                                "id": att_id,
                                "type": "image",
                                "name": metadata.description or "Attachment",
                                "content": f"/api/attachments/{att_id}",
                                "mime_type": metadata.mime_type,
                                "size": metadata.size,
                            })
                        except Exception:
                            pass

                message_dict["metadata"] = {
                    "attachment_ids": metadata_dict["attachment_ids"],
                    "attachments": attachments
                }
        except Exception:
            pass
```

### Frontend Changes

#### 1. Streaming Synthesis

**File**: `frontend/src/chat/useStreamingResponse.js` ~line 170

Add handler after tool result processing:

```javascript
// Handle done event with auto-attachments
if (payload.attachment_ids && payload.attachments && Array.isArray(payload.attachments)) {
    console.log(`[AUTO-ATTACH] Synthesizing tool call for ${payload.attachments.length} attachments`);

    const syntheticToolCall = {
        id: `web_attach_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        name: 'attach_to_response',
        arguments: JSON.stringify({ attachment_ids: payload.attachment_ids }),
        result: JSON.stringify({
            status: 'attachments_queued',
            count: payload.attachments.length,
            attachments: payload.attachments
        }),
        attachments: payload.attachments,
        _synthetic: true
    };

    toolCalls.push(syntheticToolCall);
    onToolCall([...toolCalls]);
}
```

#### 2. History Loading Synthesis

**File**: `frontend/src/chat/ChatApp.tsx` ~line 445

After processing explicit tool calls:

```javascript
// Synthesize attach_to_response if auto-attachments present
if (msg.metadata?.attachment_ids && msg.metadata?.attachments) {
    console.log(`[AUTO-ATTACH-HISTORY] Synthesizing for msg ${msg.internal_id}`);

    content.push({
        type: 'tool-call',
        toolCallId: `history_attach_${msg.internal_id}`,
        toolName: 'attach_to_response',
        args: { attachment_ids: msg.metadata.attachment_ids },
        argsText: JSON.stringify({ attachment_ids: msg.metadata.attachment_ids }),
        result: JSON.stringify({
            status: 'attachments_queued',
            count: msg.metadata.attachments.length,
            attachments: msg.metadata.attachments
        }),
        attachments: msg.metadata.attachments
    });
}
```

## Testing Strategy

### Unit Tests

#### Backend Tests

**File**: `tests/unit/test_processing_multimodal_integration.py`

```python
async def test_done_event_includes_attachment_metadata():
    """Test that done events include full attachment metadata, not just IDs."""
    # Mock tool that returns attachment
    # Verify done event has both attachment_ids and attachments array
    # Verify attachments have required fields (id, type, name, content, mime_type, size)
```

#### Frontend Tests

**File**: `frontend/src/chat/__tests__/useStreamingResponse.test.tsx` (to be created)

```typescript
describe('Auto-attachment synthesis', () => {
    it('synthesizes attach_to_response tool call from done event', () => {
        // Mock SSE stream with attachments in done event
        // Verify synthetic tool call is created
        // Verify it has correct structure
    });
});
```

**File**: `frontend/src/chat/__tests__/ChatApp.test.tsx` (to be created)

```typescript
describe('Message history loading', () => {
    it('synthesizes attach_to_response from message metadata', () => {
        // Mock messages API response with attachment metadata
        // Verify synthetic tool call appears in loaded messages
    });
});
```

### Integration Tests

#### Playwright Tests

**File**: `tests/functional/web/test_chat_ui_attachment_response.py`

```python
@pytest.mark.playwright
async def test_auto_attachments_display_in_streaming():
    """Test that auto-attached tool results appear without explicit attach_to_response"""
    # Use camera tool that returns image
    # Verify attachment appears in message
    # Verify it uses attach_to_response UI

@pytest.mark.playwright
async def test_auto_attachments_persist_in_history():
    """Test that auto-attachments appear when reloading conversation"""
    # Create conversation with auto-attached image
    # Reload page
    # Verify attachment still appears
```

### Manual Testing Checklist

- [ ] Camera tool shows image immediately (streaming context)
- [ ] Refresh page - attachment still visible (history context)
- [ ] Multiple attachments in one message work
- [ ] Explicit attach_to_response tool still works
- [ ] No console errors about synthetic tool calls
- [ ] Telegram interface unaffected by changes
- [ ] Message history database has no synthetic tools

## Success Criteria

- [ ] Auto-attachments display in streaming context
- [ ] Auto-attachments display when loading history
- [ ] No synthetic tools stored in message history database
- [ ] Telegram interface unaffected
- [ ] Clean backend - no UI-specific workarounds
- [ ] All unit tests pass
- [ ] All Playwright tests pass
- [ ] No regression in existing attachment handling

## Migration Path

When assistant-ui adds native assistant message attachment support in a future version:

1. Remove synthesis code from `useStreamingResponse.js`
2. Remove synthesis code from `ChatApp.tsx`
3. Add `<AssistantMessageAttachments />` to `Thread.tsx` AssistantMessage component
4. Update message conversion to pass attachments properly

Backend requires no changes - it already sends the correct data structure.

## Risks and Mitigations

### Risk: ID Collisions

**Impact**: Synthetic tool call IDs might collide with real ones **Mitigation**: Use unique prefix
(`web_attach_`, `history_attach_`) and timestamps

### Risk: React Key Warnings

**Impact**: Unstable keys if IDs aren't deterministic for history **Mitigation**: Use
`history_attach_${msg.internal_id}` for history (stable)

### Risk: Performance with Many Attachments

**Impact**: Fetching metadata for many attachments in history could be slow **Mitigation**: Add
caching layer if needed (not implemented initially)

### Risk: Frontend/Backend Desync

**Impact**: Frontend assumes backend format **Mitigation**: Defensive coding with type checks and
fallbacks

## Implementation Timeline

1. **Backend done event** - Add full attachment metadata (30 min)
2. **Backend history API** - Include metadata in responses (30 min)
3. **Frontend streaming** - Synthesize in useStreamingResponse (30 min)
4. **Frontend history** - Synthesize in ChatApp (30 min)
5. **Testing** - Unit + integration + manual (2 hours)
6. **Verification** - Ensure other interfaces unaffected (15 min)

**Total Estimated Time**: 4-5 hours

## References

- Original multimodal design: `docs/design/multimodal-manipulation.md`
- Auto-attachment implementation: `src/family_assistant/processing.py:519-526`
- Existing attachment UI: `frontend/src/chat/AttachToResponseTool.tsx`
- assistant-ui documentation: https://www.assistant-ui.com/docs/guides/Attachments

## Status

**Status**: In Progress **Created**: 2025-01-XX **Last Updated**: 2025-01-XX **Implementation PR**:
TBD
