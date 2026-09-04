"""
Image generation backend protocols and implementations.

This module defines the protocol for image generation backends and provides
concrete implementations including mock (PIL-based), Gemini API, and OpenAI API backends.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import mimetypes
import random
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from family_assistant.llm.messages import MessageReasoningInfo
from family_assistant.observability.metrics import instrumented_llm_request

if TYPE_CHECKING:
    from google.genai.client import DebugConfig

    from family_assistant.config_models import OpenAIImageRequestConfig

# Optional imports for production use
try:
    from google import genai
    from google.genai import types as genai_types

    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    import openai

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageReference:
    """Image bytes plus MIME type metadata for provider reference inputs."""

    content: bytes
    mime_type: str = "image/png"


type ImageReferenceInput = bytes | ImageReference
type ImageReferenceInputs = ImageReferenceInput | Sequence[ImageReferenceInput]


def _normalize_image_references(
    image_bytes: ImageReferenceInputs,
) -> list[ImageReference]:
    """Normalize one or more image references to metadata-rich inputs."""
    image_inputs = (
        [image_bytes]
        if isinstance(image_bytes, bytes | ImageReference)
        else list(image_bytes)
    )
    return [
        image_input
        if isinstance(image_input, ImageReference)
        else ImageReference(content=image_input)
        for image_input in image_inputs
    ]


# The formats OpenAI's image-edit endpoint accepts, and the extension the upload
# has to be named with for it to recognize one. The endpoint reads the filename,
# so a name that disagrees with the bytes is itself a rejection.
_OPENAI_EDIT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def _openai_upload_extension(reference: ImageReference) -> str:
    """Name an upload after the bytes' real format rather than the declared type."""
    try:
        with Image.open(io.BytesIO(reference.content)) as image:
            sniffed = _OPENAI_EDIT_EXTENSIONS.get(image.format or "")
    except OSError:
        # Unreadable here does not mean unreadable there; fall back to the
        # declared type and let the endpoint be the judge.
        sniffed = None
    return sniffed or mimetypes.guess_extension(reference.mime_type) or ".png"


def _reencode_for_openai_edit(content: bytes) -> tuple[bytes, str]:
    """
    Re-encode an image to a baseline form the edit endpoint accepts.

    Cameras produce JPEGs that every viewer opens but the edit endpoint rejects
    outright -- CMYK or otherwise exotic colour modes, unusual ICC profiles,
    orientation held only in EXIF. Decoding and re-saving discards all of that:
    the pixels survive, the encoding quirks do not.
    """
    with Image.open(io.BytesIO(content)) as image:
        oriented = ImageOps.exif_transpose(image) or image
        has_alpha = oriented.mode in {"RGBA", "LA", "PA"} or "transparency" in (
            oriented.info
        )
        converted = oriented.convert("RGBA" if has_alpha else "RGB")

    buffer = io.BytesIO()
    if has_alpha:
        converted.save(buffer, format="PNG")
        return buffer.getvalue(), ".png"
    converted.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue(), ".jpg"


def _build_openai_edit_files(
    references: Sequence[ImageReference], *, normalize: bool
) -> list[io.BytesIO]:
    """
    Build named upload files for the edit endpoint.

    Decodes images with Pillow, so run it off the event loop.
    """
    image_files: list[io.BytesIO] = []
    for index, reference in enumerate(references, start=1):
        if normalize:
            content, extension = _reencode_for_openai_edit(reference.content)
        else:
            content, extension = reference.content, _openai_upload_extension(reference)
        image_file = io.BytesIO(content)
        image_file.name = f"image_{index}{extension}"
        image_files.append(image_file)
    return image_files


def _is_invalid_image_file_error(error: Exception) -> bool:
    """Return whether OpenAI rejected the uploaded bytes rather than the request."""
    if getattr(error, "code", None) == "invalid_image_file":
        return True
    return "invalid_image_file" in str(error)


def _served_model(response: Any) -> str | None:  # noqa: ANN401 - provider response
    """The model a provider says it actually served, where it says so.

    Both the Gemini and OpenAI image responses carry the served model, which an
    alias or provider-side routing can make different from the requested one.
    """
    return getattr(response, "model", None) or getattr(response, "model_version", None)


