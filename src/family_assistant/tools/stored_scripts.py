"""Tools for managing and executing stored scripts.

Provides CRUD operations for a script library and the ability to execute
stored scripts by name.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
    ScriptTimeoutError,
)
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.tools.execute_script import _extract_attachment_ids_from_result
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


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

    validation = ScriptValidator(tool_definitions=tool_definitions).validate(
        code,
        input_names=list(parameters_schema["properties"].keys())
        if parameters_schema and "properties" in parameters_schema
        else None,
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


async def run_script_tool(
    exec_context: ToolExecutionContext,
    name: str,
    # ast-grep-ignore: no-dict-any - arbitrary parameters passed as script globals
    parameters: dict[str, Any] | None = None,
) -> ToolResult:
    """Execute a stored script by name."""
    db = exec_context.db_context
    script = await db.scripts.get_by_name(name)
    if script is None:
        return ToolResult(data={"error": f"Script '{name}' not found"})

    # Validate parameters against schema if both are present
    if script.parameters_schema and parameters:
        schema_props = script.parameters_schema.get("properties", {})
        required = script.parameters_schema.get("required", [])
        for req in required:
            if req not in parameters:
                return ToolResult(data={"error": f"Missing required parameter: {req}"})
        # Warn about unknown parameters
        unknown = set(parameters.keys()) - set(schema_props.keys())
        if unknown:
            logger.warning(f"Script '{name}' received unknown parameters: {unknown}")

    # Build globals from parameters
    globals_dict = dict(parameters) if parameters else None

    try:
        return await _execute_script_code(
            exec_context, script.script_code, globals_dict
        )
    except Exception as e:
        logger.error(f"Error running stored script '{name}': {e}", exc_info=True)
        return ToolResult(data={"error": f"Script execution failed: {e}"})


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
# Helpers
# ---------------------------------------------------------------------------


def _get_tools_provider(exec_context: ToolExecutionContext) -> Any:  # noqa: ANN401
    """Extract the tools provider from the execution context."""
    if hasattr(exec_context, "tools_provider") and exec_context.tools_provider:
        return exec_context.tools_provider
    if exec_context.processing_service and hasattr(
        exec_context.processing_service, "tools_provider"
    ):
        return exec_context.processing_service.tools_provider
    return None


async def _execute_script_code(
    exec_context: ToolExecutionContext,
    script_code: str,
    # ast-grep-ignore: no-dict-any - arbitrary globals injected into script execution namespace
    globals_dict: dict[str, Any] | None,
) -> ToolResult:
    """Execute script code and return a ToolResult.

    Shared logic between run_script and any future callers.
    """
    config = ScriptConfig(
        max_execution_time=600.0,
        enable_print=True,
        enable_debug=False,
        allowed_tools=None,
        deny_all_tools=False,
    )

    tools_provider = _get_tools_provider(exec_context)
    engine = MontyEngine(
        tools_provider=tools_provider,
        config=config,
        default_timezone=exec_context.timezone,
    )

    try:
        result = await engine.evaluate_async(
            script=script_code,
            globals_dict=globals_dict,
            execution_context=exec_context
            if (tools_provider or exec_context.attachment_registry)
            else None,
        )
    except ScriptSyntaxError as e:
        error_msg = (
            f"Syntax error at line {e.line}: {e}" if e.line else f"Syntax error: {e}"
        )
        return ToolResult(data={"error": error_msg})
    except ScriptTimeoutError as e:
        return ToolResult(
            data={"error": f"Script timed out after {e.timeout_seconds}s"}
        )
    except ScriptExecutionError as e:
        return ToolResult(data={"error": f"Script execution failed: {e}"})

    # Format response (simplified version of execute_script_tool logic)
    attachment_ids = _extract_attachment_ids_from_result(result)

    if result is None:
        text = "Script executed successfully with no return value."
    elif isinstance(result, dict | list):
        try:
            text = f"Script result:\n{json.dumps(result, indent=2)}"
        except TypeError:
            text = f"Script result: {result}"
    else:
        text = f"Script result: {result}"

    attachments = None
    if attachment_ids:
        attachments = []
        for aid in attachment_ids:
            mime_type = "application/octet-stream"
            if exec_context.attachment_registry and exec_context.db_context:
                try:
                    metadata = await exec_context.attachment_registry.get_attachment(
                        exec_context.db_context, aid
                    )
                    if metadata:
                        mime_type = metadata.mime_type
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch metadata for attachment {aid}: {e}"
                    )
            attachments.append(ToolAttachment(mime_type=mime_type, attachment_id=aid))

    # ast-grep-ignore: no-dict-any - Script results can be arbitrary structures
    result_data: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    if isinstance(result, (dict, list, int, float, bool, str)):
        result_data = result  # type: ignore[assignment]
    elif result is not None:
        result_data = str(result)

    return ToolResult(text=text, attachments=attachments, data=result_data)


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
                "be executed later with run_script. Use this to save scripts that are useful for "
                "repeated tasks like data processing, report generation, or automation routines.\n\n"
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
            "name": "run_script",
            "description": (
                "Execute a stored script by name. Use list_scripts to see available scripts.\n\n"
                "The script runs in the same sandboxed environment as execute_script, "
                "with access to all enabled tools. Parameters are injected as global variables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the stored script to execute",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Parameters to pass to the script as global variables",
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
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
