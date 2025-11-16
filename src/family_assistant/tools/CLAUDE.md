# Tool Development Guide

This file provides guidance for working with tools in the Family Assistant project.

## Overview

This module contains all the tools that can be used by the LLM to perform various actions. Tools are
organized into thematic submodules for better maintainability.

The tools system follows a consistent pattern:

1. **Tool Definition**: A JSON schema that describes the tool for the LLM
2. **Tool Implementation**: The Python function that executes the tool
3. **Tool Registration**: Mapping the tool name to its implementation

## Adding a New Tool

**IMPORTANT**: Tools must be registered in TWO places:

1. **In the code** (`src/family_assistant/tools/__init__.py`):

   - Add the tool function to `AVAILABLE_FUNCTIONS` dictionary
   - Add the tool definition to the appropriate `TOOLS_DEFINITION` list

2. **In the configuration** (`config.yaml`):

   - Add the tool name to `enable_local_tools` list for each profile that should have access
   - If `enable_local_tools` is not specified for a profile, ALL tools are enabled by default

This dual registration system provides:

- **Security**: Different profiles can have different tool access (e.g., browser profile has only
  browser tools)
- **Flexibility**: Each profile can be tailored with specific tools without code changes
- **Safety**: Destructive tools can be excluded from certain profiles

### Step 1: Create the Tool Implementation

Create a new file in `src/family_assistant/tools/` (e.g., `something.py`) with:

```python
"""Description of what this module's tools do."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Tool Definitions
SOMETHING_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "tool_name",
            "description": "What this tool does and when to use it",
            "parameters": {
                "type": "object",
                "properties": {
                    "param1": {
                        "type": "string",
                        "description": "Description of param1",
                    },
                    "param2": {
                        "type": "integer",
                        "description": "Description of param2",
                        "default": 10,
                    },
                },
                "required": ["param1"],
            },
        },
    },
]

# Tool Implementation
async def tool_name_tool(
    exec_context: ToolExecutionContext,
    param1: str,
    param2: int = 10,
) -> ToolResult:
    """
    Implementation of the tool.

    Args:
        exec_context: The tool execution context
        param1: Description
        param2: Description with default

    Returns:
        ToolResult with structured data (preferred) or text message
    """
    logger.info(f"Executing tool with param1={param1}, param2={param2}")

    # Tool implementation here
    # Access database: exec_context.db_context
    # Access user info: exec_context.user_name, exec_context.conversation_id
    # Send messages: exec_context.chat_interface

    # Prefer returning structured data for programmatic access
    return ToolResult(data={"status": "success", "param1": param1, "param2": param2})
```

### Step 2: Export the Tool in `__init__.py`

Add imports and exports to `src/family_assistant/tools/__init__.py`:

```python
# Add import
from family_assistant.tools.something import (
    SOMETHING_TOOLS_DEFINITION,
    tool_name_tool,
)

# Add to __all__ list
__all__ = [
    # ... existing exports ...
    "tool_name_tool",
]

# Add to AVAILABLE_FUNCTIONS mapping
AVAILABLE_FUNCTIONS: dict[str, Callable] = {
    # ... existing functions ...
    "tool_name": tool_name_tool,
}

# Add to TOOLS_DEFINITION list
TOOLS_DEFINITION: list[dict[str, Any]] = (
    # ... existing definitions ...
    + SOMETHING_TOOLS_DEFINITION
    # ... rest ...
)
```

### Step 3: Enable the Tool in Configuration

Add the tool name to `config.yaml` under the appropriate profile's `enable_local_tools` list:

```yaml
# config.yaml
default_profile_settings:
  tools_config:
    enable_local_tools:
      # ... existing tools ...
      - "tool_name"  # Add your new tool here

service_profiles:
  - id: "default_assistant"
    # This profile inherits from default_profile_settings
    # so it will have access to "tool_name"

  - id: "browser_profile"
    tools_config:  # This REPLACES the default tools_config
      enable_local_tools:
        # Only tools listed here will be available
        # "tool_name" is NOT available unless listed
```

Example from the codebase:

```yaml
# config.yaml
service_profiles:
  - id: "default_assistant"
    tools_config:
      enable_local_tools:
        - "add_or_update_note"
        - "search_documents"
        # ... other tools this profile should have
```

