"""Tools for managing unified automations (both event and schedule-based)."""

from __future__ import annotations

import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any, cast

from family_assistant.actions import (
    WakeLlmProfileError,
    assert_wake_llm_allowed,
)
from family_assistant.scripting.validator import ScriptValidator
from family_assistant.tools.stored_scripts import (
    AUTOMATION_RUNTIME_GLOBALS,
    validate_script_action_config,
)
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from family_assistant.storage.context import DatabaseContext
    from family_assistant.storage.models import Automation
    from family_assistant.storage.repositories.automations import AutomationType
    from family_assistant.storage.types import ActionConfig
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def format_automation_datetime(dt: datetime | None, timezone: ZoneInfo) -> str:
    """
    Format a datetime object to human-readable format in the given timezone.

    The ``timezone`` must be the user's configured timezone (typically
    ``exec_context.timezone``): values exposed to the user or LLM must never
    be rendered in UTC.

    Args:
        dt: Datetime object or None
        timezone: ZoneInfo object for the target (user-facing) timezone

    Returns:
        Formatted datetime string or "Never" if input was None
    """
    if dt is None:
        return "Never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local_dt = dt.astimezone(timezone)
    return local_dt.strftime("%Y-%m-%d %H:%M %Z")


def _to_isoformat(dt: datetime | None, timezone: ZoneInfo) -> str | None:
    """
    Convert a datetime object to ISO format in the given timezone.

    The ``timezone`` must be the user's configured timezone (typically
    ``exec_context.timezone``): values exposed to the user or LLM must never
    be rendered in UTC.

    Args:
        dt: Datetime object or None
        timezone: ZoneInfo object for the target (user-facing) timezone

    Returns:
        ISO format string in local timezone or None if input was None
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(timezone).isoformat()


# Tool Definitions
AUTOMATIONS_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": """Create a new automation (event-triggered or schedule-based).

Event automations trigger when specific events occur (e.g., email received, calendar event).
Schedule automations run on a recurring schedule using RRULE format.

IMPORTANT: Times in RRULE strings (BYHOUR, BYMINUTE, etc.) are interpreted in the user's
configured timezone, NOT UTC. For example, if the user is in Australia/Sydney and asks for
"every day at 9am", use BYHOUR=9 — the system will schedule it at 9am Sydney time.

Examples:
- Event: "Send me a reminder when I receive an email from work"
- Schedule: "Wake me up every weekday at 7am" (RRULE: FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0)""",
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
  - condition_script: optional Python expression evaluated before action runs. Must return truthy to proceed.

For schedule automations:
  - recurrence_rule: RRULE string (e.g., 'FREQ=DAILY;BYHOUR=7;BYMINUTE=0'). Times are in the user's configured timezone.""",
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
  - script_code: Python code to execute (inline), OR
  - script_name: name of a stored script from the script library (use list_scripts to see available)
  - parameters: optional dict of parameters to pass to the stored script
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
                        "description": "New trigger configuration (optional). For event automations: event_filter (object), condition_script (string or null). For schedule automations: recurrence_rule (string).",
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


async def _validate_script_code_with_provider(
    tools_provider: ToolsProvider | None,
    script_code: str,
    input_names: list[str],
) -> str | None:
    tool_definitions = None
    if tools_provider:
        tool_definitions = await tools_provider.get_tool_definitions()

    validator = ScriptValidator(tool_definitions=tool_definitions)
    validation = validator.validate(
        script_code,
        input_names=input_names,
        include_tools_api=tools_provider is not None,
    )
    if not validation.is_valid:
        return f"Script validation failed: {validation.error_message}"
    return None


