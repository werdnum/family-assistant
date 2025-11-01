# Script Attachment Propagation Design

**Status**: Implemented with modifications **Date**: 2025-11-01 **Author**: Claude (via GitHub
Issue) **Updated**: 2025-11-01

## Summary

Enable attachments created by scripts (via `attachment_create()` or tool calls) to be propagated in
the `ToolResult` returned by `execute_script`, making them visible to the LLM. Scripts explicitly
return attachments using proper types (`ScriptAttachment`, `ScriptToolResult`) rather than raw UUID
strings.

## Problem Statement

### Current Behavior

Scripts can create attachments in two ways:

1. **Direct creation**: `attachment_create(content, filename, description, mime_type)` → returns
   UUID string
2. **Tool calls**: `create_vega_chart(spec=spec)` → returns UUID string (if tool returns ToolResult
   with attachments)

However, the `execute_script` tool:

- Returns only a string (line 30 in `execute_script.py`)
- Formats results as text (lines 94-118)
- **Does NOT propagate attachments** created during execution
- LLM never sees that attachments were created

**Example scenario:**

```starlark
# Script creates chart
chart_id = attachment_create(
    content=generate_chart_data(),
    filename="chart.png",
    description="Sales chart"
)

# Script calls analysis tool
analysis_id = create_data_analysis(data=my_data)

return "Created chart and analysis"
```

**What the LLM receives:**

```
"Script result: Created chart and analysis"
```

**What the LLM SHOULD receive:**

```python
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

## Design Principles

### 1. Explicit Returns (Not Auto-Tracking)

**Decision**: Scripts explicitly return what they want visible via return statements.

**Rationale**:

- Scripts are **deterministic code** written by LLMs, not interactive LLM calls
- The **return statement** makes visibility explicit
- Auto-tracking adds complexity without clear benefit
- Scripts that don't return attachments intentionally don't want them visible

**Contrast with LLM auto-attach pattern** (from `multimodal-manipulation.md`):

- LLMs can "forget" to call `attach_to_response` → needs auto-attach
- Scripts can't "forget" → explicit returns are sufficient

### 2. Proper Types (Not dict[str, Any])

**Decision**: Use typed objects (`ScriptAttachment`, `ScriptToolResult`) instead of raw UUID strings
or dicts.

**Rationale**:

- Avoids `dict[str, Any]` antipattern
- Clear type semantics (attachment vs string)
- Enables functional composition
- Consistent with existing munging pattern

**Existing munging pattern**:

- **Input**: UUID strings → `ScriptAttachment` objects (via `process_attachment_arguments`)
- **Output**: ToolResult → `ScriptAttachment`/`ScriptToolResult` (proposed)

### 3. Functional Composition

**Target use case** (data visualization profile):

```starlark
return highlight_image(
    image=create_vega_chart(
        spec=spec,
        data=jq(".[].foo", input_attachment)
    ),
    regions=[{"x": 10, "y": 20}]
)
```

This natural functional composition should work seamlessly.

## Proposed Solution

### Architecture Overview

1. **ScriptToolResult** - New type for tool results in scripts
2. **ToolsAPI.execute()** - Munge ToolResult → ScriptToolResult/ScriptAttachment
3. **AttachmentAPI.create()** - Return ScriptAttachment (not UUID string)
4. **execute_script_tool** - Extract attachments from return value, return ToolResult

### Component Changes

#### 1. New Type: ScriptToolResult

**Location**: `src/family_assistant/scripting/apis/tools.py` (or new `types.py`)

```python
from dataclasses import dataclass
from family_assistant.scripting.apis.attachments import ScriptAttachment

@dataclass
class ScriptToolResult:
    """
    Result from a tool execution in a script context.

    This wraps ToolResult to provide script-friendly types:
    - Text as string
    - Attachments as list of ScriptAttachment objects (not UUIDs)

    Can be returned directly from scripts to propagate attachments.
    """
    text: str | None = None
    attachments: list[ScriptAttachment] | None = None

    def get_text(self) -> str | None:
        """Get the text result."""
        return self.text

    def get_attachments(self) -> list[ScriptAttachment]:
        """Get all attachments (empty list if none)."""
        return self.attachments or []