**Note**: If `enable_local_tools` is not specified for a profile, ALL tools defined in the code are
enabled by default.

## Tool Execution Context

Tools receive a `ToolExecutionContext` object with the following attributes:

- `interface_type`: Type of interface (e.g., 'telegram', 'web')
- `conversation_id`: Unique conversation identifier
- `user_name`: Name of the user making the request
- `turn_id`: Optional turn identifier
- `db_context`: Database context for data access
- `chat_interface`: Optional interface for sending messages
- `timezone_str`: User's timezone (default: "UTC")
- `request_confirmation_callback`: Optional callback for user confirmation
- `processing_service`: The processing service instance
- `embedding_generator`: Optional embedding generator
- `new_task_event`: Event for notifying task worker
- `clock`: Clock instance for time operations
- `indexing_source`: Source for document indexing events
- `home_assistant_client`: Home Assistant API client (if configured)

## Special Context Injection

Some parameters are automatically injected by the `LocalToolsProvider`:

- If your tool has a parameter named `exec_context` with type `ToolExecutionContext`, it will be
  injected
- If your tool has a parameter named `db_context` with type `DatabaseContext`, it will be injected
- If your tool has a parameter named `embedding_generator` with type `EmbeddingGenerator`, it will
  be injected
- If your tool has a parameter named `calendar_config` with type `dict[str, Any]`, it will be
  injected

## Structured Data in Tool Results

### Overview

Tools can return results with structured data for programmatic access by tests and scripts, while
maintaining human-readable text for the LLM. The `ToolResult` class supports three patterns:

1. **Data-only**: For simple operations where structured data is sufficient
2. **Both text and data**: When human-readable text adds significant context
3. **Text-only**: For simple messages or backward compatibility

**Important**: When you return `ToolResult(data=...)`, the LLM automatically receives the data as
formatted JSON text (via `get_text()`), while scripts and tests can access the native Python objects
directly (via `get_data()` or `.data` attribute).

### Three Patterns

**1. Data-Only (simple operations)**

Use when structured data tells the complete story:

```python
from family_assistant.tools.types import ToolResult

async def enable_something_tool(exec_context: ToolExecutionContext, item_id: int) -> ToolResult:
    success = await perform_operation(item_id)
    if success:
        return ToolResult(data={"id": item_id, "enabled": True})
    else:
        return ToolResult(data={"error": f"Item {item_id} not found"})
```

The text is auto-generated via JSON serialization (suitable for LLM consumption).

**2. Both Fields (human context adds value)**

Use when explanation or formatting enhances understanding:

```python
async def create_something_tool(
    exec_context: ToolExecutionContext,
    name: str,
    config: dict
) -> ToolResult:
    item_id = await create_item(name, config)
    next_run = calculate_next_run(config)

    return ToolResult(
        text=f"Created item '{name}' (ID: {item_id}). Next run: {format_datetime(next_run)}",
        data={
            "id": item_id,
            "name": name,
            "next_run": next_run.isoformat()
        }
    )
```

The LLM receives the human-friendly text, while tests/scripts can access the structured data.

**3. Text-Only (for backward compatibility)**

Simple messages or when structure isn't needed:

```python
async def simple_operation_tool(exec_context: ToolExecutionContext) -> ToolResult:
    return ToolResult(text="Operation completed successfully")
```

### Fallback Behavior

The `ToolResult` class provides automatic fallbacks:

**data → text**: JSON serialization

```python
result = ToolResult(data={"x": 1, "y": 2})
result.get_text()  # Returns '{\n  "x": 1,\n  "y": 2\n}'
```

**text → data**: JSON parse, else return string

```python
result = ToolResult(text='{"x": 1}')
result.get_data()  # Returns {"x": 1}

result = ToolResult(text='Error message')
result.get_data()  # Returns "Error message" (string, not dict)
```

### Accessing Data in Tests

Tests can use `.get_data()` to access structured data with fallback handling:

```python
result = await my_tool(...)

# Get structured data (with fallback)
data = result.get_data()

# Handle both dict and string results
if isinstance(data, dict):
    item_id = data["id"]
    assert data.get("success") is True
elif isinstance(data, str):
    # Handle string result (error message, etc.)
    assert "error" in data.lower()
```

### Guidelines

**Populate both when:**

