# Tool Development Guide

This file provides guidance for working with tools in the Family Assistant project. Tools are
organized into thematic submodules under `src/family_assistant/tools/`.

## Adding a New Tool

A tool needs a JSON schema definition, an async implementation, a registration entry, and a policy
rule. Registration lives in `src/family_assistant/tools/__init__.py`; runtime access is denied
unless `tools_policy` also allows it, so both are required.

### Step 1: Create the Tool Implementation

Create a new file in `src/family_assistant/tools/` (e.g. `something.py`), following the convention
in the existing modules of importing `ToolExecutionContext` under `TYPE_CHECKING`.

```python
"""Description of what this module's tools do."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

SOMETHING_TOOLS_DEFINITION: list[ToolDefinition] = [
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
                },
                "required": ["param1"],
            },
        },
    },
]


async def tool_name_tool(exec_context: ToolExecutionContext, param1: str) -> ToolResult:
    """Implementation of the tool."""
    return ToolResult(data={"status": "success", "param1": param1})
```

### Step 2: Register the Tool in `__init__.py`

In `src/family_assistant/tools/__init__.py`:

- Import `SOMETHING_TOOLS_DEFINITION` and `tool_name_tool`, and add both to `__all__`.
- Add `SOMETHING_TOOLS_DEFINITION` to the `_LOCAL_TOOL_DEFINITIONS` concatenation.
- Add `"tool_name": tool_name_tool` to `_LOCAL_TOOL_IMPLEMENTATIONS`.
- Add `"tool_name": _metadata(ToolTag.…, …)` to `LOCAL_TOOL_METADATA_BY_NAME`. Tags drive policy
  matching and taint tracking; the enum is `ToolTag` in `metadata.py`.

`AVAILABLE_FUNCTIONS`, `TOOLS_DEFINITION`, `LOCAL_TOOL_REGISTRATIONS`, and `LOCAL_TOOL_DESCRIPTORS`
are derived from those three inputs — do not edit them directly.

### Step 3: Allow the Tool in Policy

Add a rule matching the tool name or one of its tags to the `tools_policy` of each profile that
should have access. A profile's own `tools_policy` replaces `default_profile_settings.tools_policy`
wholesale rather than merging with it.

The one exception is the top-level `global_tools_policy`, whose rules are injected into every
profile's policy engine regardless of that profile's own `tools_policy`, so a tool it covers is
reachable without a per-profile rule (operator policy still overrides it). See
[docs/operations/CONFIGURATION_REFERENCE.md](../../../docs/operations/CONFIGURATION_REFERENCE.md).

```yaml
default_profile_settings:
  tools_policy:
    default_decision: "deny"
    rules:
      - match: { names: ["tool_name"] }
        decision: "allow"
        priority: 10

service_profiles:
  - id: "browser_profile"
    tools_policy:
      default_decision: "deny"
      rules:
        - match: { tags_any: ["browser"] }
          decision: "allow"
          priority: 10
```

For tools that should be discoverable on demand instead of always advertised, also add their names
to `tools_config.on_demand_local_tools`.

## Tool Execution Context

Tools receive a `ToolExecutionContext` (see `types.py` for the full dataclass). Commonly used
attributes:

- `interface_type`: Type of interface (e.g. 'telegram', 'web')
- `conversation_id` / `subconversation_id` / `turn_id`
- `user_name`, `user_id`
- `db_context`: Database context for data access
- `chat_interface` / `chat_interfaces`: Optional interfaces for sending messages
- `timezone`: User's timezone as a `ZoneInfo`
- `request_confirmation_callback`: Optional callback for user confirmation
- `processing_service`, `processing_profile_id`
- `embedding_generator`, `indexing_source`
- `clock`: Clock instance for time operations
- `home_assistant_client`, `camera_backend`, `attachment_registry`, `event_sources`

Infrastructure fields (`processing_service`, `clock`, `home_assistant_client`, `event_sources`,
`attachment_registry`, `camera_backend`, `credential_resolvers`, `api_backend`, `timezone`) have no
defaults, so every construction site must pass them explicitly and the type checker catches
omissions when new infrastructure is added.

## Special Context Injection

`LocalToolsProvider` injects a parameter automatically when its name and annotation match:
`exec_context: ToolExecutionContext`, `db_context: DatabaseContext`,
`embedding_generator: EmbeddingGenerator`, or `calendar_config: dict[str, Any]`.

## Structured Data in Tool Results