def _gemini_image_usage(
    response: Any,  # noqa: ANN401 - genai GenerateContentResponse
) -> MessageReasoningInfo | None:
    """Token usage for a Gemini image request, when the API reports it.

    Gemini bills image output in tokens, so these are real spend rather than a
    proxy for it. The OpenAI image endpoints bill per image instead and report
    no comparable block, which is why only this provider passes a usage reader
    -- the call counter is what both have in common.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    # Imported here rather than at module scope: the provider client reaches
    # config_models, which reaches back to this module through the tool
    # registry. Same cycle-breaking local import as tools/camera.py.
    from family_assistant.llm.providers.google_genai_client import (  # noqa: PLC0415
        GoogleGenAIClient,
    )

    # One canonical Gemini usage mapping, reused rather than duplicated here.
    info = GoogleGenAIClient._reasoning_info_from_usage_metadata(usage)
    if info is None:
        return None
    # Gemini reports per-modality breakdowns on an image call, and bills image
    # tokens well above text on both sides, so the split has to reach the
    # buckets rather than being averaged into the text tiers.
    image_in = _gemini_modality_tokens(getattr(usage, "prompt_tokens_details", None))
    if image_in is not None:
        info["image_input_tokens"] = image_in
    image_out = _gemini_modality_tokens(
        getattr(usage, "candidates_tokens_details", None)
    )
    if image_out is not None:
        info["image_output_tokens"] = image_out
    return info


def _gemini_modality_tokens(details: Any) -> int | None:  # noqa: ANN401 - genai detail list
    """Image-modality tokens from one Gemini per-modality breakdown.

    Returns ``None`` when the breakdown is absent, so a response that does not
    report modalities emits no image bucket at all rather than a zero that
    would read as "no image tokens" when it means "not reported".
    """
    # The SDK types this Optional[list[ModalityTokenCount]]; anything else is
    # not a breakdown to read, and this parameter is Any.
    if not isinstance(details, list) or not details:
        return None
    return sum(
        getattr(entry, "token_count", 0) or 0
        for entry in details
        if str(getattr(entry, "modality", "")).upper().endswith("IMAGE")
    )


def _openai_image_usage(
    response: Any,  # noqa: ANN401 - openai ImagesResponse
) -> MessageReasoningInfo | None:
    """Token usage for an OpenAI image request, when the model reports it.

    The image models that bill per image report no usage block and yield
    nothing here, leaving the call counter as their meter. GPT Image bills in
    tokens and does report one, so those are real spend and are recorded.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    info = MessageReasoningInfo(
        prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
        completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        total_tokens=getattr(usage, "total_tokens", 0) or 0,
    )
    # GPT Image bills image tokens above text on both sides -- the source
    # image an edit sends in, and the image it generates -- so both splits have
    # to survive into the buckets. Costed at the text price, a large source
    # image understates the edit and a generated image understates every call.
    for source, key in (
        ("input_tokens_details", "image_input_tokens"),
        ("output_tokens_details", "image_output_tokens"),
    ):
        details = getattr(usage, source, None)
        image_tokens = getattr(details, "image_tokens", None) if details else None
        if image_tokens is not None:
            info[key] = image_tokens  # pyright: ignore[reportGeneralTypeIssues] - key is a literal from the tuple above
    return info


