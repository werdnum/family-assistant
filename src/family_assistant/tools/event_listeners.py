"""
Event listener CRUD tools.
"""

import json
import logging
from typing import Any

from family_assistant.storage.events import (
    EventSourceType,
    create_event_listener,
    delete_event_listener,
    get_event_listener_by_id,
    get_event_listeners,
    update_event_listener_enabled,
)
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool definitions
EVENT_LISTENER_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_event_listener",
            "description": (
                "Create a new event listener that responds to specific events. "
                "Two action types are available:\n"
                "- wake_llm: Wakes the LLM to handle complex situations requiring reasoning and judgment\n"
                "- script: Runs Starlark code automatically for simple, deterministic tasks\n\n"
                "Scripts can also use wake_llm() function to conditionally wake the LLM with custom context. "
                "The listener will only affect conversations in the same context where it was created."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "A unique name for this listener (must be unique within this conversation)",
                    },
                    "source": {
                        "type": "string",
                        "description": "Event source: 'home_assistant', 'indexing', or 'webhook'",
                        "enum": ["home_assistant", "indexing", "webhook"],
                    },
                    "listener_config": {
                        "type": "object",
                        "description": (
                            "Configuration including match_conditions and optional action_config. "
                            "Example: {'match_conditions': {'entity_id': 'person.andrew', 'new_state.state': 'Home'}}"
                        ),
                        "properties": {
                            "match_conditions": {
                                "type": "object",
                                "description": "Dictionary of conditions to match. Use dot notation for nested fields.",
                            },
                            "action_config": {
                                "type": "object",
                                "description": "Optional configuration for the wake_llm action",
                            },
                        },
                        "required": ["match_conditions"],
                    },
                    "action_type": {
                        "type": "string",
                        "description": "Type of action to execute: 'wake_llm' (default) or 'script'",
                        "enum": ["wake_llm", "script"],
                        "default": "wake_llm",
                    },
                    "script_code": {
                        "type": "string",
                        "description": "Starlark script code to execute (required if action_type is 'script')",
                    },
                    "script_config": {
                        "type": "object",
                        "description": "Optional configuration for script execution",
                        "properties": {
                            "timeout": {
                                "type": "integer",
                                "description": "Execution timeout in seconds (default: 600)",
                                "default": 600,
                            },
                        },
                    },
                    "one_time": {
                        "type": "boolean",
                        "description": "If true, the listener will be disabled after triggering once",
                        "default": False,
                    },
                },
                "required": ["name", "source", "listener_config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_event_listeners",
            "description": "List all event listeners in this conversation, with optional filtering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Optional: Filter by event source",
                        "enum": ["home_assistant", "indexing", "webhook"],
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Optional: Filter by enabled status",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event_listener",
            "description": "Delete an event listener by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "listener_id": {
                        "type": "integer",
                        "description": "The ID of the listener to delete",
                    },
                },
                "required": ["listener_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_event_listener",
            "description": "Enable or disable an event listener.",
            "parameters": {
                "type": "object",
                "properties": {
                    "listener_id": {
                        "type": "integer",
                        "description": "The ID of the listener to toggle",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "True to enable, False to disable",
                    },
                },
                "required": ["listener_id", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_event_listener_script",
            "description": "Validate Starlark script syntax before creating an event listener.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_code": {
                        "type": "string",
                        "description": "The Starlark script code to validate",
                    },
                },
                "required": ["script_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_event_listener_script",
            "description": "Test a Starlark script with a sample event to see what it would do.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_code": {
                        "type": "string",
                        "description": "The Starlark script code to test",
                    },
                    "sample_event": {
                        "type": "object",
                        "description": "Sample event data to test the script with",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["script_code", "sample_event"],
            },
        },
    },
]


