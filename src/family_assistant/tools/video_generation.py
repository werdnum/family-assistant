"""Tools for generating videos using Google's Veo model."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, cast

from google import genai
from google.genai import types

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


async def generate_video_tool(
    exec_context: ToolExecutionContext,
    prompt: str,
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

    try:
        # Initialize client
        client = genai.Client(api_key=api_key)

        # Configure video generation
        config_params = {}
        if negative_prompt:
            config_params["negative_prompt"] = negative_prompt
        if aspect_ratio:
            config_params["aspect_ratio"] = aspect_ratio
        if duration_seconds:
            config_params["duration_seconds"] = duration_seconds

        config = types.GenerateVideosConfig(**config_params)

        logger.info(
            f"Starting video generation with model {model} and prompt: {prompt[:50]}..."
        )

        # Start the operation
        operation = await client.aio.models.generate_videos(
            model=model,
            prompt=prompt,
            config=config,
        )

        logger.info(f"Video generation operation started: {operation.name}")

        # Poll for completion
        while not operation.done:
            logger.debug("Waiting for video generation to complete...")
            await asyncio.sleep(10)  # Wait 10 seconds between checks
            operation = await client.aio.operations.get(operation)

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

            logger.error(f"Video generation failed: {error_msg} (Code: {error_code})")
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
        video_bytes = await client.aio.files.download(
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