@runtime_checkable
class ImageGenerationBackend(Protocol):
    """Protocol for image generation backends."""

    @abstractmethod
    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate an image from a text prompt."""
        ...

    @abstractmethod
    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        """Transform existing image references based on text instruction."""
        ...


class MockImageBackend:
    """Mock image backend using PIL for testing and development."""

    def __init__(self) -> None:
        """Initialize the mock backend."""
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate a test image with PIL based on prompt."""
        self.logger.debug(f"Generating test image for prompt: {prompt}, style: {style}")

        # Create base image
        width, height = 512, 512
        background_color = self._get_background_color(prompt)
        img = Image.new("RGB", (width, height), color=background_color)
        draw = ImageDraw.Draw(img)

        # Try to load a font, fall back to default if not available
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 20)
            small_font = ImageFont.truetype("DejaVuSans.ttf", 16)
        except OSError:
            # Fallback to default font
            font = ImageFont.load_default()
            small_font = ImageFont.load_default()

        # Add header with generation info
        draw.rectangle([0, 0, width, 60], fill="rgba(255, 255, 255, 200)")
        draw.text((10, 10), f"Generated: {prompt[:35]}", fill="black", font=font)
        draw.text((10, 35), f"Style: {style}", fill="gray", font=small_font)

        # Add visual elements based on prompt keywords
        self._add_visual_elements(draw, prompt, width, height)

        # Add style effects
        if style == "artistic":
            self._add_artistic_effects(draw, width, height)
        elif style == "photorealistic":
            self._add_realistic_effects(draw, width, height)

        # Convert to bytes
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        """Apply mock transformation to test image."""
        self.logger.debug(f"Transforming image with instruction: {instruction}")

        image_inputs = _normalize_image_references(image_bytes)
        img = Image.open(io.BytesIO(image_inputs[0].content))

        # Add transformation indicator at top
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, img.width, 30], fill="yellow")

        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 16)
        except OSError:
            font = ImageFont.load_default()

        draw.text((5, 5), f"Transformed: {instruction[:40]}", fill="black", font=font)

        # Apply transformation effects based on instruction
        img = self._apply_transformation_effects(img, instruction)

        # Convert to bytes
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    def _get_background_color(self, prompt: str) -> tuple[int, int, int]:
        """Get background color based on prompt keywords."""
        prompt_lower = prompt.lower()

        if any(word in prompt_lower for word in ["sunset", "sunrise", "orange"]):
            return (255, 200, 150)  # Orange-ish
        elif any(word in prompt_lower for word in ["ocean", "sea", "blue", "sky"]):
            return (135, 206, 250)  # Light blue
        elif any(word in prompt_lower for word in ["forest", "green", "nature"]):
            return (144, 238, 144)  # Light green
        elif any(word in prompt_lower for word in ["night", "dark", "space"]):
            return (25, 25, 50)  # Dark blue
        elif any(word in prompt_lower for word in ["desert", "sand", "yellow"]):
            return (255, 218, 185)  # Sandy color
        else:
            return (220, 220, 220)  # Light gray default

    def _add_visual_elements(
        self, draw: ImageDraw.ImageDraw, prompt: str, width: int, height: int
    ) -> None:
        """Add visual elements based on prompt keywords."""
        prompt_lower = prompt.lower()

        # Sun/sunset
        if any(word in prompt_lower for word in ["sun", "sunset", "sunrise"]):
            draw.ellipse(
                [width - 150, 80, width - 50, 180],
                fill="orange",
                outline="red",
                width=2,
            )

        # Mountains
        if "mountain" in prompt_lower:
            points = [
                (100, height - 100),
                (200, height - 250),
                (300, height - 200),
                (400, height - 280),
                (500, height - 100),
            ]
            draw.polygon(points, fill="gray", outline="darkgray")

        # Trees/forest
        if any(word in prompt_lower for word in ["tree", "forest"]):
            for i in range(3):
                x = 150 + i * 100
                y = height - 200
                # Tree trunk
                draw.rectangle([x - 10, y, x + 10, height - 100], fill="brown")
                # Tree top
                draw.ellipse([x - 30, y - 40, x + 30, y + 20], fill="green")

        # City/buildings
        if any(word in prompt_lower for word in ["city", "building", "urban"]):
            for i in range(5):
                x = 50 + i * 80
                building_height = random.randint(100, 200)
                y = height - building_height
                draw.rectangle(
                    [x, y, x + 60, height - 50], fill="darkgray", outline="black"
                )

        # Water/ocean
        if any(word in prompt_lower for word in ["water", "ocean", "sea", "lake"]):
            # Wavy water
            for y in range(height - 150, height - 50, 10):
                for x in range(0, width, 20):
                    wave_x = x + random.randint(-5, 5)
                    draw.arc(
                        [wave_x - 10, y - 5, wave_x + 10, y + 5], 0, 180, fill="blue"
                    )

        # Stars (for night scenes)
        if "night" in prompt_lower or "star" in prompt_lower:
            for _ in range(20):
                x = random.randint(0, width)
                y = random.randint(70, height // 2)
                draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill="white")

    def _add_artistic_effects(
        self, draw: ImageDraw.ImageDraw, width: int, height: int
    ) -> None:
        """Add artistic style effects."""
        # Add some brush stroke-like elements
        for _ in range(10):
            x = random.randint(0, width)
            y = random.randint(70, height)
            color = (
                random.randint(100, 255),
                random.randint(100, 255),
                random.randint(100, 255),
            )
            draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=color)

    def _add_realistic_effects(
        self, draw: ImageDraw.ImageDraw, width: int, height: int
    ) -> None:
        """Add photorealistic style effects."""
        # Add some gradient-like effects with subtle shading
        for i in range(0, width, 50):
            shade = 240 - (i // 50) * 10
            color = (shade, shade, shade)
            draw.line([(i, height - 20), (i, height)], fill=color, width=2)

    def _apply_transformation_effects(
        self, img: Image.Image, instruction: str
    ) -> Image.Image:
        """Apply transformation effects based on instruction."""
        instruction_lower = instruction.lower()

        # Color transformations
        if any(
            phrase in instruction_lower
            for phrase in ["black and white", "grayscale", "monochrome"]
        ):
            img = img.convert("L").convert("RGB")
        elif "sepia" in instruction_lower:
            # Sepia effect using colorize
            img = ImageOps.colorize(
                img.convert("L"), black=(40, 30, 10), white=(230, 180, 100)
            )
        elif any(phrase in instruction_lower for phrase in ["darker", "darken"]):
            img = img.point(lambda p: int(p * 0.7))
        elif any(phrase in instruction_lower for phrase in ["brighter", "brighten"]):
            # Type ignore for PIL lambda function
            img = img.point(lambda p: min(255, int(p * 1.3)))  # type: ignore[arg-type]
        elif "blur" in instruction_lower:
            img = img.filter(ImageFilter.BLUR)

        # Style transformations
        if any(
            phrase in instruction_lower for phrase in ["painting", "artistic", "art"]
        ):
            # Add artistic overlay
            overlay = Image.new("RGBA", img.size, (255, 255, 255, 50))
            draw = ImageDraw.Draw(overlay)
            for _ in range(20):
                x, y = random.randint(0, img.width), random.randint(0, img.height)
                color = (
                    random.randint(100, 255),
                    random.randint(100, 255),
                    random.randint(100, 255),
                    30,
                )
                draw.ellipse([x - 10, y - 10, x + 10, y + 10], fill=color)
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

        # Content modifications (simulated)
        draw = ImageDraw.Draw(img)
        if any(phrase in instruction_lower for phrase in ["remove", "delete"]):
            # Simulate object removal by drawing over with background-like color
            avg_color = self._get_average_color(img)
            draw.ellipse(
                [
                    img.width // 3,
                    img.height // 3,
                    2 * img.width // 3,
                    2 * img.height // 3,
                ],
                fill=avg_color,
                outline=avg_color,
            )
        elif any(phrase in instruction_lower for phrase in ["add", "insert"]):
            # Simulate adding something
            draw.ellipse(
                [
                    img.width // 4,
                    img.height // 4,
                    3 * img.width // 4,
                    3 * img.height // 4,
                ],
                outline="red",
                width=3,
            )
            draw.text((img.width // 2 - 30, img.height // 2), "ADDED", fill="red")

        return img

    def _get_average_color(self, img: Image.Image) -> tuple[int, int, int]:
        """Get average color of image for background matching."""
        img_small = img.resize((1, 1))
        pixel = img_small.getpixel((0, 0))
        # Ensure we return a valid RGB tuple
        if isinstance(pixel, tuple | list) and len(pixel) >= 3:
            return (int(pixel[0]), int(pixel[1]), int(pixel[2]))
        elif isinstance(pixel, int | float):
            # Grayscale image
            gray_val = int(pixel)
            return (gray_val, gray_val, gray_val)
        else:
            # Fallback to gray
            return (128, 128, 128)


class GeminiImageBackend:
    """Gemini API backend for production image generation."""

    DEFAULT_MODEL = "gemini-3-pro-image"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        debug_config: DebugConfig | None = None,
    ) -> None:
        """Initialize the Gemini backend with API key and optional model override.

        ``debug_config`` enables the SDK's record/replay mode for integration
        tests (see tests/integration/test_gemini_image_generation.py).
        """
        if not GENAI_AVAILABLE:
            raise ImportError(
                "google-genai library required for Gemini image generation"
            )

        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.client = genai.Client(api_key=api_key, debug_config=debug_config)
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def _decode_image_bytes(self, image_data: bytes) -> bytes:
        try:
            text_data = image_data.decode("utf-8")
        except UnicodeDecodeError:
            self.logger.info("Image data is raw bytes (not UTF-8)")
            return image_data

        if text_data.startswith(("iVBOR", "/9j/", "R0lG")):
            self.logger.info("Image data is Base64-encoded bytes, decoding")
            return base64.b64decode(text_data)
        self.logger.info("Image data appears to be raw bytes")
        return image_data

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate image using Gemini API."""
        # Build full prompt with style guidance based on Gemini 3 Pro best practices
        full_prompt = prompt
        if style == "photorealistic":
            # Use a more descriptive prompt structure for photorealism
            full_prompt = (
                f"Generate a high-quality, photorealistic image. "
                f"Scene description: {prompt}. "
                f"Ensure realistic lighting, textures, and fine details. "
                f"Captured with a professional camera."
            )
        elif style == "artistic":
            # Encourage creative style and composition
            full_prompt = (
                f"Generate a stylized, artistic illustration. "
                f"Description: {prompt}. "
                f"Focus on creative style, composition, and visual flair."
            )

        self.logger.debug(f"Calling Gemini API with prompt: {full_prompt}")

        # Call Gemini image generation
        response = await instrumented_llm_request(
            provider="google",
            model=self.model,
            operation="image",
            request=lambda: self.client.aio.models.generate_content(
                model=self.model, contents=full_prompt
            ),
            usage=_gemini_image_usage,
            served_model=_served_model,
        )

        # Log response structure for debugging
        self.logger.info(f"Gemini response type: {type(response)}")
        candidate_count = (
            len(response.candidates)
            if hasattr(response, "candidates") and response.candidates
            else 0
        )
        self.logger.info(
            f"Has candidates: {hasattr(response, 'candidates')}, count: {candidate_count}"
        )

        # Extract image data from response
        if hasattr(response, "candidates") and response.candidates:
            candidate = response.candidates[0]
            self.logger.info(f"Candidate has content: {hasattr(candidate, 'content')}")
            if (
                hasattr(candidate, "content")
                and candidate.content
                and candidate.content.parts
            ):
                self.logger.info(f"Content parts count: {len(candidate.content.parts)}")
                # Find the image data following Google's recommended pattern
                for i, part in enumerate(candidate.content.parts):
                    self.logger.info(
                        f"Part {i}: inline_data is not None: {part.inline_data is not None}"
                    )
                    if part.inline_data is not None:
                        # Found image data
                        image_data = part.inline_data.data
                        data_size = (
                            len(image_data)
                            if image_data and hasattr(image_data, "__len__")
                            else "unknown"
                        )
                        self.logger.info(
                            f"inline_data.data type: {type(image_data)}, size: {data_size}"
                        )

                        # Process image data
                        final_image_data = None
                        if isinstance(image_data, str):
                            # Base64 string
                            self.logger.info("Image data is string, decoding as Base64")
                            final_image_data = base64.b64decode(image_data)
                        elif isinstance(image_data, bytes):
                            final_image_data = self._decode_image_bytes(image_data)

                        if final_image_data:
                            self.logger.info(
                                f"Returning image data, size: {len(final_image_data)} bytes"
                            )

                            # Debug: inspect first 100 bytes to understand format
                            data_preview = final_image_data[:100]
                            self.logger.info(
                                f"First 100 bytes (hex): {data_preview.hex()}"
                            )
                            self.logger.info(
                                f"First 20 bytes (ascii): {data_preview[:20]!r}"
                            )

                            # Check for common image file signatures
                            if final_image_data.startswith(b"\x89PNG"):
                                self.logger.info("Data starts with PNG signature")
                            elif final_image_data.startswith(b"\xff\xd8\xff"):
                                self.logger.info("Data starts with JPEG signature")
                            elif final_image_data.startswith(b"GIF"):
                                self.logger.info("Data starts with GIF signature")
                            else:
                                self.logger.warning(
                                    "Data does not start with known image signature"
                                )

                            # Validate with PIL
                            try:
                                img = Image.open(io.BytesIO(final_image_data))
                                self.logger.info(
                                    f"Valid image: format={img.format}, size={img.size}, mode={img.mode}"
                                )
                            except Exception as e:
                                self.logger.error(f"Invalid image data: {e}")
                                # Try to save raw data for analysis
                                try:
                                    await asyncio.to_thread(
                                        Path("/tmp/debug_image_data.bin").write_bytes,
                                        final_image_data,
                                    )
                                    self.logger.info(
                                        "Saved raw data to /tmp/debug_image_data.bin for analysis"
                                    )
                                except Exception as save_error:
                                    self.logger.error(
                                        f"Could not save debug data: {save_error}"
                                    )

                            return final_image_data

        raise ValueError("No image data found in Gemini API response")

    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        """Transform existing image references using Gemini API."""
        self.logger.debug(f"Calling Gemini image edit with instruction: {instruction}")

        image_inputs = _normalize_image_references(image_bytes)
        content_parts: list[object] = [instruction]
        content_parts.extend(
            genai_types.Part.from_bytes(
                data=image_input.content, mime_type=image_input.mime_type
            )
            for image_input in image_inputs
        )
        contents = cast("genai_types.ContentListUnion", content_parts)

        response = await instrumented_llm_request(
            provider="google",
            model=self.model,
            operation="image",
            request=lambda: self.client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"]
                ),
            ),
            usage=_gemini_image_usage,
            served_model=_served_model,
        )

        if hasattr(response, "parts") and response.parts:
            for part in response.parts:
                if part.inline_data is not None:
                    image_data = part.inline_data.data
                    if isinstance(image_data, str):
                        return base64.b64decode(image_data)
                    if isinstance(image_data, bytes):
                        return image_data

        if hasattr(response, "candidates") and response.candidates:
            candidate = response.candidates[0]
            if (
                hasattr(candidate, "content")
                and candidate.content
                and candidate.content.parts
            ):
                for part in candidate.content.parts:
                    if part.inline_data is not None:
                        image_data = part.inline_data.data
                        if isinstance(image_data, str):
                            return base64.b64decode(image_data)
                        if isinstance(image_data, bytes):
                            return image_data

        raise ValueError("No image data found in Gemini edit API response")


class OpenAIImageBackend:
    """OpenAI API backend for image generation."""

    def __init__(
        self,
        api_key: str,
        generate_config: OpenAIImageRequestConfig,
        edit_config: OpenAIImageRequestConfig,
        model: str = "gpt-image-2",
    ) -> None:
        """Initialize the OpenAI backend with API key."""
        if not OPENAI_AVAILABLE:
            raise ImportError("openai library required for OpenAI image generation")

        self.api_key = api_key
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model
        self.generate_config = generate_config
        self.edit_config = edit_config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate image using OpenAI gpt-image-2 API."""
        full_prompt = self._apply_style_to_prompt(prompt, style)

        self.logger.debug(f"Calling OpenAI image API with prompt: {full_prompt}")

        cfg = self.generate_config
        response = await instrumented_llm_request(
            provider="openai",
            model=self.model,
            operation="image",
            request=lambda: self.client.images.generate(
                model=self.model,
                prompt=full_prompt,
                n=1,
                size=cfg.size,
                quality=cfg.quality,
                output_format=cfg.output_format,
                output_compression=cfg.output_compression,
            ),
            usage=_openai_image_usage,
            served_model=_served_model,
        )

        if not response.data:
            raise ValueError("No image data in OpenAI API response")

        b64_data = response.data[0].b64_json
        if not b64_data:
            raise ValueError("No image data in OpenAI API response")

        image_bytes = base64.b64decode(b64_data)

        self.logger.info(f"OpenAI returned image: {len(image_bytes)} bytes")
        if response.usage:
            self.logger.info(
                f"Token usage: input={response.usage.input_tokens}, "
                f"output={response.usage.output_tokens}"
            )

        return image_bytes

    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        """Transform existing image references using the OpenAI edit endpoint."""
        self.logger.debug(f"Calling OpenAI image edit with instruction: {instruction}")

        image_inputs = _normalize_image_references(image_bytes)
        image_files = await asyncio.to_thread(
            _build_openai_edit_files, image_inputs, normalize=False
        )

        try:
            response = await self._request_edit(image_files, instruction)
        except openai.BadRequestError as error:
            if not _is_invalid_image_file_error(error):
                raise
            # The user's own photo is the one image we cannot substitute, so a
            # rejection of its encoding is worth a second call rather than an
            # error the model then works around with some other picture.
            self.logger.warning(
                "OpenAI rejected the uploaded image bytes (%s); "
                "retrying with re-encoded images",
                error,
            )
            normalized_files = await asyncio.to_thread(
                _build_openai_edit_files, image_inputs, normalize=True
            )
            response = await self._request_edit(normalized_files, instruction)

        if not response.data:
            raise ValueError("No image data in OpenAI edit API response")

        b64_data = response.data[0].b64_json
        if not b64_data:
            raise ValueError("No image data in OpenAI edit API response")

        result_bytes = base64.b64decode(b64_data)
        self.logger.info(f"OpenAI edit returned image: {len(result_bytes)} bytes")

        return result_bytes

    async def _request_edit(
        self, image_files: list[io.BytesIO], instruction: str
    ) -> openai.types.ImagesResponse:
        """Call the edit endpoint with already-prepared upload files."""
        # The OpenAI SDK accepts either one image file or an ordered list of files.
        image_request: io.BytesIO | list[io.BytesIO] = (
            image_files[0] if len(image_files) == 1 else image_files
        )
        cfg = self.edit_config
        return await instrumented_llm_request(
            provider="openai",
            model=self.model,
            operation="image",
            request=lambda: self.client.images.edit(
                model=self.model,
                image=cast(
                    "openai._types.FileTypes | list[openai._types.FileTypes]",
                    image_request,
                ),
                prompt=instruction,
                n=1,
                size=cfg.size,
                quality=cfg.quality,
                output_format=cfg.output_format,
                output_compression=cfg.output_compression,
            ),
            usage=_openai_image_usage,
            served_model=_served_model,
        )

    @staticmethod
    def _apply_style_to_prompt(prompt: str, style: str) -> str:
        """Inject style guidance into the prompt text."""
        if style == "photorealistic":
            return (
                f"Generate a high-quality, photorealistic image. "
                f"Scene description: {prompt}. "
                f"Ensure realistic lighting, textures, and fine details."
            )
        elif style == "artistic":
            return (
                f"Generate a stylized, artistic illustration. "
                f"Description: {prompt}. "
                f"Focus on creative style, composition, and visual flair."
            )
        return prompt


class FallbackImageBackend:
    """
    Fallback backend that tries Gemini API first, then falls back to mock.

    This is useful for production deployments where you want to use the real API
    when available but gracefully degrade to mock when there are issues.
    """

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize with optional API key."""
        self.api_key = api_key
        self.gemini_backend = None
        self.mock_backend = MockImageBackend()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        if api_key and GENAI_AVAILABLE:
            try:
                self.gemini_backend = GeminiImageBackend(api_key)
            except Exception as e:
                self.logger.warning(f"Failed to initialize Gemini backend: {e}")

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate image with fallback from Gemini to mock."""
        if self.gemini_backend:
            try:
                self.logger.debug("Attempting Gemini API generation")
                return await self.gemini_backend.generate_image(prompt, style)
            except Exception as e:
                self.logger.warning(f"Gemini API failed: {e}, falling back to mock")

        self.logger.debug("Using mock backend for generation")
        return await self.mock_backend.generate_image(prompt, style)

    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        """Transform image with fallback from Gemini to mock."""
        if self.gemini_backend:
            try:
                self.logger.debug("Attempting Gemini API transformation")
                return await self.gemini_backend.transform_image(
                    image_bytes, instruction
                )
            except Exception as e:
                self.logger.warning(f"Gemini API failed: {e}, falling back to mock")

        self.logger.debug("Using mock backend for transformation")
        return await self.mock_backend.transform_image(image_bytes, instruction)
