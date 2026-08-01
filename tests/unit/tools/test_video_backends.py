"""Tests for video generation backends and backend selection."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from family_assistant.config_models import AppConfig
from family_assistant.tools.video_backends import (
    GeminiOmniVideoBackend,
    MockVideoBackend,
    VeoVideoBackend,
    VideoGenerationError,
    VideoGenerationRequest,
    VideoReferenceImage,
)
from family_assistant.tools.video_generation import (
    _create_video_backend,  # noqa: PLC2701 - testing internal backend selection
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from family_assistant.tools.types import ToolExecutionContext

_BackendChoice = Literal["veo", "gemini_omni", "mock"]


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def _ctx_with_config(config: AppConfig | None) -> ToolExecutionContext:
    service = SimpleNamespace(app_config=config) if config is not None else None
    return cast("ToolExecutionContext", SimpleNamespace(processing_service=service))


@pytest.mark.parametrize(
    ("backend_choice", "expected"),
    [
        ("veo", VeoVideoBackend),
        ("gemini_omni", GeminiOmniVideoBackend),
        ("mock", MockVideoBackend),
    ],
)
def test_explicit_backend_choice(
    backend_choice: _BackendChoice, expected: type
) -> None:
    config = AppConfig(gemini_api_key="key", video_generation_backend=backend_choice)
    backend = _create_video_backend(_ctx_with_config(config), model_override=None)
    assert isinstance(backend, expected)


def test_explicit_backend_requires_api_key() -> None:
    config = AppConfig(video_generation_backend="veo")
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="GEMINI_API_KEY"),
    ):
        _create_video_backend(_ctx_with_config(config), model_override=None)


def test_auto_select_defaults_to_gemini_omni() -> None:
    config = AppConfig(gemini_api_key="key")
    backend = _create_video_backend(_ctx_with_config(config), model_override=None)
    assert isinstance(backend, GeminiOmniVideoBackend)
    assert backend.model == "gemini-omni-flash"


def test_auto_select_infers_omni_from_model() -> None:
    config = AppConfig(gemini_api_key="key")
    backend = _create_video_backend(
        _ctx_with_config(config), model_override="gemini-omni-flash"
    )
    assert isinstance(backend, GeminiOmniVideoBackend)
    assert backend.model == "gemini-omni-flash"


def test_auto_select_infers_veo_from_model() -> None:
    config = AppConfig(gemini_api_key="key")
    backend = _create_video_backend(
        _ctx_with_config(config), model_override="veo-3.1-fast"
    )
    assert isinstance(backend, VeoVideoBackend)
    assert backend.model == "veo-3.1-fast"


def test_auto_select_uses_veo_for_veo_only_features() -> None:
    config = AppConfig(gemini_api_key="key")
    backend = _create_video_backend(
        _ctx_with_config(config),
        model_override=None,
        requires_veo_features=True,
    )
    assert isinstance(backend, VeoVideoBackend)
    assert backend.model == "veo-3.1-generate-preview"


def test_omni_model_override_remains_authoritative_for_veo_only_features() -> None:
    config = AppConfig(gemini_api_key="key")
    backend = _create_video_backend(
        _ctx_with_config(config),
        model_override="gemini-omni-flash",
        requires_veo_features=True,
    )
    assert isinstance(backend, GeminiOmniVideoBackend)


def test_auto_select_falls_back_to_mock_without_key() -> None:
    config = AppConfig()
    with patch.dict("os.environ", {}, clear=True):
        backend = _create_video_backend(_ctx_with_config(config), model_override=None)
    assert isinstance(backend, MockVideoBackend)


def test_model_override_applies_to_configured_backend() -> None:
    config = AppConfig(gemini_api_key="key", video_generation_backend="gemini_omni")
    backend = _create_video_backend(
        _ctx_with_config(config), model_override="gemini-omni-flash-custom"
    )
    assert isinstance(backend, GeminiOmniVideoBackend)
    assert backend.model == "gemini-omni-flash-custom"


@pytest.mark.asyncio
async def test_mock_backend_returns_placeholder() -> None:
    backend = MockVideoBackend()
    result = await backend.generate_video(VideoGenerationRequest(prompt="hi"))
    assert result.content.startswith(b"\x00\x00\x00\x18ftyp")
    assert result.mime_type == "video/mp4"


# --------------------------------------------------------------------------- #
# Veo backend
# --------------------------------------------------------------------------- #


@dataclass
class _FakeImage:
    image_bytes: bytes
    mime_type: str


@dataclass
class _FakeGenerateVideosSource:
    prompt: str
    image: _FakeImage | None = None


@dataclass
class _FakeVideoGenerationReferenceImage:
    image: _FakeImage


@dataclass
class _FakeGenerateVideosConfig:
    negative_prompt: str | None = None
    aspect_ratio: str | None = None
    duration_seconds: int | None = None
    last_frame: _FakeImage | None = None
    reference_images: list[_FakeVideoGenerationReferenceImage] | None = None


def _fake_genai_types() -> SimpleNamespace:
    return SimpleNamespace(
        Image=_FakeImage,
        GenerateVideosConfig=_FakeGenerateVideosConfig,
        GenerateVideosSource=_FakeGenerateVideosSource,
        VideoGenerationReferenceImage=_FakeVideoGenerationReferenceImage,
    )


def _aio_client(async_client: MagicMock) -> MagicMock:
    """Wrap an async client in a mock exposing it via the ``.aio`` async CM."""
    client = MagicMock()
    aio = MagicMock()
    aio.__aenter__ = AsyncMock(return_value=async_client)
    aio.__aexit__ = AsyncMock(return_value=None)
    client.aio = aio
    return client


@pytest.fixture
def veo_async_client() -> Generator[MagicMock]:
    operation = MagicMock()
    operation.name = "operation/123"
    operation.done = True
    operation.error = None
    video_asset = MagicMock()
    video_asset.video = "file-ref"
    operation.response.generated_videos = [video_asset]

    async_client = MagicMock()
    async_client.models.generate_videos = AsyncMock(return_value=operation)
    async_client.operations.get = AsyncMock(return_value=operation)
    async_client.files.download = AsyncMock(return_value=b"video-content")

    with (
        patch(
            "family_assistant.tools.video_backends._create_genai_client",
            return_value=_aio_client(async_client),
        ),
        patch(
            "family_assistant.tools.video_backends._lazy_import_genai_types",
            return_value=_fake_genai_types(),
        ),
        patch("asyncio.sleep", AsyncMock()),
    ):
        yield async_client


@pytest.mark.asyncio
async def test_veo_generate_returns_downloaded_bytes(
    veo_async_client: MagicMock,
) -> None:
    backend = VeoVideoBackend("key")
    result = await backend.generate_video(
        VideoGenerationRequest(prompt="A cat", duration_seconds=8)
    )

    assert result.content == b"video-content"
    assert result.mime_type == "video/mp4"
    assert result.model == "veo-3.1-generate-preview"

    _, kwargs = veo_async_client.models.generate_videos.call_args
    assert kwargs["model"] == "veo-3.1-generate-preview"
    assert isinstance(kwargs["source"], _FakeGenerateVideosSource)
    assert kwargs["source"].prompt == "A cat"
    veo_async_client.files.download.assert_called_once_with(file="file-ref")


@pytest.mark.asyncio
async def test_veo_forces_duration_with_reference_images(
    veo_async_client: MagicMock,
) -> None:
    backend = VeoVideoBackend("key")
    await backend.generate_video(
        VideoGenerationRequest(
            prompt="ref video",
            reference_images=[
                VideoReferenceImage(content=b"img", mime_type="image/png")
            ],
            first_frame=VideoReferenceImage(content=b"first", mime_type="image/jpeg"),
            last_frame=VideoReferenceImage(content=b"last", mime_type="image/jpeg"),
            duration_seconds=4,  # should be forced to 8
        )
    )

    _, kwargs = veo_async_client.models.generate_videos.call_args
    config = kwargs["config"]
    assert config.duration_seconds == 8
    assert config.reference_images is not None
    assert config.reference_images[0].image.image_bytes == b"img"
    assert config.last_frame.image_bytes == b"last"
    assert kwargs["source"].image.image_bytes == b"first"


@pytest.mark.asyncio
async def test_veo_polls_until_done(veo_async_client: MagicMock) -> None:
    pending = MagicMock()
    pending.done = False
    pending.name = "op/pending"
    done = MagicMock()
    done.done = True
    done.error = None
    done.response.generated_videos = [MagicMock(video="file-ref")]

    veo_async_client.models.generate_videos.return_value = pending
    veo_async_client.operations.get.side_effect = [pending, done]

    backend = VeoVideoBackend("key")
    await backend.generate_video(VideoGenerationRequest(prompt="dog"))

    assert veo_async_client.operations.get.call_count == 2


@pytest.mark.asyncio
async def test_veo_raises_on_operation_error(veo_async_client: MagicMock) -> None:
    operation = MagicMock()
    operation.done = True
    operation.error = SimpleNamespace(message="Safety violation", code=400)
    veo_async_client.models.generate_videos.return_value = operation
    veo_async_client.operations.get.return_value = operation

    backend = VeoVideoBackend("key")
    with pytest.raises(VideoGenerationError, match="Safety violation"):
        await backend.generate_video(VideoGenerationRequest(prompt="unsafe"))


@pytest.mark.asyncio
async def test_veo_raises_on_timeout(veo_async_client: MagicMock) -> None:
    pending = MagicMock()
    pending.done = False
    pending.name = "op/pending"
    veo_async_client.models.generate_videos.return_value = pending
    veo_async_client.operations.get.return_value = pending

    backend = VeoVideoBackend("key")
    with (
        patch("family_assistant.tools.video_backends.time.time") as mock_time,
        pytest.raises(VideoGenerationError, match="timed out"),
    ):
        mock_time.side_effect = [1000.0, 1700.0]
        await backend.generate_video(VideoGenerationRequest(prompt="slow"))


# --------------------------------------------------------------------------- #
# Gemini Omni Flash backend
# --------------------------------------------------------------------------- #


def _video_output(data: bytes) -> SimpleNamespace:
    """A completed interaction's ``output_video`` block (inline base64 data)."""
    return SimpleNamespace(
        data=base64.b64encode(data).decode("ascii"),
        uri=None,
        mime_type="video/mp4",
    )


