# Script Attachment Propagation Design

**Status**: Proposed **Date**: 2025-11-01 **Author**: Claude (via GitHub Issue)

## Summary

Enable attachments created by scripts (via `attachment_create()` or tool calls) to be automatically
propagated in the `ToolResult` returned by `execute_script`, making them visible to the LLM.

## Problem Statement

### Current Behavior

Scripts can create attachments in two ways:

1. **Direct creation**: `attachment_create(content, filename, description, mime_type)`
2. **Tool results**: `tools_execute(tool_name, **kwargs)` where the tool returns `ToolResult` with
   attachments

Both methods store attachments successfully and return UUIDs to the script. However, the
`execute_script` tool:

- Returns only a string (line 30 in `execute_script.py`)
- Formats results as text (lines 94-118)
- **Does NOT propagate attachments** created during execution
- LLM never sees that attachments were created

### Impact

**Example scenario:**

```starlark
# Script creates chart
chart_id = attachment_create(
    content=generate_chart_data(),
    filename="chart.png",
    description="Sales chart"
)

# Script calls analysis tool
analysis_id = tools_execute("analyze_data", data=my_data)

return "Created chart and analysis"
```

**What the LLM receives:**

```
"Script result: Created chart and analysis"
```

**What the LLM should receive:**

```
ToolResult(
    text="Script result: Created chart and analysis",
    attachments=[
        ToolAttachment(attachment_id=chart_id),
        ToolAttachment(attachment_id=analysis_id)
    ]
)
```

The attachments exist in the database but the LLM has no way to know they were created or to
reference them.

## Requirements

### Functional Requirements

1. **Automatic Propagation**: Attachments created via `attachment_create()` must automatically
   appear in the `execute_script` ToolResult
2. **Tool Result Propagation**: Attachments returned by tools called via `tools_execute()` must
   automatically appear in the ToolResult
3. **Explicit Control**: Scripts must be able to explicitly specify which attachments to return
   (optional)
4. **Backward Compatibility**: Existing scripts without attachments must continue working unchanged
5. **Deduplication**: If the same attachment is referenced multiple times, it should appear only
   once

### Non-Functional Requirements

1. **Transparency**: Script authors shouldn't need to add special code for automatic propagation
2. **Consistency**: Follow existing patterns (e.g., `wake_llm` tracking)
3. **Performance**: Minimal overhead for scripts without attachments
4. **Maintainability**: Clean separation of concerns

## Current Architecture

### Key Components

#### 1. execute_script Tool

**Location**: `src/family_assistant/tools/execute_script.py:25-140`

```python
async def execute_script_tool(
    exec_context: ToolExecutionContext,
    script: str,
    globals: dict[str, Any] | None = None,
) -> str:  # ⚠️ Returns only string
    # Creates StarlarkEngine
    # Executes script
    # Formats result as string
    # Returns text only
```

#### 2. StarlarkEngine

**Location**: `src/family_assistant/scripting/engine.py:47-436`

- Executes Starlark scripts in sandboxed environment
- Provides APIs to scripts: time, JSON, attachments, tools
- Already tracks `wake_llm` contexts (lines 183-295)

#### 3. AttachmentAPI

**Location**: `src/family_assistant/scripting/apis/attachments.py:402-481`

```python
class AttachmentAPI:
    def create(self, content, filename, description, mime_type) -> str:
        # Stores file content
        # Registers in database with source_type="script"
        # Returns attachment UUID
```

#### 4. ToolsAPI

**Location**: `src/family_assistant/scripting/apis/tools.py:365-538`

```python
class ToolsAPI:
    def execute(self, tool_name, *args, **kwargs) -> str | dict[str, Any]:
        # Executes tool
        # If ToolResult has attachments:
        #   - Stores them via store_and_register_tool_attachment
        #   - Returns attachment ID(s) to script
```

### Existing Pattern: wake_llm

The codebase already implements a similar pattern for `wake_llm`:

**StarlarkEngine tracking (lines 183-295):**

```python
# Initialize accumulator
self._wake_llm_contexts: list[dict[str, Any]] = []

# Function that appends to accumulator
def wake_llm_impl(context, include_event=True):
    self._wake_llm_contexts.append({
        "context": context,
        "include_event": include_event
    })

# Retrieve after execution
def get_pending_wake_contexts(self) -> list[dict[str, Any]]:
    return self._pending_wake_contexts.copy()
```

