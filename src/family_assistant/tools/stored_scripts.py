"""Tools for managing stored scripts (CRUD).

Execution of stored scripts is handled by execute_script via its name parameter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


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
    # Validate script syntax before saving
    from family_assistant.scripting.validator import (  # noqa: PLC0415 - lazy import to break circular: scripting → tools → stored_scripts → scripting
        ScriptValidator,
    )

    tools_provider = _get_tools_provider(exec_context)
    tool_definitions = None
    if tools_provider:
        tool_definitions = await tools_provider.get_tool_definitions()

    # Build input_names from schema properties only.
    # Scripts that need automation globals (event, etc.) should declare them in parameters_schema.
    input_names: list[str] | None = None
    if parameters_schema and isinstance(parameters_schema.get("properties"), dict):
        input_names = list(parameters_schema["properties"].keys())

    validation = ScriptValidator(tool_definitions=tool_definitions).validate(
        code,
        input_names=input_names,
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
