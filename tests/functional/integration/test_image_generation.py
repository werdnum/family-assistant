"""Functional tests for image generation tools with real API usage."""

import io
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiofiles
import pytest
from PIL import Image
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.image_backends import (
    ImageGenerationBackend,
    ImageReference,
    ImageReferenceInputs,
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


class TransformRecordingImageBackend(MockImageBackend):
    """Mock backend that records the image references each transform receives."""

    def __init__(self) -> None:
        super().__init__()
        self.transform_inputs: list[ImageReferenceInputs] = []

    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        self.transform_inputs.append(image_bytes)
        return await super().transform_image(image_bytes, instruction)


async def _stored_image_attachment(
    db_engine: AsyncEngine, storage_root: Path, image_bytes: bytes
) -> ScriptAttachment:
    """Store ``image_bytes`` as a PNG attachment and wrap it the way scripts see it."""
    db_context = Database(db_engine)
    registry = AttachmentRegistry(
        storage_path=str(storage_root), db_engine=db_engine, config=None
    )
    attachment_id = str(uuid.uuid4())
    storage_dir = storage_root / attachment_id[:2]
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{attachment_id}.png"
    async with aiofiles.open(storage_path, "wb") as f:
        await f.write(image_bytes)
    await registry.register_attachment(
        db_context=db_context,
        attachment_id=attachment_id,
        source_type="user",
        source_id="test_user",
        mime_type="image/png",
        description="Test image",
        size=len(image_bytes),
        storage_path=str(storage_path),
        conversation_id="test_conversation",
    )
    metadata = await registry.get_attachment(
        db_context, attachment_id, acting_user_id=None
    )
    assert metadata is not None
    return ScriptAttachment(
        metadata=metadata, registry=registry, db_context_getter=lambda: db_context
    )


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
async def test_transform_image_mock_mode(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Test image transformation in mock mode."""

    mock_backend = TransformRecordingImageBackend()
    mock_context = MockExecutionContext(backend=mock_backend)

    test_image_bytes = await mock_backend.generate_image("original test image", "auto")
    attachment = await _stored_image_attachment(db_engine, tmp_path, test_image_bytes)

    # Test image transformation
    result = await transform_image_tool(
        mock_context,  # type: ignore[arg-type]
        image=attachment,
        instruction="make it black and white",
    )

    # Verify result
    assert isinstance(result, ToolResult)
    assert "Transformed image:" in result.get_text()
    assert result.attachments and len(result.attachments) > 0
    assert result.attachments[0].mime_type == "image/png"
    assert result.attachments[0].content is not None
    assert len(result.attachments[0].content) > 1000

    # The stored attachment's content is what reached the backend
    assert len(mock_backend.transform_inputs) == 1
    transform_input = mock_backend.transform_inputs[0]
    assert isinstance(transform_input, list)
    [reference] = transform_input
    assert isinstance(reference, ImageReference)
    assert reference.content == test_image_bytes
    assert reference.mime_type == "image/png"


@pytest.mark.parametrize("style", ["auto", "photorealistic", "artistic"])
@pytest.mark.asyncio
async def test_generate_image_forwards_style_to_backend(style: str) -> None:
    """The requested style reaches the backend alongside the prompt."""
    backend = StyleRecordingImageBackend()
    mock_context = MockExecutionContext(backend=backend)

    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]  # Image tool only needs the injected backend; this fake omits unrelated context fields.
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
        mock_context,  # type: ignore[arg-type]  # Image tool only needs the injected backend; this fake omits unrelated context fields.
        prompt="a red circle",
        style="auto",
    )

    assert result.get_text() == (
        "Error generating image: 429 RESOURCE_EXHAUSTED. "
        "quota exceeded for 'a red circle'"
    )
    assert not result.attachments