**execute_script usage (lines 90-116):**

```python
# After execution
wake_contexts = engine.get_pending_wake_contexts()

if wake_contexts:
    # Include in response text
    response_parts.append("\n--- Wake LLM Contexts ---")
    # ... format contexts
```

**Key insight**: This establishes the pattern of **accumulate-during-execution, retrieve-after**.

## Proposed Solution

### Design Overview

Implement attachment tracking using the same pattern as `wake_llm`:

1. **Track** attachment IDs during script execution
2. **Retrieve** tracked IDs after execution
3. **Include** in ToolResult returned by execute_script

### Architecture Changes

#### 1. StarlarkEngine: Add Attachment Tracking

**Location**: `src/family_assistant/scripting/engine.py`

Add tracking similar to `wake_llm_contexts`:

```python
class StarlarkEngine:
    def __init__(self, ...):
        # Existing
        self._wake_llm_contexts: list[dict[str, Any]] = []

        # New: Track created attachments
        self._created_attachments: list[str] = []

    def register_attachment(self, attachment_id: str) -> None:
        """
        Register an attachment created during script execution.

        Called by AttachmentAPI and ToolsAPI when attachments are created.
        """
        if attachment_id and attachment_id not in self._created_attachments:
            self._created_attachments.append(attachment_id)
            logger.debug(f"Registered attachment: {attachment_id}")

    def get_created_attachments(self) -> list[str]:
        """
        Get all attachments created during script execution.

        Returns:
            List of attachment IDs (UUIDs)
        """
        return self._created_attachments.copy()
```

**Placement**: Add after `get_pending_wake_contexts()` method (~line 436)

#### 2. AttachmentAPI: Register Created Attachments

**Location**: `src/family_assistant/scripting/apis/attachments.py`

Modify to accept and use engine reference:

```python
class AttachmentAPI:
    def __init__(
        self,
        attachment_registry: AttachmentRegistry,
        conversation_id: str,
        db_engine: AsyncEngine,
        main_loop: asyncio.AbstractEventLoop | None = None,
        engine: StarlarkEngine | None = None,  # New parameter
    ):
        self.attachment_registry = attachment_registry
        self.conversation_id = conversation_id
        self.db_engine = db_engine
        self.main_loop = main_loop
        self.engine = engine  # New field

    def create(
        self,
        content: bytes | str,
        filename: str,
        description: str = "",
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Create a new attachment from script-generated content."""
        # ... existing storage logic ...

        attachment_id = # ... returned from _create_async

        # Register with engine for propagation
        if self.engine:
            self.engine.register_attachment(attachment_id)

        return attachment_id
```

Update factory function:

```python
def create_attachment_api(
    execution_context: ToolExecutionContext,
    main_loop: asyncio.AbstractEventLoop | None = None,
    engine: StarlarkEngine | None = None,  # New parameter
) -> AttachmentAPI:
    """Create an AttachmentAPI instance from execution context."""
    # ... existing validation ...

    return AttachmentAPI(
        attachment_registry=execution_context.attachment_registry,
        conversation_id=execution_context.conversation_id,
        db_engine=execution_context.db_engine,
        main_loop=main_loop,
        engine=engine,  # Pass engine
    )
```

**Key change**: Add `engine` parameter and call `register_attachment()` after creating.

#### 3. ToolsAPI: Register Tool Result Attachments

**Location**: `src/family_assistant/scripting/apis/tools.py`

Modify to accept and use engine reference:

```python
class ToolsAPI:
    def __init__(
        self,
        tools_provider: ToolsProvider,
        execution_context: ToolExecutionContext,
        allowed_tools: set[str] | None = None,
        deny_all_tools: bool = False,
        main_loop: asyncio.AbstractEventLoop | None = None,
        engine: StarlarkEngine | None = None,  # New parameter
    ):
        self.tools_provider = tools_provider
        self.execution_context = execution_context
        self.allowed_tools = allowed_tools
        self.deny_all_tools = deny_all_tools
        self.main_loop = main_loop
        self.engine = engine  # New field

    def execute(self, tool_name, *args, **kwargs) -> str | dict[str, Any]:
        """Execute a tool with the given arguments."""
        # ... existing execution logic ...

        # When storing attachment (around line 463-483):
        registered_metadata = self._run_async(
            attachment_registry.store_and_register_tool_attachment(...)
        )

        # Register with engine for propagation
        if self.engine:
            self.engine.register_attachment(registered_metadata.attachment_id)

        attachment_ids.append(registered_metadata.attachment_id)
        # ... rest of existing code ...
```