- Human text provides context beyond the data (e.g., "Next run: tomorrow at 9am")
- Explanations enhance understanding (e.g., "Will trigger when sensor detects motion")
- Formatting significantly improves readability

**Use data-only when:**

- Operation is simple (enable, disable, delete, update)
- Structured data tells the complete story
- No additional context needed for LLM

**Use text-only when:**

- Simple error or success messages
- Backward compatibility with existing tools
- No structured output needed for programmatic access

### ⚠️ Common Pitfall: String Literal for Text with Data

**IMPORTANT**: When providing both `text` and `data`, the `text` field is sent to LLMs while `data`
is used by scripts and tests. A common mistake is using a **string literal** for `text`:

```python
# ❌ INCORRECT - LLM receives useless constant string (will fail lint check)
return ToolResult(text="Here is the data", data=actual_data)
return ToolResult(text="Operation completed", data=result)
return ToolResult(text="Success", data={"items": items})
```

In these cases, the LLM only sees a generic constant and has no access to the actual data!

```python
# ✅ CORRECT - Use f-string, expression, or omit text:
return ToolResult(text=f"Found {len(items)} items", data=items)
return ToolResult(text=json.dumps(result), data=result)
return ToolResult(data=result)  # text auto-generated from data
```

**Enforcement**: The codebase has an ast-grep conformance rule (`toolresult-text-literal-with-data`)
that **blocks commits** when `text` is a string literal and `data` is also provided. This prevents
the footgun at development time.

**Exemptions**: If a string literal genuinely conveys equivalent information (rare), add an
exemption:

```python
# ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
return ToolResult(text="Error: Invalid input", data={"error": "Invalid input"})
```

**Best Practice**: Prefer dynamic text (f-strings, expressions) or omit `text` entirely to let it
auto-generate from `data`.

### Examples from the Codebase

See `src/family_assistant/tools/automations.py` for comprehensive examples:

- Data-only: `enable_automation_tool`, `disable_automation_tool`, `delete_automation_tool`
- Both fields: `create_automation_tool`, `list_automations_tool`, `get_automation_tool`
- Error handling with structured data across all tools

## Tool Categories

- **Notes**: Managing personal notes (`notes.py`)
- **Tasks**: Scheduling callbacks, reminders, and actions (`tasks.py`)
  - `schedule_reminder`: Simple time-based reminders with optional follow-ups
  - `schedule_future_callback`: One-time LLM callbacks for continuing work
  - `schedule_recurring_task`: Recurring LLM callbacks with RRULE support
  - `schedule_action`: Schedule any action type (wake_llm or script) for one-time execution
  - `schedule_recurring_action`: Schedule recurring actions (wake_llm or script) with RRULE
  - `list_pending_callbacks`: View all scheduled tasks
  - `modify_pending_callback`: Change time or context of scheduled tasks
  - `cancel_pending_callback`: Cancel scheduled tasks
- **Documents**: Searching and ingesting documents (`documents.py`)
- **Communication**: Sending messages and viewing history (`communication.py`)
- **Calendar**: Managing calendar events (implemented in `calendar_integration.py`)
- **Services**: Delegating to other assistant profiles (`services.py`)
- **Events**: Querying system events (`events.py`)
- **Event Listeners**: Managing event-triggered actions (`event_listeners.py`)
- **Home Assistant**: Interacting with Home Assistant (`home_assistant.py`)
- **Confirmation**: Rendering confirmation prompts (`confirmation.py`)

## Best Practices

1. **Return ToolResult with Structured Data**: Tools should return `ToolResult` objects with
   structured data. The `data` field is automatically stringified as JSON for the LLM, while
   scripts/tests can access the structured data directly. Use appropriate pattern (data-only, both
   fields, or text-only) based on whether structured data or human context is needed. For backward
   compatibility, returning plain strings is still supported.
2. **Error Handling**: Return errors with structured data when possible:
   `ToolResult(data={"error": "message"})` or
   `ToolResult(text="Error: message", data={"error": "message"})`
3. **Logging**: Use appropriate log levels (info for normal operations, error for failures)
4. **Type Hints**: Always use proper type hints for better IDE support
5. **Docstrings**: Document all functions with clear descriptions
6. **Async**: All tool implementations should be async functions

## Async/Await and Blocking Operations

### The Golden Rule: Never Block the Event Loop

