"""Script execution tool.

This module contains a tool for executing Starlark scripts within the family assistant.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
    ScriptTimeoutError,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


async def execute_script_tool(
    exec_context: ToolExecutionContext,
    script: str,
    globals: dict[str, Any] | None = None,
) -> str:
    """
    Execute a Starlark script in a sandboxed environment.

    Args:
        exec_context: The execution context
        script: The Starlark script code to execute
        globals: Optional dictionary of global variables to inject into the script

    Returns:
        A string containing the script result or error message
    """
    # Import here to avoid circular imports
    from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine

    try:
        # Create a configuration with reasonable defaults
        config = StarlarkConfig(
            max_execution_time=30.0,  # 30 second timeout
            max_memory_mb=100,  # 100MB memory limit
            enable_print=True,  # Allow print statements
            enable_debug=False,  # No debug output by default
            allowed_tools=None,  # Allow all tools (controlled by ToolsProvider)
            deny_all_tools=False,  # Don't deny tools by default
        )

        # Create the engine with the tools provider from the context
        engine = StarlarkEngine(
            tools_provider=exec_context.processing_service.tools_provider
            if exec_context.processing_service
            else None,
            config=config,
        )

        # Execute the script asynchronously
        result = await engine.evaluate_async(
            script=script,
            globals_dict=globals,
            execution_context=exec_context,
        )

        # Format the result as a string
        if result is None:
            return "Script executed successfully with no return value."
        elif isinstance(result, (dict, list)):
            # Pretty-print JSON-serializable structures
            return f"Script result:\n{json.dumps(result, indent=2)}"
        else:
            # Convert other types to string
            return f"Script result: {result}"

    except ScriptSyntaxError as e:
        error_msg = "Syntax error in script"
        if e.line:
            error_msg += f" at line {e.line}"
        error_msg += f": {str(e)}"
        logger.error(error_msg)
        return f"Error: {error_msg}"

    except ScriptTimeoutError as e:
        error_msg = f"Script execution timed out after {e.timeout_seconds} seconds"
        logger.error(error_msg)
        return f"Error: {error_msg}"

    except ScriptExecutionError as e:
        error_msg = f"Script execution failed: {str(e)}"
        logger.error(error_msg)
        return f"Error: {error_msg}"

    except Exception as e:
        logger.error(f"Unexpected error executing script: {e}", exc_info=True)
        return f"Error: Unexpected error executing script: {e}"


# Tool Definition
SCRIPT_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": (
                "Execute a Starlark script in a sandboxed environment. The script has access to family assistant tools "
                "through the tools API. Scripts can perform complex automation tasks by combining multiple tool calls "
                "and control flow logic. Scripts have a 30-second execution timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "The Starlark script code to execute. The script can use print() for output, "
                            "call tools using their names directly (e.g., add_or_update_note(title='...', content='...')), "
                            "or use the tools API (tools_list(), tools_get(name), tools_execute(name, **args))."
                        ),
                    },
                    "globals": {
                        "type": "object",
                        "description": (
                            "Optional dictionary of global variables to inject into the script environment. "
                            "These will be available as variables in the script."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["script"],
            },
        },
    },
]