Update factory function:

```python
def create_tools_api(
    tools_provider: ToolsProvider,
    execution_context: ToolExecutionContext,
    allowed_tools: set[str] | None = None,
    deny_all_tools: bool = False,
    main_loop: asyncio.AbstractEventLoop | None = None,
    engine: StarlarkEngine | None = None,  # New parameter
) -> ToolsAPI:
    """Create a ToolsAPI instance."""
    return ToolsAPI(
        tools_provider=tools_provider,
        execution_context=execution_context,
        allowed_tools=allowed_tools,
        deny_all_tools=deny_all_tools,
        main_loop=main_loop,
        engine=engine,  # Pass engine
    )
```

**Key change**: Add `engine` parameter and call `register_attachment()` after storing tool result
attachments.

#### 4. StarlarkEngine: Wire Up Engine References

**Location**: `src/family_assistant/scripting/engine.py` (in `evaluate()` method)

Pass `self` to API factory functions:

```python
# Around line 194 (attachment API creation):
attachment_api = create_attachment_api(
    execution_context,
    main_loop=self._main_loop,
    engine=self,  # Add this
)

# Around line 213 (tools API creation):
tools_api = create_tools_api(
    self.tools_provider,
    execution_context,
    allowed_tools=self.config.allowed_tools,
    deny_all_tools=self.config.deny_all_tools,
    main_loop=self._main_loop,
    engine=self,  # Add this
)
```

#### 5. execute_script: Return ToolResult with Attachments

**Location**: `src/family_assistant/tools/execute_script.py`

Change return type and build ToolResult:

```python
from family_assistant.tools.types import ToolResult, ToolAttachment

async def execute_script_tool(
    exec_context: ToolExecutionContext,
    script: str,
    globals: dict[str, Any] | None = None,
) -> ToolResult:  # ⚠️ Changed from -> str
    """Execute a Starlark script in a sandboxed environment."""
    try:
        # ... existing execution logic ...
        result = await engine.evaluate_async(...)

        # Get created attachments from engine
        created_attachment_ids = engine.get_created_attachments()

        # Parse script result for explicit attachment IDs
        result_attachment_ids = _extract_attachment_ids_from_result(result)

        # Combine and deduplicate
        all_attachment_ids = list(dict.fromkeys(
            created_attachment_ids + result_attachment_ids
        ))

        # Check for any wake_llm contexts
        wake_contexts = engine.get_pending_wake_contexts()

        # Format the text response (existing logic)
        response_parts = []

        if result is None:
            response_parts.append("Script executed successfully with no return value.")
        elif isinstance(result, dict | list):
            response_parts.append(f"Script result:\n{json.dumps(result, indent=2)}")
        else:
            response_parts.append(f"Script result: {result}")

        if wake_contexts:
            response_parts.append("\n--- Wake LLM Contexts ---")
            # ... existing wake_llm formatting ...

        response_text = "\n".join(response_parts)

        # Build ToolResult
        attachments = None
        if all_attachment_ids:
            attachments = [
                ToolAttachment(attachment_id=att_id)
                for att_id in all_attachment_ids
            ]

        return ToolResult(
            text=response_text,
            attachments=attachments,
            data=result if isinstance(result, dict | list) else None
        )

    except ScriptSyntaxError as e:
        # Return errors as ToolResult with text only
        error_msg = # ... existing error formatting ...
        return ToolResult(text=f"Error: {error_msg}")

    # ... other exception handlers ...
```

Add helper function:

```python
def _extract_attachment_ids_from_result(result: Any) -> list[str]:
    """
    Extract attachment IDs from script return value.

    Supports:
    - Single UUID string: "550e8400-e29b-41d4-a716-446655440000"
    - Dict with attachment_id: {"text": "...", "attachment_id": "uuid"}
    - Dict with attachment_ids: {"text": "...", "attachment_ids": ["uuid1", "uuid2"]}

    Returns:
        List of attachment IDs found in result
    """
    ids = []

    # Single UUID string
    if isinstance(result, str) and _is_valid_uuid(result):
        ids.append(result)

    # Dict with attachment_id or attachment_ids
    elif isinstance(result, dict):
        # Single attachment_id
        if "attachment_id" in result:
            aid = result["attachment_id"]
            if isinstance(aid, str) and _is_valid_uuid(aid):
                ids.append(aid)

        # Multiple attachment_ids
        if "attachment_ids" in result:
            aid_list = result["attachment_ids"]
            if isinstance(aid_list, list):
                ids.extend([
                    aid for aid in aid_list
                    if isinstance(aid, str) and _is_valid_uuid(aid)
                ])

    return ids


def _is_valid_uuid(value: str) -> bool:
    """Check if string is a valid UUID."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False
```

### Behavior Specification

#### Automatic Propagation (Default)

All attachments created during script execution are automatically included:

```starlark
# Script
chart_id = attachment_create(content=data, filename="chart.png")
analysis_id = tools_execute("analyze_data", data=my_data)
return "Created visualizations"
```

**Result:**

```python
ToolResult(
    text="Script result: Created visualizations",
    attachments=[
        ToolAttachment(attachment_id=chart_id),
        ToolAttachment(attachment_id=analysis_id)
    ]
)
```

#### Explicit Control (Optional)

Scripts can explicitly specify attachments in return value:

```starlark
# Script creates 3 attachments
id1 = attachment_create(content=data1, filename="file1.txt")
id2 = attachment_create(content=data2, filename="file2.txt")
id3 = attachment_create(content=data3, filename="file3.txt")

# Return only specific ones
return {
    "text": "Created 3 files, returning first 2",
    "attachment_ids": [id1, id2]
}
```

**Design Decision: Union Behavior**

When script explicitly returns attachment IDs, the result includes:

- **All auto-tracked attachments** (id1, id2, id3 - created during execution)
- **Plus explicit return** (id1, id2 - specified in return value)
- **After deduplication**: [id1, id2, id3]

**Rationale**: Scripts shouldn't need to track every attachment manually. The explicit return is for
adding context/priority, not restricting visibility.

**Alternative considered and rejected**: Only return explicit IDs. This would require scripts to
manually track all attachments they create, defeating the purpose of automatic propagation.

#### No Attachments Case

Scripts without attachments work unchanged:

```starlark
# Script
result = search_notes(query="TODO")
return f"Found {len(result)} items"
```

**Result:**

```python
ToolResult(
    text="Script result: Found 5 items",
    attachments=None  # No attachments
)
```

### Error Handling

Errors continue to return text-only ToolResult:

```python
# Syntax error
ToolResult(text="Error: Syntax error in script at line 5: ...")

# Timeout error
ToolResult(text="Error: Script execution timed out after 600 seconds")

# Execution error
ToolResult(text="Error: Script execution failed: ...")
```

## Testing Plan

### Test Coverage

**File**: `tests/functional/tools/test_execute_script.py`

#### 1. Test Automatic Attachment Creation Propagation

```python
async def test_attachment_create_propagation(
    processing_service, test_conversation_id
):
    """Test that attachments created via attachment_create are propagated."""
    script = """
chart_id = attachment_create(
    content="chart data",
    filename="chart.png",
    description="Sales chart",
    mime_type="image/png"
)
return "Created chart"
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.text == "Script result: Created chart"
    assert result.attachments is not None
    assert len(result.attachments) == 1
    assert result.attachments[0].attachment_id  # Valid UUID
```

#### 2. Test Tool Result Attachment Propagation

```python
async def test_tool_result_attachment_propagation(
    processing_service, mock_tool_with_attachment
):
    """Test that attachments from tool results are propagated."""
    script = """
result = tools_execute("generate_report", data="test")
return "Generated report"
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1
```

#### 3. Test Multiple Attachments