**ALL tool functions MUST be async**, but declaring a function `async` is not enough. You must
ensure blocking operations don't run directly on the event loop.

**Why this matters:** When a tool blocks the event loop:

- All other operations freeze (database, HTTP, WebSockets)
- Telegram messages queue up
- Web UI becomes unresponsive
- In Starlark scripts: Creates deadlock when tool tries to use main loop → 30s timeout

**Real example:** The jq_query tool was timing out in Starlark scripts because `jq.compile()`
blocked the event loop, creating a deadlock with the async/sync bridge.

### What Operations Are Blocking?

**File I/O (always blocking unless using aiofiles):**

- `Path.read_bytes()`, `Path.write_bytes()`
- `Path.exists()`, `Path.stat()`, `Path.is_file()`
- `os.listdir()`, `os.path.exists()`
- Regular `open()` / `read()` / `write()`

**CPU-Intensive Operations:**

- `json.loads()` / `json.dumps()` on large data (>1MB)
- `jq.compile()` and `.input().all()` (jq query execution)
- PIL/Pillow: `Image.open()`, `.convert()`, `.resize()`, `.save()`
- Chart rendering: `vlc.vegalite_to_png()`, matplotlib operations
- CSV parsing: `csv.DictReader()` on large files
- `filetype.guess()` (reads file header)
- Font loading: `ImageFont.truetype()`
- Regular expressions on large text
- Any third-party library that doesn't explicitly support async

**DON'T do this (blocks event loop):**

```python
async def my_tool(...):
    content = file_path.read_bytes()  # BLOCKS!
    data = json.loads(content)         # BLOCKS on large JSON!
    img = Image.open(data)             # BLOCKS!
    result = expensive_computation()   # BLOCKS!
```

**DO this (offloads to thread pool):**

```python
async def my_tool(...):
    # Wrap blocking operations in asyncio.to_thread()
    def _blocking_work():
        content = file_path.read_bytes()
        data = json.loads(content)
        img = Image.open(io.BytesIO(data))
        return process_image(img)

    result = await asyncio.to_thread(_blocking_work)
```

### When to Use asyncio.to_thread()

**Always use it for:**

- Any file system operation (unless using `aiofiles`)
- CPU-intensive parsing/processing (JSON, CSV, XML on large data)
- Image processing (PIL, OpenCV)
- Chart/graph rendering
- Third-party libraries that don't support async (jq, filetype, etc.)
- Any operation that takes >10ms to complete

**Don't need it for:**

- Database operations (SQLAlchemy async sessions)
- HTTP requests (aiohttp, httpx async clients)
- Operations already in `aiofiles.open()` context
- Async library calls (anything you `await`)
- In-memory operations on small data (\<1KB)

### Pattern: Wrap Multiple Blocking Operations

```python
async def my_tool(exec_context, file_path: str):
    # Group related blocking operations into a helper
    def _process_file():
        # Read file
        content = Path(file_path).read_bytes()

        # Parse (potentially large)
        data = json.loads(content)

        # Process (CPU-intensive)
        result = expensive_computation(data)

        return result

    # Execute all blocking work in thread pool
    return await asyncio.to_thread(_process_file)
```

### Examples from Codebase

**Good: documents.py uses asyncio.to_thread()**

```python
# Line 298-300
has_file = file_path and await asyncio.to_thread(pathlib.Path(file_path).exists)

# Line 320
file_size = (await asyncio.to_thread(file_path_obj.stat)).st_size
```

**Good: documents.py uses aiofiles**

```python
# Lines 397-398
async with aiofiles.open(file_path, "rb") as f:
    file_content = await f.read()
```

**Good: data_manipulation.py (after fix)**

```python
def _load_and_parse_json() -> Any:  # noqa: ANN401
    content = file_path.read_bytes()
    return json.loads(content.decode("utf-8"))

json_data = await asyncio.to_thread(_load_and_parse_json)
```

**Good: data_visualization.py (after fix)**

```python
def _render_chart() -> bytes:
    if is_vega_lite:
        return vlc.vegalite_to_png(vl_spec=spec_dict, scale=scale)
    else:
        return vlc.vega_to_png(vg_spec=spec_dict, scale=scale)

png_data = await asyncio.to_thread(_render_chart)
```

### Testing Async Correctness

