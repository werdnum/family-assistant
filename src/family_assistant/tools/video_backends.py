"""Video generation backend protocols and implementations.

Defines the protocol for video generation backends and concrete
implementations:

- :class:`VeoVideoBackend` — Google's Veo models via the long-running
  ``generateVideos`` operation API (text/image-to-video, interpolation,
  reference images).
- :class:`GeminiOmniVideoBackend` — Gemini Omni Flash via the newer
  Interactions API (``client.interactions.create``), which returns the
  generated video as a ``VideoContent`` block.
- :class:`MockVideoBackend` — deterministic backend for tests/development.

The tool layer fetches attachment bytes and hands the backend a
:class:`VideoGenerationRequest`; each backend interprets the parts of the
request it supports.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NotRequired,
    Protocol,
    TypedDict,
    cast,
    runtime_checkable,
)

from family_assistant.observability.metrics import instrumented_llm_request

if TYPE_CHECKING:
    import types

    from google import genai
    from google.genai import types as genai_types
    from google.genai._gaos.types.interactions.interaction import Interaction
    from google.genai.client import AsyncClient

    from family_assistant.llm.messages import MessageReasoningInfo

logger = logging.getLogger(__name__)

# Terminal Interactions API statuses (generation finished, success or not).
_INTERACTION_TERMINAL_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "incomplete",
    "budget_exceeded",
})


class _ImageBlock(TypedDict):
    """An Interactions API image content block."""

    type: Literal["image"]
    data: str
    mime_type: str


class _TextBlock(TypedDict):
    """An Interactions API text content block."""

    type: Literal["text"]
    text: str


type _ContentBlock = _ImageBlock | _TextBlock


class _VideoResponseFormat(TypedDict):
    """Interactions ``response_format`` requesting URI video output."""

    type: Literal["video"]
    delivery: Literal["uri"]
    aspect_ratio: str
    duration: NotRequired[str]


@dataclass(frozen=True)
class VideoReferenceImage:
    """Image bytes plus MIME type used as a video generation input."""

    content: bytes
    mime_type: str = "image/png"


@dataclass
class VideoGenerationRequest:
    """Backend-agnostic description of a video to generate.

    Each backend honors the subset of fields its API supports. Veo uses every
    field; Gemini Omni Flash uses ``prompt``, ``reference_images`` and
    ``first_frame`` (as image inputs), ``aspect_ratio`` and ``duration_seconds``.
    """

    prompt: str
    reference_images: list[VideoReferenceImage] = field(default_factory=list)
    first_frame: VideoReferenceImage | None = None
    last_frame: VideoReferenceImage | None = None
    negative_prompt: str | None = None
    aspect_ratio: str = "16:9"
    duration_seconds: int | None = None


@dataclass(frozen=True)
class VideoGenerationResult:
    """Generated video bytes plus metadata."""

    content: bytes
    mime_type: str = "video/mp4"
    model: str | None = None


class VideoGenerationError(RuntimeError):
    """Raised when a backend fails to produce a video."""


@runtime_checkable
class VideoGenerationBackend(Protocol):
    """Protocol for video generation backends."""

    @abstractmethod
    async def generate_video(
        self, request: VideoGenerationRequest
    ) -> VideoGenerationResult:
        """Generate a video from the given request."""
        ...


def _lazy_import_genai_types() -> types.ModuleType:
    """Lazy-import google.genai.types to avoid xdist worker crashes from concurrent native init."""
    from google.genai import (  # noqa: PLC0415 - intentional lazy import
        types,
    )

    return types


def _create_genai_client(api_key: str) -> genai.Client:
    """Create a genai Client with lazy import to avoid xdist worker crashes."""
    from google import (  # noqa: PLC0415 - intentional lazy import
        genai,
    )

    return genai.Client(api_key=api_key)


class MockVideoBackend:
    """Deterministic mock backend that returns a tiny placeholder MP4."""

    # Minimal ftyp box — enough to be recognizably MP4-ish for tests.
    _PLACEHOLDER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"

    def __init__(self, model: str = "mock-video") -> None:
        """Initialize the mock backend."""
        self.model = model
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def generate_video(
        self, request: VideoGenerationRequest
    ) -> VideoGenerationResult:
        """Return a deterministic placeholder video."""
        self.logger.info(
            "MockVideoBackend generating placeholder for prompt: %s",
            request.prompt[:50],
        )
        return VideoGenerationResult(
            content=self._PLACEHOLDER,
            mime_type="video/mp4",
            model=self.model,
        )


class VeoVideoBackend:
    """Google Veo backend using the long-running ``generateVideos`` API."""

    DEFAULT_MODEL = "veo-3.1-generate-preview"
    # Reference-image and interpolation modes require a fixed 8s duration.
    _FIXED_DURATION_MODES_SECONDS = 8
    _POLL_INTERVAL_SECONDS = 10
    _TIMEOUT_SECONDS = 600

    def __init__(self, api_key: str, model: str | None = None) -> None:
        """Initialize the Veo backend with API key and optional model override."""
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def _to_genai_image(self, reference: VideoReferenceImage) -> genai_types.Image:
        types = _lazy_import_genai_types()
        return types.Image(image_bytes=reference.content, mime_type=reference.mime_type)

    def _force_fixed_duration(
        self, config_params: dict[str, object], mode: str
    ) -> None:
        """Pin duration to 8s; reference-image and interpolation modes require it."""
        if config_params.get("duration_seconds") != self._FIXED_DURATION_MODES_SECONDS:
            self.logger.info("Forcing duration to 8s for %s mode", mode)
            config_params["duration_seconds"] = self._FIXED_DURATION_MODES_SECONDS

    async def generate_video(
        self, request: VideoGenerationRequest
    ) -> VideoGenerationResult:
        """Generate a video with Veo, counting the generation as one request.

        The whole generate-and-poll sequence is one billable unit, so it is
        counted as one call rather than one per poll. Veo bills per second of
        video and reports no token usage, so the call counter is the meter --
        the same treatment the per-image models get.
        """
        return await instrumented_llm_request(
            provider="google",
            model=self.model,
            operation="video",
            request=lambda: self._generate_video(request),
        )

    async def _generate_video(
        self, request: VideoGenerationRequest
    ) -> VideoGenerationResult:
        """Generate a video with Veo, polling the long-running operation."""
        types = _lazy_import_genai_types()

        async with _create_genai_client(self.api_key).aio as client:
            config_params: dict[str, object] = {}
            if request.negative_prompt:
                config_params["negative_prompt"] = request.negative_prompt
            if request.aspect_ratio:
                config_params["aspect_ratio"] = request.aspect_ratio
            if request.duration_seconds is not None:
                config_params["duration_seconds"] = request.duration_seconds

            if request.reference_images:
                config_params["reference_images"] = [
                    types.VideoGenerationReferenceImage(
                        image=self._to_genai_image(reference)
                    )
                    for reference in request.reference_images[:3]
                ]
                self._force_fixed_duration(config_params, "reference images")

            if request.last_frame is not None:
                config_params["last_frame"] = self._to_genai_image(request.last_frame)
                self._force_fixed_duration(config_params, "interpolation")

            config = types.GenerateVideosConfig(**config_params)

            first_frame_obj = (
                self._to_genai_image(request.first_frame)
                if request.first_frame is not None
                else None
            )
            source = types.GenerateVideosSource(
                prompt=request.prompt, image=first_frame_obj
            )

            self.logger.info(
                "Starting Veo video generation with model %s and prompt: %s...",
                self.model,
                request.prompt[:50],
            )
            operation = await client.models.generate_videos(
                model=self.model, source=source, config=config
            )
            self.logger.info("Video generation operation started: %s", operation.name)

            start_time = time.time()
            while not operation.done:
                if time.time() - start_time > self._TIMEOUT_SECONDS:
                    raise VideoGenerationError(
                        f"Video generation timed out after {self._TIMEOUT_SECONDS} seconds."
                    )
                self.logger.debug("Waiting for video generation to complete...")
                await asyncio.sleep(self._POLL_INTERVAL_SECONDS)
                operation = await client.operations.get(operation)

            if operation.error:
                op_error = operation.error
                if isinstance(op_error, dict):
                    error_msg = op_error.get("message", "Unknown error")
                    error_code = op_error.get("code")
                else:
                    error_msg = getattr(op_error, "message", "Unknown error")
                    error_code = getattr(op_error, "code", None)
                raise VideoGenerationError(
                    f"Veo video generation failed: {error_msg} (code: {error_code})"
                )

            response = getattr(operation, "response", None)
            if not response or not response.generated_videos:
                raise VideoGenerationError("No video generated in Veo response.")

            video_asset = response.generated_videos[0]
            if not video_asset.video:
                raise VideoGenerationError(
                    "Generated video asset is missing its video file reference."
                )

            self.logger.info("Downloading Veo video content...")
            # cast to Any because Pyright doesn't recognize Video as a download input yet.
            video_bytes = await client.files.download(
                file=cast("Any", video_asset.video)
            )
            return VideoGenerationResult(
                content=video_bytes, mime_type="video/mp4", model=self.model
            )


def _omni_video_usage(
    interaction: Any,  # noqa: ANN401 - google.genai Interaction
) -> MessageReasoningInfo | None:
    """Token usage for an Omni Flash video interaction, when reported.

    Omni Flash bills its video output in tokens and reports them on the
    interaction, the same shape a managed-agent run uses, so the one canonical
    mapping is reused rather than duplicated here.
    """
    usage = getattr(interaction, "usage", None)
    if usage is None:
        return None
    # Imported here rather than at module scope: the provider client reaches
    # config_models, which reaches back to this module through the tool
    # registry. Same cycle-breaking local import as tools/camera.py.
    from family_assistant.llm.providers.google_genai_client import (  # noqa: PLC0415
        GoogleGenAIClient,
    )

    return GoogleGenAIClient._reasoning_info_from_interaction_usage(usage)


class GeminiOmniVideoBackend:
    """Gemini Omni Flash backend using the Interactions API.

    Omni Flash is a multimodal model whose video output is delivered as a
    ``VideoContent`` block (inline base64 ``data`` or a ``uri``). It does not
    use Veo's long-running ``generateVideos`` operation, so this backend builds
    Interactions ``input`` content blocks and reads the resulting outputs.
    """

    DEFAULT_MODEL = "gemini-omni-flash-preview"
    _POLL_INTERVAL_SECONDS = 5
    _TIMEOUT_SECONDS = 600

    def __init__(self, api_key: str, model: str | None = None) -> None:
        """Initialize the Omni backend with API key and optional model override."""
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @staticmethod
    def _image_block(reference: VideoReferenceImage) -> _ImageBlock:
        return {
            "type": "image",
            "data": base64.b64encode(reference.content).decode("ascii"),
            "mime_type": reference.mime_type,
        }

    def _build_input(
        self, request: VideoGenerationRequest
    ) -> str | list[_ContentBlock]:
        # first_frame anchors image-to-video; reference images guide style/content.
        images: list[VideoReferenceImage] = []
        if request.first_frame is not None:
            images.append(request.first_frame)
        images.extend(request.reference_images)

        if not images:
            return request.prompt

        blocks: list[_ContentBlock] = [self._image_block(image) for image in images]
        blocks.append({"type": "text", "text": request.prompt})
        return blocks

    async def generate_video(
        self, request: VideoGenerationRequest
    ) -> VideoGenerationResult:
        """Generate a video with Omni Flash, counting it as one request.

        Unlike Veo, Omni Flash runs through the Interactions API and reports
        token usage on the interaction, so the tokens are recorded alongside
        the call. The inner method returns that usage beside its result
        because the result type carries no room for it.
        """
        result, usage = await instrumented_llm_request(
            provider="google",
            model=self.model,
            operation="video",
            request=lambda: self._generate_video(request),
            usage=lambda pair: pair[1],
        )
        return result

    async def _generate_video(
        self, request: VideoGenerationRequest
    ) -> tuple[VideoGenerationResult, MessageReasoningInfo | None]:
        """Generate a video with Gemini Omni Flash via the Interactions API."""
        unsupported_features: list[str] = []
        if request.last_frame is not None:
            unsupported_features.append("last_frame")
        if request.negative_prompt is not None:
            unsupported_features.append("negative_prompt")
        if unsupported_features:
            raise VideoGenerationError(
                "Gemini Omni Flash does not support "
                f"{', '.join(unsupported_features)}; use a Veo model instead."
            )

        async with _create_genai_client(self.api_key).aio as client:
            # URI delivery avoids the Interactions API's 4 MB inline limit.
            response_format: _VideoResponseFormat = {
                "type": "video",
                "delivery": "uri",
                "aspect_ratio": request.aspect_ratio,
            }
            if request.duration_seconds is not None:
                response_format["duration"] = f"{request.duration_seconds}s"

            self.logger.info(
                "Starting Omni Flash video generation with model %s and prompt: %s...",
                self.model,
                request.prompt[:50],
            )
            # base64-encoding reference images can be multi-MB; keep it off the loop.
            interaction_input = await asyncio.to_thread(self._build_input, request)
            # Not a streaming request, so create() returns an Interaction.
            interaction = cast(
                "Interaction",
                await client.interactions.create(
                    model=self.model,
                    input=cast("Any", interaction_input),
                    response_format=response_format,
                ),
            )

            interaction = await self._await_completion(client, interaction)

            if interaction.status != "completed":
                raise VideoGenerationError(
                    f"Omni Flash video generation ended with status "
                    f"'{interaction.status}'."
                )

            video_bytes = await self._extract_video_bytes(client, interaction)
            return (
                VideoGenerationResult(
                    content=video_bytes, mime_type="video/mp4", model=self.model
                ),
                _omni_video_usage(interaction),
            )

    async def _await_completion(
        self, client: AsyncClient, interaction: Interaction
    ) -> Interaction:
        """Poll the interaction until it reaches a terminal status."""
        interaction_id = interaction.id
        if interaction_id is None:
            raise VideoGenerationError("Interaction is missing an id; cannot poll.")
        start_time = time.time()
        while interaction.status not in _INTERACTION_TERMINAL_STATUSES:
            if time.time() - start_time > self._TIMEOUT_SECONDS:
                raise VideoGenerationError(
                    f"Omni Flash video generation timed out after "
                    f"{self._TIMEOUT_SECONDS} seconds."
                )
            self.logger.debug("Waiting for Omni Flash generation to complete...")
            await asyncio.sleep(self._POLL_INTERVAL_SECONDS)
            interaction = cast(
                "Interaction", await client.interactions.get(interaction_id)
            )
        return interaction

    async def _extract_video_bytes(
        self, client: AsyncClient, interaction: Interaction
    ) -> bytes:
        """Pull the generated video bytes from the interaction's output_video."""
        output_video = getattr(interaction, "output_video", None)
        if output_video is not None:
            data = getattr(output_video, "data", None)
            if data:
                # Decoding a multi-MB video off the event loop.
                return await asyncio.to_thread(base64.b64decode, data)
            uri = getattr(output_video, "uri", None)
            if uri:
                self.logger.info("Downloading Omni Flash video from URI...")
                return await client.files.download(file=cast("Any", uri))
        raise VideoGenerationError("No video content found in Omni Flash response.")
