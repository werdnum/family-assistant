"""
Image generation and transformation tools.

This module provides tools for:
1. Generating images from text descriptions
2. Transforming existing images based on text instructions

Uses dependency injection with ImageGenerationBackend protocol for different
implementations (mock, Gemini API, fallback).
"""

import io
import logging
from typing import TYPE_CHECKING

from PIL import Image

from family_assistant.tools.image_backends import (
    GeminiImageBackend,
    ImageGenerationBackend,
    MockImageBackend,
)
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.scripting.apis.attachments import ScriptAttachment
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Tool Definitions
IMAGE_GENERATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate and display a new image from text description using AI",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate",
                    },
                    "style": {
                        "type": "string",
                        "enum": ["auto", "photorealistic", "artistic"],
                        "default": "auto",
                        "description": "Style of the generated image",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transform_image",
            "description": "Transform and display an existing image based on text instruction. Works for editing (remove objects), styling (make it look like a painting), or variations (same scene at night).",
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {
                        "type": "attachment",
                        "description": "The image to transform",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Natural language instruction for how to transform the image",
                    },
                },
                "required": ["image", "instruction"],
            },
        },
    },
]


def _create_image_backend(
    exec_context: "ToolExecutionContext",
) -> ImageGenerationBackend:
    """Create appropriate image backend based on configuration."""
    # For testing, we can check if there's an injected backend
    if hasattr(exec_context, "image_backend"):
        return exec_context.image_backend  # type: ignore[attr-defined]

    # Check configuration for API key
    api_key = None
    if exec_context.processing_service and hasattr(
        exec_context.processing_service, "app_config"
    ):
        app_config: AppConfig | None = exec_context.processing_service.app_config
        if app_config:
            api_key = app_config.gemini_api_key

    # Create appropriate backend
    if api_key:
        # Use Gemini backend directly - let errors surface instead of masking them
        return GeminiImageBackend(api_key)
    else:
        # Use mock backend when no API key is available
        logger.info("No Google API key found, using mock image backend")
        return MockImageBackend()


async def generate_image_tool(
    exec_context: "ToolExecutionContext", prompt: str, style: str = "auto"
) -> ToolResult:
    """
    Generate a new image from text description.

    Args:
        exec_context: The execution context
        prompt: Detailed description of the image to generate
        style: Style of the generated image (auto, photorealistic, artistic)

    Returns:
        ToolResult with generated image attachment
    """
    logger.info(f"Generating image with prompt: '{prompt}', style: {style}")

    try:
        # Get the appropriate backend
        backend = _create_image_backend(exec_context)

        # Generate the image
        image_bytes = await backend.generate_image(prompt, style)

        # Log what we received from backend
        logger.info(f"Backend returned {len(image_bytes)} bytes")

        # Validate with PIL
        try:
            img = Image.open(io.BytesIO(image_bytes))
            logger.info(
                f"Tool validated image: format={img.format}, size={img.size}, mode={img.mode}"
            )
        except Exception as e:
            logger.error(f"Tool received invalid image data from backend: {e}")

        # Create attachment
        attachment = ToolAttachment(
            content=image_bytes,
            mime_type="image/png",
            description=f"Generated image: {prompt[:50]}{'...' if len(prompt) > 50 else ''}",
        )

        return ToolResult(
            text=f"Generated image for: {prompt}", attachments=[attachment]
        )

    except Exception as e:
        logger.error(f"Error generating image: {e}", exc_info=True)
        return ToolResult(text=f"Error generating image: {str(e)}")


async def transform_image_tool(
    exec_context: "ToolExecutionContext", image: "ScriptAttachment", instruction: str
) -> ToolResult:
    """
    Transform an existing image based on text instruction.

    Works for any type of transformation:
    - Editing: "remove the car", "add clouds"
    - Styling: "make it look like a painting", "convert to anime"
    - Variations: "same scene at night", "make it more colorful"

    Args:
        exec_context: The execution context
        image: The image to transform
        instruction: Natural language instruction for transformation

    Returns:
        ToolResult with transformed image attachment
    """
    logger.info(f"Transforming image with instruction: '{instruction}'")

    try:
        # Get original image content
        original_content = await image.get_content_async()
        if not original_content:
            logger.error(f"Could not retrieve content for attachment {image.get_id()}")
            return ToolResult(text="Could not access the image content")

        # Get the appropriate backend
        backend = _create_image_backend(exec_context)

        # Transform the image
        transformed_bytes = await backend.transform_image(original_content, instruction)

        # Create attachment
        attachment = ToolAttachment(
            content=transformed_bytes,
            mime_type="image/png",
            description=f"Transformed: {instruction[:50]}{'...' if len(instruction) > 50 else ''}",
        )

        return ToolResult(
            text=f"Transformed image: {instruction}", attachments=[attachment]
        )

    except Exception as e:
        logger.error(f"Error transforming image: {e}", exc_info=True)
        return ToolResult(text=f"Error transforming image: {str(e)}")
