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

**Excluded for Security/Architecture:**

- `attachment_list()` - Intentionally not exposed to prevent attachment ID enumeration
- `attachment_send()` - Removed as redundant. Scripts should use LLM tools:
  - `attach_to_response` (for current user)
  - `send_message_to_user` (for other users)
  - `wake_llm` (to pass to LLM for processing)

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

### Phase 3: Unified Script API (COMPLETE ✅)

- [x] Extend wake_llm function to accept attachments in context
- [x] Extend tools.execute function to handle attachment parameters
- [x] Add core attachment utility functions (get, send - list intentionally excluded)
- [x] Basic attachment content processing in tools.execute
- [x] Enhanced delegate_to_service tool with attachment support
- [x] Enhanced tool schema for attachment type declarations with LLM translation
- [x] Focused test coverage for security boundaries and persistence

### Phase 4: LLM Tools (COMPLETE ✅)

- [x] Enhanced send_message_to_user tool with attachment forwarding (already existed)
- [x] get_attachment_info tool for metadata retrieval
- [x] list_user_attachments tool (DEFERRED - awaiting use cases)
- [x] Edit capabilities (DEFERRED - awaiting use cases)

### Phase 5: Testing & Documentation (COMPLETE ✅)

- [x] Focused testing for security boundaries and attachment persistence
- [x] User documentation for attachment workflows and security model
- [x] Update design document with implementation status
- [x] Large attachment testing (DEFERRED - not a priority)
- [x] Attachment cleanup testing (DEFERRED - not a priority)

## Current Commits

- `4abdd1ba`: Add attachment support to delegate_to_service tool and test wake_llm with attachments
  (Phase 3 partial)
- `df4914b3`: Implement multimodal attachment registry with proper authentication (Phase 1 & 2
  complete)
- `936555ae`: Implement configurable attachment limits across tools and services
- `c95ab3c6`: Centralize attachment size limits in config.yaml
- `472549e7`: Implement multimodal tool result support for chat and history UIs

## Next Steps (Updated Plan)

### Phase 3 Completion

1. Enhanced tool schema with attachment type declarations and LLM translation
2. Focused testing for security boundaries and attachment persistence

### Phase 4: Selective LLM Tools

3. Enhanced send_message_to_user tool with attachment forwarding
4. get_attachment_info tool for metadata retrieval

### Phase 5: Documentation

5. User documentation for attachment workflows and security model
6. Update design document with final implementation status

### Explicitly Deferred

- list_user_attachments tool (awaiting use cases)
- attachment_create() function (awaiting use cases)
- Attachment cleanup mechanisms (not a priority)
- Large attachment stress testing (existing limits sufficient)

## Current Technical Implementation

### Schema Translation for LLM Compatibility

**Challenge**: LLMs don't understand custom `"type": "attachment"` in tool schemas.

**Solution**: Two-tier schema system:

- **Internal Schema**: Tools define attachment parameters with `"type": "attachment"`
- **LLM Schema**: Translated to `"type": "string"` with descriptive text

Example transformation:

```python
# Internal tool definition
{
    "image_attachment_id": {
        "type": "attachment",
        "description": "Image to annotate"
    }
}

# Sent to LLM
{
    "image_attachment_id": {
        "type": "string",
        "description": "UUID of the image attachment to annotate"
    }
}
```

This approach provides:

- Type safety in internal validation
- LLM compatibility with standard JSON schema
- Clear expectations for attachment parameter usage

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

## Local Testing Plan

### Overview

This section outlines manual testing procedures for verifying the multimodal attachment system works
correctly in a local development environment without external dependencies like Telegram or Home
Assistant.

### Test Tools Required

#### Image Highlighting Tool

A real image processing tool for testing:

```python
# src/family_assistant/tools/image_tools.py
highlight_image(attachment_id: str, regions: list[dict]) -> ToolResult
```

This tool uses PIL to draw colored rectangles/circles on images, useful for "find the object"
scenarios.

### Core Test Scenarios

#### 1. Basic Upload and Round-trip

**Objective**: Verify user attachments get stable IDs and can be returned

**Steps**:

1. Via Web UI, upload an image with message "describe this image"
2. Verify LLM receives attachment and provides description
3. Ask "send the same image back to me"
4. Verify original image is displayed in response

**Expected Result**: Same image appears in chat response with stable attachment ID

#### 2. Tool-Generated Attachments

**Objective**: Verify tools can generate and return attachments

**Steps**:

