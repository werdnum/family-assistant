"""Tools for generating videos using Google's Veo model."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, cast

from google import genai
from google.genai import types

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)

logger = logging.getLogger(__name__)

# ast-grep-ignore: no-dict-any - Tool definition schema uses dict structure
VIDEO_GENERATION_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generates a video from a text prompt using Google's Veo model. The operation is asynchronous and may take some time (usually a few minutes). Returns the generated video as an attachment.",
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
                        "description": "Optional list of up to 3 reference images (style/content guide) for Veo 3.1.",
                        "maxItems": 3,
                    },
                    "first_frame_image": {
                        "type": "attachment",
                        "description": "The initial image to animate (for Image-to-Video or Interpolation).",
                    },
                    "last_frame_image": {
                        "type": "attachment",
                        "description": "The final image for interpolation. Must be used with `first_frame_image`.",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Text describing what not to include in the video.",
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
                        "default": "veo-3.1-generate-preview",
                        "description": "The model to use for generation. Defaults to veo-3.1-generate-preview.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
]


async def _process_image_attachment(
    attachment: ScriptAttachment, label: str
) -> types.Image | None:
    """
    Helper to process a single attachment into a Google GenAI Image object.

    Args:
        attachment: The ScriptAttachment object to process.
        label: A label for logging (e.g., "reference image", "first frame").

    Returns:
        A types.Image object if successful, or None if content retrieval fails.
    """
    if not isinstance(attachment, ScriptAttachment):
        logger.warning(f"Invalid object for {label}: {type(attachment)}")
        return None

    try:
        content = await attachment.get_content_async()
        if content:
            mime_type = attachment.get_mime_type()
            logger.info(
                f"Processed {label} attachment {attachment.get_id()} ({len(content)} bytes)"
            )
            return types.Image(image_bytes=content, mime_type=mime_type)
        else:
            logger.warning(
                f"Could not retrieve content for {label} attachment {attachment.get_id()}"
            )
            return None
    except Exception as e:
        logger.error(f"Error processing {label} attachment {attachment.get_id()}: {e}")
        return None


async def generate_video_tool(
    exec_context: ToolExecutionContext,
    prompt: str,
    images: list[ScriptAttachment] | None = None,
    first_frame_image: ScriptAttachment | None = None,
    last_frame_image: ScriptAttachment | None = None,
    negative_prompt: str | None = None,
    aspect_ratio: str = "16:9",
    duration_seconds: str = "8",
    model: str = "veo-3.1-generate-preview",
) -> ToolResult:
    """
    Generates a video using Google's Veo model.

    Args:
        exec_context: The tool execution context.
        prompt: The text prompt for video generation.
        images: Optional list of reference images.
        first_frame_image: Optional first frame image.
        last_frame_image: Optional last frame image.
        negative_prompt: Optional negative prompt.
        aspect_ratio: Aspect ratio ("16:9" or "9:16").
        duration_seconds: Duration in seconds ("4", "6", or "8").
        model: Model identifier.

    Returns:
        ToolResult containing the video attachment.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
        return ToolResult(
            text="Error: GEMINI_API_KEY is not set in the environment.",
            data={"error": "GEMINI_API_KEY missing"},
        )

    # Use client.aio as async context manager
    async with genai.Client(api_key=api_key).aio as client:
        try:
            # Configure video generation
            config_params = {}
            if negative_prompt:
                config_params["negative_prompt"] = negative_prompt
            if aspect_ratio:
                config_params["aspect_ratio"] = aspect_ratio
            if duration_seconds:
                config_params["duration_seconds"] = int(duration_seconds)

            # Handle reference images (Style/Content guide)
            if images:
                # Handle single attachment passed by mistake (if middleware flattens it)
                images_list = images if isinstance(images, list) else [images]

                # Enforce maxItems: 3
                if len(images_list) > 3:
                    logger.warning(
                        f"Too many reference images ({len(images_list)}), truncating to 3."
                    )
                    images_list = images_list[:3]

                # Process images concurrently
                image_results = await asyncio.gather(*[
                    _process_image_attachment(img, "reference image")
                    for img in images_list
                ])
                reference_images = [
                    types.VideoGenerationReferenceImage(image=img)
                    for img in image_results
                    if img is not None
                ]

                if reference_images:
                    config_params["reference_images"] = reference_images
                    # Docs say duration must be 8 when using reference images
                    if config_params.get("duration_seconds") != 8:
                        logger.info("Forcing duration to 8s for reference images mode")
                        config_params["duration_seconds"] = 8

            # Handle Last Frame (for interpolation)
            if last_frame_image:
                last_frame_obj = await _process_image_attachment(
                    last_frame_image, "last frame"
                )
                if last_frame_obj:
                    config_params["last_frame"] = last_frame_obj
                    # Docs say duration must be 8 when using interpolation
                    if config_params.get("duration_seconds") != 8:
                        logger.info("Forcing duration to 8s for interpolation mode")
                        config_params["duration_seconds"] = 8

            config = types.GenerateVideosConfig(**config_params)

            # Handle First Frame (Image-to-Video or Interpolation)
            first_frame_obj = None
            if first_frame_image:
                first_frame_obj = await _process_image_attachment(
                    first_frame_image, "first frame"
                )

            # Construct source
            source = types.GenerateVideosSource(prompt=prompt, image=first_frame_obj)

            logger.info(
                f"Starting video generation with model {model} and prompt: {prompt[:50]}..."
            )

            # Start the operation
            # Note: client is the AsyncClient here (from .aio)
            operation = await client.models.generate_videos(
                model=model,
                source=source,
                config=config,
            )

            logger.info(f"Video generation operation started: {operation.name}")

            # Poll for completion
            start_time = time.time()
            timeout_seconds = 600  # 10 minutes timeout

            while not operation.done:
                if time.time() - start_time > timeout_seconds:
                    # ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
                    return ToolResult(
                        text=f"Error: Video generation timed out after {timeout_seconds} seconds.",
                        data={"error": "Timeout", "timeout_seconds": timeout_seconds},
                    )

                logger.debug("Waiting for video generation to complete...")
                await asyncio.sleep(10)  # Wait 10 seconds between checks
                operation = await client.operations.get(operation)

            if operation.error:
                # Handle API error (e.g., safety filters)
                # Pyright might see operation.error as dict or object
                op_error = operation.error
                if isinstance(op_error, dict):
                    error_msg = op_error.get("message", "Unknown error")
                    error_code = op_error.get("code")
                else:
                    error_msg = getattr(op_error, "message", "Unknown error")
                    error_code = getattr(op_error, "code", None)

                logger.error(
                    f"Video generation failed: {error_msg} (Code: {error_code})"
                )
                return ToolResult(
                    text=f"Error generating video: {error_msg}",
                    data={"error": error_msg, "code": error_code},
                )

            # Download the video
            if (
                not hasattr(operation, "response")
                or not operation.response
                or not operation.response.generated_videos
            ):
                # ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
                return ToolResult(
                    text="Error: No video generated in response.",
                    data={"error": "No video generated"},
                )

            video_asset = operation.response.generated_videos[0]

            if not video_asset.video:
                # ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
                return ToolResult(
                    text="Error: Generated video asset is missing video file reference.",
                    data={"error": "Missing video file"},
                )

            logger.info("Downloading video content...")

            # Download using async client - returns bytes
            # cast to Any because Pyright might not recognize Video as valid input for download yet
            video_bytes = await client.files.download(
                file=cast("Any", video_asset.video)
            )

            # Create attachment
            attachment = ToolAttachment(
                content=video_bytes,
                mime_type="video/mp4",
                description=f"Generated video: {prompt[:50]}...",
            )

            return ToolResult(
                text=f"Video generated successfully! (Model: {model})",
                attachments=[attachment],
                data={
                    "status": "success",
                    "model": model,
                    "prompt": prompt,
                },
            )

        except Exception as e:
            logger.error(f"Error in generate_video_tool: {e}", exc_info=True)
            return ToolResult(
                text=f"An error occurred during video generation: {str(e)}",
                data={"error": str(e)},
            )