async def validate_action_scripts_with_provider(
    db_context: DatabaseContext,
    tools_provider: ToolsProvider | None,
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
    action_config: dict[str, Any],
) -> str | None:
    """Validate a script action_config's code against a profile's tool set.

    The automation will execute under the profile that created it, so the script
    is validated against that same profile's ``tools_provider``. Inline
    ``script_code`` is validated directly with the automation runtime globals.
    ``script_name`` configs load the referenced stored script and validate its
    code too: stored scripts are global and were only validated against whatever
    profile saved them, so the creating profile may lack tools the script uses.
    Assumes structural validation (``validate_script_action_config``) already
    passed. Returns an error message, or None if valid.
    """
    script_code = action_config.get("script_code")
    if script_code:
        return await _validate_script_code_with_provider(
            tools_provider,
            script_code,
            input_names=sorted(AUTOMATION_RUNTIME_GLOBALS),
        )

    script_name = action_config.get("script_name")
    if not script_name:
        return None
    stored = await db_context.scripts.get_by_name(script_name)
    if stored is None:
        return f"Stored script '{script_name}' not found"

    # The script sees the automation runtime globals plus its declared schema
    # parameters and whatever parameters the automation supplies.
    input_names: set[str] = set(AUTOMATION_RUNTIME_GLOBALS)
    schema = stored.parameters_schema or {}
    if isinstance(schema.get("properties"), dict):
        input_names.update(k for k in schema["properties"] if isinstance(k, str))
    if isinstance(schema.get("required"), list):
        input_names.update(k for k in schema["required"] if isinstance(k, str))
    parameters = action_config.get("parameters")
    if isinstance(parameters, dict):
        input_names.update(k for k in parameters if isinstance(k, str))

    error = await _validate_script_code_with_provider(
        tools_provider,
        stored.script_code,
        input_names=sorted(input_names),
    )
    if error:
        return f"Stored script '{script_name}': {error}"
    return None


async def validate_action_scripts(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
    action_config: dict[str, Any],
) -> str | None:
    """Validate a script action_config against the creating profile's tools."""
    tools_provider = None
    if exec_context.tools_provider:
        tools_provider = exec_context.tools_provider
    elif (
        exec_context.processing_service
        and exec_context.processing_service.tools_provider
    ):
        tools_provider = exec_context.processing_service.tools_provider
    return await validate_action_scripts_with_provider(
        exec_context.db_context, tools_provider, action_config
    )