Your tests won't catch blocking operations because:

- Tests use small data (blocking is fast)
- Tests don't measure event loop responsiveness
- asyncio allows mixing sync/async code

**Manual verification needed:**

1. Search for file I/O operations not wrapped in `asyncio.to_thread()` or `aiofiles`
2. Search for CPU-intensive library calls (PIL, jq, csv, json.loads on paths)
3. Review PR changes for new blocking operations
4. Use `python -m pytest --durations=10` to find slow tests that might have blocking operations

## Testing Tools

Tools can be tested through the web UI at `/tools` or programmatically in tests. See the functional
tests in `tests/functional/` for examples.

## Creating Custom Tool UIs

### Overview

After implementing a tool, consider creating a custom UI component to enhance the user experience.
While the generic `/tools` page provides automatic form generation for all tools, custom UIs offer
significant benefits:

- **Better User Experience**: Rich input widgets (date pickers, file uploads, code editors)
- **Validation & Feedback**: Context-specific validation and helpful error messages
- **Visualization**: Display results in domain-specific formats (charts, timelines, previews)
- **Syntax Highlighting**: Show scripts and code with proper highlighting for debugging
- **Integration**: Seamlessly integrate with other UI components and workflows

### When to Create a Custom UI

**Create a custom UI when:**

- The tool is frequently used by end users
- The tool has complex parameters (scripts, schedules, JSON configs)
- Results need special formatting or visualization
- The tool is part of a larger feature (automations, calendar, tasks)
- Scripts or code need syntax highlighting for debugging

**Skip custom UIs for:**

- Internal/diagnostic tools primarily used by the LLM
- Simple tools with 1-2 basic parameters
- Rarely used administrative tools
- Tools that work well with generic JSON input

### Frontend Tool UI Architecture

Custom tool UIs are React components in `frontend/src/chat/ToolUI.jsx`. The system automatically
displays these UIs when tools appear in chat messages.

**Key Components:**

1. **ToolUI.jsx**: Main component that routes tool names to custom renderers
2. **ToolFallback**: Generic renderer using `ToolParameterViewer` for JSON display
3. **Individual Tool Components**: Custom renderers for specific tools

Example structure in ToolUI.jsx:

```jsx
// In ToolUI.jsx
export function ToolUI({ tool }) {
  const toolName = tool.name || tool.tool_name;

  switch (toolName) {
    case 'my_tool':
      return <MyToolUI tool={tool} />;
    default:
      return <ToolFallback tool={tool} />;
  }
}

// Custom tool component
function MyToolUI({ tool }) {
  const args = tool.arguments || tool.args || {};

  return (
    <div className="tool-ui">
      <div className="tool-header">
        <StatusIcon status={tool.status} />
        <span className="tool-name">{tool.name}</span>
      </div>

      {/* Display parameters with custom formatting */}
      <div className="tool-parameters">
        <label>Parameter:</label>
        <span>{args.param1}</span>
      </div>

      {/* Display result with custom visualization */}
      {tool.result && (
        <div className="tool-result">
          {/* Custom result rendering */}
        </div>
      )}

      {/* Show attachments if present */}
      {tool.attachments && tool.attachments.length > 0 && (
        <AttachmentDisplay attachments={tool.attachments} />
      )}
    </div>
  );
}
```

### Script and Code Display

**For tools that work with scripts or code, ALWAYS include syntax highlighting:**

```jsx
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';

function ScriptToolUI({ tool }) {
  const args = tool.arguments || tool.args || {};
  const scriptCode = args.script_code || args.code;

  return (
    <div className="script-tool">
      <label>Script:</label>
      <SyntaxHighlighter
        language="python"
        style={vscDarkPlus}
        customStyle={{
          margin: 0,
          borderRadius: '4px',
          fontSize: '0.875rem',
        }}
      >
        {scriptCode}
      </SyntaxHighlighter>
    </div>
  );
}
```

This is crucial for debugging - seeing script syntax highlighted makes it much easier to spot
errors.

### Displaying Tool Parameters

The project recently standardized on using `ToolParameterViewer` for displaying parameters in the
fallback UI. This provides:

- Rich JSON viewer with syntax highlighting
- Collapsible sections for complex objects
- Proper handling of arrays and nested structures

For custom UIs, you can use `ToolParameterViewer` for complex parameters while providing custom
displays for key parameters:

