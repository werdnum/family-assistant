"""Mock image processing tools for testing attachment workflows.

This module contains simple mock tools for testing image processing workflows
without requiring actual image processing dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from family_assistant.scripting.apis.attachments import ScriptAttachment
    from family_assistant.tools.types import ToolExecutionContext


# Tool Definitions
MOCK_IMAGE_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "mock_camera_snapshot",
            "description": (
                "Mock tool to simulate taking a camera snapshot. "
                "Returns a fake image attachment for testing workflows. "
                "This is only for testing and development purposes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The camera entity ID (ignored in mock implementation).",
                        "default": "camera.mock",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "annotate_image",
            "description": (
                "Mock tool to annotate an image with text overlay. "
                "This is a test/demo tool that creates a new annotated version of the image. "
                "In a real implementation, this would use image processing libraries to add text, arrows, or other annotations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the image attachment to annotate. Must be an existing image attachment from the current conversation.",
                    },
                    "annotation_text": {
                        "type": "string",
                        "description": "Text to overlay on the image as an annotation.",
                        "default": "Annotated Image",
                    },
                    "position": {
                        "type": "string",
                        "enum": [
                            "top-left",
                            "top-right",
                            "bottom-left",
                            "bottom-right",
                            "center",
                        ],
                        "description": "Position where to place the annotation text.",
                        "default": "top-left",
                    },
                },
                "required": ["image_attachment_id"],
            },
        },
    },
]


# Tool Implementations
async def mock_camera_snapshot_tool(
    exec_context: ToolExecutionContext,
    entity_id: str = "camera.mock",
) -> ToolResult:
    """
    Mock tool to simulate taking a camera snapshot.

    This creates a fake image attachment for testing purposes.
    In a real implementation, this would connect to Home Assistant
    and capture an actual camera image.

    Args:
        exec_context: The execution context
        entity_id: Camera entity ID (ignored in mock)

    Returns:
        ToolResult with mock image attachment
    """
    logger = logging.getLogger(__name__)

    logger.info(f"Mock camera snapshot for entity: {entity_id}")

    # Create mock image content (fake PNG header + some data)
    mock_png_header = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10"
    )
    mock_image_data = (
        mock_png_header + b"mock_camera_image_data_for_testing" + b"\x00" * 100
    )

    # Create the mock camera attachment
    camera_attachment = ToolAttachment(
        content=mock_image_data,
        mime_type="image/png",
        description=f"Mock camera snapshot from {entity_id}",
    )

    success_message = f"Successfully captured mock snapshot from camera '{entity_id}'. Image ready for processing."

    logger.info(f"Created mock camera snapshot: {len(mock_image_data)} bytes")

    return ToolResult(text=success_message, attachment=camera_attachment)


async def annotate_image_tool(
    exec_context: ToolExecutionContext,
    image_attachment_id: ScriptAttachment,
    annotation_text: str = "Annotated Image",
    position: str = "top-left",
) -> ToolResult:
    """
    Mock tool to annotate an image with text overlay.

    This is a simple mock tool that creates a new attachment with modified content
    to simulate image annotation. In a real implementation, this would use PIL,
    OpenCV, or similar libraries to add actual visual annotations.

    Args:
        exec_context: The execution context
        image_attachment_id: ScriptAttachment object containing the image to annotate
        annotation_text: Text to add as annotation (default: "Annotated Image")
        position: Where to place the text (default: "top-left")

    Returns:
        ToolResult with annotated image attachment and success message
    """
    logger = logging.getLogger(__name__)

    attachment_id = image_attachment_id.get_id()
    logger.info(
        f"Mock annotating image {attachment_id} with text '{annotation_text}' at {position}"
    )

    try:
        # Check if it's an image
        if not image_attachment_id.get_mime_type().startswith("image/"):
            logger.warning(
                f"Attachment {attachment_id} is not an image (type: {image_attachment_id.get_mime_type()})"
            )
            return ToolResult(
                text=f"Error: Attachment is not an image (type: {image_attachment_id.get_mime_type()})",
                attachment=None,
            )

        # Get original image content from the ScriptAttachment object
        original_content = await image_attachment_id.get_content_async()

        if not original_content:
            logger.error(f"Could not retrieve content for attachment {attachment_id}")
            return ToolResult(
                text="Error: Could not retrieve image content", attachment=None
            )

        # Create mock annotated content
        # In a real implementation, this would use PIL or similar to add text overlay
        mock_annotation = f"\n\n# MOCK ANNOTATION: {annotation_text} at {position}\n# Original size: {len(original_content)} bytes".encode()
        annotated_content = original_content + mock_annotation

        # Determine new filename
        original_filename = image_attachment_id.get_filename() or "image.png"
        base_name = (
            original_filename.rsplit(".", 1)[0]
            if "." in original_filename
            else original_filename
        )
        extension = (
            original_filename.rsplit(".", 1)[1] if "." in original_filename else "png"
        )
        new_filename = f"{base_name}_annotated.{extension}"

        # Create the annotated attachment
        annotated_attachment = ToolAttachment(
            content=annotated_content,
            mime_type=image_attachment_id.get_mime_type(),
            description=f"Mock annotated version of {image_attachment_id.get_description() or 'image'} with '{annotation_text}' at {position}",
        )

        success_message = (
            f"Successfully mock-annotated image '{original_filename}' with text '{annotation_text}' "
            f"at position '{position}'. Created annotated version as '{new_filename}'."
        )

        logger.info(f"Created mock annotated image: {new_filename}")

        return ToolResult(text=success_message, attachment=annotated_attachment)

    except Exception as e:
        logger.error(
            f"Error mock-annotating image {image_attachment_id}: {e}", exc_info=True
        )
        return ToolResult(
            text=f"Error: Failed to annotate image: {str(e)}", attachment=None
        )