`ToolResult` carries `text`, `data`, and `attachments`; at least one of `text`/`data` must be set.
The LLM receives `get_text()`, while tests and scripts read `get_data()`. Both fall back to the
other field: `data` is JSON-serialized to produce text, and `text` is JSON-parsed to produce data
(returning the raw string when it isn't JSON).

Pick a pattern:

- **Data-only** for simple operations (enable, disable, delete, update) where the structured data
  tells the whole story: `return ToolResult(data={"id": item_id, "enabled": True})`.
- **Both fields** when human-readable context adds something the data doesn't, e.g.
  `ToolResult(text=f"Created '{name}' (ID: {item_id}). Next run: {…}", data={…})`.
- **Text-only** for simple messages: `ToolResult(text="Operation completed successfully")`.

Tests should use `result.get_data()` and handle both the dict and string shapes, since a text-only
error result falls back to a string.

### Common Pitfall: String Literal for Text with Data

When you pass both `text` and `data`, the LLM only sees `text` — so a constant string literal hides
the actual data from the model:

```python
# Wrong: the LLM receives a useless constant.
return ToolResult(text="Here is the data", data=actual_data)

# Right: dynamic text, or omit text and let it auto-generate from data.
return ToolResult(text=f"Found {len(items)} items", data=items)
return ToolResult(data=result)
```

The ast-grep conformance rule `toolresult-text-literal-with-data` blocks commits that do this. If a
literal genuinely conveys equivalent information, add an exemption comment:

```python
# ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
return ToolResult(text="Error: Invalid input", data={"error": "Invalid input"})
```

See `src/family_assistant/tools/automations.py` for worked examples of all three patterns and of
error handling with structured data.

## Scheduling Tools

`tasks.py` covers one-time scheduling only: `schedule_reminder`, `schedule_future_callback`,
`schedule_action`, `list_pending_callbacks`, `modify_pending_callback`, `cancel_pending_callback`.
For **recurring** schedules use the automations framework (`create_automation` with
`automation_type="schedule"`) in `automations.py` — there is no legacy "recurring task" tool.

## Async/Await and Blocking Operations

All tool implementations are async, but declaring `async` is not enough: blocking work on the event
loop freezes every other operation (database, HTTP, WebSockets, Telegram delivery) and can hang
scripts waiting on async progress. The `jq_query` tool once timed out in scripts because
`jq.compile()` blocked the loop.

Wrap in `asyncio.to_thread()` (or use `aiofiles`) any:

- Filesystem call — `Path.read_bytes()`, `Path.exists()`, `Path.stat()`, `os.listdir()`, `open()`
- `json.loads()` / `json.dumps()` on large data, CSV parsing, regex over large text
- `jq.compile()` and `.input().all()`
- PIL/Pillow work (`Image.open()`, `.resize()`, `.save()`), `ImageFont.truetype()`
- Chart rendering (`vlc.vegalite_to_png()`, matplotlib)
- `filetype.guess()`
- Any third-party library without async support, or anything taking >10ms

You don't need it for SQLAlchemy async sessions, async HTTP clients, anything already inside
`aiofiles.open()`, or small in-memory work.

Group related blocking calls into one helper and offload once:

```python
async def my_tool(exec_context, file_path: str):
    def _process_file():
        content = Path(file_path).read_bytes()
        data = json.loads(content)
        return expensive_computation(data)

    return await asyncio.to_thread(_process_file)
```

`documents.py` (both `asyncio.to_thread` and `aiofiles`), `data_manipulation.py`, and
`data_visualization.py` show the pattern in the codebase.

Tests will not catch blocking calls — they use small data and don't measure loop responsiveness. New
blocking operations have to be caught by review.

## Testing Tools

Tools can be exercised through the web UI at `/tools`, which auto-generates a form for every tool,
or programmatically — see `tests/functional/` for examples.

## Creating Custom Tool UIs

Custom tool UIs are React components in `frontend/src/chat/ToolUI.jsx`, registered in the
`toolUIsByName` map exported from that file. `DynamicToolUI.tsx` looks a tool name up in that map
and falls back to `ToolFallback` when there is no entry. Components receive `args`, `result`,
`status`, and `attachments` as props.

Create a custom UI when the tool is frequently used by end users, has complex parameters (scripts,
schedules, JSON configs), needs domain-specific result visualization, or is part of a larger feature
(automations, calendar, tasks). Skip it for internal/diagnostic tools, tools with one or two basic
parameters, and rarely-used administrative tools — `ToolFallback` handles those, and it also remains
the comprehensive parameter view for tools that do have custom UIs.

Practical points:

- Use the `CodeHighlight` wrapper already defined in `ToolUI.jsx` for scripts and code rather than
  importing `react-syntax-highlighter` directly; it lazy-loads Prism and its theme.
- Use `ToolParameterViewer` (`@/components/tools/ToolParameterViewer`, default export, props `data`
  and `toolName`) for complex or complete parameter dumps, and custom markup for the key fields.
- Show status (pending, success, error) and surface errors prominently; put detail in expandable
  sections.

Test custom UIs with Vitest and React Testing Library following the patterns in
[frontend/CLAUDE.md](../../../frontend/CLAUDE.md), including missing parameters and error states.
