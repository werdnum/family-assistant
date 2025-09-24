"""Real image processing tools for multimodal attachment workflows.

This module contains actual image processing tools using PIL/Pillow
for testing and demonstrating attachment manipulation capabilities.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw

from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from family_assistant.scripting.apis.attachments import ScriptAttachment
    from family_assistant.tools.types import ToolExecutionContext

# Tool Definitions
IMAGE_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "highlight_image",
            "description": (
                "Draw colored rectangles or circles to highlight regions on an image. "
                "Useful for marking objects, areas of interest, or annotations. "
                "Returns a new image with the highlighted regions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the image attachment to highlight regions on. Must be an existing image attachment from the current conversation.",
                    },
                    "regions": {
                        "type": "array",
                        "description": "List of regions to highlight on the image",
                        "items": {
                            "type": "object",
                            "properties": {
                                "x": {
                                    "type": "number",
                                    "description": "X coordinate of the region (left edge for rectangle, center for circle)",
                                },
                                "y": {
                                    "type": "number",
                                    "description": "Y coordinate of the region (top edge for rectangle, center for circle)",
                                },
                                "width": {
                                    "type": "number",
                                    "description": "Width of rectangle or diameter of circle",
                                },
                                "height": {
                                    "type": "number",
                                    "description": "Height of rectangle (ignored for circles)",
                                    "default": None,
                                },
                                "color": {
                                    "type": "string",
                                    "enum": [
                                        "red",
                                        "green",
                                        "blue",
                                        "yellow",
                                        "orange",
                                        "purple",
                                        "cyan",
                                        "magenta",
                                    ],
                                    "description": "Color of the highlight",
                                    "default": "red",
                                },
                                "shape": {
                                    "type": "string",
                                    "enum": ["rectangle", "circle"],
                                    "description": "Shape of the highlight",
                                    "default": "rectangle",
                                },
                                "thickness": {
                                    "type": "number",
                                    "description": "Thickness of the outline in pixels",
                                    "default": 3,
                                },
                            },
                            "required": ["x", "y", "width"],
                        },
                        "minItems": 1,
                    },
                },
                "required": ["image_attachment_id", "regions"],
            },
        },
    },
]


# Color mapping
COLOR_MAP = {
    "red": "#FF0000",
    "green": "#00FF00",
    "blue": "#0000FF",
    "yellow": "#FFFF00",
    "orange": "#FFA500",
    "purple": "#800080",
    "cyan": "#00FFFF",
    "magenta": "#FF00FF",
}


async def highlight_image_tool(
    exec_context: ToolExecutionContext,
    image_attachment_id: ScriptAttachment,
    regions: list[dict[str, Any]],
) -> ToolResult:
    """
    Highlight regions on an image by drawing colored rectangles or circles.

    This tool uses PIL to draw shapes on images, useful for marking objects
    or areas of interest for testing attachment processing workflows.

    Args:
        exec_context: The execution context
        image_attachment_id: ScriptAttachment object containing the image to process
        regions: List of region dictionaries with shape, position, size, and style

    Returns:
        ToolResult with highlighted image attachment and success message
    """
    logger = logging.getLogger(__name__)

    attachment_id = image_attachment_id.get_id()
    logger.info(f"Highlighting {len(regions)} regions on image {attachment_id}")

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

        # Get original image content from the ScriptAttachment
        original_content = image_attachment_id.get_content()

        if not original_content:
            logger.error(
                f"Could not retrieve content for attachment {image_attachment_id}"
            )
            return ToolResult(
                text="Error: Could not retrieve image content", attachment=None
            )

        # Load image with PIL
        try:
            with Image.open(io.BytesIO(original_content)) as original_img:
                # Convert to RGB if necessary for drawing
                if original_img.mode != "RGB":
                    img = original_img.convert("RGB")
                else:
                    img = original_img

                # Create a copy for modification
                highlighted_img = img.copy()
                draw = ImageDraw.Draw(highlighted_img)

                # Draw each region
                regions_drawn = []
                for i, region in enumerate(regions):
                    try:
                        x = region["x"]
                        y = region["y"]
                        width = region["width"]
                        height = region.get("height", width)  # Default to square/circle
                        color = COLOR_MAP.get(region.get("color", "red"), "#FF0000")
                        shape = region.get("shape", "rectangle")
                        thickness = region.get("thickness", 3)

                        if shape == "rectangle":
                            # Draw rectangle outline
                            draw.rectangle(
                                [x, y, x + width, y + height],
                                outline=color,
                                width=thickness,
                            )
                            regions_drawn.append(
                                f"rectangle at ({x},{y}) {width}x{height} in {region.get('color', 'red')}"
                            )

                        elif shape == "circle":
                            # Draw circle outline (ellipse with equal width/height)
                            radius = width / 2
                            draw.ellipse(
                                [x - radius, y - radius, x + radius, y + radius],
                                outline=color,
                                width=thickness,
                            )
                            regions_drawn.append(
                                f"circle at ({x},{y}) radius {radius} in {region.get('color', 'red')}"
                            )

                        else:
                            logger.warning(
                                f"Unknown shape '{shape}' in region {i}, skipping"
                            )

                    except (KeyError, ValueError, TypeError) as e:
                        logger.warning(f"Invalid region {i}: {e}, skipping")
                        continue

                # Save highlighted image to bytes
                output_buffer = io.BytesIO()
                highlighted_img.save(output_buffer, format="PNG")
                highlighted_content = output_buffer.getvalue()

        except Exception as e:
            logger.error(f"Error processing image with PIL: {e}")
            return ToolResult(
                text=f"Error: Failed to process image: {str(e)}", attachment=None
            )

        # Determine new filename
        original_filename = image_attachment_id.get_filename() or "image.png"
        base_name = (
            original_filename.rsplit(".", 1)[0]
            if "." in original_filename
            else original_filename
        )
        new_filename = f"{base_name}_highlighted.png"

        # Create the highlighted attachment
        highlighted_attachment = ToolAttachment(
            content=highlighted_content,
            mime_type="image/png",
            description=f"Highlighted version of {image_attachment_id.get_description() or 'image'} with {len(regions_drawn)} regions marked",
        )

        success_message = (
            f"Successfully highlighted image '{original_filename}' with {len(regions_drawn)} regions: "
            f"{', '.join(regions_drawn)}. Created highlighted version as '{new_filename}'."
        )

        logger.info(f"Created highlighted image: {new_filename}")

        return ToolResult(text=success_message, attachment=highlighted_attachment)

    except Exception as e:
        logger.error(
            f"Error highlighting image {image_attachment_id}: {e}", exc_info=True
        )
        return ToolResult(
            text=f"Error: Failed to highlight image: {str(e)}", attachment=None
        )