def _omni_interaction(
    status: str = "completed", output_video: SimpleNamespace | None = None
) -> SimpleNamespace:
    return SimpleNamespace(id="interaction-1", status=status, output_video=output_video)


@pytest.fixture
def omni_async_client() -> Generator[MagicMock]:
    async_client = MagicMock()
    async_client.interactions.create = AsyncMock(
        return_value=_omni_interaction(output_video=_video_output(b"omni-video"))
    )
    async_client.interactions.get = AsyncMock()
    async_client.files.get = AsyncMock(
        return_value=SimpleNamespace(state=SimpleNamespace(name="ACTIVE"))
    )
    async_client.files.download = AsyncMock(return_value=b"uri-video")

    with (
        patch(
            "family_assistant.tools.video_backends._create_genai_client",
            return_value=_aio_client(async_client),
        ),
        patch("asyncio.sleep", AsyncMock()),
    ):
        yield async_client


@pytest.mark.asyncio
async def test_omni_returns_video_bytes(omni_async_client: MagicMock) -> None:
    backend = GeminiOmniVideoBackend("key")
    result = await backend.generate_video(
        VideoGenerationRequest(prompt="a dancing robot", aspect_ratio="9:16")
    )

    assert result.content == b"omni-video"
    assert result.mime_type == "video/mp4"
    assert result.model == "gemini-omni-flash"

    _, kwargs = omni_async_client.interactions.create.call_args
    assert kwargs["model"] == "gemini-omni-flash"
    assert kwargs["input"] == "a dancing robot"
    assert kwargs["response_format"] == {
        "type": "video",
        "delivery": "uri",
        "aspect_ratio": "9:16",
    }