1. Ask "generate a test image with text 'Hello Testing'"
2. Verify mock camera tool creates attachment
3. Ask "highlight any interesting areas in that image"
4. Verify highlight tool processes and returns modified image

**Expected Result**: Modified image with highlighted regions appears in response

#### 3. Script Processing Workflow

**Objective**: Verify scripts can process user attachments

**Test Scripts**:

- `echo_attachment.star` - Simple round-trip test
- `process_image.star` - Image processing workflow

**Steps**:

1. Upload image to trigger test script
2. Script receives attachment ID in context
3. Script processes via tools.execute()
4. Script returns result via wake_llm()

**Expected Result**: Processed image returned with script modifications

#### 4. Multi-step Processing Chain

**Objective**: Verify attachment IDs persist through tool chains

**Steps**:

1. Upload image: "process this image"
2. LLM uses highlight tool to mark regions
3. LLM uses annotate tool to add text
4. LLM returns final processed image

**Expected Result**: Each step preserves attachment chain, final image shows all modifications

#### 5. Cross-Profile Processing

**Objective**: Verify attachments work with profile delegation

**Steps**:

1. Configure vision_expert profile in config
2. Upload image to default profile
3. Request "delegate this to vision expert for analysis"
4. Verify attachment is forwarded correctly

**Expected Result**: Vision profile receives attachment and processes it

### Validation Checklist

For each test scenario, verify:

- [ ] Attachment IDs are UUIDs (not guessable)
- [ ] Images display correctly in web UI
- [ ] Attachment metadata appears in SSE events
- [ ] Database entries created in attachment_metadata table
- [ ] File storage works (files created in configured directory)
- [ ] Tool results include attachment_id in response
- [ ] Scripts can access attachments via attachment_get()
- [ ] No errors in logs during processing

### Debugging Utilities

#### Debug Attachment Registry

```python
# Tool for inspecting attachment state
debug_attachments(conversation_id: str) -> dict
```

Shows all attachments in current conversation with metadata.

#### Test Data Generator

```python
# Create multiple test images for batch testing
generate_test_images_batch(count: int = 3) -> list[str]
```

#### Enhanced Logging

Enable DEBUG logging for:

- `family_assistant.services.attachment_registry`
- `family_assistant.tools`
- `family_assistant.processing`

### Common Issues and Solutions

**Attachment not found errors**:

- Check attachment_metadata table for entry
- Verify file exists in storage directory
- Check conversation_id matches

**Images not displaying in UI**:

- Verify SSE events include attachment metadata
- Check content_url is accessible
- Verify MIME type is supported

**Script attachment access issues**:

- Ensure attachment ID is valid UUID
- Check attachment belongs to same conversation
- Verify script has proper context

### Success Criteria

The testing is successful when:

1. ✅ User attachments get stable IDs and persist in conversation
2. ✅ Tools can generate and return new attachments
3. ✅ Scripts can process both user and tool attachments
4. ✅ Attachments display correctly in web UI
5. ✅ Multi-step workflows preserve attachment chains
6. ✅ Cross-profile delegation includes attachments
7. ✅ No memory leaks or file system issues
8. ✅ All attachment operations are properly logged

## Design Notes

- Attachment cleanup will be conservative (better to keep than delete by mistake)
- Focus on core functionality before optimization
- Simple permission model prioritized over complex access control
- Keep memory usage efficient with lazy loading

## Automatic Tool Attachment Display (Addendum)

### Problem Statement

When tools return attachments (e.g., camera snapshots, generated images), the LLM often doesn't
realize it should call `attach_to_response` to send them to the user. This results in attachments
being stored but not displayed, creating a poor user experience where users ask for images but don't
see them.

### Solution: Auto-Attachment with LLM Override

#### Core Mechanism

1. **Automatic Collection**: When any tool returns a `ToolResult` with an attachment, automatically
   add it to `pending_attachment_ids`
2. **LLM Override**: If the LLM calls `attach_to_response`, treat this as taking explicit control:
   - Replace (not append to) the auto-collected list with LLM-specified attachments
   - This allows the LLM to show only final results in multi-step workflows
3. **Default Display**: If the LLM doesn't call `attach_to_response`, all auto-collected attachments
   are sent with the response

#### Implementation Changes

1. **src/family_assistant/processing.py** (~line 738)

   ```python
   # After storing tool result attachment
   if result.attachment and attachment_metadata:
       attachment_id = attachment_metadata["attachment_id"]
       if attachment_id not in pending_attachment_ids:
           pending_attachment_ids.append(attachment_id)
           logger.info(f"Auto-queued tool attachment {attachment_id} for display")
   ```

   Modify attach_to_response handling (~line 512):

   ```python
   if function_name == "attach_to_response":
       # LLM is taking control - replace auto-collected with explicit list
       pending_attachment_ids.clear()
       pending_attachment_ids.extend(attachment_ids)
       logger.info("LLM explicitly controlling attachments via attach_to_response")
   ```

