"""Tools for generating videos using Google's Veo model."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types

from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Constants
GENERATED_VIDEOS_DIR = Path("src/family_assistant/static/generated")

# Ensure the directory exists
GENERATED_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


VIDEO_GENERATION_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generates a video from a text prompt using Google's Veo model. The operation is asynchronous and may take some time (usually a few minutes). Returns a URL/path to the generated video.",
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
        ToolResult containing the path to the generated video.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
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
            error_msg = operation.error.message or "Unknown error"
            error_code = operation.error.code
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
            return ToolResult(
                text="Error: No video generated in response.",
                data={"error": "No video generated"},
            )

        video_asset = operation.response.generated_videos[0]

        # Generate filename
        filename = f"video_{uuid.uuid4().hex}.mp4"
        file_path = GENERATED_VIDEOS_DIR / filename

        logger.info(f"Downloading video to {file_path}...")

        # Download using async client
        await client.aio.files.download(file=video_asset.video)

        # Save to disk (blocking operation run in thread)
        # The save method writes the downloaded content from memory to disk
        await asyncio.to_thread(video_asset.video.save, file_path)

        relative_path = f"/static/generated/{filename}"

        return ToolResult(
            text=f"Video generated successfully! You can view it here: {relative_path}",
            data={
                "status": "success",
                "file_path": str(file_path),
                "url": relative_path,
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