```

**Design notes**:

- Only `attachments` (plural), not `attachment` (singular) - consistent naming
- Provides getter methods for Starlark-friendly access
- Simple dataclass, no complex logic

#### 2. Update ToolsAPI.execute() Return Type

**Location**: `src/family_assistant/scripting/apis/tools.py:365-538`

**Current behavior**:

- Stores ToolResult attachments, returns UUID string or dict

**New behavior**:

- Stores ToolResult attachments, returns ScriptAttachment or ScriptToolResult

```python
def execute(
    self,
    tool_name: str,
    *args: Any,
    **kwargs: Any,
) -> str | ScriptAttachment | ScriptToolResult:  # Changed return type
    """Execute a tool with the given arguments."""

    # ... existing security checks and argument processing ...

    # Execute tool
    result = self._run_async(
        self.tools_provider.execute_tool(
            name=tool_name,
            arguments=processed_kwargs,
            context=self.execution_context,
        )
    )

    logger.debug(f"Tool '{tool_name}' executed successfully")

    # Handle ToolResult with attachments
    if isinstance(result, ToolResult):
        if result.attachments:
            attachment_registry = self.execution_context.attachment_registry
            if not attachment_registry:
                logger.warning(
                    f"Tool '{tool_name}' returned attachments but attachment_registry not available"
                )
                return result.to_string()

            # Store attachments and collect IDs (existing logic)
            attachment_ids = []
            for attachment in result.attachments:
                # ... existing storage logic ...
                attachment_ids.append(registered_metadata.attachment_id)

            # NEW: Convert stored attachment IDs to ScriptAttachment objects
            script_attachments = []
            for attachment_id in attachment_ids:
                script_att = self._run_async(
                    fetch_attachment_object(attachment_id, self.execution_context)
                )
                if script_att:
                    script_attachments.append(script_att)
                else:
                    logger.warning(f"Could not fetch stored attachment {attachment_id}")

            # Return appropriate type based on content
            if len(script_attachments) == 1 and not (result.text and result.text.strip()):
                # Single attachment, no meaningful text → return ScriptAttachment
                logger.debug(f"Returning single ScriptAttachment from '{tool_name}'")
                return script_attachments[0]
            else:
                # Text + attachments or multiple attachments → return ScriptToolResult
                return ScriptToolResult(
                    text=result.text,
                    attachments=script_attachments if script_attachments else None
                )
        else:
            # ToolResult with no attachments → return text
            return result.to_string()

    # Convert result to JSON string if it's a dict or list (existing logic)
    elif isinstance(result, dict | list):
        return json.dumps(result)
    else:
        return str(result)
```

**Key changes**:

- Fetch `ScriptAttachment` objects using `fetch_attachment_object()` (existing utility)
- Return `ScriptAttachment` for single attachment with no text
- Return `ScriptToolResult` for text + attachments or multiple attachments
- Return text string for no attachments

#### 3. Update AttachmentAPI.create() Return Type

**Location**: `src/family_assistant/scripting/apis/attachments.py:402-481`

**Current**: Returns UUID string **New**: Returns ScriptAttachment object

```python
def create(
    self,
    content: bytes | str,
    filename: str,
    description: str = "",
    mime_type: str = "application/octet-stream",
) -> ScriptAttachment:  # Changed from -> str
    """
    Create a new attachment from script-generated content.

    Returns:
        ScriptAttachment object (not UUID string)
    """
    try:
        # Existing async execution logic...
        if self.main_loop:
            future = asyncio.run_coroutine_threadsafe(
                self._create_async(content, filename, description, mime_type),
                self.main_loop,
            )
            attachment_metadata = future.result(timeout=30)
        else:
            attachment_metadata = asyncio.run(
                self._create_async(content, filename, description, mime_type)
            )

        # NEW: Return ScriptAttachment instead of UUID string
        return ScriptAttachment(
            metadata=attachment_metadata,
            registry=self.attachment_registry,
            db_context_getter=lambda: DatabaseContext(engine=self.db_engine),
        )

    except Exception as e:
        logger.error(f"Error creating attachment: {e}")
        raise ValueError(f"Failed to create attachment: {e}") from e

