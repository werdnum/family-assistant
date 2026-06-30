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
    ImageReference,
    MockImageBackend,
    OpenAIImageBackend,
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
            "description": "Edit, transform, or combine one or more existing reference images based on text instructions. Use this for object removal/addition, style changes, variations, or merging elements from multiple images.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {
                        "type": "attachment",
                        "description": "Single image to transform. Prefer `images` when using multiple references.",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "attachment"},
                        "description": "One or more reference images to edit, transform, or combine. When combining images, put the primary image first.",
                        "minItems": 1,
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Natural language instruction for how to transform the image",
                    },
                },
                "required": ["instruction"],
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

    # Check configuration for API keys
    app_config: AppConfig | None = None
    if exec_context.processing_service and hasattr(
        exec_context.processing_service, "app_config"
    ):
        app_config = exec_context.processing_service.app_config

    backend_choice = app_config.image_generation_backend if app_config else None
    openai_key = app_config.openai_api_key if app_config else None
    gemini_key = app_config.gemini_api_key if app_config else None

    if backend_choice == "openai":
        if not openai_key:
            raise ValueError(
                "image_generation_backend is 'openai' but OPENAI_API_KEY is not set"
            )
        assert app_config is not None
        return OpenAIImageBackend(
            openai_key,
            model=app_config.openai_image.model,
            generate_config=app_config.openai_image.default_generate,
            edit_config=app_config.openai_image.default_edit,
        )
    elif backend_choice == "gemini":
        if not gemini_key:
            raise ValueError(
                "image_generation_backend is 'gemini' but GEMINI_API_KEY is not set"
            )
        assert app_config is not None
        return GeminiImageBackend(gemini_key, model=app_config.gemini_image.model)
    elif backend_choice == "mock":
        return MockImageBackend()
    # No explicit backend — auto-detect from available API keys
    elif openai_key:
        logger.info("No image_generation_backend configured, auto-selecting OpenAI")
        assert app_config is not None  # openai_key is None when app_config is None
        return OpenAIImageBackend(
            openai_key,
            model=app_config.openai_image.model,
            generate_config=app_config.openai_image.default_generate,
            edit_config=app_config.openai_image.default_edit,
        )
    elif gemini_key:
        logger.info("No image_generation_backend configured, auto-selecting Gemini")
        assert app_config is not None  # gemini_key is None when app_config is None
        return GeminiImageBackend(gemini_key, model=app_config.gemini_image.model)
    else:
        logger.info("No image_generation_backend or API keys configured, using mock")
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
    exec_context: "ToolExecutionContext",
    instruction: str,
    image: "ScriptAttachment | None" = None,
    images: "list[ScriptAttachment] | None" = None,
) -> ToolResult:
    """
    Transform an existing image based on text instruction.

    Works for any type of transformation:
    - Editing: "remove the car", "add clouds"
    - Styling: "make it look like a painting", "convert to anime"
    - Variations: "same scene at night", "make it more colorful"

    Args:
        exec_context: The execution context
        instruction: Natural language instruction for transformation
        image: A single image to transform.
        images: One or more reference images to transform or combine.

    Returns:
        ToolResult with transformed image attachment
    """
    logger.info(f"Transforming image with instruction: '{instruction}'")

    try:
        image_attachments: list[ScriptAttachment] = []
        if image is not None:
            image_attachments.append(image)
        if images:
            image_attachments.extend(images)

        if not image_attachments:
            return ToolResult(text="Error: Provide at least one image to transform")

        image_contents: list[ImageReference] = []
        for image_attachment in image_attachments:
            content = await image_attachment.get_content_async()
            if not content:
                logger.error(
                    "Could not retrieve content for attachment %s",
                    image_attachment.get_id(),
                )
                return ToolResult(text="Could not access the image content")
            image_contents.append(
                ImageReference(
                    content=content,
                    mime_type=image_attachment.get_mime_type() or "image/png",
                )
            )

        # Get the appropriate backend
        backend = _create_image_backend(exec_context)

        # Transform the image
        transformed_bytes = await backend.transform_image(image_contents, instruction)

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
