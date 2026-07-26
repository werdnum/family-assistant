"""Tools for generating videos using Google's Veo and Gemini Omni Flash models."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools.types import (
    ToolAttachment,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.tools.video_backends import (
    GeminiOmniVideoBackend,
    MockVideoBackend,
    VeoVideoBackend,
    VideoGenerationBackend,
    VideoGenerationError,
    VideoGenerationRequest,
    VideoReferenceImage,
)

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig

logger = logging.getLogger(__name__)

VIDEO_GENERATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generates a video from a text prompt (and optional reference images) using Google's video models. The operation is asynchronous and may take some time (usually a few minutes). Returns the generated video as an attachment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The text description for the video. Supports audio cues, camera motion, style, etc.",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "attachment"},
                        "description": "Optional list of up to 3 reference images (style/content guide).",
                        "maxItems": 3,
                    },
                    "first_frame_image": {
                        "type": "attachment",
                        "description": "The initial image to animate (for Image-to-Video or Interpolation).",
                    },
                    "last_frame_image": {
                        "type": "attachment",
                        "description": "The final image for interpolation. Must be used with `first_frame_image`. (Veo only.)",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Text describing what not to include in the video. (Veo only.)",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["16:9", "9:16"],
                        "default": "16:9",
                        "description": "The video's aspect ratio. Default is 16:9.",
                    },
                    "duration_seconds": {
                        "type": "string",
                        "enum": ["4", "6", "8"],
                        "default": "8",
                        "description": "Length of the generated video in seconds. Default is 8.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override. Defaults to Gemini Omni Flash (gemini-omni-flash-preview) for fast conversational video. A `veo-*` id, or use of `negative_prompt` or `last_frame_image` without a model override, selects Veo for cinematic, higher-quality clips instead.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
]


async def _build_reference_image(
    attachment: ScriptAttachment, label: str
) -> VideoReferenceImage | None:
    """Fetch an attachment's bytes into a VideoReferenceImage.

    Args:
        attachment: The ScriptAttachment object to process.
        label: A label for logging (e.g., "reference image", "first frame").

    Returns:
        A VideoReferenceImage if successful, or None if content retrieval fails.
    """
    if not isinstance(attachment, ScriptAttachment):
        logger.warning(f"Invalid object for {label}: {type(attachment)}")
        return None

    try:
        content = await attachment.get_content_async()
        if not content:
            logger.warning(
                f"Could not retrieve content for {label} attachment {attachment.get_id()}"
            )
            return None
        mime_type = attachment.get_mime_type() or "image/png"
        logger.info(
            f"Processed {label} attachment {attachment.get_id()} ({len(content)} bytes)"
        )
        return VideoReferenceImage(content=content, mime_type=mime_type)
    except Exception as e:
        logger.error(f"Error processing {label} attachment {attachment.get_id()}: {e}")
        return None


def _resolve_app_config(exec_context: ToolExecutionContext) -> AppConfig | None:
    """Best-effort lookup of the AppConfig from the processing service."""
    if exec_context.processing_service and hasattr(
        exec_context.processing_service, "app_config"
    ):
        return exec_context.processing_service.app_config  # type: ignore[no-any-return]
    return None


def _is_veo_model(model: str | None) -> bool:
    return model is not None and model.lower().startswith("veo")


def _create_video_backend(
    exec_context: ToolExecutionContext,
    model_override: str | None,
    *,
    requires_veo_features: bool = False,
) -> VideoGenerationBackend:
    """Select a video generation backend from config (or infer from the model)."""
    if hasattr(exec_context, "video_backend"):
        return exec_context.video_backend  # type: ignore[attr-defined,no-any-return]

    app_config = _resolve_app_config(exec_context)
    backend_choice = app_config.video_generation_backend if app_config else None
    api_key = (app_config.gemini_api_key if app_config else None) or os.getenv(
        "GEMINI_API_KEY"
    )

    if backend_choice == "mock":
        return MockVideoBackend()

    if backend_choice == "veo":
        if not api_key:
            raise ValueError(
                "video_generation_backend is 'veo' but GEMINI_API_KEY is not set"
            )
        veo_model = model_override or (
            app_config.veo_video.model if app_config else None
        )
        return VeoVideoBackend(api_key, model=veo_model)

    if backend_choice == "gemini_omni":
        if not api_key:
            raise ValueError(
                "video_generation_backend is 'gemini_omni' but GEMINI_API_KEY is not set"
            )
        omni_model = model_override or (
            app_config.gemini_omni_video.model if app_config else None
        )
        return GeminiOmniVideoBackend(api_key, model=omni_model)

    # No explicit backend — infer from the requested model, defaulting to
    # Gemini Omni Flash.
    if not api_key:
        logger.info(
            "No video_generation_backend or GEMINI_API_KEY configured, using mock"
        )
        return MockVideoBackend()

    if model_override and _is_veo_model(model_override):
        logger.info("Inferring Veo backend from model %s", model_override)
        return VeoVideoBackend(api_key, model=model_override)

    if requires_veo_features and model_override is None:
        logger.info("Auto-selecting Veo backend for Veo-only request features")
        return VeoVideoBackend(
            api_key,
            model=app_config.veo_video.model if app_config else None,
        )

    logger.info("Auto-selecting Gemini Omni Flash video backend")
    return GeminiOmniVideoBackend(
        api_key,
        model=model_override
        or (app_config.gemini_omni_video.model if app_config else None),
    )


async def generate_video_tool(
    exec_context: ToolExecutionContext,
    prompt: str,
    images: list[ScriptAttachment] | None = None,
    first_frame_image: ScriptAttachment | None = None,
    last_frame_image: ScriptAttachment | None = None,
    negative_prompt: str | None = None,
    aspect_ratio: str = "16:9",
    duration_seconds: str = "8",
    model: str | None = None,
) -> ToolResult:
    """
    Generates a video using Google's Veo or Gemini Omni Flash models.

    Args:
        exec_context: The tool execution context.
        prompt: The text prompt for video generation.
        images: Optional list of reference images.
        first_frame_image: Optional first frame image.
        last_frame_image: Optional last frame image (Veo interpolation).
        negative_prompt: Optional negative prompt (Veo only).
        aspect_ratio: Aspect ratio ("16:9" or "9:16").
        duration_seconds: Duration in seconds ("4", "6", or "8").
        model: Optional model identifier override.

    Returns:
        ToolResult containing the video attachment.
    """
    try:
        backend = _create_video_backend(
            exec_context,
            model,
            requires_veo_features=(
                last_frame_image is not None or negative_prompt is not None
            ),
        )
    except ValueError as e:
        # ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
        return ToolResult(text=f"Error: {e}", data={"error": str(e)})

    reference_images: list[VideoReferenceImage] = []
    if images:
        images_list = images if isinstance(images, list) else [images]
        if len(images_list) > 3:
            logger.warning(
                f"Too many reference images ({len(images_list)}), truncating to 3."
            )
            images_list = images_list[:3]
        processed = await asyncio.gather(*[
            _build_reference_image(image, "reference image") for image in images_list
        ])
        reference_images = [image for image in processed if image is not None]

    first_frame = (
        await _build_reference_image(first_frame_image, "first frame")
        if first_frame_image
        else None
    )
    last_frame = (
        await _build_reference_image(last_frame_image, "last frame")
        if last_frame_image
        else None
    )

    request = VideoGenerationRequest(
        prompt=prompt,
        reference_images=reference_images,
        first_frame=first_frame,
        last_frame=last_frame,
        negative_prompt=negative_prompt,
        aspect_ratio=aspect_ratio,
        duration_seconds=int(duration_seconds) if duration_seconds else None,
    )

    try:
        result = await backend.generate_video(request)
    except VideoGenerationError as e:
        logger.error(f"Video generation failed: {e}")
        return ToolResult(text=f"Error generating video: {e}", data={"error": str(e)})
    except Exception as e:
        logger.exception(f"Error in generate_video_tool: {e}")
        return ToolResult(
            text=f"An error occurred during video generation: {e!s}",
            data={"error": str(e)},
        )

    attachment = ToolAttachment(
        content=result.content,
        mime_type=result.mime_type,
        description=f"Generated video: {prompt[:50]}...",
    )
    return ToolResult(
        text=f"Video generated successfully! (Model: {result.model})",
        attachments=[attachment],
        data={"status": "success", "model": result.model, "prompt": prompt},
    )