@pytest.mark.asyncio
async def test_omni_passes_duration_to_response_format(
    omni_async_client: MagicMock,
) -> None:
    backend = GeminiOmniVideoBackend("key")
    await backend.generate_video(
        VideoGenerationRequest(prompt="a quick clip", duration_seconds=3)
    )

    _, kwargs = omni_async_client.interactions.create.call_args
    assert kwargs["response_format"]["duration"] == "3s"


@pytest.mark.asyncio
async def test_omni_builds_image_input_blocks(omni_async_client: MagicMock) -> None:
    backend = GeminiOmniVideoBackend("key")
    await backend.generate_video(
        VideoGenerationRequest(
            prompt="animate this",
            first_frame=VideoReferenceImage(content=b"first", mime_type="image/jpeg"),
            reference_images=[
                VideoReferenceImage(content=b"ref", mime_type="image/png")
            ],
        )
    )

    _, kwargs = omni_async_client.interactions.create.call_args
    blocks = kwargs["input"]
    assert blocks[0] == {
        "type": "image",
        "data": base64.b64encode(b"first").decode("ascii"),
        "mime_type": "image/jpeg",
    }
    assert blocks[1]["type"] == "image"
    assert blocks[1]["data"] == base64.b64encode(b"ref").decode("ascii")
    assert blocks[2] == {"type": "text", "text": "animate this"}


