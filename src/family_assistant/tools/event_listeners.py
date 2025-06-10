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
                "Create a new event listener that will wake you when specific events occur. "
                "The listener will only wake conversations in the same context where it was created."
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
]


async def create_event_listener_tool(
    exec_context: ToolExecutionContext,
    name: str,
    source: str,
    listener_config: dict[str, Any],
    one_time: bool = False,
) -> str:
    """
    Create a new event listener.

    Args:
        exec_context: Tool execution context
        name: Unique name for the listener
        source: Event source (must be valid EventSourceType)
        listener_config: Configuration with match_conditions and optional action_config
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

        # Extract match_conditions and action_config
        match_conditions = listener_config.get("match_conditions")
        if not match_conditions:
            return json.dumps({
                "success": False,
                "message": "listener_config must contain 'match_conditions'",
            })

        action_config = listener_config.get("action_config", {})

        # Create the listener
        listener_id = await create_event_listener(
            db_context=exec_context.db_context,
            name=name,
            source_id=source,
            match_conditions=match_conditions,
            conversation_id=exec_context.conversation_id,
            interface_type=exec_context.interface_type,
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
