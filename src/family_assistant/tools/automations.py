"""Tools for managing unified automations (both event and schedule-based)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.storage.repositories.automations import AutomationType
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

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
) -> str:
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
        Success message with automation ID
    """
    try:
        # Check name availability
        (
            is_available,
            error_msg,
        ) = await exec_context.db_context.automations.check_name_available(
            name=name,
            conversation_id=exec_context.conversation_id,
        )
        if not is_available:
            return f"Error: {error_msg}"

        if automation_type == "event":
            # Create event automation
            source_id = trigger_config.get("event_source")
            match_conditions = trigger_config.get("event_filter", {})

            if not source_id:
                return "Error: 'event_source' is required in trigger_config for event automations"

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

            return f"Created event automation '{name}' (ID: {automation_id}). It will trigger when '{source_id}' events occur."

        else:  # schedule
            # Create schedule automation
            recurrence_rule = trigger_config.get("recurrence_rule")

            if not recurrence_rule:
                return "Error: 'recurrence_rule' is required in trigger_config for schedule automations"

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
            if automation and automation.get("next_scheduled_at"):
                next_run = automation["next_scheduled_at"].strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            else:
                next_run = "unknown"

            return f"Created schedule automation '{name}' (ID: {automation_id}). Next run: {next_run}"

    except ValueError as e:
        logger.error(f"Validation error creating automation: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"Error creating automation: {e}", exc_info=True)
        return f"Error creating automation: {e}"


async def list_automations_tool(
    exec_context: ToolExecutionContext,
    automation_type: str | None = None,
    enabled_only: bool = False,
) -> str:
    """
    List all automations.

    Args:
        exec_context: Tool execution context
        automation_type: Filter by type ('event' or 'schedule'), None for all
        enabled_only: Only show enabled automations

    Returns:
        Formatted list of automations
    """
    try:
        # Validate automation_type if provided
        type_filter: AutomationType | None = (
            _validate_automation_type(automation_type) if automation_type else None
        )

        automations, _total_count = await exec_context.db_context.automations.list_all(
            conversation_id=exec_context.conversation_id,
            automation_type=type_filter,
            enabled=True if enabled_only else None,
        )

        if not automations:
            filter_desc = f" {automation_type}" if automation_type else ""
            enabled_desc = " enabled" if enabled_only else ""
            return f"No{enabled_desc}{filter_desc} automations found."

        # Format results
        lines = [f"Found {len(automations)} automation(s):\n"]
        for auto in automations:
            status = "✓ enabled" if auto.get("enabled") else "✗ disabled"
            auto_type = auto.get("type", "unknown")
            lines.append(f"  [{auto['id']}] {auto['name']} ({auto_type}) - {status}")
            if auto.get("description"):
                lines.append(f"      {auto['description']}")

            # Show trigger info
            if auto_type == "event":
                source = auto.get("event_source", "unknown")
                lines.append(f"      Trigger: {source} events")
            else:  # schedule
                next_run = auto.get("next_scheduled_at")
                if next_run:
                    lines.append(
                        f"      Next run: {next_run.strftime('%Y-%m-%d %H:%M UTC')}"
                    )

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error listing automations: {e}", exc_info=True)
        return f"Error listing automations: {e}"


async def get_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> str:
    """
    Get details of a specific automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        Formatted automation details
    """
    try:
        type_param = _validate_automation_type(automation_type)

        automation = await exec_context.db_context.automations.get_by_id(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=exec_context.conversation_id,
        )

        if not automation:
            return f"Error: Automation {automation_id} not found"

        # Format details
        status = "enabled" if automation.get("enabled") else "disabled"
        lines = [
            f"Automation: {automation['name']} (ID: {automation_id})",
            f"Type: {automation.get('type')}",
            f"Status: {status}",
        ]

        if automation.get("description"):
            lines.append(f"Description: {automation['description']}")

        # Trigger info
        auto_type = automation.get("type")
        if auto_type == "event":
            lines.append(f"Event source: {automation.get('event_source')}")
            if automation.get("event_filter"):
                lines.append(f"Event filter: {automation['event_filter']}")
        else:  # schedule
            lines.append(f"Recurrence rule: {automation.get('recurrence_rule')}")
            if automation.get("next_scheduled_at"):
                lines.append(
                    f"Next run: {automation['next_scheduled_at'].strftime('%Y-%m-%d %H:%M UTC')}"
                )
            if automation.get("last_execution_at"):
                lines.append(
                    f"Last run: {automation['last_execution_at'].strftime('%Y-%m-%d %H:%M UTC')}"
                )

        # Action info
        action_type = automation.get("action_type")
        lines.append(f"Action: {action_type}")
        if automation.get("action_config"):
            config = automation["action_config"]
            if action_type == "wake_llm" and config.get("context"):
                lines.append(f"Context: {config['context']}")
            elif action_type == "script" and config.get("script_code"):
                lines.append(f"Script: {config['script_code'][:100]}...")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error getting automation: {e}", exc_info=True)
        return f"Error getting automation: {e}"


async def update_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
    trigger_config: dict[str, Any] | None = None,
    action_config: dict[str, Any] | None = None,
    description: str | None = None,
) -> str:
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
        Success or error message
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # Verify exists
        existing = await exec_context.db_context.automations.get_by_id(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=exec_context.conversation_id,
        )

        if not existing:
            return f"Error: Automation {automation_id} not found"

        if automation_type == "event":
            # Update event automation - merge with existing values
            # Note: source_id cannot be changed for event listeners

            # Check if event_filter is explicitly provided in trigger_config
            if trigger_config and "event_filter" in trigger_config:
                match_conditions = trigger_config["event_filter"]
            else:
                # Preserve existing match_conditions, trying both possible keys
                match_conditions = existing.get("match_conditions") or existing.get(
                    "event_filter"
                )

            # Default to empty dict if still None
            if match_conditions is None:
                match_conditions = {}

            success = await exec_context.db_context.events.update_event_listener(
                listener_id=automation_id,
                conversation_id=exec_context.conversation_id,
                name=existing["name"],  # Keep existing name
                description=description
                if description is not None
                else existing.get("description"),
                match_conditions=match_conditions,
                action_config=action_config
                if action_config is not None
                else existing.get("action_config"),
                one_time=existing.get("one_time", False),
                enabled=existing.get("enabled", True),
            )

        else:  # schedule
            # Update schedule automation - only pass non-None values
            recurrence_rule = (
                trigger_config.get("recurrence_rule") if trigger_config else None
            )

            # Only pass parameters that were actually provided (not None)
            update_kwargs: dict[str, Any] = {
                "automation_id": automation_id,
                "conversation_id": exec_context.conversation_id,
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
            return f"Successfully updated automation {automation_id}"
        else:
            return f"Error: Failed to update automation {automation_id}"

    except ValueError as e:
        logger.error(f"Validation error updating automation: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"Error updating automation: {e}", exc_info=True)
        return f"Error updating automation: {e}"


async def enable_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> str:
    """
    Enable an automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        Success or error message
    """
    try:
        type_param = _validate_automation_type(automation_type)

        success = await exec_context.db_context.automations.update_enabled(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=exec_context.conversation_id,
            enabled=True,
        )

        if success:
            return f"Enabled automation {automation_id}"
        else:
            return f"Error: Automation {automation_id} not found"

    except Exception as e:
        logger.error(f"Error enabling automation: {e}", exc_info=True)
        return f"Error enabling automation: {e}"


async def disable_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> str:
    """
    Disable an automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        Success or error message
    """
    try:
        type_param = _validate_automation_type(automation_type)

        success = await exec_context.db_context.automations.update_enabled(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=exec_context.conversation_id,
            enabled=False,
        )

        if success:
            return f"Disabled automation {automation_id}"
        else:
            return f"Error: Automation {automation_id} not found"

    except Exception as e:
        logger.error(f"Error disabling automation: {e}", exc_info=True)
        return f"Error disabling automation: {e}"


async def delete_automation_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> str:
    """
    Delete an automation permanently.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        Success or error message
    """
    try:
        type_param = _validate_automation_type(automation_type)

        success = await exec_context.db_context.automations.delete(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=exec_context.conversation_id,
        )

        if success:
            return f"Deleted automation {automation_id}"
        else:
            return f"Error: Automation {automation_id} not found"

    except Exception as e:
        logger.error(f"Error deleting automation: {e}", exc_info=True)
        return f"Error deleting automation: {e}"


async def get_automation_stats_tool(
    exec_context: ToolExecutionContext,
    automation_id: int,
    automation_type: str,
) -> str:
    """
    Get execution statistics for an automation.

    Args:
        exec_context: Tool execution context
        automation_id: Automation ID
        automation_type: 'event' or 'schedule'

    Returns:
        Formatted statistics
    """
    try:
        type_param = _validate_automation_type(automation_type)

        # First verify the automation belongs to this conversation
        automation = await exec_context.db_context.automations.get_by_id(
            automation_id=automation_id,
            automation_type=type_param,
            conversation_id=exec_context.conversation_id,
        )

        if not automation:
            return f"Error: Automation {automation_id} not found"

        stats = await exec_context.db_context.automations.get_execution_stats(
            automation_id=automation_id,
            automation_type=type_param,
        )

        if not stats:
            return f"Error: No statistics found for automation {automation_id}"

        lines = [
            f"Statistics for automation {automation_id}:",
            f"Total executions: {stats.get('total_executions', 0)}",
        ]

        if stats.get("last_execution_at"):
            lines.append(
                f"Last execution: {stats['last_execution_at'].strftime('%Y-%m-%d %H:%M UTC')}"
            )

        if stats.get("next_scheduled_at"):
            lines.append(
                f"Next scheduled: {stats['next_scheduled_at'].strftime('%Y-%m-%d %H:%M UTC')}"
            )

        recent = stats.get("recent_executions", [])
        if recent:
            lines.append(f"\nRecent executions ({len(recent)}):")
            for execution in recent[:5]:  # Show top 5
                status = execution.get("status", "unknown")
                created = execution.get("created_at")
                if created:
                    lines.append(
                        f"  - {created.strftime('%Y-%m-%d %H:%M UTC')}: {status}"
                    )

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error getting automation stats: {e}", exc_info=True)
        return f"Error getting automation stats: {e}"
