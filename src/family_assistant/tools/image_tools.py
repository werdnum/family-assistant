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
            "description": """Draw colored rectangles or circles to highlight regions on an image. \
Useful for marking objects, areas of interest, or annotations. \
Creates and displays a new image with the highlighted regions.

Bounding box coordinates are in normalized [0, 1000] format (Gemini object detection format). \
Format is [y_min, x_min, y_max, x_max] where coordinates are normalized to 0-1000.

Example region format:
{
  "box": [200, 100, 400, 300],
  "label": "chicken",
  "color": "red"
}

Note: thickness is automatically scaled to 1% of image size if not specified.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the image attachment to highlight regions on. Must be an existing image attachment from the current conversation.",
                    },
                    "regions": {
                        "type": "array",
                        "description": "List of regions to highlight on the image. Each region is a bounding box with optional styling.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "box": {
                                    "type": "array",
                                    "description": "Bounding box in Gemini format: [y_min, x_min, y_max, x_max] normalized to [0, 1000]",
                                    "items": {"type": "number"},
                                    "minItems": 4,
                                    "maxItems": 4,
                                },
                                "label": {
                                    "type": "string",
                                    "description": "Optional description of what is being highlighted (e.g., 'chicken', 'car', 'person')",
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
                                    "description": "Shape of the highlight (note: circle uses bounding box center and treats width as diameter)",
                                    "default": "rectangle",
                                },
                                "thickness": {
                                    "type": "number",
                                    "description": "Thickness of the outline in pixels",
                                    "default": 3,
                                },
                            },
                            "required": ["box"],
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
        original_content = await image_attachment_id.get_content_async()

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
                # Save format before any conversion (e.g., "JPEG", "PNG")
                original_format = original_img.format or "PNG"

                # Convert to RGB if necessary for drawing
                if original_img.mode != "RGB":
                    img = original_img.convert("RGB")
                else:
                    img = original_img

                # Create a copy for modification
                highlighted_img = img.copy()
                draw = ImageDraw.Draw(highlighted_img)

                # Get image dimensions for coordinate scaling
                img_width, img_height = highlighted_img.size

                # Calculate default thickness as ~1% of smaller dimension, minimum 2px
                default_thickness = max(2, int(min(img_width, img_height) * 0.01))

                # Validate all regions first - fail fast if any are invalid
                for i, region in enumerate(regions):
                    try:
                        # Validate required fields
                        box = region["box"]
                        if not isinstance(box, list) or len(box) != 4:
                            return ToolResult(
                                text=f"Error: Invalid region {i}: box must be a list of 4 numbers [y_min, x_min, y_max, x_max]",
                                attachment=None,
                            )

                        # Validate shape if specified
                        shape = region.get("shape", "rectangle")
                        if shape not in {"rectangle", "circle"}:
                            return ToolResult(
                                text=f"Error: Invalid shape '{shape}' in region {i}. Must be 'rectangle' or 'circle'.",
                                attachment=None,
                            )
                    except KeyError as e:
                        return ToolResult(
                            text=f"Error: Invalid region {i}: missing required field {e}",
                            attachment=None,
                        )
                    except (ValueError, TypeError) as e:
                        return ToolResult(
                            text=f"Error: Invalid region {i}: {e}",
                            attachment=None,
                        )

                # All regions validated - proceed with drawing
                regions_drawn = []
                for region in regions:
                    # Extract bounding box (Gemini format: [y_min, x_min, y_max, x_max] normalized to [0, 1000])
                    box = region["box"]
                    y_min_norm, x_min_norm, y_max_norm, x_max_norm = box

                    # Scale normalized [0, 1000] coordinates to actual pixel coordinates
                    x_min = (x_min_norm / 1000.0) * img_width
                    y_min = (y_min_norm / 1000.0) * img_height
                    x_max = (x_max_norm / 1000.0) * img_width
                    y_max = (y_max_norm / 1000.0) * img_height

                    # Convert to x, y, width, height for logging
                    x = x_min
                    y = y_min
                    width = x_max - x_min
                    height = y_max - y_min

                    # Get optional attributes
                    label = region.get("label", "")
                    color = COLOR_MAP.get(region.get("color", "red"), "#FF0000")
                    shape = region.get("shape", "rectangle")
                    thickness = region.get("thickness", default_thickness)

                    if shape == "rectangle":
                        # Draw rectangle outline using bounding box coordinates
                        draw.rectangle(
                            [x_min, y_min, x_max, y_max],
                            outline=color,
                            width=thickness,
                        )
                        label_str = f" ({label})" if label else ""
                        regions_drawn.append(
                            f"rectangle at ({x},{y}) {width}x{height}{label_str} in {region.get('color', 'red')}"
                        )
                    elif shape == "circle":
                        # Draw circle outline using bounding box center
                        center_x = (x_min + x_max) / 2
                        center_y = (y_min + y_max) / 2
                        radius = width / 2  # Use width as diameter
                        draw.ellipse(
                            [
                                center_x - radius,
                                center_y - radius,
                                center_x + radius,
                                center_y + radius,
                            ],
                            outline=color,
                            width=thickness,
                        )
                        label_str = f" ({label})" if label else ""
                        regions_drawn.append(
                            f"circle at ({center_x},{center_y}) radius {radius}{label_str} in {region.get('color', 'red')}"
                        )

                # Save highlighted image to bytes preserving original format
                output_buffer = io.BytesIO()
                if original_format == "JPEG":
                    # Use JPEG with good quality and optimization for photos
                    highlighted_img.save(
                        output_buffer, format="JPEG", quality=85, optimize=True
                    )
                else:
                    # Preserve other formats (PNG, etc.)
                    highlighted_img.save(output_buffer, format=original_format)
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
