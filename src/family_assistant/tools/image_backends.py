"""
Image generation backend protocols and implementations.

This module defines the protocol for image generation backends and provides
concrete implementations including mock (PIL-based) and Gemini API backends.
"""

import base64
import io
import logging
import random
from abc import abstractmethod
from typing import Protocol, runtime_checkable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Optional import for production use
try:
    from google import genai

    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

logger = logging.getLogger(__name__)


@runtime_checkable
class ImageGenerationBackend(Protocol):
    """Protocol for image generation backends."""

    @abstractmethod
    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate an image from a text prompt."""
        ...

    @abstractmethod
    async def transform_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """Transform an existing image based on text instruction."""
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

    async def transform_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """Apply mock transformation to test image."""
        self.logger.debug(f"Transforming image with instruction: {instruction}")

        img = Image.open(io.BytesIO(image_bytes))

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
            # Simple sepia effect
            img = img.convert("L")
            sepia_img = Image.new("RGB", img.size)
            pixels = list(img.getdata())
            sepia_pixels = [(int(p * 0.9), int(p * 0.7), int(p * 0.4)) for p in pixels]
            sepia_img.putdata(sepia_pixels)
            img = sepia_img
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
        if isinstance(pixel, (tuple, list)) and len(pixel) >= 3:
            return (int(pixel[0]), int(pixel[1]), int(pixel[2]))
        elif isinstance(pixel, (int, float)):
            # Grayscale image
            gray_val = int(pixel)
            return (gray_val, gray_val, gray_val)
        else:
            # Fallback to gray
            return (128, 128, 128)


class GeminiImageBackend:
    """Gemini API backend for production image generation."""

    def __init__(self, api_key: str) -> None:
        """Initialize the Gemini backend with API key."""
        if not GENAI_AVAILABLE:
            raise ImportError(
                "google-genai library required for Gemini image generation"
            )

        self.api_key = api_key
        self.client = genai.Client(api_key=api_key)
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        """Generate image using Gemini API."""
        # Build full prompt with style guidance
        full_prompt = prompt
        if style == "photorealistic":
            full_prompt = f"Create a photorealistic image of {prompt}"
        elif style == "artistic":
            full_prompt = (
                f"Create an artistic image of {prompt} in a painted or stylized manner"
            )

        self.logger.debug(f"Calling Gemini API with prompt: {full_prompt}")

        # Call Gemini image generation
        response = await self.client.aio.models.generate_content(
            model="gemini-2.5-flash-image-preview", contents=full_prompt
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
                            # Could be raw bytes or Base64-encoded bytes
                            # Check if it looks like Base64 by examining the content
                            try:
                                # Try to decode as UTF-8 first to see if it's Base64 text
                                text_data = image_data.decode("utf-8")
                                if text_data.startswith((
                                    "iVBOR",
                                    "/9j/",
                                    "R0lG",
                                )):  # PNG, JPEG, GIF Base64 headers
                                    self.logger.info(
                                        "Image data is Base64-encoded bytes, decoding"
                                    )
                                    final_image_data = base64.b64decode(text_data)
                                else:
                                    self.logger.info(
                                        "Image data appears to be raw bytes"
                                    )
                                    final_image_data = image_data
                            except UnicodeDecodeError:
                                # Not valid UTF-8, assume raw bytes
                                self.logger.info("Image data is raw bytes (not UTF-8)")
                                final_image_data = image_data

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
                                    with open("/tmp/debug_image_data.bin", "wb") as f:
                                        f.write(final_image_data)
                                    self.logger.info(
                                        "Saved raw data to /tmp/debug_image_data.bin for analysis"
                                    )
                                except Exception as save_error:
                                    self.logger.error(
                                        f"Could not save debug data: {save_error}"
                                    )

                            return final_image_data

        raise ValueError("No image data found in Gemini API response")

    async def transform_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """Transform an existing image using Gemini API."""
        # For now, this would require the Gemini edit/transform endpoint
        # which may not be available yet. Fall back to mock for now.
        self.logger.warning(
            "Gemini image transformation not yet implemented, falling back to mock"
        )
        mock_backend = MockImageBackend()
        return await mock_backend.transform_image(image_bytes, instruction)


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

    async def transform_image(self, image_bytes: bytes, instruction: str) -> bytes:
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