```jsx
import { ToolParameterViewer } from './ToolParameterViewer';

function MyToolUI({ tool }) {
  const args = tool.arguments || tool.args || {};

  return (
    <div>
      {/* Custom display for important parameters */}
      <div className="key-params">
        <span>{args.name}</span>
      </div>

      {/* Use ToolParameterViewer for complex/all parameters */}
      <details>
        <summary>All Parameters</summary>
        <ToolParameterViewer parameters={args} />
      </details>
    </div>
  );
}
```

### Fallback UI and Message History

The generic `ToolFallback` component is used for:

1. Tools without custom UIs
2. Displaying all parameters comprehensively (even for tools with custom UIs, when expanded)
3. Message history debugging

Tools with custom UIs still benefit from the fallback display for detailed debugging. The custom UI
should focus on the most important aspects while ensuring all data is accessible.

### Complete Example: Automation Tool UI

Here's a comprehensive example showing script highlighting, parameter display, and result
formatting:

```jsx
function CreateAutomationUI({ tool }) {
  const args = tool.arguments || tool.args || {};
  const { name, automation_type, trigger_config, action_type, action_config } = args;

  return (
    <div className="automation-tool">
      <div className="automation-header">
        <StatusIcon status={tool.status} />
        <strong>{name}</strong>
        <span className="type-badge">{automation_type}</span>
      </div>

      <div className="automation-config">
        {/* Trigger Display */}
        <section>
          <h4>Trigger</h4>
          {automation_type === 'schedule' ? (
            <div>
              <label>Schedule:</label>
              <code>{trigger_config?.recurrence_rule}</code>
            </div>
          ) : (
            <div>
              <label>Event Source:</label>
              <span>{trigger_config?.event_source}</span>
            </div>
          )}
        </section>

        {/* Action Display with Script Highlighting */}
        <section>
          <h4>Action: {action_type}</h4>
          {action_type === 'script' && action_config?.script_code && (
            <SyntaxHighlighter language="python" style={vscDarkPlus}>
              {action_config.script_code}
            </SyntaxHighlighter>
          )}
          {action_type === 'wake_llm' && (
            <div>
              <label>Context:</label>
              <p>{action_config?.context}</p>
            </div>
          )}
        </section>
      </div>

      {/* Result Display */}
      {tool.result && (
        <div className="tool-result">
          {typeof tool.result === 'string' ? (
            <p>{tool.result}</p>
          ) : (
            <ToolParameterViewer parameters={tool.result} />
          )}
        </div>
      )}
    </div>
  );
}
```

### Testing Custom Tool UIs

Test your custom UIs using the patterns in `frontend/CLAUDE.md`:

1. **Component Tests**: Use Vitest and React Testing Library
2. **MSW Mocks**: Mock API responses for tool execution
3. **Visual Testing**: Manually test in the chat interface and message history
4. **Edge Cases**: Test with missing parameters, errors, complex data

Example test:

```javascript
import { render, screen } from '@testing-library/react';
import { ToolUI } from './ToolUI';

test('displays script with syntax highlighting', () => {
  const tool = {
    name: 'create_automation',
    arguments: {
      action_type: 'script',
      action_config: {
        script_code: 'print("Hello")'
      }
    },
    status: 'success'
  };

  render(<ToolUI tool={tool} />);
  expect(screen.getByText(/print/)).toBeInTheDocument();
});
```

### Best Practices

1. **Always Highlight Code**: Use syntax highlighting for scripts, code, and structured configs
2. **Show Status**: Include status icons (pending, success, error)
3. **Handle Errors**: Display error messages prominently
4. **Progressive Disclosure**: Show key info first, detailed params in expandable sections
5. **Consistent Styling**: Follow existing UI patterns for consistency
6. **Accessibility**: Use semantic HTML and ARIA labels
7. **Performance**: Lazy-load syntax highlighter for better initial load
8. **Testing**: Test with real data from actual tool executions

### Integration with Generic Tool UI

The `/tools` page provides automatic form generation for ALL tools. This is useful for:

- Testing new tools during development
- Accessing rarely-used tools without custom UIs
- Administrative/diagnostic operations
- Exploring tool capabilities

Custom UIs complement this by providing optimized experiences for frequently-used or complex tools.