```python
async def test_multiple_attachments(processing_service, test_conversation_id):
    """Test that multiple attachments are all propagated."""
    script = """
id1 = attachment_create(content="data1", filename="file1.txt")
id2 = attachment_create(content="data2", filename="file2.txt")
id3 = tools_execute("generate_chart", data="test")
return "Created 3 attachments"
"""

    result = await execute_script_tool(exec_context, script)

    assert result.attachments is not None
    assert len(result.attachments) == 3
```

#### 4. Test Explicit Attachment Return

```python
async def test_explicit_attachment_return(
    processing_service, test_conversation_id
):
    """Test explicit attachment_ids in return value."""
    script = """
id1 = attachment_create(content="data1", filename="file1.txt")
id2 = attachment_create(content="data2", filename="file2.txt")
return {"text": "Created files", "attachment_ids": [id1]}
"""

    result = await execute_script_tool(exec_context, script)

    # Should include both auto-tracked AND explicit (union)
    assert result.attachments is not None
    assert len(result.attachments) == 2  # Both id1 and id2
```

#### 5. Test Deduplication

```python
async def test_attachment_deduplication(
    processing_service, test_conversation_id
):
    """Test that duplicate attachment IDs are deduplicated."""
    script = """
id1 = attachment_create(content="data", filename="file.txt")
return {"attachment_ids": [id1, id1, id1]}  # Same ID 3 times
"""

    result = await execute_script_tool(exec_context, script)

    assert result.attachments is not None
    assert len(result.attachments) == 1  # Deduplicated
```

#### 6. Test No Attachments

```python
async def test_no_attachments(processing_service):
    """Test scripts without attachments work unchanged."""
    script = """
result = 2 + 2
return f"Result is {result}"
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.text == "Script result: Result is 4"
    assert result.attachments is None
```

#### 7. Test Backward Compatibility

```python
async def test_backward_compatibility(processing_service):
    """Test existing scripts continue working."""
    # This is the same as test_no_attachments but with realistic script
    script = """
notes = json_decode(search_notes(query="TODO"))
count = len(notes) if notes else 0
return f"Found {count} TODO items"
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert "TODO items" in result.text
    assert result.attachments is None or len(result.attachments) == 0
```

#### 8. Test Error Cases

```python
async def test_error_returns_text_only(processing_service):
    """Test that errors return ToolResult with text only."""
    script = "invalid syntax !@#"

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert "Error:" in result.text
    assert result.attachments is None
```

### Integration Testing

Verify end-to-end behavior:

1. **LLM receives attachments**: Test that LLM gets attachment references
2. **Attachment accessibility**: Test that LLM can reference attachments by ID
3. **Multi-turn conversations**: Test attachments work across conversation turns

## Migration Plan

### Phase 1: Implementation (Breaking Change)

1. **Add tracking to StarlarkEngine**

   - Add `_created_attachments` list
   - Add `register_attachment()` method
   - Add `get_created_attachments()` method

2. **Update AttachmentAPI**

   - Add `engine` parameter
   - Call `engine.register_attachment()` in `create()`
   - Update factory function

3. **Update ToolsAPI**

   - Add `engine` parameter
   - Call `engine.register_attachment()` in `execute()`
   - Update factory function

4. **Wire up engine in StarlarkEngine.evaluate()**

   - Pass `engine=self` to `create_attachment_api()`
   - Pass `engine=self` to `create_tools_api()`

5. **Update execute_script_tool**

   - Change return type to `ToolResult`
   - Add `_extract_attachment_ids_from_result()` helper
   - Add `_is_valid_uuid()` helper
   - Build ToolResult with attachments
   - Update error handling to return ToolResult

### Phase 2: Testing

1. **Add unit tests** for new methods
2. **Add integration tests** for execute_script
3. **Run full test suite** to verify backward compatibility
4. **Manual testing** with real scripts

### Phase 3: Documentation

1. **Update execute_script tool description** to mention automatic attachment propagation
2. **Update scripting.md** user documentation
3. **Add examples** of attachment propagation to docs

### Breaking Changes

**execute_script return type change**: `str` → `ToolResult`

**Impact analysis:**

- ✅ Tool infrastructure handles ToolResult natively (no changes needed)
- ✅ LLM receives ToolResult messages (already supported)
- ⚠️ Tests expect string results (need updates)
- ⚠️ Any direct callers of `execute_script_tool()` (need updates)

**Mitigation:**

