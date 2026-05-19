# Family Assistant Tools

This module contains all the tools that can be used by the LLM to perform various actions. Tools are
organized into thematic submodules for better maintainability.

## Architecture

The tools system follows a consistent pattern:

1. **Tool Definition**: A JSON schema that describes the tool for the LLM
2. **Tool Implementation**: The Python function that executes the tool
3. **Tool Registration**: Mapping the tool name to its implementation

## Adding a New Tool

To add a new tool to the assistant, follow these steps:

### 1. Create the Tool Implementation

Create a new file in `src/family_assistant/tools/` (e.g., `something.py`) with:

```python
"""Description of what this module's tools do."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from family_assistant.tools.types import ToolDefinition

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Tool Definitions
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
) -> str:
    """
    Implementation of the tool.

    Args:
        exec_context: The tool execution context
        param1: Description
        param2: Description with default

    Returns:
        A string result (all tools must return strings)
    """
    logger.info(f"Executing tool with param1={param1}, param2={param2}")

    # Tool implementation here
    # Access database: exec_context.db_context
    # Access user info: exec_context.user_name, exec_context.conversation_id
    # Send messages: exec_context.chat_interface

    return "Success message or result"
```

### 2. Export the Tool in `__init__.py`

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
TOOLS_DEFINITION: list[ToolDefinition] = (
    # ... existing definitions ...
    +SOMETHING_TOOLS_DEFINITION
    # ... rest ...
)
```

### 3. Allow the Tool in Policy

**IMPORTANT**: Tools must be allowed by `tools_policy` for each profile that should have access to
them. Tool access is denied unless a matching policy rule allows it.

Add the tool name or one of its metadata tags to the appropriate profile policy:

```yaml
# config.yaml
default_profile_settings:
  tools_policy:
    rules:
      - match: { names: ["tool_name"] }
        decision: "allow"
        priority: 10

service_profiles:
  - id: "default_assistant"
    # This profile inherits from default_profile_settings
    # so it will have access to "tool_name"

  - id: "browser_profile"
    tools_policy:
      default_decision: "deny"
      rules:
        - match: { tags_any: ["browser"] }
          decision: "allow"
          priority: 10
```

For tools that should appear in the on-demand catalog instead of the always-advertised tool list,
also add their names to `tools_config.on_demand_local_tools`.

## Tool Execution Context

Tools receive a `ToolExecutionContext` object with the following attributes:

- `interface_type`: Type of interface (e.g., 'telegram', 'web')
- `conversation_id`: Unique conversation identifier
- `user_name`: Name of the user making the request
- `turn_id`: Optional turn identifier
- `db_context`: Database context for data access
- `chat_interface`: Optional interface for sending messages
- `timezone`: User's timezone as a `ZoneInfo` object (default: `ZoneInfo("UTC")`)
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

## Tool Categories

- **Notes**: Managing personal notes (`notes.py`)
- **Tasks**: Scheduling one-time callbacks, reminders, and actions (`tasks.py`)
  - `schedule_reminder`: Simple time-based reminders with optional follow-ups
  - `schedule_future_callback`: One-time LLM callbacks for continuing work
  - `schedule_action`: Schedule any action type (wake_llm or script) for one-time execution
  - `list_pending_callbacks`: View pending reminders and one-time callbacks
  - `modify_pending_callback`: Change time or context of a scheduled callback
  - `cancel_pending_callback`: Cancel a scheduled callback
  - For **recurring** schedules, use the automations framework (`create_automation` with
    `automation_type="schedule"`) in `automations.py`.
- **Documents**: Searching and ingesting documents (`documents.py`)
- **Communication**: Sending messages and viewing history (`communication.py`)
- **Calendar**: Managing calendar events (implemented in `calendar_integration.py`)
- **Services**: Delegating to other assistant profiles (`services.py`)
- **Events**: Querying system events (`events.py`)
- **Event Listeners**: Managing event-triggered actions (`event_listeners.py`)
- **Home Assistant**: Interacting with Home Assistant (`home_assistant.py`)
- **Confirmation**: Rendering confirmation prompts (`confirmation.py`)

## Best Practices

1. **Return Strings**: All tools must return strings. Convert other types to string representations.
2. **Error Handling**: Return error messages as strings starting with "Error:"
3. **Logging**: Use appropriate log levels (info for normal operations, error for failures)
4. **Type Hints**: Always use proper type hints for better IDE support
5. **Docstrings**: Document all functions with clear descriptions
6. **Async**: All tool implementations should be async functions

## Testing Tools

Tools can be tested through the web UI at `/tools` or programmatically in tests. See the functional
tests in `tests/functional/` for examples.
