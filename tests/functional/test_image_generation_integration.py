"""Functional tests for image generation tools with real API usage."""

import os
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from family_assistant.tools.image_backends import (
    ImageGenerationBackend,
    MockImageBackend,
)
from family_assistant.tools.image_generation import (
    generate_image_tool,
    transform_image_tool,
)
from family_assistant.tools.types import ToolAttachment, ToolResult


@dataclass
class MockProcessingService:
    """Mock processing service for testing."""

    app_config: dict


class MockExecutionContext:
    """Mock execution context for testing."""

    def __init__(
        self, config: dict, backend: ImageGenerationBackend | None = None
    ) -> None:
        self.processing_service = MockProcessingService(config)
        if backend:
            self.image_backend = backend


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"),
    reason="No Google API key found - skipping functional tests",
)
@pytest.mark.gemini_live
@pytest.mark.asyncio
async def test_generate_image_with_real_api() -> None:
    """Test image generation with real Gemini API (requires API key).

    Known flaky test: This test can intermittently fail with "No image data found in
    Gemini API response". The Gemini API may return valid responses but without the
    expected inline_data, possibly due to API rate limiting or transient service issues.
    If this test fails, rerun it individually to verify it's not a regression.
    """
    # Get API key from environment
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    # Create mock context with real API configuration
    config = {"use_mock_images": False, "google_api_key": api_key}
    mock_context = MockExecutionContext(config)

    # Test image generation
    result = await generate_image_tool(
        mock_context,  # type: ignore[arg-type]
        prompt="a simple red circle on a white background",
        style="auto",
    )

    if result.get_text().startswith("Error generating image:"):
        pytest.skip("Gemini image generation unavailable in test environment")

    # Verify result
    assert isinstance(result, ToolResult)
    assert "Generated image for:" in result.get_text()
    assert result.attachments and len(result.attachments) > 0
    assert isinstance(result.attachments[0], ToolAttachment)
    assert result.attachments[0].mime_type == "image/png"
    assert result.attachments[0].content is not None
    assert len(result.attachments[0].content) > 1000  # Should be a real image


@pytest.mark.asyncio
async def test_generate_image_mock_mode() -> None:
    """Test image generation in mock mode (always works)."""
    # Create mock context with injected mock backend
    config = {}
    mock_backend = MockImageBackend()
    mock_context = MockExecutionContext(config, backend=mock_backend)

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
    config = {}
    mock_context = MockExecutionContext(config)

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
    config = {}
    mock_backend = MockImageBackend()
    mock_context = MockExecutionContext(config, backend=mock_backend)

    # Create mock attachment with test image
    mock_attachment = AsyncMock()
    mock_attachment.get_id.return_value = "test-attachment-id"
    mock_attachment.get_description.return_value = "Test image"
    mock_attachment.get_mime_type.return_value = "image/png"

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


@pytest.mark.asyncio
async def test_various_styles() -> None:
    """Test different image generation styles."""
    config = {}
    mock_backend = MockImageBackend()
    mock_context = MockExecutionContext(config, backend=mock_backend)

    styles = ["auto", "photorealistic", "artistic"]
    prompts = [
        "a simple geometric shape",
        "a landscape with mountains",
        "abstract art with flowing lines",
    ]

    for style in styles:
        for prompt in prompts:
            result = await generate_image_tool(mock_context, prompt=prompt, style=style)  # type: ignore[arg-type]

            # Verify each result
            assert isinstance(result, ToolResult)
            assert result.attachments and len(result.attachments) > 0
            assert result.attachments[0].content is not None
            assert len(result.attachments[0].content) > 1000
            assert prompt in result.get_text()


@pytest.mark.asyncio
async def test_error_handling() -> None:
    """Test error handling scenarios."""
    config = {}
    mock_backend = MockImageBackend()
    mock_context = MockExecutionContext(config, backend=mock_backend)

    # Test with very long prompt
    long_prompt = "a" * 1000  # Very long prompt
    result = await generate_image_tool(mock_context, prompt=long_prompt, style="auto")  # type: ignore[arg-type]

    # Should still work
    assert isinstance(result, ToolResult)
    assert result.attachments and len(result.attachments) > 0

    # Test with empty prompt (edge case)
    result = await generate_image_tool(mock_context, prompt="", style="auto")  # type: ignore[arg-type]

    # Should still work
    assert isinstance(result, ToolResult)
    assert result.attachments and len(result.attachments) > 0


if __name__ == "__main__":
    """Run functional tests manually."""
    import asyncio

    async def run_manual_tests() -> None:
        """Run tests manually for development."""
        print("Running functional image generation tests...")

        # Test mock mode (always works)
        print("\n1. Testing mock mode...")
        await test_generate_image_mock_mode()
        print("✓ Mock mode test passed")

        # Test no API key scenario
        print("\n2. Testing without API key...")
        await test_generate_image_no_api_key()
        print("✓ No API key test passed")

        # Test image transformation
        print("\n3. Testing image transformation...")
        await test_transform_image_mock_mode()
        print("✓ Image transformation test passed")

        # Test various styles
        print("\n4. Testing various styles...")
        await test_various_styles()
        print("✓ Various styles test passed")

        # Test error handling
        print("\n5. Testing error handling...")
        await test_error_handling()
        print("✓ Error handling test passed")

        # Test with real API if available
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if api_key:
            print(f"\n6. Testing with real API (key found: {api_key[:10]}...)...")
            try:
                await test_generate_image_with_real_api()
                print("✓ Real API test passed")
            except Exception as e:
                print(f"✗ Real API test failed: {e}")
        else:
            print(
                "\n6. Skipping real API test (no GEMINI_API_KEY or GOOGLE_API_KEY found)"
            )

        print("\nAll functional tests completed!")

    # Run the tests
    asyncio.run(run_manual_tests())