- Update all tests in same PR
- Search codebase for direct calls to `execute_script_tool()`
- ToolResult.text provides backward-compatible text content

## Open Questions

### 1. Explicit Return Behavior

**Question**: When script explicitly returns `attachment_ids`, should we:

- **Option A**: Return only explicit IDs (ignore auto-tracked)
- **Option B**: Return only auto-tracked IDs (ignore explicit)
- **Option C**: Return union of both (deduplicated)

**Recommendation**: **Option C (Union)** - Provides maximum flexibility without requiring scripts to
track everything manually.

**Decision**: Pending approval ✅

### 2. Attachment Metadata

**Question**: Should ToolAttachment include more metadata (mime_type, description, filename)?

**Current**: Only `attachment_id` is included in ToolAttachment

**Pros of adding metadata:**

- LLM can see what the attachment is without fetching
- Richer context for decision-making

**Cons:**

- Increased payload size
- Need to fetch metadata from database
- Duplicates data already available via attachment API

**Recommendation**: Start with IDs only, add metadata if needed later.

**Decision**: Pending approval ✅

### 3. Performance Impact

**Question**: Does fetching attachment metadata add significant overhead?

**Analysis**:

- Tracking: O(1) per attachment creation (just append to list)
- Retrieval: O(n) where n = number of attachments (typically < 10)
- Deduplication: O(n) using dict.fromkeys()
- No database queries needed (IDs already available)

**Recommendation**: Negligible performance impact.

### 4. Tool Description Update

**Question**: How much detail about attachment propagation should be in the tool description?

**Options:**

- Minimal: "Attachments created during execution are automatically included"
- Detailed: Full explanation with examples

**Recommendation**: Brief mention in main description + example in "Working with attachments"
section.

**Decision**: Pending approval ✅

## Security Considerations

### Attachment Access Control

**Question**: Should scripts be able to return attachments they didn't create?

**Current behavior**: Scripts can only:

- Create new attachments (via `attachment_create`)
- Access attachments in their conversation (via `attachment_get`)
- Pass attachment IDs to tools

**Proposed behavior**: Same as current - no changes to access control.

**Security boundary**: Conversation-scoped attachment access remains enforced.

### ID Validation

**Question**: Should we validate that returned attachment IDs exist and belong to the conversation?

**Recommendation**: No validation in execute_script (trust attachment registry).

**Rationale:**

- Attachment access is already conversation-scoped in AttachmentRegistry
- Invalid IDs will fail when LLM tries to access them
- Validation would require database query per attachment
- Error handling exists at access time

## Alternative Designs Considered

### Alternative 1: Explicit Return Only

**Design**: Only propagate attachments explicitly returned by script

**Pros:**

- Explicit control for script author
- No "magic" behavior

**Cons:**

- Scripts must track all attachment IDs manually
- Easy to forget to include attachments
- Defeats purpose of simplifying script authoring

**Rejected**: Too burdensome for script authors

### Alternative 2: Global Registry

**Design**: Use global registry instead of engine tracking

**Pros:**

- No need to pass engine reference
- Simpler wiring

**Cons:**

- Violates "no mutable global state" principle
- Harder to test (need to clean up between tests)
- Thread safety concerns
- Goes against project architecture guidelines

**Rejected**: Violates core design principles

### Alternative 3: Context Manager Pattern

**Design**: Use context manager to track attachments

```python
with attachment_tracker() as tracker:
    result = execute_script(...)
    attachments = tracker.get_attachments()
```

**Pros:**

- Clear scope of tracking
- Automatic cleanup

**Cons:**

- More complex wiring
- Overkill for this use case
- StarlarkEngine already manages lifecycle

**Rejected**: Unnecessary complexity

## Success Criteria

### Implementation Complete When:

1. ✅ All code changes implemented
2. ✅ All tests pass (existing + new)
3. ✅ Linters pass (ruff, basedpyright, pylint)
4. ✅ Documentation updated
5. ✅ Manual testing confirms expected behavior

### Acceptance Criteria:

1. **Automatic propagation works**: Attachments created via `attachment_create()` appear in
   ToolResult
