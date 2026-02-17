"""Tests for video generation tools."""

import os
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.tools.video_generation import (
    generate_video_tool,
)


@pytest.fixture
def mock_exec_context() -> MagicMock:
    """Create a mock execution context."""
    return MagicMock(spec=ToolExecutionContext)


@pytest.fixture
def mock_genai_client() -> Generator[MagicMock]:
    """Mock the genai.Client."""
    with patch(
        "family_assistant.tools.video_generation.genai.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Mock async operation
        mock_operation = MagicMock()
        mock_operation.name = "operation/123"
        mock_operation.done = True
        mock_operation.error = None

        # Mock response
        mock_video_asset = MagicMock()
        mock_video_asset.video = "file-ref"

        mock_response = MagicMock()
        mock_response.generated_videos = [mock_video_asset]
        mock_operation.response = mock_response

        # Create an async client that will be yielded by the context manager
        mock_async_client = MagicMock()
        mock_async_client.models.generate_videos = AsyncMock(
            return_value=mock_operation
        )
        mock_async_client.operations.get = AsyncMock(return_value=mock_operation)
        mock_async_client.files.download = AsyncMock(return_value=b"video-content")

        # Setup .aio as an async context manager that yields mock_async_client
        mock_aio = MagicMock()
        mock_aio.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_aio.__aexit__ = AsyncMock(return_value=None)
        mock_client.aio = mock_aio

        yield mock_async_client


@pytest.mark.asyncio
async def test_generate_video_tool_success(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test successful video generation."""
    # Lazy import to avoid xdist worker crashes from concurrent genai initialization
    from google.genai import types  # noqa: PLC0415 - intentional lazy import

    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await generate_video_tool(
            mock_exec_context,
            prompt="A video of a cat",
            aspect_ratio="16:9",
            duration_seconds="8",
        )

        # Verify result
        assert isinstance(result, ToolResult)
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert result.data["status"] == "success"
        assert "A video of a cat" in result.data["prompt"]

        # Verify attachment
        assert result.attachments is not None
        assert len(result.attachments) == 1
        attachment = result.attachments[0]
        assert isinstance(attachment, ToolAttachment)
        assert attachment.content == b"video-content"
        assert attachment.mime_type == "video/mp4"

        # Verify client calls
        mock_genai_client.models.generate_videos.assert_called_once()
        _, kwargs = mock_genai_client.models.generate_videos.call_args

        # Verify source
        assert "source" in kwargs
        assert isinstance(kwargs["source"], types.GenerateVideosSource)
        assert kwargs["source"].prompt == "A video of a cat"

        # Verify config parameters
        config = kwargs["config"]
        # Check duration_seconds access via snake_case (sdk default) or raw dict check if model dump
        # We assume the object is passed correctly
        assert config.duration_seconds == 8

        mock_genai_client.files.download.assert_called_once_with(file="file-ref")


@pytest.mark.asyncio
async def test_generate_video_multimodal(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test video generation with reference images and first/last frame."""
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("asyncio.sleep", AsyncMock()),
    ):
        # Create mock ScriptAttachment objects
        ref_img1 = MagicMock(spec=ScriptAttachment)
        ref_img1.get_content_async = AsyncMock(return_value=b"img1")
        ref_img1.get_mime_type.return_value = "image/png"
        ref_img1.get_id.return_value = "ref1"

        first_frame = MagicMock(spec=ScriptAttachment)
        first_frame.get_content_async = AsyncMock(return_value=b"first")
        first_frame.get_mime_type.return_value = "image/jpeg"
        first_frame.get_id.return_value = "first1"

        last_frame = MagicMock(spec=ScriptAttachment)
        last_frame.get_content_async = AsyncMock(return_value=b"last")
        last_frame.get_mime_type.return_value = "image/jpeg"
        last_frame.get_id.return_value = "last1"

        result = await generate_video_tool(
            mock_exec_context,
            prompt="A multimodal video",
            images=[ref_img1],
            first_frame_image=first_frame,
            last_frame_image=last_frame,
            duration_seconds="4",  # Should be forced to 8
        )

        assert isinstance(result, ToolResult)
        # ast-grep-ignore: no-dict-any - ToolResult.data is flexible
        result_data: dict[str, Any] = result.data  # type: ignore
        assert result_data["status"] == "success"

        # Verify client calls
        mock_genai_client.models.generate_videos.assert_called_once()
        _, kwargs = mock_genai_client.models.generate_videos.call_args

        source = kwargs["source"]
        config = kwargs["config"]

        # Check First Frame in Source
        assert source.image is not None
        assert source.image.image_bytes == b"first"

        # Check Last Frame in Config
        assert config.last_frame is not None
        assert config.last_frame.image_bytes == b"last"

        # Check Reference Images in Config
        assert config.reference_images is not None
        assert len(config.reference_images) == 1
        assert config.reference_images[0].image.image_bytes == b"img1"

        # Check Duration Forced to 8s
        assert config.duration_seconds == 8


@pytest.mark.asyncio
async def test_generate_video_invalid_attachments(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test video generation with invalid attachments (should skip them)."""
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("asyncio.sleep", AsyncMock()),
    ):
        # Invalid object passed as attachment
        invalid_att = MagicMock()  # Not ScriptAttachment spec

        # Valid attachment but empty content
        empty_att = MagicMock(spec=ScriptAttachment)
        empty_att.get_content_async = AsyncMock(return_value=None)
        empty_att.get_id.return_value = "empty1"

        result = await generate_video_tool(
            mock_exec_context,
            prompt="Invalid inputs",
            images=[invalid_att, empty_att],
            first_frame_image=empty_att,
        )

        assert isinstance(result, ToolResult)
        # ast-grep-ignore: no-dict-any - ToolResult.data is flexible
        result_data: dict[str, Any] = result.data  # type: ignore
        assert result_data["status"] == "success"

        _, kwargs = mock_genai_client.models.generate_videos.call_args
        source = kwargs["source"]
        config = kwargs["config"]

        # Should be None/Empty because inputs were invalid
        assert source.image is None
        assert not hasattr(config, "reference_images") or not config.reference_images


@pytest.mark.asyncio
async def test_generate_video_tool_missing_api_key(
    mock_exec_context: MagicMock,
) -> None:
    """Test missing API key."""
    env_without_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    with patch.dict("os.environ", env_without_key, clear=True):
        result = await generate_video_tool(mock_exec_context, prompt="A video of a cat")

        assert isinstance(result, ToolResult)
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert "error" in result.data
        assert "GEMINI_API_KEY missing" in result.data["error"]


@pytest.mark.asyncio
async def test_generate_video_tool_api_error(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test API error handling."""
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("asyncio.sleep", AsyncMock()),
    ):
        # Setup operation with error
        mock_operation = MagicMock()
        mock_operation.done = True
        mock_operation.error = MagicMock()
        mock_operation.error.message = "Safety violation"
        mock_operation.error.code = 400

        mock_genai_client.models.generate_videos.return_value = mock_operation
        # Mock immediate return for polling loop if it checks 'done' immediately
        mock_genai_client.operations.get.return_value = mock_operation

        result = await generate_video_tool(mock_exec_context, prompt="Unsafe prompt")

        assert isinstance(result, ToolResult)
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert "error" in result.data
        assert "Safety violation" in result.data["error"]


@pytest.mark.asyncio
async def test_generate_video_tool_polling(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test polling mechanism."""
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("asyncio.sleep", AsyncMock()) as mock_sleep,
    ):
        # Setup operation states: not done, then done
        op_pending = MagicMock()
        op_pending.done = False
        op_pending.name = "op/pending"

        op_done = MagicMock()
        op_done.done = True
        op_done.name = "op/done"
        op_done.error = None
        op_done.response.generated_videos = [MagicMock()]

        # Initial call returns pending operation
        mock_genai_client.models.generate_videos.return_value = op_pending
        # Polling calls: return pending first time, then done second time
        mock_genai_client.operations.get.side_effect = [op_pending, op_done]
        mock_genai_client.files.download.return_value = b"video-content"

        await generate_video_tool(mock_exec_context, prompt="A video of a dog")

        # Verify polling
        # Called initial + 2 polls
        assert mock_genai_client.operations.get.call_count == 2
        # Sleep called twice
        assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_generate_video_tool_timeout(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test timeout during polling."""
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("asyncio.sleep", AsyncMock()),
        patch("time.time") as mock_time,
    ):
        # Setup operation states: always not done
        op_pending = MagicMock()
        op_pending.done = False
        op_pending.name = "op/pending"

        mock_genai_client.models.generate_videos.return_value = op_pending
        mock_genai_client.operations.get.return_value = op_pending

        # Mock time to advance past timeout
        # Initial call + loop check
        # We need start_time, then next check > timeout
        mock_time.side_effect = [1000.0, 1700.0]  # 700s > 600s

        result = await generate_video_tool(
            mock_exec_context, prompt="A video of a slow cat"
        )

        assert isinstance(result, ToolResult)
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert "error" in result.data
        assert "Timeout" in result.data["error"]