async def _create_async(
    self,
    content: bytes | str,
    filename: str,
    description: str,
    mime_type: str,
) -> AttachmentMetadata:  # Changed return type for clarity
    """Async implementation of create - returns metadata."""
    # ... existing implementation ...
    return attachment_metadata
```

**Key changes**:

- Return `ScriptAttachment` object wrapping the metadata
- `_create_async` returns `AttachmentMetadata` for clarity
- Maintains existing storage logic

#### 4. Update Tool Wrapper in StarlarkEngine

**Location**: `src/family_assistant/scripting/engine.py:237-269`

**Current**: Wrappers call `tools_api.execute()` and return `str | dict[str, Any]` **New**: Update
return type annotation to match new behavior

```python
# In StarlarkEngine.evaluate() around line 243
def make_tool_wrapper(
    name: str,
) -> Callable[..., str | ScriptAttachment | ScriptToolResult]:  # Updated annotation
    def tool_wrapper(
        *args: Any,
        **kwargs: Any,
    ) -> str | ScriptAttachment | ScriptToolResult:  # Updated annotation
        """Execute the tool with the given arguments."""
        if args:
            return tools_api.execute(name, *args, **kwargs)
        return tools_api.execute(name, **kwargs)

    return tool_wrapper
```

**Note**: This is just a type annotation update. The wrapper still calls `tools_api.execute()` which
now returns proper types.

## Implementation Challenges and Alternatives

### Challenge: Starlark JSON Serialization Constraint

During implementation, we discovered a **critical constraint** with starlark-pyo3: it uses JSON as
an intermediate format for serialization between Python and Starlark.

**From starlark-pyo3 documentation:**

> "To convert values between starlark and Python, JSON is currently being used as an intermediate
> format"

**Implication**: Only JSON-serializable Python types can cross the Python↔Starlark boundary:

- ✅ Strings, numbers, booleans, None
- ✅ Lists and dicts with JSON-serializable contents
- ❌ Custom Python objects (like `ScriptAttachment`, `ScriptToolResult`)

**Error encountered** when returning `ScriptAttachment` objects:

```python
TypeError: Object of type ScriptAttachment is not JSON serializable
```

This error occurs **inside starlark-pyo3** when it tries to serialize the script's return value, not
in our display code. There's no way to provide a custom JSON encoder for this process.

### Alternatives Explored

#### Option 1: Make dataclasses JSON-serializable (Rejected)

**Approach**: Add `.to_dict()` methods or use libraries like `dataclasses-json`/`pydantic`

**Why rejected:**

- We don't control starlark-pyo3's internal JSON serialization
- Would require scripts to manually call `.to_dict()` before returning
- Error-prone for script authors (easy to forget)
- Doesn't solve the fundamental starlark-pyo3 constraint

**Example of error-prone pattern:**

```starlark
# Easy to forget .to_dict()
att = attachment_create(...)
att  # ERROR: not JSON serializable

# Must remember:
att.to_dict()  # Works, but error-prone
```

#### Option 2: Return plain UUID strings (Rejected)

**Approach**: Return just the attachment UUID as a string

**Why rejected:**

- Loses all metadata (filename, mime_type, size, description)
- Poor developer UX - scripts can't access attachment info
- Makes functional composition awkward
- Harder for LLM to understand what was created

**Example of poor UX:**

```starlark
# What is this? Can't tell without fetching metadata
att = attachment_create(...)  # Returns "550e8400-..."
print(att)  # Just a UUID string, no context

# Can't access filename, mime_type, etc.
```

#### Option 3: Dict-based API (Chosen)

**Approach**: Return dictionaries with structured metadata fields

**Why chosen:**

- ✅ **JSON-serializable**: Works seamlessly with starlark-pyo3
- ✅ **Good UX**: Scripts can access fields like `att["filename"]`, `att["mime_type"]`
- ✅ **Functional composition**: Pass dicts directly to other tools
- ✅ **Clear structure**: LLM can see what was created
- ✅ **Type safety where possible**: Keep typed classes for internal Python code

**Implementation pattern:**

```starlark
# Returns dict with full metadata
att = attachment_create(
    content="data",
    filename="chart.png",
    mime_type="image/png"
)

