"""Tools for managing stored scripts (CRUD).

Execution of stored scripts is handled by execute_script via its name parameter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.apis.keychute import (
    get_keychute_config,
    keychute_external_function_names,
)
from family_assistant.security.definition_records import authoring_taint_state
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.storage.database import Database
    from family_assistant.storage.types import ActionConfig
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Globals automatically injected by handle_script_execution when running scripts
# from automations. If a stored script's parameters_schema marks any of these as
# required, they count as implicitly satisfied during automation validation.
AUTOMATION_RUNTIME_GLOBALS = frozenset({
    "event",
    "conversation_id",
    "listener_id",
    "listener_name",
})


def _validate_parameters_schema_shape(
    # ast-grep-ignore: no-dict-any - JSON Schema is genuinely arbitrary structure
    schema: dict[str, Any],
) -> str | None:
    """Validate the shape of a parameters_schema.

    Ensures the schema is structurally usable by downstream validation:
    - 'properties' (if present) must be a dict with string keys
    - 'required' (if present) must be a list of strings

    Returns an error message on failure, or None if valid.
    """
    if not isinstance(schema, dict):
        return f"schema: expected dict, got {type(schema).__name__}"

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            return f"'properties': expected dict, got {type(properties).__name__}"
        for key in properties:
            if not isinstance(key, str):
                return f"'properties' keys must be strings, got {type(key).__name__}"

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            return f"'required': expected list, got {type(required).__name__}"
        for item in required:
            if not isinstance(item, str):
                return f"'required' entries must be strings, got {type(item).__name__}"

    return None


async def validate_script_action_config(
    db_context: Database,
    # ast-grep-ignore: no-dict-any - action_config comes from LLM tool args as plain dict
    action_config: ActionConfig | dict[str, Any],
) -> str | None:
    """Validate a script action_config.

    Checks:
    - Exactly one of script_code or script_name is provided
    - If script_name: the stored script exists
    - If parameters: it is a dict
    - If stored script has parameters_schema with required fields: all required keys are present

    Returns an error message string on failure, or None if valid.
    """
    has_code = bool(action_config.get("script_code"))
    has_name = bool(action_config.get("script_name"))
    if has_code and has_name:
        return "Provide either 'script_code' or 'script_name', not both"
    if not has_code and not has_name:
        return "script action requires 'script_code' or 'script_name' in action_config"
    if not has_name:
        return None

    name = action_config.get("script_name")
    if not name:
        return None
    if not isinstance(name, str):
        return f"'script_name': expected str, got {type(name).__name__}"
    stored = await db_context.scripts.get_by_name(name)
    if stored is None:
        return f"Stored script '{name}' not found"

    raw_params = action_config.get("parameters")
    if raw_params is not None and not isinstance(raw_params, dict):
        return f"'parameters': expected dict, got {type(raw_params).__name__}"

    if stored.parameters_schema:
        params = raw_params or {}
        required = stored.parameters_schema.get("required", [])
        if not isinstance(required, list):
            required = []
        for req in required:
            # Automation runtime globals are injected by handle_script_execution,
            # so they don't need to be supplied via action_config parameters.
            if req in AUTOMATION_RUNTIME_GLOBALS:
                continue
            if req not in params:
                return f"Stored script '{name}' requires parameter '{req}'"

    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _get_tools_provider(exec_context: ToolExecutionContext) -> Any:  # noqa: ANN401 - tools_provider type varies across implementations
    """Extract the tools provider from the execution context."""
    if hasattr(exec_context, "tools_provider") and exec_context.tools_provider:
        return exec_context.tools_provider
    if exec_context.processing_service and hasattr(
        exec_context.processing_service, "tools_provider"
    ):
        return exec_context.processing_service.tools_provider
    return None


async def save_script_tool(
    exec_context: ToolExecutionContext,
    name: str,
    description: str,
    code: str,
    # ast-grep-ignore: no-dict-any - JSON Schema is genuinely arbitrary structure
    parameters_schema: dict[str, Any] | None = None,
) -> ToolResult:
    """Save or update a stored script."""
    # Validate parameters_schema shape
    if parameters_schema is not None:
        schema_error = _validate_parameters_schema_shape(parameters_schema)
        if schema_error:
            return ToolResult(
                data={"error": f"Invalid parameters_schema: {schema_error}"}
            )

    # Validate script syntax before saving
    from family_assistant.scripting.validator import (  # noqa: PLC0415 - lazy import to break circular: scripting → tools → stored_scripts → scripting
        ScriptValidator,
    )

    tools_provider = _get_tools_provider(exec_context)
    tool_definitions = None
    if tools_provider:
        tool_definitions = await tools_provider.get_tool_definitions()

    # Build input_names from schema properties and required fields.
    # Scripts that need automation globals (event, etc.) should declare them in parameters_schema.
    input_names: list[str] | None = None
    if parameters_schema:
        declared: set[str] = set()
        if isinstance(parameters_schema.get("properties"), dict):
            declared.update(parameters_schema["properties"].keys())
        if isinstance(parameters_schema.get("required"), list):
            declared.update(
                k for k in parameters_schema["required"] if isinstance(k, str)
            )
        if declared:
            input_names = sorted(declared)

    validation = ScriptValidator(tool_definitions=tool_definitions).validate(
        code,
        input_names=input_names,
        extra_external_functions=keychute_external_function_names(
            get_keychute_config(exec_context)
        ),
        include_tools_api=tools_provider is not None,
        include_attachment_api=bool(exec_context.attachment_registry),
    )
    if not validation.is_valid:
        return ToolResult(
            data={
                "error": f"Script validation failed: {validation.error_message}",
            }
        )

    db = exec_context.db_context
    script = await db.scripts.save(
        name=name,
        description=description,
        script_code=code,
        parameters_schema=parameters_schema,
        definition_taint_state=authoring_taint_state(exec_context.taint_tracker),
        definition_gate=exec_context.definition_gate_outcome,
    )
    return ToolResult(
        data={
            "name": script.name,
            "description": script.description,
            "parameters_schema": script.parameters_schema,
            "created_at": script.created_at.isoformat(),
            "updated_at": script.updated_at.isoformat(),
        }
    )


async def list_scripts_tool(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """List all stored scripts."""
    db = exec_context.db_context
    scripts = await db.scripts.list_all()
    if not scripts:
        return ToolResult(data={"scripts": [], "count": 0})
    return ToolResult(
        data={
            "scripts": [
                {
                    "name": s.name,
                    "description": s.description,
                    "parameters_schema": s.parameters_schema,
                }
                for s in scripts
            ],
            "count": len(scripts),
        }
    )


async def get_script_tool(
    exec_context: ToolExecutionContext,
    name: str,
) -> ToolResult:
    """Get full details of a stored script including code."""
    db = exec_context.db_context
    script = await db.scripts.get_by_name(name)
    if script is None:
        return ToolResult(data={"error": f"Script '{name}' not found"})
    return ToolResult(
        data={
            "name": script.name,
            "description": script.description,
            "script_code": script.script_code,
            "parameters_schema": script.parameters_schema,
            "created_at": script.created_at.isoformat(),
            "updated_at": script.updated_at.isoformat(),
        }
    )


async def delete_script_tool(
    exec_context: ToolExecutionContext,
    name: str,
) -> ToolResult:
    """Delete a stored script."""
    db = exec_context.db_context
    deleted = await db.scripts.delete(name)
    if not deleted:
        return ToolResult(data={"error": f"Script '{name}' not found"})
    return ToolResult(data={"deleted": True, "name": name})


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

STORED_SCRIPTS_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "save_script",
            "description": (
                "Save a reusable script to the script library. Scripts are stored by name and can "
                "be executed later via execute_script(name='...'). Use this to save scripts that "
                "are useful for repeated tasks like data processing, report generation, or "
                "automation routines.\n\n"
                "Scripts are validated before saving. If validation fails, an error is returned.\n"
                "Scripts saved here can also be referenced by automations using script_name in action_config."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique name for the script (e.g. 'daily-cleanup', 'generate-report')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what the script does",
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "The Python script code. Same sandbox rules as execute_script: "
                            "no classes, no yield, no with statements. "
                            "All enabled tools are available as functions."
                        ),
                    },
                    "parameters_schema": {
                        "type": "object",
                        "description": (
                            "Optional JSON Schema describing the parameters the script expects. "
                            "Parameters are passed as global variables when the script runs.\n"
                            'Example: {"type": "object", "properties": {"days_old": {"type": "integer"}}, '
                            '"required": ["days_old"]}'
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "description", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scripts",
            "description": "List all stored scripts with their names, descriptions, and parameter schemas.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_script",
            "description": "Get full details of a stored script including its code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the script to retrieve",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_script",
            "description": "Delete a stored script from the library.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the script to delete",
                    },
                },
                "required": ["name"],
            },
        },
    },
]