# Tool Implementations
async def create_automation_tool(
    exec_context: ToolExecutionContext,
    name: str,
    automation_type: str,
    # ast-grep-ignore: no-dict-any - trigger config has varying keys per automation type
    trigger_config: dict[str, Any],
    action_type: str,
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
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

        # wake_llm actions do not honor the creating profile at execution time, so
        # a confined profile (allow_wake_llm disabled) must not create one (it
        # would run under the default trusted profile). Fail loudly at creation.
        if action_type == "wake_llm":
            try:
                assert_wake_llm_allowed(action_type, exec_context.allow_wake_llm)
            except WakeLlmProfileError as err:
                return ToolResult(text=f"Error: {err}", data={"error": str(err)})

        # Validate script action_config
        if action_type == "script":
            script_error = await validate_script_action_config(
                exec_context.db_context, action_config
            )
            if script_error:
                return ToolResult(
                    text=f"Error: {script_error}", data={"error": script_error}
                )
            validation_error = await validate_action_scripts(
                exec_context, action_config
            )
            if validation_error:
                return ToolResult(
                    text=f"Error: {validation_error}",
                    data={"error": validation_error},
                )
            # Note: the "neither script_code nor script_name" case is already
            # rejected by validate_script_action_config above.

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

            condition_script = trigger_config.get("condition_script")

            automation_id = await exec_context.db_context.events.create_event_listener(
                name=name,
                source_id=source_id,
                match_conditions=match_conditions,
                action_type=action_type,
                action_config=cast("ActionConfig", action_config),
                conversation_id=exec_context.conversation_id,
                interface_type=exec_context.interface_type,
                description=description,
                condition_script=condition_script,
                processing_profile_id=exec_context.processing_profile_id,
                created_by_user_id=exec_context.user_id,
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
                action_config=cast("ActionConfig", action_config),
                conversation_id=exec_context.conversation_id,
                interface_type=exec_context.interface_type,
                description=description,
                timezone=exec_context.timezone,
                processing_profile_id=exec_context.processing_profile_id,
                created_by_user_id=exec_context.user_id,
            )

            # Get the automation to show next scheduled time
            automation = await exec_context.db_context.schedule_automations.get_by_id(
                automation_id
            )
            next_scheduled_at = (
                automation.get("next_scheduled_at") if automation else None
            )
            next_run = format_automation_datetime(
                next_scheduled_at, exec_context.timezone
            )

            # Return structured data with human-readable text
            result_data = {
                "id": automation_id,
                "name": name,
                "type": "schedule",
                "next_run": _to_isoformat(next_scheduled_at, exec_context.timezone),
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
                    lines.append(
                        f"      Next run: {format_automation_datetime(next_run, exec_context.timezone)}"
                    )

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
                auto_data["next_scheduled_at"] = _to_isoformat(
                    next_run, exec_context.timezone
                )
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
            if automation.condition_script:
                lines.append(f"Condition script: {automation.condition_script}")
        else:  # schedule
            lines.append(f"Recurrence rule: {automation.recurrence_rule}")
            next_scheduled = automation.next_scheduled_at
            if next_scheduled:
                lines.append(
                    f"Next run: {format_automation_datetime(next_scheduled, exec_context.timezone)}"
                )
            last_execution = automation.last_execution_at
            if last_execution:
                lines.append(
                    f"Last run: {format_automation_datetime(last_execution, exec_context.timezone)}"
                )

        # Action info
        action_type = automation.action_type
        lines.append(f"Action: {action_type}")
        if automation.action_config:
            config = automation.action_config
            if action_type == "wake_llm" and config.get("context"):
                lines.append(f"Context: {config['context']}")
            elif action_type == "script":
                if config.get("script_name"):
                    lines.append(f"Script: {config['script_name']} (stored)")
                    if config.get("parameters"):
                        lines.append(f"Parameters: {config['parameters']}")
                elif config.get("script_code"):
                    lines.append(f"Script:\n{config['script_code']}")

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
            result_data["condition_script"] = automation.condition_script
        else:  # schedule
            result_data["recurrence_rule"] = automation.recurrence_rule
            next_scheduled = automation.next_scheduled_at
            if next_scheduled:
                result_data["next_scheduled_at"] = _to_isoformat(
                    next_scheduled, exec_context.timezone
                )
            last_execution = automation.last_execution_at
            if last_execution:
                result_data["last_execution_at"] = _to_isoformat(
                    last_execution, exec_context.timezone
                )
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
    # ast-grep-ignore: no-dict-any - trigger config has varying keys per automation type
    trigger_config: dict[str, Any] | None = None,
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
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

        # Cross-profile update denial: updating an automation re-stamps its
        # execution provenance to the updating profile, which would silently move
        # a confined automation (and its script) to full-trust execution. Refuse
        # tool-path updates from a different profile than the owner and direct the
        # caller to delegate. (The web admin API stays permissive, like the notes
        # API.)
        if (
            existing.processing_profile_id is not None
            and exec_context.processing_profile_id is not None
            and existing.processing_profile_id != exec_context.processing_profile_id
        ):
            error_msg = (
                f"Automation {automation_id} is owned by profile "
                f"'{existing.processing_profile_id}' and cannot be updated from "
                f"profile '{exec_context.processing_profile_id}'. Delegate to the "
                "owning profile to change it."
            )
            return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

        # When a script automation's action_config (and therefore its script) is
        # being changed, validate the new config and its script against the
        # updating profile's tools and re-stamp creator provenance, so the
        # updated script is validated and executed under the same (updating)
        # profile. wake_llm action_config edits are not scripts and skip the
        # script validators.
        restamp_profile_id: str | None = None
        restamp_user_id: str | None = None
        if action_config is not None:
            restamp_profile_id = exec_context.processing_profile_id
            restamp_user_id = exec_context.user_id
            if existing.action_type == "script":
                script_error = await validate_script_action_config(
                    exec_context.db_context, action_config
                )
                if script_error:
                    return ToolResult(
                        text=f"Error: {script_error}", data={"error": script_error}
                    )
                validation_error = await validate_action_scripts(
                    exec_context, action_config
                )
                if validation_error:
                    return ToolResult(
                        text=f"Error: {validation_error}",
                        data={"error": validation_error},
                    )

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

            # Extract condition_script from trigger_config if provided
            if trigger_config and "condition_script" in trigger_config:
                condition_script = trigger_config["condition_script"]
            else:
                condition_script = existing.condition_script

            success = await exec_context.db_context.events.update_event_listener(
                listener_id=automation_id,
                conversation_id=existing.conversation_id,
                name=existing.name,  # Keep existing name
                description=description
                if description is not None
                else existing.description,
                match_conditions=match_conditions,
                action_config=cast(
                    "ActionConfig",
                    action_config
                    if action_config is not None
                    else existing.action_config,
                ),
                one_time=existing.one_time or False,
                enabled=existing.enabled,
                condition_script=condition_script,
                processing_profile_id=restamp_profile_id,
                created_by_user_id=restamp_user_id,
            )

        else:  # schedule
            # Update schedule automation - only pass non-None values
            recurrence_rule = (
                trigger_config.get("recurrence_rule") if trigger_config else None
            )

            # Only pass parameters that were actually provided (not None)
            # ast-grep-ignore: no-dict-any - forwarded kwargs with varying keys per schedule update call
            update_kwargs: dict[str, Any] = {
                "automation_id": automation_id,
                "conversation_id": existing.conversation_id,
                "timezone": exec_context.timezone,
            }
            if recurrence_rule is not None:
                update_kwargs["recurrence_rule"] = recurrence_rule
            if action_config is not None:
                update_kwargs["action_config"] = action_config
                if restamp_profile_id is not None:
                    update_kwargs["processing_profile_id"] = restamp_profile_id
                if restamp_user_id is not None:
                    update_kwargs["created_by_user_id"] = restamp_user_id
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
            timezone=exec_context.timezone,
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
            timezone=exec_context.timezone,
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
        # ast-grep-ignore: no-dict-any - Dynamic response dict with mixed value types (int, str, list)
        stats_data: dict[str, Any] = {
            "automation_id": automation_id,
            "total_executions": stats.get("total_executions", 0),
        }

        last_execution_at = stats.get("last_execution_at")
        if last_execution_at:
            lines.append(
                f"Last execution: {format_automation_datetime(last_execution_at, exec_context.timezone)}"
            )
            stats_data["last_execution_at"] = _to_isoformat(
                last_execution_at, exec_context.timezone
            )

        next_scheduled_at = stats.get("next_scheduled_at")
        if next_scheduled_at:
            lines.append(
                f"Next scheduled: {format_automation_datetime(next_scheduled_at, exec_context.timezone)}"
            )
            stats_data["next_scheduled_at"] = _to_isoformat(
                next_scheduled_at, exec_context.timezone
            )

        recent = stats.get("recent_executions", [])
        if recent:
            lines.append(f"\nRecent executions ({len(recent)}):")
            recent_list = []
            for execution in recent[:5]:  # Show top 5
                status = execution.get("status", "unknown")
                created = execution.get("created_at")
                if created:
                    lines.append(
                        f"  - {format_automation_datetime(created, exec_context.timezone)}: {status}"
                    )
                    recent_list.append({
                        "created_at": _to_isoformat(created, exec_context.timezone),
                        "status": status,
                    })
            stats_data["recent_executions"] = recent_list

        text = "\n".join(lines)
        return ToolResult(text=text, data=stats_data)

    except Exception as e:
        logger.error(f"Error getting automation stats: {e}", exc_info=True)
        error_msg = f"Error getting automation stats: {e}"
        return ToolResult(text=error_msg, data={"error": error_msg})