# att = {"id": "550e...", "filename": "chart.png", "mime_type": "image/png", ...}

# Can access fields
print("Created: " + att["filename"])

# Pass to other tools directly
chart = create_vega_chart(
    spec=spec,
    data_attachments=[att]  # Tools extract ID automatically
)
```

### Final Architecture: Hybrid Approach

**At Starlark boundary** (what scripts see):

- `attachment_create()` returns: `dict[str, Any]` with
  `{"id", "filename", "mime_type", "size", "description"}`
- Tools return: `str | dict[str, Any]` (either text or attachment dict/multi-attachment dict)

**Internally** (Python code):

- Keep `ScriptAttachment` class for type safety and IDE support
- Keep `ScriptToolResult` class for documentation clarity
- Use these types for internal Python code that doesn't cross Starlark boundary

**Extraction** (execute_script.py):

- Extract IDs from dicts with `"id"` field
- Handle both legacy formats and new dict format
- Build `ToolResult` with proper `ToolAttachment` objects

**Benefits of hybrid approach:**

1. **Works around starlark-pyo3 constraints**: Dicts are JSON-serializable
2. **Maintains type safety**: Internal Python code uses typed classes
3. **Good script UX**: Structured dicts with clear fields
4. **Backward compatible**: Can still extract from various formats
5. **Documentation clarity**: Design docs can describe ideal types, implementation uses dicts

**Trade-offs accepted:**

- Can't use Python type hints in starlark (Starlark doesn't have type hints anyway)
- Dict fields accessed via strings (`att["id"]`) instead of attributes (`att.id`)
- ast-grep requires exemptions for `dict[str, Any]` at Starlark boundary (justified by constraint)

#### 5. Update execute_script_tool Return Type

**Location**: `src/family_assistant/tools/execute_script.py:25-140`

**Current**: Returns `str` **New**: Returns `ToolResult`

```python
from family_assistant.tools.types import ToolResult, ToolAttachment
from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.scripting.apis.tools import ScriptToolResult

async def execute_script_tool(
    exec_context: ToolExecutionContext,
    script: str,
    globals: dict[str, Any] | None = None,
) -> ToolResult:  # Changed from -> str
    """
    Execute a Starlark script in a sandboxed environment.

    Returns:
        ToolResult with text and any attachments returned by the script
    """
    try:
        # ... existing engine creation and execution ...
        result = await engine.evaluate_async(
            script=script,
            globals_dict=globals,
            execution_context=exec_context if tools_provider else None,
        )

        # Extract attachment IDs from return value
        attachment_ids = _extract_attachment_ids_from_result(result)

        # Check for wake_llm contexts (existing logic)
        wake_contexts = engine.get_pending_wake_contexts()

        # Format text response (existing logic)
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

        # Build ToolResult with attachments
        attachments = None
        if attachment_ids:
            attachments = [
                ToolAttachment(attachment_id=aid)
                for aid in attachment_ids
            ]

        return ToolResult(
            text=response_text,
            attachments=attachments,
            data=result if isinstance(result, dict | list) else None
        )

    except ScriptSyntaxError as e:
        # Return errors as ToolResult with text only
        error_msg = f"Syntax error in script"
        if e.line:
            error_msg += f" at line {e.line}"
        error_msg += f": {str(e)}"
        logger.error(error_msg)
        return ToolResult(text=f"Error: {error_msg}")

    except ScriptTimeoutError as e:
        error_msg = f"Script execution timed out after {e.timeout_seconds} seconds"
        logger.error(error_msg)
        return ToolResult(text=f"Error: {error_msg}")

    except ScriptExecutionError as e:
        error_msg = f"Script execution failed: {str(e)}"
        logger.error(error_msg)
        return ToolResult(text=f"Error: {error_msg}")

    except Exception as e:
        logger.error(f"Unexpected error executing script: {e}", exc_info=True)
        return ToolResult(text=f"Error: Unexpected error executing script: {e}")