@pytest.mark.asyncio
async def test_omni_polls_until_completed(omni_async_client: MagicMock) -> None:
    omni_async_client.interactions.create.return_value = _omni_interaction(
        status="in_progress"
    )
    omni_async_client.interactions.get.side_effect = [
        _omni_interaction(status="in_progress"),
        _omni_interaction(output_video=_video_output(b"polled-video")),
    ]

    backend = GeminiOmniVideoBackend("key")
    result = await backend.generate_video(VideoGenerationRequest(prompt="slow video"))

    assert result.content == b"polled-video"
    assert omni_async_client.interactions.get.call_count == 2


@pytest.mark.asyncio
async def test_omni_downloads_uri_when_no_inline_data(
    omni_async_client: MagicMock,
) -> None:
    uri_output = SimpleNamespace(
        data=None, uri="https://files/video", mime_type="video/mp4"
    )
    omni_async_client.interactions.create.return_value = _omni_interaction(
        output_video=uri_output
    )

    backend = GeminiOmniVideoBackend("key")
    result = await backend.generate_video(VideoGenerationRequest(prompt="big video"))

    assert result.content == b"uri-video"
    omni_async_client.files.get.assert_called_once_with(name="files/video")
    omni_async_client.files.download.assert_called_once_with(file="https://files/video")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "video_request",
    [
        VideoGenerationRequest(
            prompt="interpolate",
            last_frame=VideoReferenceImage(content=b"last"),
        ),
        VideoGenerationRequest(prompt="exclude rain", negative_prompt="rain"),
    ],
)
async def test_omni_rejects_unsupported_veo_features(
    omni_async_client: MagicMock, video_request: VideoGenerationRequest
) -> None:
    backend = GeminiOmniVideoBackend("key")

    with pytest.raises(VideoGenerationError, match="use a Veo model"):
        await backend.generate_video(video_request)

    omni_async_client.interactions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_omni_raises_when_no_video_output(omni_async_client: MagicMock) -> None:
    omni_async_client.interactions.create.return_value = _omni_interaction(
        output_video=None
    )

    backend = GeminiOmniVideoBackend("key")
    with pytest.raises(VideoGenerationError, match="No video content"):
        await backend.generate_video(VideoGenerationRequest(prompt="text only"))


@pytest.mark.asyncio
async def test_omni_raises_on_failed_status(omni_async_client: MagicMock) -> None:
    omni_async_client.interactions.create.return_value = _omni_interaction(
        status="failed"
    )

    backend = GeminiOmniVideoBackend("key")
    with pytest.raises(VideoGenerationError, match="status 'failed'"):
        await backend.generate_video(VideoGenerationRequest(prompt="doomed"))
