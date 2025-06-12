"""Home Assistant integration tools.

This module contains tools for interacting with Home Assistant, including
rendering templates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Tool Definitions
HOME_ASSISTANT_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "render_home_assistant_template",
            "description": (
                "Renders a Home Assistant Jinja2 template and returns the result. "
                "This tool allows you to evaluate templates using Home Assistant's current state, "
                "including all entities, attributes, and template functions available in HA. "
                "Common uses include getting entity states, performing calculations, "
                "or formatting data using Home Assistant's template engine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "description": (
                            "The Jinja2 template string to render. Can use all Home Assistant "
                            "template functions and filters, such as states(), state_attr(), "
                            "now(), as_timestamp(), etc. Example: '{{ states(\"sensor.temperature\") }}'"
                        ),
                    },
                },
                "required": ["template"],
            },
        },
    },
]


# Tool Implementation
async def render_home_assistant_template_tool(
    exec_context: ToolExecutionContext,
    template: str,
) -> str:
    """
    Renders a Home Assistant template and returns the result.

    Args:
        exec_context: The tool execution context
        template: The Jinja2 template string to render

    Returns:
        The rendered template result as a string, or an error message
    """
    logger.info(f"Rendering Home Assistant template: {template[:100]}...")

    # Check if Home Assistant client is available in context
    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return "Error: Home Assistant integration is not configured or available."

    ha_client = exec_context.home_assistant_client

    try:
        # Import homeassistant_api to check for the method
        from homeassistant_api.errors import HomeassistantAPIError
    except ImportError:
        logger.error("homeassistant_api library is not installed")
        return "Error: Home Assistant API library is not installed."

    try:
        # Use the async method to render the template
        rendered_result = await ha_client.async_get_rendered_template(template=template)

        if rendered_result is None:
            logger.warning("Template rendering returned None")
            return "Template rendered to empty result"

        # Convert to string if needed
        result_str = str(rendered_result).strip()

        logger.info(f"Successfully rendered template, result length: {len(result_str)}")
        return result_str

    except HomeassistantAPIError as e:
        logger.error(f"Home Assistant API error rendering template: {e}", exc_info=True)
        return f"Error: Home Assistant API error - {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error rendering template: {e}", exc_info=True)
        return f"Error: Failed to render template - {str(e)}"