```

**Key changes**:

- Return `ToolResult` instead of string
- Extract attachment IDs from script return value
- Build `ToolResult` with attachments
- All error cases return `ToolResult` with text only

#### 6. Helper Function: Extract Attachment IDs

**Location**: `src/family_assistant/tools/execute_script.py` (new function)

```python
def _extract_attachment_ids_from_result(result: Any) -> list[str]:
    """
    Extract attachment IDs from script return value.

    Supports:
    - ScriptAttachment object
    - ScriptToolResult object
    - List of ScriptAttachments
    - UUID strings (backward compatibility)
    - Dicts with attachments/attachment_ids keys (backward compatibility)

    Args:
        result: The script return value

    Returns:
        List of attachment UUIDs (deduplicated)
    """
    ids = []

    # Single ScriptAttachment
    if isinstance(result, ScriptAttachment):
        return [result.get_id()]

    # ScriptToolResult
    if isinstance(result, ScriptToolResult):
        return [att.get_id() for att in result.get_attachments()]

    # List of attachments or UUIDs
    if isinstance(result, list):
        for item in result:
            if isinstance(item, ScriptAttachment):
                ids.append(item.get_id())
            elif isinstance(item, str) and _is_valid_uuid(item):
                ids.append(item)  # Backward compatibility
        return ids

    # Dict with attachments (backward compatibility)
    if isinstance(result, dict):
        # Check for attachments key
        if "attachments" in result:
            items = result["attachments"]
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, ScriptAttachment):
                        ids.append(item.get_id())
                    elif isinstance(item, str) and _is_valid_uuid(item):
                        ids.append(item)

        # Check for attachment_ids key (legacy)
        if "attachment_ids" in result:
            items = result["attachment_ids"]
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, ScriptAttachment):
                        ids.append(item.get_id())
                    elif isinstance(item, str) and _is_valid_uuid(item):
                        ids.append(item)

        return list(dict.fromkeys(ids))  # Deduplicate preserving order

    # Single UUID string (backward compatibility)
    if isinstance(result, str) and _is_valid_uuid(result):
        return [result]

    return []


