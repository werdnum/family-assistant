# Multimodal Tool Result and User Attachment Manipulation Design

## Overview

Enable scripts and LLMs to manipulate both tool-generated and user-sourced multimodal content
(especially images) through attachment IDs, allowing for complex workflows like image editing,
analysis, and cross-tool/profile forwarding.

## Use Cases

### Tool-Generated Attachment Use Cases

1. **Camera retrieval and send**: "Retrieve the image from my camera and send it to me"
2. **Script-driven analysis**: Script retrieves camera image and passes to LLM for examination
3. **Attachment chaining**: Tool A creates image → Script passes to Tool B → Result sent to user
4. **Multi-step workflows**: Search documents → Extract images → Analyze → Summarize with visuals

### User-Sourced Attachment Use Cases

5. **Image editing workflow**: User sends image → LLM forwards to editing tool → Result returned
6. **Cross-profile processing**: User sends image → Default profile forwards to specialized vision
   profile
7. **Batch processing**: User sends multiple images → Script processes each → Results aggregated
8. **Document analysis**: User uploads PDF → Extract images/text → Analyze separately → Combined
   report
9. **Image comparison**: User sends two images → Script compares → Returns differences

## Current State Analysis

### User Attachment Flow

1. **Web UI**: Attachments sent via `/api/v1/chat/conversations/{id}/messages` with base64 content
2. **Storage**: Attachments stored in message history with metadata (type, content_url)
3. **Processing**: Attachments passed to LLM as `image_url` content parts
4. **Missing**: No attachment ID system for user attachments, no way to reference them

### Tool Attachment Flow

1. **Tool execution**: Returns `ToolResult` with `ToolAttachment`
2. **Storage**: AttachmentService stores bytes, generates UUID
3. **Streaming**: Attachment metadata sent via SSE with attachment_id
4. **Display**: Frontend shows images using content_url

## Design

### Core Components

#### 1. Unified Attachment Registry

- Single registry for ALL attachments (user-sourced and tool-generated)
- User attachments get IDs upon receipt
- Maintains source metadata (user/tool/script)
- Tracks lifecycle and references

#### 2. Attachment Storage Enhancement

```python
class AttachmentMetadata:
    attachment_id: str  # UUID
    source_type: str    # "user", "tool", "script"
    source_id: str      # user_id, tool_name, script_id
    mime_type: str
    description: str
    size: int
    content_url: str    # For retrieval
    created_at: datetime
    conversation_id: str
    message_id: str | None  # Link to originating message
    references: list[str]   # Who has accessed this
```

#### 3. Script Attachment API

**Unified API Approach**: Extend existing script functions to seamlessly handle attachments rather
than creating parallel APIs.

Enhanced Starlark functions:

- `wake_llm(context)` - Extended to accept `attachments` in context dict ✅ IMPLEMENTED
- `tools.execute(tool_name, **kwargs)` - Extended to accept attachment IDs as parameters ✅
  IMPLEMENTED
- `attachment_get(attachment_id)` - Get metadata ✅ IMPLEMENTED
- `attachment_send(attachment_id, message=None)` - Send to user ✅ IMPLEMENTED

**Excluded for Security:**

- `attachment_list()` - Intentionally not exposed to prevent attachment ID enumeration

**Deferred for Future:**

- `attachment_create(content, mime_type, description)` - Create new attachment (awaiting use case)

#### 4. LLM Attachment Tools

New/enhanced tools:

- `send_attachment` - Display any attachment to user
- `forward_attachment` - Forward to another tool/profile
- `edit_attachment` - Apply modifications (resize, crop, filter)
- `get_attachment_info` - Get metadata
- `list_user_attachments` - List attachments from current conversation

#### 5. Processing Pipeline Changes

1. **On user message with attachments**:

   - Store attachments via AttachmentService
   - Generate attachment IDs
   - Add IDs to message metadata
   - Make IDs available to scripts/tools

2. **On tool result with attachment**:

   - Current flow continues (already has IDs)
   - Register in unified registry

3. **On script/LLM reference**:

   - Validate access permissions
   - Retrieve via ID
   - Track reference

## Implementation Plan

### Step 1: Design Document

