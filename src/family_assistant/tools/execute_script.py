"""Script execution tool.

This module contains a tool for executing Python scripts within the family assistant.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.scripting.apis.keychute import (
    add_keychute_http_api,
    get_keychute_config,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
    ScriptTimeoutError,
)
from family_assistant.scripting.monty_engine import MontyEngine, ScriptOutputBuffer
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def _is_valid_uuid(value: str) -> bool:
    """Check if string is a valid UUID."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _extract_ids_from_list(items: list[Any]) -> list[str]:
    """Extract attachment IDs from a list of items (recursively handles nested lists)."""
    ids = []
    for item in items:
        if isinstance(item, ScriptAttachment):
            # Legacy: ScriptAttachment object (keeping for backwards compatibility)
            ids.append(item.get_id())
        elif isinstance(item, dict) and "id" in item:
            # New: Attachment dict from attachment_create() or tools
            if _is_valid_uuid(item["id"]):
                ids.append(item["id"])
        elif isinstance(item, str) and _is_valid_uuid(item):
            # Legacy: UUID string
            ids.append(item)
        elif isinstance(item, list):
            # Recursively extract from nested lists
            ids.extend(_extract_ids_from_list(item))
        elif (
            isinstance(item, dict)
            and "attachments" in item
            and isinstance(item["attachments"], list)
        ):
            # Handle dicts with attachments field (from tools that return multiple attachments)
            ids.extend(_extract_ids_from_list(item["attachments"]))
    return ids


def _prepend_captured_output(error_text: str, output_buffer: ScriptOutputBuffer) -> str:
    """Prepend any captured print() output to an error message.

    A script that fails partway through may have printed diagnostics before
    raising. Surfacing them alongside the error helps the LLM debug.
    """
    captured = output_buffer.getvalue()
    if captured.strip():
        return f"--- Script Output ---\n{captured.rstrip()}\n\n{error_text}"
    return error_text


def _extract_attachment_ids_from_result(result: Any) -> list[str]:  # noqa: ANN401
    """
    Extract attachment IDs from script return value.

    Supports:
    - ScriptAttachment object
    - List of ScriptAttachments or dicts with "id" field
    - Dict with "id" field (from attachment_create())
    - UUID strings (backward compatibility)
    - Dicts with attachments/attachment_ids keys (backward compatibility)

    Args:
        result: The script return value

    Returns:
        List of attachment UUIDs (deduplicated)
    """
    # Single ScriptAttachment
    if isinstance(result, ScriptAttachment):
        return [result.get_id()]

    # List of attachments or UUIDs
    if isinstance(result, list):
        return _extract_ids_from_list(result)

    # Dict with attachments (backward compatibility)
    if isinstance(result, dict):
        ids = []

        # Check if this dict itself is an attachment (has "id" field with valid UUID)
        # Safely get the ID and check its type before validation
        attachment_id = result.get("id")
        if isinstance(attachment_id, str) and _is_valid_uuid(attachment_id):
            ids.append(attachment_id)

        # Check for attachments key
        if "attachments" in result and isinstance(result["attachments"], list):
            ids.extend(_extract_ids_from_list(result["attachments"]))

        # Check for attachment_ids key (legacy)
        if "attachment_ids" in result and isinstance(result["attachment_ids"], list):
            ids.extend(_extract_ids_from_list(result["attachment_ids"]))

        return list(dict.fromkeys(ids))  # Deduplicate preserving order

    # Single UUID string (backward compatibility)
    if isinstance(result, str) and _is_valid_uuid(result):
        return [result]

    return []


