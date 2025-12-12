"""Tests for video generation tools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from family_assistant.tools.types import ToolExecutionContext, ToolResult, ToolAttachment
from family_assistant.tools.video_generation import (
    generate_video_tool,
)


@pytest.fixture
def mock_exec_context() -> MagicMock:
    """Create a mock execution context."""
    return MagicMock(spec=ToolExecutionContext)


@pytest.fixture
def mock_genai_client() -> MagicMock:
    """Mock the genai.Client."""
    with patch("family_assistant.tools.video_generation.genai.Client") as mock_client_cls:
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

        # Setup async methods
        mock_client.aio.models.generate_videos = AsyncMock(return_value=mock_operation)
        mock_client.aio.operations.get = AsyncMock(return_value=mock_operation)
        mock_client.aio.files.download = AsyncMock(return_value=b"video-content")

        yield mock_client


@pytest.mark.asyncio
async def test_generate_video_tool_success(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test successful video generation."""
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
         patch("asyncio.sleep", AsyncMock()):

        result = await generate_video_tool(
            mock_exec_context,
            prompt="A video of a cat",
            aspect_ratio="16:9",
            duration_seconds="8"
        )

        # Verify result
        assert isinstance(result, ToolResult)
        assert result.data is not None
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
        mock_genai_client.aio.models.generate_videos.assert_called_once()
        mock_genai_client.aio.files.download.assert_called_once_with(file="file-ref")


@pytest.mark.asyncio
async def test_generate_video_tool_missing_api_key(
    mock_exec_context: MagicMock
) -> None:
    """Test missing API key."""
    with patch.dict("os.environ", {}, clear=True):
        result = await generate_video_tool(
            mock_exec_context,
            prompt="A video of a cat"
        )

        assert isinstance(result, ToolResult)
        assert result.data is not None
        assert "error" in result.data
        assert "GEMINI_API_KEY missing" in result.data["error"]


@pytest.mark.asyncio
async def test_generate_video_tool_api_error(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test API error handling."""
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
         patch("asyncio.sleep", AsyncMock()):

        # Setup operation with error
        mock_operation = MagicMock()
        mock_operation.done = True
        mock_operation.error = MagicMock()
        mock_operation.error.message = "Safety violation"
        mock_operation.error.code = 400

        mock_genai_client.aio.models.generate_videos.return_value = mock_operation
        # Mock immediate return for polling loop if it checks 'done' immediately
        mock_genai_client.aio.operations.get.return_value = mock_operation

        result = await generate_video_tool(
            mock_exec_context,
            prompt="Unsafe prompt"
        )

        assert isinstance(result, ToolResult)
        assert result.data is not None
        assert "error" in result.data
        assert "Safety violation" in result.data["error"]


@pytest.mark.asyncio
async def test_generate_video_tool_polling(
    mock_exec_context: MagicMock, mock_genai_client: MagicMock
) -> None:
    """Test polling mechanism."""
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
         patch("asyncio.sleep", AsyncMock()) as mock_sleep:

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
        mock_genai_client.aio.models.generate_videos.return_value = op_pending
        # Polling calls: return pending first time, then done second time
        mock_genai_client.aio.operations.get.side_effect = [op_pending, op_done]
        mock_genai_client.aio.files.download.return_value = b"video-content"

        await generate_video_tool(
            mock_exec_context,
            prompt="A video of a dog"
        )

        # Verify polling
        # Called initial + 2 polls
        assert mock_genai_client.aio.operations.get.call_count == 2
        # Sleep called twice
        assert mock_sleep.call_count == 2