async def create_event_listener_tool(
    exec_context: ToolExecutionContext,
    name: str,
    source: str,
    listener_config: dict[str, Any],
    action_type: str = "wake_llm",
    script_code: str | None = None,
    script_config: dict[str, Any] | None = None,
    one_time: bool = False,
) -> str:
    """
    Create a new event listener.

    Args:
        exec_context: Tool execution context
        name: Unique name for the listener
        source: Event source (must be valid EventSourceType)
        listener_config: Configuration with match_conditions and optional action_config
        action_type: Type of action to execute ("wake_llm" or "script")
        script_code: Starlark script code (required if action_type is "script")
        script_config: Optional configuration for script execution
        one_time: Whether listener should auto-disable after first trigger

    Returns:
        JSON string with success status and listener ID
    """
    try:
        # Validate source
        if source not in [e.value for e in EventSourceType]:
            return json.dumps({
                "success": False,
                "message": f"Invalid source '{source}'. Must be one of: {', '.join(e.value for e in EventSourceType)}",
            })

        # Validate action_type
        if action_type not in ["wake_llm", "script"]:
            return json.dumps({
                "success": False,
                "message": f"Invalid action_type '{action_type}'. Must be 'wake_llm' or 'script'",
            })

        # Validate script_code if action_type is script
        if action_type == "script" and not script_code:
            return json.dumps({
                "success": False,
                "message": "script_code is required when action_type is 'script'",
            })

        # Extract match_conditions and action_config
        match_conditions = listener_config.get("match_conditions")
        if not match_conditions:
            return json.dumps({
                "success": False,
                "message": "listener_config must contain 'match_conditions'",
            })

        # Build action_config based on action_type
        if action_type == "script":
            action_config = {
                "script_code": script_code,
                **(script_config or {}),
            }
        else:
            action_config = listener_config.get("action_config", {})

        # Create the listener
        listener_id = await create_event_listener(
            db_context=exec_context.db_context,
            name=name,
            source_id=source,
            match_conditions=match_conditions,
            conversation_id=exec_context.conversation_id,
            interface_type=exec_context.interface_type,
            action_type=action_type,
            action_config=action_config,
            one_time=one_time,
            enabled=True,
        )

        logger.info(
            f"Created event listener '{name}' (ID: {listener_id}) for "
            f"{exec_context.interface_type}:{exec_context.conversation_id}"
        )

        return json.dumps({
            "success": True,
            "listener_id": listener_id,
            "message": f"Created listener '{name}' with ID {listener_id}",
        })

    except ValueError as e:
        # Handle unique constraint violation
        return json.dumps({
            "success": False,
            "message": str(e),
        })
    except Exception as e:
        # Check for unique constraint violation in the error message
        error_str = str(e)
        if "UNIQUE constraint failed" in error_str and "name" in error_str:
            return json.dumps({
                "success": False,
                "message": f"An event listener named '{name}' already exists in this conversation",
            })
        logger.error(f"Error creating event listener: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "message": f"Failed to create listener: {str(e)}",
        })


async def list_event_listeners_tool(
    exec_context: ToolExecutionContext,
    source: str | None = None,
    enabled: bool | None = None,
) -> str:
    """
    List event listeners for the current conversation.

    Args:
        exec_context: Tool execution context
        source: Optional filter by event source
        enabled: Optional filter by enabled status

    Returns:
        JSON string with list of listeners
    """
    try:
        # Validate source if provided
        if source is not None and source not in [e.value for e in EventSourceType]:
            return json.dumps({
                "success": False,
                "message": f"Invalid source '{source}'. Must be one of: {', '.join(e.value for e in EventSourceType)}",
            })

        # Get listeners
        listeners = await get_event_listeners(
            db_context=exec_context.db_context,
            conversation_id=exec_context.conversation_id,
            source_id=source,
            enabled=enabled,
        )

        # Format listener data for response
        formatted_listeners = []
        for listener in listeners:
            formatted_listeners.append({
                "id": listener["id"],
                "name": listener["name"],
                "source": listener["source_id"],
                "enabled": listener["enabled"],
                "one_time": listener["one_time"],
                "daily_executions": listener["daily_executions"],
                "last_execution_at": (
                    listener["last_execution_at"].isoformat()
                    if listener["last_execution_at"]
                    else None
                ),
                "created_at": listener["created_at"].isoformat(),
            })

        return json.dumps({
            "success": True,
            "listeners": formatted_listeners,
            "count": len(formatted_listeners),
        })

    except Exception as e:
        logger.error(f"Error listing event listeners: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "message": f"Failed to list listeners: {str(e)}",
        })


