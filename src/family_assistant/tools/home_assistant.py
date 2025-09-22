"""Home Assistant integration tools.

This module contains tools for interacting with Home Assistant, including
rendering templates and retrieving camera snapshots.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import (
    ToolAttachment,
    ToolResult,
    get_attachment_limits,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def detect_image_mime_type(content: bytes) -> str:
    """
    Detect MIME type from image content based on file signatures.

    Args:
        content: The binary image content

    Returns:
        The detected MIME type string, defaults to "image/jpeg"
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    elif content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return "image/gif"
    elif content.startswith(b"RIFF") and b"WEBP" in content[:12]:
        return "image/webp"
    else:
        return "image/jpeg"  # Default fallback


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
                "or formatting data using Home Assistant's template engine.\n\n"
                "Returns: A string containing the rendered template result. "
                "On success, returns the evaluated template output as a string (empty results return 'Template rendered to empty result'). "
                "If HA not configured, returns 'Error: Home Assistant integration is not configured or available.'. "
                "If HA API not installed, returns 'Error: Home Assistant API library is not installed.'. "
                "On API error, returns 'Error: Home Assistant API error - [error details]'. "
                "On other errors, returns 'Error: Failed to render template - [error details]'."
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
    {
        "type": "function",
        "function": {
            "name": "get_camera_snapshot",
            "description": (
                "Retrieves a current snapshot/image from a Home Assistant camera entity. "
                "The image will be displayed to you for analysis. "
                "If no camera_entity_id is provided, returns a list of available cameras.\n\n"
                "Common camera entities include doorbell cameras, security cameras, and webcams. "
                "Examples: camera.front_door, camera.doorbell_camera, camera.backyard_cam\n\n"
                "Returns: On success with entity_id, returns the camera image. "
                "Without entity_id, returns list of available cameras. "
                "On errors, returns descriptive error messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": (
                            "The Home Assistant entity ID of the camera (e.g., 'camera.front_door'). "
                            "If not provided, returns a list of all available camera entities."
                        ),
                    },
                },
                "required": [],
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
        from homeassistant_api.errors import (  # noqa: PLC0415
            HomeassistantAPIError,
        )
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


async def get_camera_snapshot_tool(
    exec_context: ToolExecutionContext,
    camera_entity_id: str | None = None,
) -> ToolResult:
    """
    Retrieves a snapshot from a Home Assistant camera or lists available cameras.

    Args:
        exec_context: The tool execution context containing HA client
        camera_entity_id: The entity ID of the camera (e.g., 'camera.front_door')
                         If not provided, returns list of available cameras.

    Returns:
        ToolResult with image attachment when entity_id is provided,
        or string with list of available cameras when entity_id is omitted
    """
    logger.info(f"Getting camera snapshot: entity_id={camera_entity_id}")

    # Check if Home Assistant client is available
    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

    ha_client = exec_context.home_assistant_client

    # If no entity_id provided, list available cameras
    if not camera_entity_id:
        try:
            # Get all entities to find cameras
            states = await ha_client.async_get_states()

            # Filter for camera entities
            cameras = []
            for entity in states:
                if entity.entity_id.startswith("camera."):
                    # Get friendly name if available
                    friendly_name = entity.attributes.get(
                        "friendly_name", entity.entity_id
                    )
                    if friendly_name and friendly_name != entity.entity_id:
                        cameras.append(f"- {entity.entity_id} ({friendly_name})")
                    else:
                        cameras.append(f"- {entity.entity_id}")

            if not cameras:
                return ToolResult(text="No camera entities found in Home Assistant.")

            return ToolResult(
                text="Available cameras in Home Assistant:\n" + "\n".join(cameras)
            )

        except Exception as e:
            logger.error(f"Error listing cameras: {e}", exc_info=True)
            return ToolResult(text=f"Error listing available cameras: {str(e)}")

    # Use the HA client's custom camera snapshot method to get raw binary data
    try:
        image_content = await ha_client.async_get_camera_snapshot(camera_entity_id)

        # Check image size (multimodal limit from config)
        image_size = len(image_content)
        _, max_multimodal_size = get_attachment_limits(exec_context)
        if image_size > max_multimodal_size:
            max_mb = max_multimodal_size / (1024 * 1024)
            logger.warning(
                f"Camera image is {image_size / (1024 * 1024):.1f}MB, exceeds {max_mb:.0f}MB limit"
            )
            return ToolResult(
                text=f"Error: Camera image too large ({image_size / (1024 * 1024):.1f}MB), exceeds {max_mb:.0f}MB limit"
            )

        logger.info(f"Successfully retrieved camera snapshot: {image_size} bytes")

        # Detect MIME type from image content
        mime_type = detect_image_mime_type(image_content)

        # Return image as attachment
        return ToolResult(
            text=f"Retrieved snapshot from camera '{camera_entity_id}'",
            attachment=ToolAttachment(
                mime_type=mime_type,
                content=image_content,
                description=f"Camera snapshot from {camera_entity_id}",
            ),
        )

    except Exception as e:
        logger.error(f"Error getting camera snapshot: {e}", exc_info=True)
        return ToolResult(text=f"Error: Failed to retrieve camera snapshot: {str(e)}")
