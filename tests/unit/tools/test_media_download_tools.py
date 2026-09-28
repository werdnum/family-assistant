"""Tests for media download tools."""

from collections.abc import Generator
from inspect import signature
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from yt_dlp.utils import DownloadError

from family_assistant.tools.media_download import (
    MEDIA_DOWNLOAD_TOOLS_DEFINITION,
    _sanitize_title,  # noqa: PLC2701 - unit tests need direct access to internal helpers
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


def _fake_download(
    mock_yt_dlp_module: MagicMock,
    filename: str,
    content: bytes,
    info: dict[str, object],
    *,
    include_filepath: bool = True,
) -> None:
    """Make the external downloader produce a file in its requested destination."""

    def youtube_dl(options: dict[str, object]) -> MagicMock:
        output_dir = Path(cast("str", options["outtmpl"])).parent
        mock_ydl = MagicMock()

        def extract_info(_url: str, *, download: bool) -> dict[str, object]:
            assert download
            file_path = output_dir / filename
            file_path.write_bytes(content)
            if include_filepath:
                return {**info, "requested_downloads": [{"filepath": str(file_path)}]}
            return info

        mock_ydl.extract_info.side_effect = extract_info
        mock_ydl.__enter__.return_value = mock_ydl
        return mock_ydl

    mock_yt_dlp_module.YoutubeDL.side_effect = youtube_dl


def test_tool_definition_structure() -> None:
    """The advertised arguments match the callable tool interface."""
    assert len(MEDIA_DOWNLOAD_TOOLS_DEFINITION) == 1
    tool_def = MEDIA_DOWNLOAD_TOOLS_DEFINITION[0]

    assert tool_def["type"] == "function"
    assert tool_def["function"]["name"] == "download_media"
    assert "description" in tool_def["function"]

    # Cast to dict for test assertions on optional TypedDict keys
    params = cast("dict[str, Any]", tool_def["function"]["parameters"])
    assert params["type"] == "object"
    assert "url" in params["properties"]
    assert "audio_only" in params["properties"]
    assert "metadata_only" in params["properties"]
    assert params["required"] == ["url"]
    callable_parameters = signature(download_media_tool).parameters
    assert set(params["properties"]) == set(callable_parameters) - {"exec_context"}
    assert set(params["required"]) == {
        name
        for name, parameter in callable_parameters.items()
        if name != "exec_context" and parameter.default is parameter.empty
    }


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
    assert result.data["error_type"] == "metadata_extraction_failed"
    assert result.text is not None
    assert "Could not extract metadata" in result.text


@pytest.mark.asyncio
async def test_download_media_metadata_extraction_error(
    mock_exec_context: MagicMock, mock_yt_dlp: MagicMock
) -> None:
    """An extractor ValueError is reported as a metadata failure."""
    mock_yt_dlp.extract_info.side_effect = ValueError(
        "Could not extract metadata from URL"
    )

    result = await download_media_tool(
        mock_exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "metadata_extraction_failed"
    assert "Could not extract metadata from URL" in result.get_text()
    mock_yt_dlp.extract_info.assert_called_once_with(
        "https://example.com/video", download=False
    )


@pytest.mark.asyncio
async def test_download_media_video_success(
    mock_exec_context: MagicMock,
) -> None:
    """Test successful video download."""
    test_content = b"fake video content"

    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        _fake_download(
            mock_yt_dlp_module,
            "Test Video.mp4",
            test_content,
            {
                "title": "Test Video",
                "duration": 60,
                "uploader": "Test Uploader",
                "upload_date": "20240101",
                "webpage_url": "https://example.com/video",
                "extractor": "youtube",
            },
        )

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
) -> None:
    """Test audio-only download."""
    test_content = b"fake audio content"

    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        _fake_download(
            mock_yt_dlp_module,
            "Test Audio.m4a",
            test_content,
            {
                "title": "Test Audio",
                "duration": 180,
                "uploader": "Test Uploader",
                "upload_date": "20240101",
                "webpage_url": "https://example.com/video",
                "extractor": "youtube",
            },
        )

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
) -> None:
    """Test handling of files exceeding size limit."""
    with (
        patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module,
        patch(
            "family_assistant.tools.media_download.get_attachment_limits",
            return_value=(50, 20),  # 50 bytes max
        ),
    ):
        _fake_download(
            mock_yt_dlp_module,
            "Large Video.mp4",
            b"x" * 100,
            {
                "title": "Large Video",
                "duration": 3600,
            },
        )

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
) -> None:
    """Test fallback file search when filepath is not in requested_downloads."""
    test_content = b"video content"

    with patch("family_assistant.tools.media_download.yt_dlp") as mock_yt_dlp_module:
        _fake_download(
            mock_yt_dlp_module,
            "actual_video.mp4",
            test_content,
            {
                "title": "Test Video",
                "duration": 60,
                "ext": "mp4",
            },
            include_filepath=False,
        )

        result = await download_media_tool(
            mock_exec_context,
            url="https://example.com/video",
        )

    # Should find the mp4 file via glob fallback
    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.attachments is not None
    assert result.attachments[0].content == test_content