2. **Tool results work**: Attachments from tool calls appear in ToolResult
3. **Explicit control works**: Scripts can specify attachment_ids in return value
4. **Deduplication works**: Duplicate IDs appear only once
5. **Backward compatible**: Existing scripts without attachments work unchanged
6. **Errors handled**: Error cases return text-only ToolResult
7. **LLM receives attachments**: Integration test confirms LLM can see and use attachments

## Future Enhancements

### Potential Improvements:

1. **Attachment metadata in ToolResult**: Include mime_type, description, filename
2. **Attachment filtering**: Allow scripts to mark attachments as "internal only" vs "return to LLM"
3. **Attachment ordering**: Preserve order or allow scripts to specify priority
4. **Attachment grouping**: Group related attachments together
5. **Lazy loading**: Only fetch attachment metadata when LLM requests it
6. **Size limits**: Warn or fail if too many attachments created

### Not in Scope:

- Attachment editing or deletion from scripts
- Attachment permissions beyond conversation scope
- Cross-conversation attachment sharing
- Attachment versioning
- Streaming attachments

## References

### Related Code

- `src/family_assistant/tools/execute_script.py` - Tool implementation
- `src/family_assistant/scripting/engine.py` - Script execution engine
- `src/family_assistant/scripting/apis/attachments.py` - Attachment API
- `src/family_assistant/scripting/apis/tools.py` - Tools API
- `src/family_assistant/tools/types.py` - ToolResult and ToolAttachment
- `tests/functional/tools/test_execute_script.py` - Existing tests
- `tests/functional/test_script_attachment_api.py` - Attachment API tests

### Related Design Docs

- `docs/design/multimodal-tool-results.md` - ToolResult design
- `docs/design/auto-attachment-display.md` - Attachment display
- `docs/design/json-attachment-handling.md` - JSON attachment handling
- `docs/design/scripting/` - Scripting system design

### User Documentation

- `docs/user/scripting.md` - Scripting guide for users

## Appendix: Code Examples

### Example 1: Simple Attachment Creation

```starlark
# Script creates a report
report_content = "# Report\n\nGenerated at: " + time_now()
report_id = attachment_create(
    content=report_content,
    filename="report.md",
    description="Weekly report",
    mime_type="text/markdown"
)

return "Report generated successfully"
```

**Result sent to LLM:**

```python
ToolResult(
    text="Script result: Report generated successfully",
    attachments=[ToolAttachment(attachment_id=report_id)]
)
```

### Example 2: Tool with Attachment Result

```starlark
# Script calls visualization tool
chart_result = tools_execute(
    "create_chart",
    data=[1, 2, 3, 4, 5],
    chart_type="line"
)

return "Chart created: " + chart_result
```

**Result sent to LLM:**

```python
ToolResult(
    text="Script result: Chart created: {attachment_id}",
    attachments=[ToolAttachment(attachment_id=chart_id)]
)
```

### Example 3: Multiple Attachments

```starlark
# Script creates multiple visualizations
results = []

for metric in ["sales", "revenue", "growth"]:
    chart_id = tools_execute(
        "create_chart",
        metric=metric,
        period="monthly"
    )
    results.append({"metric": metric, "chart_id": chart_id})

return {"created_charts": results}
```

**Result sent to LLM:**

```python
ToolResult(
    text="Script result:\n{...json...}",
    attachments=[
        ToolAttachment(attachment_id=chart_id_1),
        ToolAttachment(attachment_id=chart_id_2),
        ToolAttachment(attachment_id=chart_id_3)
    ],
    data={"created_charts": [...]}
)
```

### Example 4: Explicit Control

```starlark
# Script creates temporary and final attachments
temp_id = attachment_create(content="temp", filename="temp.txt")
intermediate_id = attachment_create(content="intermediate", filename="intermediate.txt")
final_id = attachment_create(content="final", filename="final.txt")

# Only return the final result
return {
    "text": "Processing complete",
    "attachment_ids": [final_id]
}
```

**Result sent to LLM:**

```python
ToolResult(
    text="Script result:\n{...json...}",
    attachments=[
        # Union of auto-tracked and explicit
        ToolAttachment(attachment_id=temp_id),
        ToolAttachment(attachment_id=intermediate_id),
        ToolAttachment(attachment_id=final_id)
    ]
)
```

______________________________________________________________________

**End of Design Document**