def _is_valid_uuid(value: str) -> bool:
    """Check if string is a valid UUID."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False
```

## Script Usage Examples (As Implemented)

### Example 1: Simple Attachment Creation

```starlark
# Create attachment - returns dict with metadata
chart = attachment_create(
    content=chart_data,
    filename="sales_chart.png",
    description="Q4 Sales Chart",
    mime_type="image/png"
)

# chart = {"id": "550e...", "filename": "sales_chart.png", "mime_type": "image/png", ...}

# Return it to make it visible to LLM
chart  # Last expression is returned
```

**Result**:

```python
ToolResult(
    text="Script result: Attachment(id=550e..., mime_type=image/png, size=1024 bytes)",
    attachments=[ToolAttachment(mime_type="image/png", attachment_id="550e...")]
)
```

### Example 2: Tool Call Returns Attachment

```starlark
# Tool returns dict with attachment info
chart = create_vega_chart(
    spec='{"mark": "bar", ...}',
    data_attachments=[sales_data]
)

# chart = {"id": "...", "filename": "chart.png", "mime_type": "image/png", ...}

# Access fields
print("Created: " + chart["filename"])

# Return it
chart
```

### Example 3: Functional Composition (Target Use Case)

```starlark
# Compose tools naturally - dicts pass through seamlessly
chart = create_vega_chart(
    spec=vega_spec,
    data_attachments=[jq_query(input_attachment, ".[].revenue")]
)

# Highlight the chart (accepts dict, extracts ID automatically)
highlighted = highlight_image(
    image=chart,
    regions=[{"x": 10, "y": 20, "width": 100, "height": 50}]
)

highlighted  # Return final result
```

**This works because**:

- `jq_query()` processes input attachment and returns dict
- `create_vega_chart()` accepts dict in `data_attachments`, extracts ID
- `create_vega_chart()` returns dict with new attachment
- `highlight_image()` accepts dict as `image` parameter, extracts ID
- Final result is dict that gets propagated with correct metadata

### Example 4: Multiple Attachments

```starlark
# Create multiple visualizations
sales_chart = create_chart(data=sales, type="bar")
revenue_chart = create_chart(data=revenue, type="line")
growth_chart = create_chart(data=growth, type="area")

# All are dicts: {"id": "...", "filename": "...", ...}

# Return list to show all
[sales_chart, revenue_chart, growth_chart]
```

**Result**:

```python
ToolResult(
    text="Script result: [multiple attachments listed]",
    attachments=[
        ToolAttachment(mime_type="image/png", attachment_id=sales_chart["id"]),
        ToolAttachment(mime_type="image/png", attachment_id=revenue_chart["id"]),
        ToolAttachment(mime_type="image/png", attachment_id=growth_chart["id"])
    ]
)
```

### Example 5: Tool Result with Text and Attachments

```starlark
# Tool returns dict with text + attachments
result = analyze_and_visualize(data=sales_data)

# result = {"text": "Analysis: Sales up 25%", "attachments": [{...}, {...}]}

# Access fields
print(result["text"])
for att in result["attachments"]:
    print("- " + att["filename"])

# Return the whole result (all attachments extracted)
result
```

**Result**:

```python
ToolResult(
    text="Script result: [dict with text and attachments]",
    attachments=[ToolAttachment(...), ToolAttachment(...)]  # All extracted
)
```

### Example 6: Selective Returns (Intermediate Results)

```starlark
# Create multiple attachments during processing
raw_data = attachment_create(content=raw, filename="raw.json")
intermediate = attachment_create(content=processed, filename="intermediate.json")
final_chart = create_chart(data=processed)

# All are dicts, but only return final
final_chart  # Only this is visible to LLM

# raw_data and intermediate are NOT visible to LLM (not returned)
# They exist in the database but won't be propagated
```

### Example 7: Accessing Attachment Metadata

```starlark
# Create attachment
data_file = attachment_create(
    content="Temperature readings: 72, 75, 73",
    filename="temp_data.txt",
    description="Temperature sensor data",
    mime_type="text/plain"
)

# Access metadata fields
print("Created: " + data_file["filename"])
print("Type: " + data_file["mime_type"])
print("Size: " + str(data_file["size"]) + " bytes")
print("ID: " + data_file["id"])

# Pass to tool
chart = create_chart(data_source=data_file["id"])

# Or pass whole dict (tool extracts ID)
chart = create_chart(data_source=data_file)

chart
```

## Testing Plan

### Test Coverage

**File**: `tests/functional/tools/test_execute_script.py`

#### 1. Test Single ScriptAttachment Return

```python
async def test_script_returns_single_attachment(
    processing_service, test_conversation_id
):
    """Test script that returns a single ScriptAttachment."""
    script = """
chart = attachment_create(
    content="chart data",
    filename="chart.png",
    description="Test chart",
    mime_type="image/png"
)
return chart
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1
    assert result.attachments[0].attachment_id  # Valid UUID
```

#### 2. Test Tool Result with Attachment

```python
async def test_tool_returns_attachment(
    processing_service, mock_tool_with_attachment
):
    """Test tool that returns ToolResult with attachment."""
    script = """
chart = create_vega_chart(spec={"mark": "bar"}, data=[1, 2, 3])
return chart
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1
```

#### 3. Test Multiple Attachments List

```python
async def test_script_returns_attachment_list(
    processing_service, test_conversation_id
):
    """Test script that returns list of attachments."""
    script = """
chart1 = attachment_create(content="data1", filename="chart1.png")
chart2 = attachment_create(content="data2", filename="chart2.png")
chart3 = attachment_create(content="data3", filename="chart3.png")
return [chart1, chart2, chart3]
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 3
```

#### 4. Test ScriptToolResult Return

```python
async def test_tool_returns_script_tool_result(
    processing_service, mock_tool_returning_text_and_attachment
):
    """Test tool that returns ScriptToolResult with text and attachments."""
    script = """
result = analyze_and_chart(data=[1, 2, 3, 4, 5])
# result is ScriptToolResult with text and attachments
return result
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) >= 1
    assert "analysis" in result.text.lower()  # Contains text from tool
```

#### 5. Test Functional Composition

```python
async def test_functional_composition(
    processing_service, mock_chart_and_highlight_tools
):
    """Test functional composition of tools returning attachments."""
    script = """