async def delete_event_listener_tool(
    exec_context: ToolExecutionContext,
    listener_id: int,
) -> str:
    """
    Delete an event listener.

    Args:
        exec_context: Tool execution context
        listener_id: ID of the listener to delete

    Returns:
        JSON string with success status
    """
    try:
        # Get listener info first for better error messages
        listener = await get_event_listener_by_id(
            db_context=exec_context.db_context,
            listener_id=listener_id,
            conversation_id=exec_context.conversation_id,
        )

        if not listener:
            return json.dumps({
                "success": False,
                "message": f"Listener with ID {listener_id} not found",
            })

        # Delete the listener
        deleted = await delete_event_listener(
            db_context=exec_context.db_context,
            listener_id=listener_id,
            conversation_id=exec_context.conversation_id,
        )

        if deleted:
            return json.dumps({
                "success": True,
                "message": f"Deleted listener '{listener['name']}' (ID: {listener_id})",
            })
        else:
            return json.dumps({
                "success": False,
                "message": f"Failed to delete listener {listener_id}",
            })

    except Exception as e:
        logger.error(f"Error deleting event listener: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "message": f"Failed to delete listener: {str(e)}",
        })


async def toggle_event_listener_tool(
    exec_context: ToolExecutionContext,
    listener_id: int,
    enabled: bool,
) -> str:
    """
    Toggle an event listener's enabled status.

    Args:
        exec_context: Tool execution context
        listener_id: ID of the listener to toggle
        enabled: True to enable, False to disable

    Returns:
        JSON string with success status
    """
    try:
        # Get listener info first for better error messages
        listener = await get_event_listener_by_id(
            db_context=exec_context.db_context,
            listener_id=listener_id,
            conversation_id=exec_context.conversation_id,
        )

        if not listener:
            return json.dumps({
                "success": False,
                "message": f"Listener with ID {listener_id} not found",
            })

        # Update the enabled status
        updated = await update_event_listener_enabled(
            db_context=exec_context.db_context,
            listener_id=listener_id,
            conversation_id=exec_context.conversation_id,
            enabled=enabled,
        )

        if updated:
            status = "enabled" if enabled else "disabled"
            return json.dumps({
                "success": True,
                "message": f"Listener '{listener['name']}' is now {status}",
            })
        else:
            return json.dumps({
                "success": False,
                "message": f"Failed to update listener {listener_id}",
            })

    except Exception as e:
        logger.error(f"Error toggling event listener: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "message": f"Failed to toggle listener: {str(e)}",
        })


async def validate_event_listener_script_tool(
    exec_context: ToolExecutionContext,
    script_code: str,
) -> str:
    """
    Validate Starlark script syntax.

    Args:
        exec_context: Tool execution context
        script_code: Starlark script code to validate

    Returns:
        JSON string with validation result
    """
    import re

    try:
        # Import starlark-pyo3 for syntax validation
        import starlark

        # Try to parse the script
        starlark.parse("event_listener.star", script_code)

        return json.dumps({
            "success": True,
            "message": "Script syntax is valid",
        })

    except starlark.StarlarkError as e:
        # Extract error details from the error message
        error_msg = str(e)
        # Try to extract line/column info from error message
        line_match = re.search(r"line (\d+)", error_msg)
        line = int(line_match.group(1)) if line_match else None

        logger.warning(f"Starlark script validation failed: {error_msg}")
        return json.dumps({
            "success": False,
            "error": f"Syntax error: {error_msg}",
            "line": line,
        })
    except Exception as e:
        logger.error(f"Error validating script: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"Validation error: {str(e)}",
        })


async def test_event_listener_script_tool(
    exec_context: ToolExecutionContext,
    script_code: str,
    sample_event: dict[str, Any],
    timeout: int = 5,
) -> str:
    """
    Test a script with a sample event.

    Args:
        exec_context: Tool execution context
        script_code: Starlark script code to test
        sample_event: Sample event data
        timeout: Execution timeout in seconds

    Returns:
        JSON string with test results
    """
    try:
        from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine

        # Create a test engine with limited timeout
        engine = StarlarkEngine(
            tools_provider=exec_context.tools_provider,
            config=StarlarkConfig(
                max_execution_time=timeout,
                deny_all_tools=False,
            ),
        )

        # Prepare test context
        context = {
            "event": sample_event,
            "conversation_id": exec_context.conversation_id,
            "listener_id": "test_listener",
        }

        # Execute the script
        result = await engine.evaluate_async(
            script=script_code,
            globals_dict=context,
            execution_context=exec_context if exec_context.tools_provider else None,
        )

        return json.dumps({
            "success": True,
            "message": "Script executed successfully",
            "result": result if result is not None else "No return value",
        })

    except Exception as e:
        error_type = type(e).__name__
        return json.dumps({
            "success": False,
            "error": f"{error_type}: {str(e)}",
            "message": "Script execution failed",
        })
