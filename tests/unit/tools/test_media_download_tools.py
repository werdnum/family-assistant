"""Tests for media download tools."""

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from yt_dlp.utils import DownloadError

from family_assistant.tools.media_download import (
    MEDIA_DOWNLOAD_TOOLS_DEFINITION,
    download_media_tool,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)


@pytest.fixture
def mock_exec_context() -> MagicMock:
    """Create a mock execution context."""
    context = MagicMock(spec=ToolExecutionContext)
    context.processing_service = None
    return context


@pytest.fixture
def mock_yt_dlp() -> Generator[MagicMock]:
    """Mock yt_dlp for testing without actual downloads."""
    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        mock_ydl = MagicMock()
        mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value = mock_ydl

        yield mock_ydl


def test_tool_definition_structure() -> None:
    """Test that the tool definition has the correct structure."""
    assert len(MEDIA_DOWNLOAD_TOOLS_DEFINITION) == 1
    tool_def = MEDIA_DOWNLOAD_TOOLS_DEFINITION[0]

    assert tool_def["type"] == "function"
    assert tool_def["function"]["name"] == "download_media"
    assert "description" in tool_def["function"]

    params = tool_def["function"]["parameters"]
    assert params["type"] == "object"
    assert "url" in params["properties"]
    assert "audio_only" in params["properties"]
    assert "metadata_only" in params["properties"]
    assert params["required"] == ["url"]