return highlight_image(
    image=create_vega_chart(spec={"mark": "bar"}, data=[1, 2, 3]),
    regions=[{"x": 10, "y": 20, "width": 100, "height": 50}]
)
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1  # Final highlighted image
```

#### 6. Test Selective Return (Not All Attachments)

```python
async def test_selective_attachment_return(
    processing_service, test_conversation_id
):
    """Test that only returned attachments are visible."""
    script = """
# Create 3 attachments
temp1 = attachment_create(content="temp1", filename="temp1.txt")
temp2 = attachment_create(content="temp2", filename="temp2.txt")
final = attachment_create(content="final", filename="final.txt")

# Only return final
return final
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1  # Only final, not temp1/temp2
```

#### 7. Test Backward Compatibility (UUID Strings)

```python
async def test_backward_compat_uuid_string(
    processing_service, test_conversation_id
):
    """Test backward compatibility with UUID string returns."""
    script = """
chart = attachment_create(content="data", filename="chart.png")
# Return UUID string (old style)
return chart.get_id()
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1
```

#### 8. Test No Attachments

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
    assert result.attachments is None or len(result.attachments) == 0
```

#### 9. Test Error Cases Return Text-Only

```python
async def test_error_returns_text_only(processing_service):
    """Test that errors return ToolResult with text only."""
    script = "invalid syntax !@#"

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert "Error:" in result.text
    assert result.attachments is None
```

#### 10. Test Deduplication

```python
async def test_attachment_deduplication(
    processing_service, test_conversation_id
):
    """Test that duplicate attachments in lists are deduplicated."""
    script = """
chart = attachment_create(content="data", filename="chart.png")
# Return same attachment multiple times
return [chart, chart, chart]
"""

    result = await execute_script_tool(exec_context, script)

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1  # Deduplicated
```

## Implementation Plan

### Phase 1: Type Definitions and Core Changes

1. ✅ **Add ScriptToolResult type** to `scripting/apis/tools.py`
2. ✅ **Update ToolsAPI.execute()** to return proper types
3. ✅ **Update AttachmentAPI.create()** to return ScriptAttachment
4. ✅ **Update tool wrapper** type annotations in StarlarkEngine

### Phase 2: execute_script Changes

5. ✅ **Add \_extract_attachment_ids_from_result()** helper function
6. ✅ **Add \_is_valid_uuid()** helper function
7. ✅ **Update execute_script_tool()** to return ToolResult with attachments
8. ✅ **Update error handling** to return ToolResult

### Phase 3: Testing

09. ✅ **Add unit tests** for new types and helper functions
10. ✅ **Add integration tests** for execute_script with attachments
11. ✅ **Run full test suite** to verify backward compatibility
12. ✅ **Manual testing** with real scripts

### Phase 4: Documentation

13. ✅ **Update execute_script tool description** to include typed examples
14. ✅ **Update scripting.md** user documentation with new patterns
15. ✅ **Add examples** of functional composition

## Breaking Changes

**execute_script return type change**: `str` → `ToolResult`

**Impact analysis**:

- ✅ Tool infrastructure handles ToolResult natively (no changes needed)
- ✅ LLM receives ToolResult messages (already supported)
- ⚠️ Tests expect string results (need updates)
- ⚠️ Any direct callers of `execute_script_tool()` (need updates - search codebase)

**Mitigation**:

- Update all tests in same PR
- Search codebase for direct calls: `git grep "execute_script_tool"`
- ToolResult.text provides backward-compatible text content
- Infrastructure already handles ToolResult properly

**Script compatibility**:

- ✅ Scripts that return strings/numbers/dicts still work
- ✅ Scripts that don't return attachments unchanged
- ✅ Backward compatible with UUID string returns
- ✅ New typed returns are additive (opt-in)

## Benefits