2. **Tool Descriptions** - Update to clarify behavior:

   - "Captures and displays camera image to the user"
   - "Generates and displays an image based on the description"

3. **System Prompt** (prompts.yaml):

   ```yaml
   * Tools that generate images or files automatically display them with your response
   * To control which attachments are shown (e.g., only final results), use attach_to_response
   * Progressive disclosure: The web UI shows all tool results; Telegram shows only final attachments
   ```

#### Interface-Specific Behavior

- **Web UI**: Shows all tool results inline (progressive disclosure already exists)
  - TODO: Improve multimodal tool result display in tool call UI
- **Telegram**: Shows only final attachments (those in `pending_attachment_ids` at response end)
  - Better for linear chat format
  - Avoids cluttering conversation with intermediate images

#### Examples

**Simple Case**: "Show me the front door camera"

- `get_camera_snapshot` returns image → auto-queued → displayed

**Multi-Step Case**: "Get the camera image and highlight any people"

- `get_camera_snapshot` returns image → auto-queued
- `highlight_image` returns image → auto-queued
- Both displayed (user sees original and highlighted)

**LLM Control Case**: Same multi-step, but LLM calls `attach_to_response([highlighted_id])`

- Auto-queued attachments replaced
- Only highlighted image displayed

#### Benefits

- **Better Default UX**: Users get attachments automatically when expected
- **Backward Compatible**: Existing `attach_to_response` calls continue working
- **LLM Control**: Can override when appropriate for cleaner output
- **Interface Appropriate**: Different behavior for web vs Telegram

#### Implementation Status

- [ ] Modify processing.py to auto-queue tool attachments
- [ ] Update attach_to_response handling for LLM override
- [ ] Update tool descriptions for clarity
- [ ] Update system prompt in prompts.yaml
- [ ] Update and create tests for new behavior

## Attachment Selection for High-Volume Results (Addendum)

### Problem Statement

When tools like camera_analyst retrieve many images (e.g., reviewing camera footage throughout a
morning), the auto-attachment mechanism would send ALL thumbnails with the response. This causes:

1. **Telegram limits**: Media groups max out at 10 items
2. **Poor UX**: User overwhelmed with thumbnails when they want curated highlights
3. **Bandwidth waste**: Sending redundant/similar images

### Solution: Automatic Attachment Selection

When an agent turn ends with more attachments than a configurable threshold, the system
automatically re-prompts the LLM to select the most relevant attachments.

#### Configuration

In `AppConfig`:

- `attachment_selection_threshold: int = 3` - Trigger selection when > this many attachments
- `max_response_attachments: int = 6` - Maximum attachments per response

#### Mechanism

```
[Normal processing completes]
    ↓
[Check: len(pending_attachment_ids) > threshold?]
    ↓ Yes
[Create selection prompt with attachment metadata]
    ↓
[Call LLM with tool_choice="required" and only attach_to_response tool]
    ↓
[LLM returns attach_to_response call with selected IDs]
    ↓
[Update pending_attachment_ids with selection]
    ↓
[Yield "done" event with curated attachments]
```

#### Selection Prompt

The LLM receives:

- Count of available attachments
- The user's original query
- List of attachment metadata (ID, description, MIME type)
- Instruction to prioritize: direct answers, representative samples, key findings

#### Fallback Behavior

If selection fails (LLM error, no tool call returned), falls back to first N attachments where N =
`max_response_attachments`.

#### Implementation Status

- [x] Add Gemini forced function call support (`tool_choice="required"`)
- [x] Add `attachment_selection_threshold` and `max_response_attachments` to AppConfig
- [x] Implement `_select_attachments_for_response()` in ProcessingService
- [x] Add selection logic before "done" event in `process_message_stream()`
- [x] Unit tests for Gemini tool_choice modes
- [x] Unit tests for attachment selection logic

#### Files Modified

- `src/family_assistant/llm/providers/google_genai_client.py` - Gemini forced function calls
- `src/family_assistant/config_models.py` - New config fields
- `src/family_assistant/processing.py` - Selection logic (~lines 596-627, 1556-1667)
- `tests/unit/llm/providers/test_google_genai_tool_choice.py` - New tests
- `tests/unit/processing/test_attachment_selection.py` - New tests
