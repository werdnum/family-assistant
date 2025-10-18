"""Tools for managing unified automations (both event and schedule-based)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.storage.context import DatabaseContext
    from family_assistant.storage.models import Automation
    from family_assistant.storage.repositories.automations import AutomationType
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def _format_datetime(dt: datetime | None) -> str:
    """
    Format a datetime object to human-readable format.

    Args:
        dt: Datetime object or None

    Returns:
        Formatted datetime string or "Never" if input was None
    """
    if dt is None:
        return "Never"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _to_isoformat(dt: datetime | None) -> str | None:
    """
    Convert a datetime object to ISO format.

    Args:
        dt: Datetime object or None

    Returns:
        ISO format string or None if input was None
    """
    if dt is None:
        return None
    return dt.isoformat()


# Tool Definitions
AUTOMATIONS_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": """Create a new automation (event-triggered or schedule-based).

Event automations trigger when specific events occur (e.g., email received, calendar event).
Schedule automations run on a recurring schedule using RRULE format.

Examples:
- Event: "Send me a reminder when I receive an email from work"
- Schedule: "Wake me up every weekday at 7am" (RRULE: FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR)""",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique name for the automation",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation: 'event' for event-triggered, 'schedule' for time-based",
                    },
                    "trigger_config": {
                        "type": "object",
                        "description": """Configuration for the trigger.
For event automations:
  - event_source: string (e.g., 'email_received', 'calendar_event')
  - event_filter: object with filtering criteria

For schedule automations:
  - recurrence_rule: RRULE string (e.g., 'FREQ=DAILY;BYHOUR=7')""",
                    },
                    "action_type": {
                        "type": "string",
                        "enum": ["wake_llm", "script"],
                        "description": "Action to perform: 'wake_llm' to notify you, 'script' to run code",
                    },
                    "action_config": {
                        "type": "object",
                        "description": """Configuration for the action.
For wake_llm:
  - context: string with optional context for the LLM