Create `docs/design/multimodal-manipulation.md` with complete design

### Step 2: Unified Attachment Registry

1. Extend AttachmentService with registry functionality
2. Add `attachment_metadata` table to database
3. Create AttachmentRegistry class for lifecycle management
4. Add conversation-scoped access control

### Step 3: User Attachment Processing

1. Modify chat_api.py to store user attachments
2. Generate attachment IDs on receipt
3. Add IDs to message metadata
4. Update processing.py to pass attachment context

### Step 4: Script Attachment API

1. Implement attachment functions in Starlark engine
2. Add permission checking for attachment access
3. Support both metadata and content retrieval
4. Enable tool forwarding with attachments

### Step 5: LLM Attachment Tools

1. Create attachment manipulation tools
2. Add forward_attachment for cross-tool/profile use
3. Implement edit_attachment with basic operations
4. Update tool definitions

### Step 6: Database Schema

```sql
CREATE TABLE attachment_metadata (
    attachment_id UUID PRIMARY KEY,
    source_type VARCHAR(20) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    description TEXT,
    size INTEGER,
    content_url TEXT,
    storage_path TEXT,
    conversation_id VARCHAR(255),
    message_id INTEGER,
    created_at TIMESTAMP NOT NULL,
    metadata JSONB
);

CREATE INDEX idx_attachment_conversation ON attachment_metadata(conversation_id);
CREATE INDEX idx_attachment_source ON attachment_metadata(source_type, source_id);
```

### Step 7: Testing

1. User attachment upload and reference tests
2. Cross-tool attachment forwarding tests
3. Script manipulation of user attachments
4. Permission/access control tests
5. Frontend display of forwarded attachments

### Step 8: Documentation

1. Update scripting guide with attachment examples
2. Document attachment lifecycle
3. Add user guide for image editing workflows
4. API documentation for attachment endpoints

## Technical Considerations

### Metadata Structure Differences

**User Attachments**:

- No `source_tool` (use `source_type: "user"`)
- Has `original_filename` field
- May have user-provided description

**Tool Attachments**:

- Has `source_tool` field
- Generated descriptions
- May have tool-specific metadata

### Permission Model

**Simplified "ID = Access" Model:**

1. **Direct Access**: If you have an attachment ID, you can access it
2. **No Discovery**: No listing/browsing mechanism (prevents ID enumeration)
3. **UUID Security**: Attachment IDs are UUIDs (not guessable)
4. **Authenticated Access**: General authentication required, but no complex per-attachment
   permissions

**ID Distribution Methods:**

- LLM provides IDs to scripts based on conversation context
- Tools return attachment IDs as results
- Direct parameter passing (for known IDs)

**Security Rationale:** This model aligns with the application's threat model where authenticated
users are trusted not to be malicious. Complex permission checking is avoided in favor of making IDs
non-discoverable.

### Memory Management

1. Lazy loading of content (metadata first, content on demand)
2. Streaming for large attachments
3. Automatic cleanup of orphaned attachments
4. Size limits enforcement (20MB for multimodal)

## Example Workflows

### User Image Editing

```python
# User: "Edit this image to be black and white"
# User sends image attachment

# Get user's attachment (unified API)
attachments = attachment.list(source_type="user")
if attachments:
    attachment_id = attachments[0]["attachment_id"]

    # Forward to editing tool (unified API)
    result = tools.execute("edit_image",
                          attachment_id=attachment_id,
                          operation="grayscale")

    # Send result back (utility function)
    attachment.send(result["attachment_id"], "Here's your black and white image")
```

### Script Processing User Image

```starlark
# Get user's attachment (unified API)
attachments = attachment.list(source_type="user")
if len(attachments) > 0:
    img_id = attachments[0]["attachment_id"]

    # Analyze with LLM (unified API)
    wake_llm({
        "message": "What objects are in this image?",
        "attachments": [img_id]
    })

    # Forward to another tool if needed (unified API)
    result = tools.execute("extract_text_from_image",
                          attachment_id=img_id)
    if result:
        print("Extracted text:", result)

    # Send results (utility function)
    attachment.send(img_id)
```

### Cross-Profile Processing

