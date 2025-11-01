"""Script execution tool.

This module contains a tool for executing Starlark scripts within the family assistant.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.scripting.apis.tools import ScriptToolResult
from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine
from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
    ScriptTimeoutError,
)
from family_assistant.tools.types import ToolAttachment, ToolResult

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


def _extract_ids_from_list(items: list[Any]) -> list[str]:  # noqa: ANN401
    """Extract attachment IDs from a list of items."""
    ids = []
    for item in items:
        if isinstance(item, ScriptAttachment):
            ids.append(item.get_id())
        elif isinstance(item, str) and _is_valid_uuid(item):
            ids.append(item)  # Backward compatibility
    return ids


def _extract_attachment_ids_from_result(result: Any) -> list[str]:  # noqa: ANN401
    """
    Extract attachment IDs from script return value.

    Supports:
    - ScriptAttachment object
    - ScriptToolResult object
    - List of ScriptAttachments
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

    # ScriptToolResult
    if isinstance(result, ScriptToolResult):
        return [att.get_id() for att in result.get_attachments()]

    # List of attachments or UUIDs
    if isinstance(result, list):
        return _extract_ids_from_list(result)

    # Dict with attachments (backward compatibility)
    if isinstance(result, dict):
        ids = []
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
    script: str,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    globals: dict[str, Any] | None = None,
) -> ToolResult:
    """
    Execute a Starlark script in a sandboxed environment.

    Args:
        exec_context: The execution context
        script: The Starlark script code to execute
        globals: Optional dictionary of global variables to inject into the script

    Returns:
        ToolResult with text and any attachments returned by the script
    """
    try:
        # Create a configuration with reasonable defaults
        config = StarlarkConfig(
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

        # Create the engine with the tools provider (may be None)
        engine = StarlarkEngine(
            tools_provider=tools_provider,
            config=config,
        )

        # Execute the script asynchronously
        result = await engine.evaluate_async(
            script=script,
            globals_dict=globals,
            execution_context=exec_context
            if tools_provider
            else None,  # Only pass context if we have tools
        )

        # Extract attachment IDs from return value
        attachment_ids = _extract_attachment_ids_from_result(result)

        # Check for any wake_llm contexts
        wake_contexts = engine.get_pending_wake_contexts()

        # Format the response
        response_parts = []

        # Add the script result
        if result is None:
            response_parts.append("Script executed successfully with no return value.")
        elif isinstance(result, dict | list):
            # Pretty-print JSON-serializable structures
            response_parts.append(f"Script result:\n{json.dumps(result, indent=2)}")
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
            # For attachment references (ID only), mime_type is a placeholder
            attachments = [
                ToolAttachment(
                    mime_type="application/octet-stream",  # Placeholder for references
                    attachment_id=aid,
                )
                for aid in attachment_ids
            ]

        # Prepare data field (typed as dict or list or None)
        # ast-grep-ignore: no-dict-any - Script results can be arbitrary structures
        result_data: dict[str, Any] | str | None = None
        if isinstance(result, (dict, list)):
            result_data = result  # type: ignore[assignment]

        return ToolResult(
            text=response_text,
            attachments=attachments,
            data=result_data,
        )

    except ScriptSyntaxError as e:
        error_msg = "Syntax error in script"
        if e.line:
            error_msg += f" at line {e.line}"
        error_msg += f": {str(e)}"
        logger.error(error_msg)
        return ToolResult(text=f"Error: {error_msg}")

    except ScriptTimeoutError as e:
        error_msg = f"Script execution timed out after {e.timeout_seconds} seconds"
        logger.error(error_msg)
        return ToolResult(text=f"Error: {error_msg}")

    except ScriptExecutionError as e:
        error_msg = f"Script execution failed: {str(e)}"
        logger.error(error_msg)
        return ToolResult(text=f"Error: {error_msg}")

    except Exception as e:
        logger.error(f"Unexpected error executing script: {e}", exc_info=True)
        return ToolResult(text=f"Error: Unexpected error executing script: {e}")


# Tool Definition
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
SCRIPT_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": (
                "Execute a Starlark script in a sandboxed environment for automation and complex operations.\n\n"
                "**IMPORTANT: Before writing scripts, please read the scripting documentation first!**\n"
                "Use the command: `get_user_documentation_content filename='scripting.md'`\n\n"
                "**What is Starlark?**\n"
                "Starlark is a Python-like language that LOOKS like Python but has important differences:\n"
                "• NO try/except - errors terminate the script\n"
                "• NO while loops - use for loops instead\n"
                "• NO isinstance() - use type() comparisons\n"
                "• Limited standard library\n\n"
                "**Tool Documentation:**\n"
                "To see available tools and their parameters, ask: 'Show me the available tools'\n\n"
                "**Execution Environment:**\n"
                "• Timeout: 10 minutes maximum execution time (to allow for external API calls)\n"
                "• Sandboxed: No file system or network access\n"
                "• Deterministic: No random numbers or current time\n\n"
                "**Built-in Functions:**\n"
                "• Type conversions: bool(), int(), str(), list(), tuple(), dict()\n"
                "• Collections: len(), range(), sorted(), reversed(), enumerate(), zip()\n"
                "• Logic: all(), any(), max(), min()\n"
                "• Object inspection: type(), dir(), getattr(), hasattr()\n"
                "• Control: print(), fail()\n"
                "• JSON: json_encode(value), json_decode(string)\n"
                "• LLM Wake: wake_llm(context, include_event=True) - Request LLM attention with context (string or dict)\n\n"
                "**Family Assistant Tools:**\n"
                "All enabled tools are available as functions. Call them directly:\n"
                "• add_or_update_note(title='...', content='...')\n"
                "• search_notes(query='...')\n"
                "• send_email(to='...', subject='...', body='...')\n\n"
                "**Note**: Tools currently return string results. For tools that return structured data\n"
                "(like lists or dicts), use json_decode() to parse the result:\n"
                "```starlark\n"
                "result_str = search_notes(query='TODO')\n"
                "notes = json_decode(result_str) if result_str != '[]' else []\n"
                "```\n\n"
                "**Tools API Functions:**\n"
                "• tools_list() - List all available tools with descriptions\n"
                "• tools_get(name) - Get detailed info about a specific tool\n"
                "• tools_execute(name, **kwargs) - Execute a tool by name\n\n"
                "**Attachment API Functions:**\n"
                "• attachment_get(attachment_id) - Get attachment metadata by ID\n"
                "Note: attachment_list() and attachment_send() are not available.\n"
                "Scripts must receive attachment IDs from LLM context, tool results, or parameters.\n"
                "To send attachments, use LLM tools: attach_to_response, send_message_to_user, or wake_llm.\n\n"
                "**Example Scripts:**\n\n"
                "1. Simple note creation:\n"
                "```starlark\n"
                "result = add_or_update_note(\n"
                "    title='Meeting Notes',\n"
                "    content='Discussed project timeline'\n"
                ")\n"
                "print('Created note:', result)\n"
                "```\n\n"
                "2. Conditional logic with search:\n"
                "```starlark\n"
                "def process_todos():\n"
                "    # Search returns a JSON string, so decode it\n"
                "    result_str = search_notes(query='TODO')\n"
                "    notes = json_decode(result_str) if result_str and result_str != '[]' else []\n"
                "    \n"
                "    if len(notes) > 0:\n"
                "        print('Found', len(notes), 'TODO items')\n"
                "        for note in notes:\n"
                "            print('-', note['title'])\n"
                "    else:\n"
                "        print('No TODO items found')\n"
                "    return notes\n"
                "\n"
                "# Call the function\n"
                "process_todos()\n"
                "```\n\n"
                "3. Complex automation:\n"
                "```starlark\n"
                "def create_project_summary():\n"
                "    # Search for notes, create summary, send email\n"
                "    result_str = search_notes(query='Project Alpha')\n"
                "    project_notes = json_decode(result_str) if result_str and result_str != '[]' else []\n"
                "    \n"
                "    summary = 'Project Alpha Summary:\\n\\n'\n"
                "    for note in project_notes:\n"
                "        summary += '- ' + note['title'] + '\\n'\n"
                "    \n"
                "    if len(project_notes) > 0:\n"
                "        add_or_update_note(\n"
                "            title='Project Alpha Summary',\n"
                "            content=summary\n"
                "        )\n"
                "        result = send_email(\n"
                "            to='team@example.com',\n"
                "            subject='Weekly Project Summary',\n"
                "            body=summary\n"
                "        )\n"
                "        return {'notes_found': len(project_notes), 'email_result': result}\n"
                "    else:\n"
                "        return {'notes_found': 0, 'email_result': 'No notes to summarize'}\n"
                "\n"
                "# Execute the function\n"
                "create_project_summary()\n"
                "```\n\n"
                "4. Working with user attachments (attachment ID provided by LLM):\n"
                "```starlark\n"
                "# Attachment ID passed to script by LLM based on conversation context\n"
                "# Example: LLM calls execute_script with attachment_id parameter\n"
                "def process_user_attachment(attachment_id):\n"
                "    # Get attachment details\n"
                "    attachment_info = attachment_get(attachment_id)\n"
                "    \n"
                "    if attachment_info:\n"
                "        print('Processing:', attachment_info['description'])\n"
                "        \n"
                "        # Process the attachment with a tool (ID auto-converted to content)\n"
                "        if 'image' in attachment_info['mime_type']:\n"
                "            analysis = tools_execute('analyze_image', image_data=attachment_id)\n"
                "            # Send results back to user via LLM tools\n"
                "            wake_llm({'message': 'Analysis: ' + analysis, 'attachments': [attachment_id]})\n"
                "        else:\n"
                "            # Use attach_to_response LLM tool to send attachment back\n"
                "            tools_execute('attach_to_response', attachment_ids=[attachment_id])\n"
                "    else:\n"
                "        print('Attachment not found or not accessible')\n"
                "\n"
                "# Call with attachment ID from LLM context\n"
                "# process_user_attachment('uuid-from-llm-context')\n"
                "```\n\n"
                "**Language Differences from Python:**\n"
                "• No imports or modules\n"
                "• No classes (only functions and structs)\n"
                "• No while loops (use for loops with range)\n"
                "• No exceptions (use fail() to stop with error)\n"
                "• No set literals (use dict keys as workaround)\n"
                "• Strings are immutable\n"
                "• Integer division with // (not /)\n"
                "• **Important**: if/else statements can only be used inside functions, not at top level\n"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "The Starlark script code to execute. Must be valid Starlark syntax.\n\n"
                            "The script can:\n"
                            "• Define variables and functions\n"
                            "• Use control flow (if/else, for loops)\n"
                            "• Call any available Family Assistant tool\n"
                            "• Process and transform data\n"
                            "• Print output for debugging\n"
                            "• Return a value (the last expression is returned)\n\n"
                            "Scripts should be self-contained and handle errors gracefully.\n\n"
                            "**Wake LLM Contexts:**\n"
                            "If the script calls wake_llm(), the contexts will be included in the response.\n"
                            "This allows the LLM to receive and process wake requests from scripts.\n\n"
                            "**Return Values:**\n"
                            "Returns a string containing the script execution result:\n"
                            "• On success with return value: 'Script result: [value]' or for dict/list: 'Script result:\\n[JSON formatted]'\n"
                            "• On success without return: 'Script executed successfully with no return value.'\n"
                            "• If wake_llm() called: Also includes '--- Wake LLM Contexts ---' section with context details\n"
                            "• On syntax error: 'Error: Syntax error in script [at line N]: [details]'\n"
                            "• On timeout: 'Error: Script execution timed out after [N] seconds'\n"
                            "• On execution error: 'Error: Script execution failed: [details]'\n"
                            "• On unexpected error: 'Error: Unexpected error executing script: [details]'"
                        ),
                    },
                    "globals": {
                        "type": "object",
                        "description": (
                            "Optional dictionary of global variables to inject into the script.\n\n"
                            "These variables will be available in the script's global scope.\n"
                            "Useful for passing configuration, data, or context to the script.\n\n"
                            "Example:\n"
                            "{\n"
                            '  "user_email": "john@example.com",\n'
                            '  "project_id": 123,\n'
                            '  "tags": ["important", "urgent"]\n'
                            "}\n\n"
                            "These can then be used in the script as regular variables."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["script"],
            },
        },
    },
]
