"""Functional tests for image generation tools with real API usage."""

import io
import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from pydantic import SecretStr

from family_assistant.config_models import AppConfig
from family_assistant.tools.image_backends import (
    ImageGenerationBackend,
    MockImageBackend,
)
from family_assistant.tools.image_generation import (
    generate_image_tool,
    transform_image_tool,
)
from family_assistant.tools.types import ToolResult

TRANSIENT_GEMINI_ERROR_STATUSES = ("RESOURCE_EXHAUSTED", "UNAVAILABLE")


@dataclass
class MockProcessingService:
    """Mock processing service for testing."""

    app_config: AppConfig


class MockExecutionContext:
    """Mock execution context for testing."""

    def __init__(
        self,
        app_config: AppConfig | None = None,
        backend: ImageGenerationBackend | None = None,
    ) -> None:
        self.processing_service = MockProcessingService(app_config or AppConfig())
        if backend:
            self.image_backend = backend


class StyleRecordingImageBackend(MockImageBackend):
    """Mock backend that records each generation request it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.generate_requests: list[tuple[str, str]] = []

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        self.generate_requests.append((prompt, style))
        return await super().generate_image(prompt, style)


class QuotaExhaustedImageBackend(MockImageBackend):
    """Backend whose generation fails the way an exhausted provider quota does."""

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        raise RuntimeError(f"429 RESOURCE_EXHAUSTED. quota exceeded for {prompt!r}")


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"),
    reason="No Google API key found - skipping functional tests",
)
@pytest.mark.gemini_live
@pytest.mark.flaky(
    reruns=2,
    reason="Gemini API may return valid responses without expected inline_data due to rate limiting or transient service issues",
)
@pytest.mark.asyncio
async def test_generate_image_with_real_api() -> None:
    """Test image generation with real Gemini API (requires API key)."""
    # Get API key from environment
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    # Create mock context with real API configuration
    mock_context = MockExecutionContext(
        AppConfig(gemini_api_key=SecretStr(api_key) if api_key else None)
    )

    # Test image generation
    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]
        prompt="a simple red circle on a white background",
        style="auto",
    )

    text = result.get_text()
    if text.startswith("Error generating image:") and any(
        status in text for status in TRANSIENT_GEMINI_ERROR_STATUSES
    ):
        pytest.skip(f"Gemini image generation temporarily unavailable: {text}")

    assert text == "Generated image for: a simple red circle on a white background"
    assert result.attachments
    attachment = result.attachments[0]
    assert attachment.mime_type == "image/png"
    assert attachment.content is not None
    with Image.open(io.BytesIO(attachment.content)) as img:
        assert img.width > 0
        assert img.height > 0


@pytest.mark.asyncio
async def test_generate_image_mock_mode() -> None:
    """Test image generation in mock mode (always works)."""
    # Create mock context with injected mock backend
    mock_backend = MockImageBackend()
    mock_context = MockExecutionContext(backend=mock_backend)

    # Test image generation
    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]
        prompt="a beautiful sunset over mountains",
        style="photorealistic",
    )

    # Verify result
    assert isinstance(result, ToolResult)
    assert "Generated image for:" in result.get_text()
    assert result.attachments and len(result.attachments) > 0
    assert result.attachments[0].mime_type == "image/png"
    assert result.attachments[0].content is not None
    assert len(result.attachments[0].content) > 1000


@pytest.mark.asyncio
async def test_generate_image_no_api_key() -> None:
    """Test image generation without API key (should fall back to mock)."""
    # Create mock context without API key - should auto-create mock backend
    mock_context = MockExecutionContext()

    # Test image generation
    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]
        prompt="a test image",
        style="auto",
    )

    # Verify result (should work in mock mode)
    assert isinstance(result, ToolResult)
    assert "Generated image for:" in result.get_text()
    assert result.attachments and len(result.attachments) > 0


@pytest.mark.asyncio
async def test_transform_image_mock_mode() -> None:
    """Test image transformation in mock mode."""

    # Create mock context with injected mock backend
    mock_backend = MockImageBackend()
    mock_context = MockExecutionContext(backend=mock_backend)

    # Create mock attachment with test image
    mock_attachment = AsyncMock()
    mock_attachment.get_id = Mock(return_value="test-attachment-id")
    mock_attachment.get_description = Mock(return_value="Test image")
    mock_attachment.get_mime_type = Mock(return_value="image/png")

    # Generate test image content using mock backend
    test_image_bytes = await mock_backend.generate_image("original test image", "auto")
    mock_attachment.get_content_async.return_value = test_image_bytes

    # Test image transformation
    result = await transform_image_tool(
        mock_context,  # type: ignore[arg-type]
        image=mock_attachment,
        instruction="make it black and white",
    )

    # Verify result
    assert isinstance(result, ToolResult)
    assert "Transformed image:" in result.get_text()
    assert result.attachments and len(result.attachments) > 0
    assert result.attachments[0].mime_type == "image/png"
    assert result.attachments[0].content is not None
    assert len(result.attachments[0].content) > 1000

    # Verify attachment was accessed
    mock_attachment.get_content_async.assert_called_once()


@pytest.mark.parametrize("style", ["auto", "photorealistic", "artistic"])
@pytest.mark.asyncio
async def test_generate_image_forwards_style_to_backend(style: str) -> None:
    """The requested style reaches the backend alongside the prompt."""
    backend = StyleRecordingImageBackend()
    mock_context = MockExecutionContext(backend=backend)

    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]
        prompt="a landscape with mountains",
        style=style,
    )

    assert backend.generate_requests == [("a landscape with mountains", style)]
    assert result.get_text() == "Generated image for: a landscape with mountains"
    assert result.attachments


@pytest.mark.asyncio
async def test_generate_image_reports_backend_failure_without_attachment() -> None:
    """A backend failure becomes an error result naming the cause, with no image."""
    mock_context = MockExecutionContext(backend=QuotaExhaustedImageBackend())

    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]
        prompt="a red circle",
        style="auto",
    )

    assert result.get_text() == (
        "Error generating image: 429 RESOURCE_EXHAUSTED. "
        "quota exceeded for 'a red circle'"
    )
    assert not result.attachments