1. **Explicit Control**: Scripts control visibility via return values (no magic auto-tracking)
2. **Proper Types**: No `dict[str, Any]` antipattern - clear type semantics
3. **Functional Composition**: Natural composition like `highlight_image(create_vega_chart(...))`
4. **Backward Compatible**: UUID strings still work
5. **Simple Implementation**: No complex tracking infrastructure needed
6. **Consistent Pattern**: Matches existing munging (UUID → ScriptAttachment on input)
7. **LLM-Friendly**: Few-shot examples guide LLM to use proper patterns

## Tool Description Updates

Update `execute_script` tool description to include typed examples:

````python
"**Returning Attachments:**\n"
"To make attachments visible to the LLM and user, return them from your script:\n\n"
"```starlark\n"
"# Single attachment\n"
"chart = attachment_create(content=data, filename='chart.png')\n"
"return chart  # Attachment displayed to user\n\n"
"# Multiple attachments\n"
"chart1 = create_chart(data=sales)\n"
"chart2 = create_chart(data=revenue)\n"
"return [chart1, chart2]  # Both displayed\n\n"
"# Functional composition\n"
"return highlight_image(\n"
"    image=create_vega_chart(spec=spec, data=data),\n"
"    regions=[{'x': 10, 'y': 20, 'width': 100, 'height': 50}]\n"
")\n"
"```\n\n"
"**Important:** Only returned attachments are visible to the LLM.\n"
"Attachments not returned are stored but not propagated.\n"
````

## Success Criteria

### Implementation Complete When:

1. ✅ ScriptToolResult type added
2. ✅ ToolsAPI.execute() returns proper types
3. ✅ AttachmentAPI.create() returns ScriptAttachment
4. ✅ execute_script returns ToolResult with attachments
5. ✅ All tests pass (existing + new)
6. ✅ Linters pass (ruff, basedpyright, pylint)
7. ✅ Documentation updated

### Acceptance Criteria:

1. **Explicit returns work**: Scripts that return ScriptAttachment/ScriptToolResult propagate
   attachments
2. **Tool composition works**: Functional composition like `highlight(create_chart(...))` works
3. **Multiple attachments work**: Lists of attachments propagated correctly
4. **Backward compatible**: Existing scripts without attachments work unchanged
5. **UUID strings work**: Old-style UUID string returns still supported
6. **Errors handled**: Error cases return text-only ToolResult
7. **LLM receives attachments**: Integration test confirms LLM can see and use attachments

## Future Enhancements

### Potential Improvements:

1. **Attachment metadata in ToolResult**: Include mime_type, description without LLM fetching
2. **Lazy evaluation**: Only fetch attachment content when accessed
3. **Attachment streaming**: Support for large attachments
4. **Rich result types**: More structured result types beyond ScriptToolResult

### Not in Scope:

- Auto-tracking (explicitly rejected - scripts use explicit returns)
- Attachment editing or deletion from scripts
- Cross-conversation attachment sharing
- Attachment versioning

## References

### Related Code

- `src/family_assistant/tools/execute_script.py` - Tool implementation
- `src/family_assistant/scripting/engine.py` - Script execution engine
- `src/family_assistant/scripting/apis/attachments.py` - Attachment API and ScriptAttachment
- `src/family_assistant/scripting/apis/tools.py` - Tools API (to add ScriptToolResult)
- `src/family_assistant/tools/attachment_utils.py` - Attachment munging utilities
- `src/family_assistant/tools/types.py` - ToolResult and ToolAttachment
- `tests/functional/tools/test_execute_script.py` - Existing tests
- `tests/functional/test_script_attachment_api.py` - Attachment API tests

### Related Design Docs

- `docs/design/multimodal-manipulation.md` - Attachment system and LLM auto-attach pattern
  (contrast)
- `docs/design/multimodal-tool-results.md` - ToolResult design
- `docs/design/auto-attachment-display.md` - LLM auto-attach (not applicable to scripts)
- `docs/design/json-attachment-handling.md` - JSON attachment handling
- `docs/design/scripting/` - Scripting system design

### User Documentation

- `docs/user/scripting.md` - Scripting guide for users (needs updates)

______________________________________________________________________

**End of Design Document**