```python
# User sends complex image for analysis
# Default profile forwards to vision specialist

attachments = attachment.list(source_type="user")
if attachments:
    # Use profile delegation (unified API)
    wake_llm({
        "message": "Provide detailed analysis of this image",
        "attachments": [attachments[0]["attachment_id"]],
        "profile": "vision_expert"
    })
```

## Success Criteria

1. User attachments get stable IDs that persist in conversation
2. Scripts can manipulate both user and tool attachments
3. Attachments can be forwarded between tools and profiles
4. No regression in existing functionality
5. Clear permission boundaries maintained
6. Memory efficient handling of large attachments

## Security Considerations

1. Attachment IDs are UUIDs (not guessable)
2. Conversation-scoped access by default
3. No cross-user attachment access
4. Size and type validation on all operations
5. Rate limiting on attachment operations
6. Audit trail for attachment access

## Backward Compatibility

1. Existing tool attachments continue working
2. Messages without attachment IDs handled gracefully
3. Frontend works with both old and new attachment formats
4. API maintains compatibility

## Implementation Status

### Phase 1: Foundation (COMPLETE ✅)

- [x] Design document created
- [x] Centralized attachment configuration in config.yaml
- [x] Enhanced AttachmentService with configurable limits
- [x] Updated tools to use centralized limits (home_assistant, documents)
- [x] Message history already has attachments JSON column
- [x] Database schema migration for attachment_metadata table
- [x] Unified AttachmentRegistry class

### Phase 2: User Attachment Processing (COMPLETE ✅)

- [x] Chat API modification for user attachment storage
- [x] Processing pipeline updates with attachment claiming
- [x] Message metadata enhancement with attachment IDs
- [x] Proper authentication and authorization

### Phase 3: Unified Script API (PARTIALLY COMPLETE)

- [x] Extend wake_llm function to accept attachments in context
- [x] Extend tools.execute function to handle attachment parameters
- [x] Add core attachment utility functions (get, list, send)
- [x] Basic attachment content processing in tools.execute
- [ ] Enhanced tool schema for attachment type declarations
- [ ] Comprehensive test coverage for attachment functionality

### Phase 4: LLM Tools (Planned)

- [ ] Attachment manipulation tools
- [ ] Cross-profile forwarding
- [ ] Edit capabilities

### Phase 5: Testing & Documentation (Planned)

- [ ] Comprehensive testing
- [ ] User documentation
- [ ] API documentation

## Current Commits

- `df4914b3`: Implement multimodal attachment registry with proper authentication (Phase 1 & 2
  complete)
- `936555ae`: Implement configurable attachment limits across tools and services
- `c95ab3c6`: Centralize attachment size limits in config.yaml
- `472549e7`: Implement multimodal tool result support for chat and history UIs

## Next Steps (In Order)

1. Extend wake_llm function to accept attachments in context
2. Extend tools.execute function to handle attachment parameters
3. Add core attachment utility functions (get, list, create, send)
4. Add permission checking and conversation scoping
5. Write comprehensive tests for unified attachment API

## Current Technical Implementation

### Attachment Processing in tools.execute

The `tools.execute` function currently:

1. Detects UUID-formatted parameters using `_is_attachment_id()`
2. Fetches attachment content via `_process_attachment_arguments()`
3. Replaces attachment IDs with raw content bytes before tool execution

### Security Implementation

- `attachment_list()` not exposed in script API to prevent ID enumeration
- Scripts must receive attachment IDs via LLM context, tool results, or parameters
- UUID validation ensures only valid attachment IDs are processed
- Conversation-scoped access enforced at registry level

### Next Steps (Immediate)

1. **Tool Schema Enhancement**: Add explicit attachment type support in tool definitions
2. **Test Coverage**: Comprehensive tests for existing attachment functionality
3. **Documentation**: Update script tool help and examples

### Future Work

- `attachment_create()` function (when use case emerges)
- MCP server attachment support (when needed)
- Privileged access for system scripts (if required)

## Design Notes

- Attachment cleanup will be conservative (better to keep than delete by mistake)
- Focus on core functionality before optimization
- Simple permission model prioritized over complex access control
- Keep memory usage efficient with lazy loading