For script:
  - script_code: Python code to execute
  - task_name: optional name for the script execution""",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of what this automation does",
                    },
                },
                "required": [
                    "name",
                    "automation_type",
                    "trigger_config",
                    "action_type",
                    "action_config",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": "List all automations, optionally filtered by type (event/schedule) or enabled status",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Filter by automation type (omit to show all)",
                    },
                    "enabled_only": {
                        "type": "boolean",
                        "description": "Only show enabled automations (default: false)",
                        "default": False,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_automation",
            "description": "Get details of a specific automation by ID",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of the automation",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation",
                    },
                },
                "required": ["automation_id", "automation_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_automation",
            "description": "Update an existing automation's configuration",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of the automation to update",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation",
                    },
                    "trigger_config": {
                        "type": "object",
                        "description": "New trigger configuration (optional)",
                    },
                    "action_config": {
                        "type": "object",
                        "description": "New action configuration (optional)",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                    },
                },
                "required": ["automation_id", "automation_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enable_automation",
            "description": "Enable a disabled automation",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of the automation",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation",
                    },
                },
                "required": ["automation_id", "automation_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disable_automation",
            "description": "Disable an automation temporarily (can be re-enabled later)",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of the automation",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation",
                    },
                },
                "required": ["automation_id", "automation_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": "Permanently delete an automation",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of the automation",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation",
                    },
                },
                "required": ["automation_id", "automation_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_automation_stats",
            "description": "Get execution statistics and recent history for an automation",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of the automation",
                    },
                    "automation_type": {
                        "type": "string",
                        "enum": ["event", "schedule"],
                        "description": "Type of automation",
                    },
                },
                "required": ["automation_id", "automation_type"],
            },
        },
    },
]


# Helper function to fetch and validate an automation exists
async def _get_automation_or_error(
    db_context: DatabaseContext,
    automation_id: int,
    automation_type: str,
) -> Automation:
    """
    Fetch an automation by ID, allowing access from any conversation.

    Args:
        db_context: Database context
        automation_id: ID of the automation to fetch
        automation_type: Type of automation ('event' or 'schedule')

    Returns:
        Automation object

    Raises:
        ValueError: If automation not found or validation fails
    """
    validated_type = _validate_automation_type(automation_type)
    automation = await db_context.automations.get_by_id(
        automation_id=automation_id,
        automation_type=validated_type,
        conversation_id=None,
    )

    if not automation:
        raise ValueError(f"Automation {automation_id} not found")

    return automation


# Helper function for type-safe automation type casting
def _validate_automation_type(automation_type: str) -> AutomationType:
    """
    Validate and cast automation type string to AutomationType Literal.

    Args:
        automation_type: String that should be 'event' or 'schedule'

    Returns:
        AutomationType Literal type

    Raises:
        ValueError: If automation_type is not valid
    """
    if automation_type not in {"event", "schedule"}:
        raise ValueError(
            f"Invalid automation_type: {automation_type}. Must be 'event' or 'schedule'"
        )
    return automation_type  # type: ignore[return-value]


# Tool Implementations
async def create_automation_tool(
    exec_context: ToolExecutionContext,
    name: str,
    automation_type: str,
    trigger_config: dict[str, Any],
    action_type: str,
    action_config: dict[str, Any],
    description: str | None = None,
) -> ToolResult:
    """
    Create a new automation (event or schedule-based).

    Args:
        exec_context: Tool execution context
        name: Unique name for the automation
        automation_type: 'event' or 'schedule'
        trigger_config: Trigger configuration (event_source/filter or recurrence_rule)
        action_type: 'wake_llm' or 'script'
        action_config: Action configuration
        description: Optional description

    Returns:
        ToolResult with structured data containing automation ID and details
    """
    try:
        # Validate automation_type first
        validated_type = _validate_automation_type(automation_type)

        # Check name availability
        (
            is_available,
            error_msg,
        ) = await exec_context.db_context.automations.check_name_available(
            name=name,
            conversation_id=exec_context.conversation_id,
        )
        if not is_available:
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

        if validated_type == "event":
            # Create event automation
            source_id = trigger_config.get("event_source")
            match_conditions = trigger_config.get("event_filter", {})

            if not source_id:
                error_msg = (
                    "'event_source' is required in trigger_config for event automations"
                )
                return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

            automation_id = await exec_context.db_context.events.create_event_listener(
                name=name,
                source_id=source_id,
                match_conditions=match_conditions,
                action_type=action_type,
                action_config=action_config,
                conversation_id=exec_context.conversation_id,
                interface_type=exec_context.interface_type,
                description=description,
            )

            # Return structured data with human-readable text
            result_data = {
                "id": automation_id,
                "name": name,
                "type": "event",
                "event_source": source_id,
            }
            text = f"Created event automation '{name}' (ID: {automation_id}). It will trigger when '{source_id}' events occur."
            return ToolResult(text=text, data=result_data)

        else:  # schedule
            # Create schedule automation
            recurrence_rule = trigger_config.get("recurrence_rule")

            if not recurrence_rule:
                error_msg = "'recurrence_rule' is required in trigger_config for schedule automations"
                return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

            automation_id = await exec_context.db_context.schedule_automations.create(
                name=name,
                recurrence_rule=recurrence_rule,
                action_type=action_type,
                action_config=action_config,
                conversation_id=exec_context.conversation_id,
                interface_type=exec_context.interface_type,
                description=description,
            )

            # Get the automation to show next scheduled time
            automation = await exec_context.db_context.schedule_automations.get_by_id(
                automation_id
            )
            next_scheduled_at = (
                automation.get("next_scheduled_at") if automation else None
            )
            next_run = _format_datetime(next_scheduled_at)

            # Return structured data with human-readable text
            result_data = {
                "id": automation_id,
                "name": name,
                "type": "schedule",
                "next_run": _to_isoformat(next_scheduled_at),
            }
            text = f"Created schedule automation '{name}' (ID: {automation_id}). Next run: {next_run}"
            return ToolResult(text=text, data=result_data)

    except ValueError as e:
        logger.error(f"Validation error creating automation: {e}")
        error_msg = str(e)
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    except Exception as e:
        logger.error(f"Error creating automation: {e}", exc_info=True)
        error_msg = f"Error creating automation: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def list_automations_tool(
    exec_context: ToolExecutionContext,
    automation_type: str | None = None,
    enabled_only: bool = False,
) -> ToolResult:
    """
    List all automations.

    Args:
        exec_context: Tool execution context
        automation_type: Filter by type ('event' or 'schedule'), None for all
        enabled_only: Only show enabled automations

    Returns:
        ToolResult with structured list of automations
    """
    try:
        # Validate automation_type if provided
        type_filter: AutomationType | None = (
            _validate_automation_type(automation_type) if automation_type else None
        )

        automations, _total_count = await exec_context.db_context.automations.list_all(
            conversation_id=None,
            automation_type=type_filter,
            enabled=True if enabled_only else None,
        )

        if not automations:
            filter_desc = f" {automation_type}" if automation_type else ""
            enabled_desc = " enabled" if enabled_only else ""
            text = f"No{enabled_desc}{filter_desc} automations found."
            return ToolResult(text=text, data={"automations": []})

        # Format results for display
        lines = [f"Found {len(automations)} automation(s):\n"]
        automation_list = []

        for auto in automations:
            status = "✓ enabled" if auto.enabled else "✗ disabled"
            auto_type = auto.type
            lines.append(f"  [{auto.id}] {auto.name} ({auto_type}) - {status}")
            if auto.description:
                lines.append(f"      {auto.description}")

            # Show trigger info
            if auto_type == "event":
                source = auto.source_id or "unknown"
                lines.append(f"      Trigger: {source} events")
            else:  # schedule
                next_run = auto.next_scheduled_at
                if next_run:
                    lines.append(f"      Next run: {_format_datetime(next_run)}")

            # Build structured data
            auto_data = {
                "id": auto.id,
                "name": auto.name,
                "type": auto_type,
                "enabled": auto.enabled,
            }
            if auto.description:
                auto_data["description"] = auto.description
            if auto_type == "event":
                auto_data["event_source"] = auto.source_id
            elif next_run:
                auto_data["next_scheduled_at"] = _to_isoformat(next_run)
            automation_list.append(auto_data)

        text = "\n".join(lines)
        return ToolResult(text=text, data={"automations": automation_list})

    except Exception as e:
        logger.error(f"Error listing automations: {e}", exc_info=True)
        error_msg = f"Error listing automations: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def get_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> ToolResult:
    """
    Get details of a specific automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        ToolResult with formatted automation details and structured data
    """
    try:
        type_param = _validate_automation_type(automation_type)

        automation = await exec_context.db_context.automations.get_by_id(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=None,
        )

        if not automation:
            error_msg = f"Automation {automation_id} not found"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

        # Format details
        status = "enabled" if automation.enabled else "disabled"
        lines = [
            f"Automation: {automation.name} (ID: {automation_id})",
            f"Type: {automation.type}",
            f"Status: {status}",
        ]

        if automation.description:
            lines.append(f"Description: {automation.description}")

        # Trigger info
        auto_type = automation.type
        if auto_type == "event":
            lines.append(f"Event source: {automation.source_id}")
            if automation.match_conditions:
                lines.append(f"Event filter: {automation.match_conditions}")
        else:  # schedule
            lines.append(f"Recurrence rule: {automation.recurrence_rule}")
            next_scheduled = automation.next_scheduled_at
            if next_scheduled:
                lines.append(f"Next run: {_format_datetime(next_scheduled)}")
            last_execution = automation.last_execution_at
            if last_execution:
                lines.append(f"Last run: {_format_datetime(last_execution)}")

        # Action info
        action_type = automation.action_type
        lines.append(f"Action: {action_type}")
        if automation.action_config:
            config = automation.action_config
            if action_type == "wake_llm" and config.get("context"):
                lines.append(f"Context: {config['context']}")
            elif action_type == "script" and config.get("script_code"):
                lines.append(f"Script: {config['script_code'][:100]}...")

        # Build structured data
        result_data = {
            "id": automation_id,
            "name": automation.name,
            "type": auto_type,
            "enabled": automation.enabled,
            "action_type": action_type,
        }
        if automation.description:
            result_data["description"] = automation.description
        if auto_type == "event":
            result_data["event_source"] = automation.source_id
            if automation.match_conditions:
                result_data["event_filter"] = automation.match_conditions
        else:  # schedule
            result_data["recurrence_rule"] = automation.recurrence_rule
            next_scheduled = automation.next_scheduled_at
            if next_scheduled:
                result_data["next_scheduled_at"] = _to_isoformat(next_scheduled)
            last_execution = automation.last_execution_at
            if last_execution:
                result_data["last_execution_at"] = _to_isoformat(last_execution)
        if automation.action_config:
            result_data["action_config"] = automation.action_config

        text = "\n".join(lines)
        return ToolResult(text=text, data=result_data)

    except Exception as e:
        logger.error(f"Error getting automation: {e}", exc_info=True)
        error_msg = f"Error getting automation: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def update_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
    trigger_config: dict[str, Any] | None = None,
    action_config: dict[str, Any] | None = None,
    description: str | None = None,
) -> ToolResult:
    """
    Update an automation's configuration.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'
        trigger_config: New trigger configuration (optional)
        action_config: New action configuration (optional)
        description: New description (optional)

    Returns:
        ToolResult with success or error message and structured data
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # Verify exists
        existing = await exec_context.db_context.automations.get_by_id(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=None,
        )

        if not existing:
            error_msg = f"Automation {automation_id} not found"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

        if automation_type == "event":
            # Update event automation - merge with existing values
            # Note: source_id cannot be changed for event listeners

            # Check if event_filter is explicitly provided in trigger_config
            if trigger_config and "event_filter" in trigger_config:
                match_conditions = trigger_config["event_filter"]
            else:
                # Preserve existing match_conditions
                match_conditions = existing.match_conditions

            # Default to empty dict if still None
            if match_conditions is None:
                match_conditions = {}

            success = await exec_context.db_context.events.update_event_listener(
                listener_id=automation_id,
                conversation_id=existing.conversation_id,
                name=existing.name,  # Keep existing name
                description=description
                if description is not None
                else existing.description,
                match_conditions=match_conditions,
                action_config=action_config
                if action_config is not None
                else existing.action_config,
                one_time=existing.one_time or False,
                enabled=existing.enabled,
            )

        else:  # schedule
            # Update schedule automation - only pass non-None values
            recurrence_rule = (
                trigger_config.get("recurrence_rule") if trigger_config else None
            )

            # Only pass parameters that were actually provided (not None)
            update_kwargs: dict[str, Any] = {
                "automation_id": automation_id,
                "conversation_id": existing.conversation_id,
            }
            if recurrence_rule is not None:
                update_kwargs["recurrence_rule"] = recurrence_rule
            if action_config is not None:
                update_kwargs["action_config"] = action_config
            if description is not None:
                update_kwargs["description"] = description

            success = await exec_context.db_context.schedule_automations.update(
                **update_kwargs
            )

        if success:
            return ToolResult(data={"id": automation_id, "success": True})
        else:
            error_msg = f"Failed to update automation {automation_id}"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    except ValueError as e:
        logger.error(f"Validation error updating automation: {e}")
        error_msg = str(e)
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    except Exception as e:
        logger.error(f"Error updating automation: {e}", exc_info=True)
        error_msg = f"Error updating automation: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def enable_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> ToolResult:
    """
    Enable an automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        ToolResult with success or error message and structured data
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # Get the automation to retrieve its conversation_id
        automation = await _get_automation_or_error(
            exec_context.db_context, automation_id, automation_type
        )

        success = await exec_context.db_context.automations.update_enabled(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=automation.conversation_id,
            enabled=True,
        )

        if success:
            return ToolResult(data={"id": automation_id, "enabled": True})
        else:
            error_msg = f"Automation {automation_id} not found"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    except ValueError as e:
        logger.error(f"Validation error enabling automation: {e}")
        error_msg = str(e)
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    except Exception as e:
        logger.error(f"Error enabling automation: {e}", exc_info=True)
        error_msg = f"Error enabling automation: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def disable_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> ToolResult:
    """
    Disable an automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        ToolResult with success or error message and structured data
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # Get the automation to retrieve its conversation_id
        automation = await _get_automation_or_error(
            exec_context.db_context, automation_id, automation_type
        )

        success = await exec_context.db_context.automations.update_enabled(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=automation.conversation_id,
            enabled=False,
        )

        if success:
            return ToolResult(data={"id": automation_id, "enabled": False})
        else:
            error_msg = f"Automation {automation_id} not found"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    except ValueError as e:
        logger.error(f"Validation error disabling automation: {e}")
        error_msg = str(e)
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    except Exception as e:
        logger.error(f"Error disabling automation: {e}", exc_info=True)
        error_msg = f"Error disabling automation: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def delete_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> ToolResult:
    """
    Delete an automation permanently.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        ToolResult with success or error message and structured data
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # Get the automation to retrieve its conversation_id
        automation = await _get_automation_or_error(
            exec_context.db_context, automation_id, automation_type
        )

        success = await exec_context.db_context.automations.delete(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=automation.conversation_id,
        )

        if success:
            return ToolResult(data={"id": automation_id, "deleted": True})
        else:
            error_msg = f"Automation {automation_id} not found"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    except ValueError as e:
        logger.error(f"Validation error deleting automation: {e}")
        error_msg = str(e)
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    except Exception as e:
        logger.error(f"Error deleting automation: {e}", exc_info=True)
        error_msg = f"Error deleting automation: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})


async def get_automation_stats_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> ToolResult:
    """
    Get execution statistics for an automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        ToolResult with formatted statistics and structured data
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # First verify the automation exists
        automation = await exec_context.db_context.automations.get_by_id(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=None,
        )

        if not automation:
            error_msg = f"Automation {automation_id} not found"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

        stats = await exec_context.db_context.automations.get_execution_stats(
            automation_id=automation_id,
            automation_type=type_param,
        )

        if not stats:
            error_msg = f"No statistics found for automation {automation_id}"
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

        lines = [
            f"Statistics for automation {automation_id}:",
            f"Total executions: {stats.get('total_executions', 0)}",
        ]

        # Build structured stats data
        stats_data = {
            "automation_id": automation_id,
            "total_executions": stats.get("total_executions", 0),
        }

        last_execution_at = stats.get("last_execution_at")
        if last_execution_at:
            lines.append(f"Last execution: {_format_datetime(last_execution_at)}")
            stats_data["last_execution_at"] = _to_isoformat(last_execution_at)

        next_scheduled_at = stats.get("next_scheduled_at")
        if next_scheduled_at:
            lines.append(f"Next scheduled: {_format_datetime(next_scheduled_at)}")
            stats_data["next_scheduled_at"] = _to_isoformat(next_scheduled_at)

        recent = stats.get("recent_executions", [])
        if recent:
            lines.append(f"\nRecent executions ({len(recent)}):")
            recent_list = []
            for execution in recent[:5]:  # Show top 5
                status = execution.get("status", "unknown")
                created = execution.get("created_at")
                if created:
                    lines.append(f"  - {_format_datetime(created)}: {status}")
                    recent_list.append({
                        "created_at": _to_isoformat(created),
                        "status": status,
                    })
            stats_data["recent_executions"] = recent_list

        text = "\n".join(lines)
        return ToolResult(text=text, data=stats_data)

    except Exception as e:
        logger.error(f"Error getting automation stats: {e}", exc_info=True)
        error_msg = f"Error getting automation stats: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})