@pytest.mark.asyncio
async def test_download_media_various_formats(
    mock_exec_context: MagicMock,
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
        with patch(
            "family_assistant.tools.media_download.yt_dlp"
        ) as mock_yt_dlp_module:
            _fake_download(
                mock_yt_dlp_module,
                f"test{ext}",
                b"content",
                {
                    "title": "Test",
                    "duration": 60,
                },
            )

            result = await download_media_tool(
                mock_exec_context,
                url="https://example.com/video",
            )

        # ast-grep-ignore: no-dict-any - ToolResult.data can be dict, list, str, etc.
        result_data: dict[str, Any] = result.data  # type: ignore[assignment] - data is validated above
        assert result_data["mime_type"] == expected_mime, f"Failed for {ext}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://ftp.example.com/file.mp4",
        "example.com/video",
        "https:///path/only",
    ],
)
async def test_download_media_rejects_invalid_url(
    mock_exec_context: MagicMock, mock_yt_dlp: MagicMock, url: str
) -> None:
    """Invalid URLs are rejected before reaching the downloader."""
    result = await download_media_tool(mock_exec_context, url=url)

    assert isinstance(result.data, dict)
    assert result.data["error"] == "invalid_url"
    assert result.data["error_type"] == "validation_failed"
    mock_yt_dlp.extract_info.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["https://example.com/video", "http://example.com/video"]
)
async def test_download_media_accepts_http_urls(
    mock_exec_context: MagicMock, mock_yt_dlp: MagicMock, url: str
) -> None:
    """HTTP and HTTPS URLs reach the downloader."""
    mock_yt_dlp.extract_info.return_value = {"title": "Video"}
    result = await download_media_tool(mock_exec_context, url=url, metadata_only=True)

    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    mock_yt_dlp.extract_info.assert_called_once_with(url, download=False)


# Filename sanitization tests


def test_sanitize_title_normal_title() -> None:
    """Test sanitization of normal titles."""
    result = _sanitize_title("My Video Title")
    assert result == "My Video Title"


def test_sanitize_title_with_special_chars() -> None:
    """Test sanitization removes dangerous characters."""
    result = _sanitize_title("Video: Test / Something")
    # Should not contain : or /
    assert "/" not in result
    assert ":" not in result
    assert result  # Should not be empty


def test_sanitize_title_consecutive_dots() -> None:
    """Test that consecutive dots are collapsed."""
    result = _sanitize_title("Video...Title")
    assert ".." not in result


def test_sanitize_title_leading_trailing_dots() -> None:
    """Test that leading/trailing dots are removed."""
    result = _sanitize_title("...hidden")
    assert not result.startswith(".")

    result = _sanitize_title("file...")
    assert not result.endswith(".")


def test_sanitize_title_empty_after_sanitization() -> None:
    """Test that empty titles fallback to 'download'."""
    result = _sanitize_title("///")
    assert result == "download"
