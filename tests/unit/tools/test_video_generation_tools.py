"""Tests for the generate_video tool (request building + backend dispatch).

Backend API behavior is covered in test_video_backends.py. These tests use an
injected fake backend (the ``video_backend`` seam on the execution context) to
verify how the tool turns its arguments into a VideoGenerationRequest and how it
wraps the backend's result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.tools.video_backends import (
    VideoGenerationError,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from family_assistant.tools.video_generation import generate_video_tool


class _RecordingBackend:
    """Fake backend that records the request and returns canned bytes."""

    def __init__(
        self,
        result: VideoGenerationResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or VideoGenerationResult(
            content=b"video-bytes", mime_type="video/mp4", model="fake-model"
        )
        self.error = error
        self.request: VideoGenerationRequest | None = None

    async def generate_video(
        self, request: VideoGenerationRequest
    ) -> VideoGenerationResult:
        self.request = request
        if self.error is not None:
            raise self.error
        return self.result


def _exec_context(backend: _RecordingBackend) -> ToolExecutionContext:
    """An execution context carrying an injected video backend."""
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(processing_service=None, video_backend=backend),
    )


def _attachment(
    content: bytes | None, mime: str = "image/png", att_id: str = "a1"
) -> ScriptAttachment:
    attachment = MagicMock(spec=ScriptAttachment)
    attachment.get_content_async = AsyncMock(return_value=content)
    attachment.get_mime_type.return_value = mime
    attachment.get_id.return_value = att_id
    return cast("ScriptAttachment", attachment)


@pytest.mark.asyncio
async def test_generate_video_returns_attachment_from_backend() -> None:
    backend = _RecordingBackend()

    result = await generate_video_tool(
        _exec_context(backend),
        prompt="A video of a cat",
        aspect_ratio="9:16",
        duration_seconds="6",
    )

    assert isinstance(result, ToolResult)
    assert result.data == {
        "status": "success",
        "model": "fake-model",
        "prompt": "A video of a cat",
    }
    assert result.attachments is not None
    attachment = result.attachments[0]
    assert isinstance(attachment, ToolAttachment)
    assert attachment.content == b"video-bytes"
    assert attachment.mime_type == "video/mp4"

    # The tool forwarded its arguments into the request.
    assert backend.request is not None
    assert backend.request.prompt == "A video of a cat"
    assert backend.request.aspect_ratio == "9:16"
    assert backend.request.duration_seconds == 6


@pytest.mark.asyncio
async def test_generate_video_builds_reference_and_frame_images() -> None:
    backend = _RecordingBackend()
    ref = _attachment(b"ref", "image/png", "ref1")
    first = _attachment(b"first", "image/jpeg", "first1")
    last = _attachment(b"last", "image/jpeg", "last1")

    await generate_video_tool(
        _exec_context(backend),
        prompt="A multimodal video",
        images=[ref],
        first_frame_image=first,
        last_frame_image=last,
        negative_prompt="no rain",
    )

    request = backend.request
    assert request is not None
    assert [img.content for img in request.reference_images] == [b"ref"]
    assert request.first_frame is not None and request.first_frame.content == b"first"
    assert request.last_frame is not None and request.last_frame.content == b"last"
    assert request.first_frame.mime_type == "image/jpeg"
    assert request.negative_prompt == "no rain"


@pytest.mark.asyncio
async def test_generate_video_truncates_reference_images_to_three() -> None:
    backend = _RecordingBackend()
    images = [_attachment(f"img{i}".encode(), att_id=f"img{i}") for i in range(5)]

    await generate_video_tool(
        _exec_context(backend), prompt="too many refs", images=images
    )

    assert backend.request is not None
    assert len(backend.request.reference_images) == 3


@pytest.mark.asyncio
async def test_generate_video_skips_invalid_and_empty_attachments() -> None:
    backend = _RecordingBackend()
    invalid = cast("ScriptAttachment", MagicMock())  # not a ScriptAttachment
    empty = _attachment(None, att_id="empty")

    await generate_video_tool(
        _exec_context(backend),
        prompt="invalid inputs",
        images=[invalid, empty],
        first_frame_image=empty,
    )

    assert backend.request is not None
    assert backend.request.reference_images == []
    assert backend.request.first_frame is None


@pytest.mark.asyncio
async def test_generate_video_reports_backend_error() -> None:
    backend = _RecordingBackend(error=VideoGenerationError("safety filter blocked"))

    result = await generate_video_tool(_exec_context(backend), prompt="unsafe")

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert "safety filter blocked" in result.data["error"]
    assert not result.attachments


@pytest.mark.asyncio
async def test_generate_video_reports_unexpected_error() -> None:
    backend = _RecordingBackend(error=RuntimeError("boom"))

    result = await generate_video_tool(_exec_context(backend), prompt="oops")

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert "boom" in result.data["error"]