@pytest.mark.asyncio
async def test_download_media_metadata_only(
    mock_exec_context: MagicMock, mock_yt_dlp: MagicMock
) -> None:
    """Test metadata extraction without downloading."""
    mock_yt_dlp.extract_info.return_value = {
        "title": "Test Video",
        "duration": 125,
        "uploader": "Test Uploader",
        "upload_date": "20240101",
        "view_count": 1000,
        "description": "Test description",
        "thumbnail": "https://example.com/thumb.jpg",
        "webpage_url": "https://example.com/video",
        "extractor": "generic",
    }

    result = await download_media_tool(
        mock_exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert "metadata" in result.data

    metadata = result.data["metadata"]
    assert metadata["title"] == "Test Video"
    assert metadata["duration"] == 125
    assert metadata["uploader"] == "Test Uploader"

    # Verify extract_info was called without download
    mock_yt_dlp.extract_info.assert_called_once_with(
        "https://example.com/video", download=False
    )


@pytest.mark.asyncio
async def test_download_media_metadata_format_duration(
    mock_exec_context: MagicMock, mock_yt_dlp: MagicMock
) -> None:
    """Test that duration is formatted correctly in text output."""
    # Test hours
    mock_yt_dlp.extract_info.return_value = {
        "title": "Long Video",
        "duration": 3725,  # 1h 2m 5s
        "uploader": "Test",
        "extractor": "youtube",
    }

    result = await download_media_tool(
        mock_exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert result.text is not None
    assert "1h 2m 5s" in result.text


@pytest.mark.asyncio
async def test_download_media_metadata_no_info(
    mock_exec_context: MagicMock, mock_yt_dlp: MagicMock
) -> None:
    """Test handling when extract_info returns None."""
    mock_yt_dlp.extract_info.return_value = None

    result = await download_media_tool(
        mock_exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert "error" in result.data


@pytest.mark.asyncio
async def test_download_media_video_success(
    mock_exec_context: MagicMock,
    tmp_path: Path,
) -> None:
    """Test successful video download."""
    # Create a test file
    test_file = tmp_path / "Test Video.mp4"
    test_content = b"fake video content"
    test_file.write_bytes(test_content)

    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        mock_ydl = MagicMock()
        mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = {
            "title": "Test Video",
            "duration": 60,
            "uploader": "Test Uploader",
            "upload_date": "20240101",
            "webpage_url": "https://example.com/video",
            "extractor": "youtube",
            "requested_downloads": [{"filepath": str(test_file)}],
        }

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = str(tmp_path)

            result = await download_media_tool(
                mock_exec_context,
                url="https://example.com/video",
            )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.data["title"] == "Test Video"
    assert result.data["mime_type"] == "video/mp4"

    # Verify attachment
    assert result.attachments is not None
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert isinstance(attachment, ToolAttachment)
    assert attachment.content == test_content
    assert attachment.mime_type == "video/mp4"


@pytest.mark.asyncio
async def test_download_media_audio_only(
    mock_exec_context: MagicMock,
    tmp_path: Path,
) -> None:
    """Test audio-only download."""
    # Create a test file
    test_file = tmp_path / "Test Audio.m4a"
    test_content = b"fake audio content"
    test_file.write_bytes(test_content)

    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        mock_ydl = MagicMock()
        mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = {
            "title": "Test Audio",
            "duration": 180,
            "uploader": "Test Uploader",
            "upload_date": "20240101",
            "webpage_url": "https://example.com/video",
            "extractor": "youtube",
            "requested_downloads": [{"filepath": str(test_file)}],
        }

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = str(tmp_path)

            result = await download_media_tool(
                mock_exec_context,
                url="https://example.com/video",
                audio_only=True,
            )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.data["mime_type"] == "audio/mp4"

    # Verify attachment
    assert result.attachments is not None
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.mime_type == "audio/mp4"
    assert "Audio" in attachment.description


@pytest.mark.asyncio
async def test_download_media_file_too_large(
    mock_exec_context: MagicMock,
    tmp_path: Path,
) -> None:
    """Test handling of files exceeding size limit."""
    # Create a test file
    test_file = tmp_path / "Large Video.mp4"
    # Mock file size check to return a huge size
    test_file.write_bytes(b"x" * 100)

    with (
        patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module,
        patch(
            "family_assistant.tools.media_download.get_attachment_limits",
            return_value=(50, 20),  # 50 bytes max
        ),
    ):
        mock_ydl = MagicMock()
        mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = {
            "title": "Large Video",
            "duration": 3600,
            "requested_downloads": [{"filepath": str(test_file)}],
        }

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = str(tmp_path)

            result = await download_media_tool(
                mock_exec_context,
                url="https://example.com/large-video",
            )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data.get("error") == "file_too_large"


@pytest.mark.asyncio
async def test_download_media_download_error(
    mock_exec_context: MagicMock,
) -> None:
    """Test handling of yt-dlp download errors."""
    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value.extract_info.side_effect = DownloadError(
            "Video unavailable"
        )

        result = await download_media_tool(
            mock_exec_context,
            url="https://example.com/unavailable",
        )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data.get("error") == "download_failed"
    assert "unavailable" in result.data.get("message", "").lower()


@pytest.mark.asyncio
async def test_download_media_fallback_file_search(
    mock_exec_context: MagicMock,
    tmp_path: Path,
) -> None:
    """Test fallback file search when filepath is not in requested_downloads."""
    # Create a test file with different name than expected
    test_file = tmp_path / "actual_video.mp4"
    test_content = b"video content"
    test_file.write_bytes(test_content)

    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        mock_ydl = MagicMock()
        mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value = mock_ydl
        # No requested_downloads, force fallback path
        mock_ydl.extract_info.return_value = {
            "title": "Test Video",
            "duration": 60,
            "ext": "mp4",
        }

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = str(tmp_path)

            result = await download_media_tool(
                mock_exec_context,
                url="https://example.com/video",
            )

    # Should find the mp4 file via glob fallback
    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"


@pytest.mark.asyncio
async def test_download_media_various_formats(
    mock_exec_context: MagicMock,
    tmp_path: Path,
) -> None:
    """Test MIME type detection for various formats."""
    format_tests: list[tuple[str, str]] = [
        (".mp4", "video/mp4"),
        (".m4a", "audio/mp4"),
        (".webm", "video/webm"),
        (".mkv", "video/x-matroska"),
        (".mp3", "audio/mpeg"),
        (".opus", "audio/opus"),
    ]

    for ext, expected_mime in format_tests:
        test_file = tmp_path / f"test{ext}"
        test_file.write_bytes(b"content")

        with patch(
            "family_assistant.tools.media_download.yt_dlp"
        ) as mock_yt_dlp_module:
            mock_ydl = MagicMock()
            mock_yt_dlp_module.YoutubeDL.return_value.__enter__.return_value = mock_ydl
            mock_ydl.extract_info.return_value = {
                "title": "Test",
                "duration": 60,
                "requested_downloads": [{"filepath": str(test_file)}],
            }

            with patch("tempfile.TemporaryDirectory") as mock_tempdir:
                mock_tempdir.return_value.__enter__.return_value = str(tmp_path)

                result = await download_media_tool(
                    mock_exec_context,
                    url="https://example.com/video",
                )

        # ast-grep-ignore: no-dict-any - ToolResult.data can be dict, list, str, etc.
        result_data: dict[str, Any] = result.data  # type: ignore[assignment] - data is validated above
        assert result_data["mime_type"] == expected_mime, f"Failed for {ext}"

        # Cleanup for next iteration
        test_file.unlink()