async def execute_script_tool(
    exec_context: ToolExecutionContext,
    script: str | None = None,
    # ast-grep-ignore: no-dict-any - arbitrary globals injected into script execution namespace
    globals: dict[str, Any] | None = None,
    name: str | None = None,
    # ast-grep-ignore: no-dict-any - arbitrary parameters passed as script globals
    parameters: dict[str, Any] | None = None,
) -> ToolResult:
    """
    Execute a Python script in a sandboxed environment.

    Provide either `script` (inline code) or `name` (stored script lookup).

    Args:
        exec_context: The execution context
        script: The Python script code to execute (inline mode)
        globals: Optional dictionary of global variables to inject into the script
        name: Name of a stored script to execute (stored script mode)
        parameters: Parameters to pass to a stored script as global variables

    Returns:
        ToolResult with text and any attachments returned by the script
    """
    output_buffer = ScriptOutputBuffer()
    try:
        # Reject ambiguous calls with both script and name
        if name and script:
            error_msg = "Provide either 'script' (inline) or 'name' (stored), not both"
            return ToolResult(
                text=f"Error: {error_msg}",
                data={
                    "status": "error",
                    "error_type": "validation_error",
                    "error": error_msg,
                },
            )

        # Resolve stored script by name
        if name and not script:
            db = exec_context.db_context
            stored_script = await db.scripts.get_by_name(name)
            if stored_script is None:
                return ToolResult(
                    text=f"Error: Script '{name}' not found",
                    data={
                        "status": "error",
                        "error_type": "not_found",
                        "error": f"Script '{name}' not found",
                    },
                )
            script = stored_script.script_code

            # Validate parameters against schema
            if stored_script.parameters_schema:
                params = parameters or {}
                required = stored_script.parameters_schema.get("required", [])
                if not isinstance(required, list):
                    required = []
                for req in required:
                    if req not in params:
                        error_msg = f"Missing required parameter: {req}"
                        return ToolResult(
                            text=f"Error: {error_msg}",
                            data={
                                "status": "error",
                                "error_type": "validation_error",
                                "error": error_msg,
                            },
                        )

            # Merge parameters into globals
            if parameters:
                globals = dict(globals or {})
                globals.update(parameters)

        if not script:
            error_msg = "Either 'script' (inline code) or 'name' (stored script) must be provided"
            return ToolResult(
                text=f"Error: {error_msg}",
                data={
                    "status": "error",
                    "error_type": "validation_error",
                    "error": error_msg,
                },
            )

        keychute_config = get_keychute_config(exec_context)
        if keychute_config is not None:
            globals = add_keychute_http_api(
                globals,
                config=keychute_config,
                script_source=script,
                execution_context=exec_context,
            )
        # Create a configuration with reasonable defaults
        config = ScriptConfig(
            max_execution_time=600.0,  # 10 minute timeout for scripts that may make external calls
            enable_print=True,  # Allow print statements
            enable_debug=False,  # No debug output by default
            allowed_tools=None,  # Allow all tools (controlled by ToolsProvider)
            deny_all_tools=False,  # Don't deny tools by default
        )

        # Get the tools provider from the context if available
        tools_provider = None

        # First try to get it directly from the context (for API calls)
        if hasattr(exec_context, "tools_provider") and exec_context.tools_provider:
            tools_provider = exec_context.tools_provider
        # Otherwise try to get it from processing_service (for normal calls)
        elif exec_context.processing_service and hasattr(
            exec_context.processing_service, "tools_provider"
        ):
            tools_provider = exec_context.processing_service.tools_provider

        # Log whether tools are available
        if tools_provider:
            logger.info(
                f"Script execution with tools provider available: {type(tools_provider).__name__}"
            )
        else:
            logger.warning(
                "Script execution without tools provider - tool functions will not be available. "
                "This may happen when execute_script is called outside of normal processing flow."
            )

        # Validate script before execution (lazy import to avoid circular dependency)
        from family_assistant.scripting.validator import (  # noqa: PLC0415 - lazy import to break circular: scripting → tools → execute_script → scripting
            ScriptValidator,
        )

        tool_definitions = None
        if tools_provider:
            tool_definitions = await tools_provider.get_tool_definitions()
        # Split globals into non-callable inputs and callable external functions.
        # MontyEngine exposes callable globals as external functions at runtime,
        # so validation must treat them as functions, not plain variables.
        input_names: list[str] | None = None
        callable_names: list[str] | None = None
        if globals:
            input_names = [k for k, v in globals.items() if not callable(v)]
            callable_names = [k for k, v in globals.items() if callable(v)]
        has_attachment_registry = bool(exec_context.attachment_registry)
        validation = ScriptValidator(tool_definitions=tool_definitions).validate(
            script,
            input_names=input_names,
            extra_external_functions=callable_names,
            include_tools_api=tools_provider is not None,
            include_attachment_api=has_attachment_registry,
        )
        if not validation.is_valid:
            first_error = validation.errors[0] if validation.errors else None
            if first_error and first_error.message.startswith("Syntax error"):
                error_msg = "Syntax error in script"
                if first_error.line:
                    error_msg += f" at line {first_error.line}"
                error_msg += f": {first_error.message}"
                error_type = "syntax_error"
            else:
                error_msg = f"Script validation failed: {validation.error_message}"
                error_type = "validation_error"

            logger.error(error_msg)
            return ToolResult(
                text=f"Error: {error_msg}",
                data={
                    "status": "error",
                    "error_type": error_type,
                    "error": error_msg,
                },
            )

        # Create the engine with the tools provider (may be None)
        engine = MontyEngine(
            tools_provider=tools_provider,
            config=config,
            default_timezone=exec_context.timezone,
        )

        # Execute the script asynchronously. MontyEngine.evaluate_async marks the
        # context as in-script so tools that would otherwise defer their result to a
        # later conversation message (e.g. delegate_to_service's async handoff) run
        # synchronously and return their result to the script instead.
        result = await engine.evaluate_async(
            script=script,
            globals_dict=globals,
            execution_context=exec_context
            if (tools_provider or exec_context.attachment_registry)
            else None,  # Pass context if we have tools or attachment registry
            output_buffer=output_buffer,
        )

        # Extract attachment IDs from return value
        attachment_ids = _extract_attachment_ids_from_result(result)

        # Check for any wake_llm contexts
        wake_contexts = engine.get_pending_wake_contexts()

        # Format the response
        response_parts = []

        # Surface anything the script printed so the LLM can read print() output.
        captured_output = output_buffer.getvalue()
        if captured_output.strip():
            response_parts.append(
                f"--- Script Output ---\n{captured_output.rstrip()}\n"
            )

        # Add the script result (but skip if it's just an attachment dict being propagated)
        if result is None:
            response_parts.append("Script executed successfully with no return value.")
        elif isinstance(result, ScriptAttachment):
            # Legacy: ScriptAttachment - show metadata
            response_parts.append(
                f"Script result: Attachment(id={result.get_id()}, "
                f"mime_type={result.get_mime_type()}, size={result.get_size()})"
            )
        elif (
            isinstance(result, dict)
            and "id" in result
            and _is_valid_uuid(result.get("id", ""))
        ):
            # Attachment dict - show summary
            response_parts.append(
                f"Script result: Attachment(id={result['id']}, "
                f"mime_type={result.get('mime_type', 'unknown')}, "
                f"size={result.get('size', 0)} bytes)"
            )
        elif isinstance(result, dict | list):
            # Pretty-print JSON-serializable structures
            try:
                response_parts.append(f"Script result:\n{json.dumps(result, indent=2)}")
            except TypeError:
                # Contains non-serializable objects, show string representation
                response_parts.append(f"Script result: {result}")
        else:
            # Convert other types to string
            response_parts.append(f"Script result: {result}")

        # Add wake_llm contexts if any
        if wake_contexts:
            response_parts.append("\n--- Wake LLM Contexts ---")
            for i, wake_context in enumerate(wake_contexts):
                response_parts.append(f"\nWake Context {i + 1}:")
                response_parts.append(
                    f"Include Event: {wake_context.get('include_event', True)}"
                )
                response_parts.append(
                    f"Context: {json.dumps(wake_context.get('context', {}), indent=2)}"
                )

        response_text = "\n".join(response_parts)

        # Build ToolResult with attachments
        attachments = None
        if attachment_ids:
            # Fetch actual metadata for each attachment to get correct mime_type
            attachments = []
            for aid in attachment_ids:
                mime_type = "application/octet-stream"  # Default fallback

                # Try to fetch actual metadata if we have attachment_registry
                if exec_context.attachment_registry and exec_context.db_context:
                    try:
                        metadata = (
                            await exec_context.attachment_registry.get_attachment(
                                exec_context.db_context,
                                aid,
                                acting_user_id=exec_context.user_id,
                            )
                        )
                        if metadata:
                            mime_type = metadata.mime_type
                    except Exception as e:
                        logger.warning(
                            f"Failed to fetch metadata for attachment {aid}: {e}"
                        )

                attachments.append(
                    ToolAttachment(
                        mime_type=mime_type,
                        attachment_id=aid,
                    )
                )

        # Prepare data field - preserve structured data for programmatic access
        # ast-grep-ignore: no-dict-any - Script results can be arbitrary structures
        result_data: dict[str, Any] | list[Any] | str | int | float | bool | None = None
        if isinstance(result, (dict, list, int, float, bool, str)):
            # Preserve structured data for programmatic access
            result_data = result  # type: ignore[assignment]
        elif result is not None and not isinstance(result, ScriptAttachment):
            # For other types, convert to string
            result_data = str(result)

        return ToolResult(
            text=response_text,
            attachments=attachments,
            data=result_data,
        )

    except ScriptSyntaxError as e:
        error_msg = "Syntax error in script"
        if e.line:
            error_msg += f" at line {e.line}"
        error_msg += f": {e!s}"
        logger.error(error_msg)
        return ToolResult(
            text=f"Error: {error_msg}",
            data={
                "status": "error",
                "error_type": "syntax_error",
                "error": error_msg,
            },
        )

    except ScriptTimeoutError as e:
        error_msg = f"Script execution timed out after {e.timeout_seconds} seconds"
        logger.error(error_msg)
        return ToolResult(
            text=_prepend_captured_output(f"Error: {error_msg}", output_buffer),
            data={
                "status": "error",
                "error_type": "timeout_error",
                "error": error_msg,
            },
        )

    except ScriptExecutionError as e:
        error_msg = f"Script execution failed: {e!s}"
        logger.error(error_msg)
        return ToolResult(
            text=_prepend_captured_output(f"Error: {error_msg}", output_buffer),
            data={
                "status": "error",
                "error_type": "execution_error",
                "error": error_msg,
            },
        )

    except Exception as e:
        logger.exception(f"Unexpected error executing script: {e}")
        error_msg = f"Unexpected error executing script: {e}"
        return ToolResult(
            text=_prepend_captured_output(f"Error: {error_msg}", output_buffer),
            data={
                "status": "error",
                "error_type": "unexpected_error",
                "error": error_msg,
            },
        )


# Tool Definition
SCRIPT_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": (
                "Execute an inline or stored Python script in a sandboxed environment. "
                "Before writing or debugging a script, load `scripting.md` with "
                "`get_user_documentation_content`; it documents the language constraints, "
                "available APIs, brokered HTTP, attachments, and return values. Use `script` "
                "with optional `globals` for inline code, or `name` with optional `parameters` "
                "for a stored script. Enabled Family Assistant tools and configured script "
                "APIs are available as functions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "Inline Python source. Load `scripting.md` with "
                            "`get_user_documentation_content` before writing or "
                            "debugging scripts. Omit this when using `name`."
                        ),
                    },
                    "globals": {
                        "type": "object",
                        "description": (
                            "Optional global values for inline `script` code. "
                            "Do not use with a stored script."
                        ),
                        "additionalProperties": True,
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Stored script name. Use `list_scripts` to discover names; "
                            "omit `script` and pass arguments with `parameters`."
                        ),
                    },
                    "parameters": {
                        "type": "object",
                        "description": (
                            "Optional arguments for the stored script selected by `name`; "
                            "validated against its parameter schema."
                        ),
                        "additionalProperties": True,
                    },
                },
            },
        },
    },
]
