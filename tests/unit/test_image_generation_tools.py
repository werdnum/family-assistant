"""Tests for image generation tools."""

import io
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from family_assistant.tools.image_backends import (
    MockImageBackend,
)
from family_assistant.tools.image_generation import (
    generate_image_tool,
    transform_image_tool,
)
from family_assistant.tools.types import ToolAttachment, ToolResult


class TestMockImageBackend:
    """Test the mock image backend."""

    @pytest.fixture
    def mock_backend(self) -> MockImageBackend:
        """Create a mock backend instance."""
        return MockImageBackend()

    @pytest.mark.asyncio
    async def test_generate_image_basic(self, mock_backend: MockImageBackend) -> None:
        """Test basic image generation."""
        image_bytes = await mock_backend.generate_image("test prompt", "auto")

        # Verify we got valid PNG bytes
        assert isinstance(image_bytes, bytes)
        assert len(image_bytes) > 0

        # Verify it's a valid image
        img = Image.open(io.BytesIO(image_bytes))
        assert img.format == "PNG"
        assert img.size == (512, 512)

    @pytest.mark.asyncio
    async def test_generate_image_with_keywords(
        self, mock_backend: MockImageBackend
    ) -> None:
        """Test image generation with various keywords."""
        # Test sunset
        sunset_bytes = await mock_backend.generate_image(
            "sunset over mountains", "photorealistic"
        )
        assert len(sunset_bytes) > 0

        # Test city
        city_bytes = await mock_backend.generate_image(
            "cyberpunk city at night", "artistic"
        )
        assert len(city_bytes) > 0

        # Verify different prompts produce different images
        assert sunset_bytes != city_bytes

    @pytest.mark.asyncio
    async def test_transform_image_basic(self, mock_backend: MockImageBackend) -> None:
        """Test basic image transformation."""
        # Create a test image first
        original_bytes = await mock_backend.generate_image("test image", "auto")

        # Transform it
        transformed_bytes = await mock_backend.transform_image(
            original_bytes, "make it darker"
        )

        # Verify transformation
        assert isinstance(transformed_bytes, bytes)
        assert len(transformed_bytes) > 0
        assert transformed_bytes != original_bytes  # Should be different

    @pytest.mark.asyncio
    async def test_transform_image_grayscale(
        self, mock_backend: MockImageBackend
    ) -> None:
        """Test grayscale transformation."""
        original_bytes = await mock_backend.generate_image("colorful sunset", "auto")
        transformed_bytes = await mock_backend.transform_image(
            original_bytes, "convert to black and white"
        )

        # Load both images
        Image.open(io.BytesIO(original_bytes))
        Image.open(io.BytesIO(transformed_bytes))

        # Verify transformation applied (can't easily test exact grayscale, but should be different)
        assert transformed_bytes != original_bytes

    @pytest.mark.asyncio
    async def test_transform_image_blur(self, mock_backend: MockImageBackend) -> None:
        """Test blur transformation."""
        original_bytes = await mock_backend.generate_image("sharp image", "auto")
        transformed_bytes = await mock_backend.transform_image(
            original_bytes, "add blur effect"
        )

        assert len(transformed_bytes) > 0
        assert transformed_bytes != original_bytes


class TestImageGenerationTools:
    """Test the actual tool functions."""

    @pytest.fixture
    def mock_exec_context(self) -> Mock:
        """Create a mock execution context with injected mock backend."""
        context = Mock()
        context.image_backend = MockImageBackend()
        context.processing_service = None  # Not needed when backend is injected
        return context

    @pytest.fixture
    async def mock_script_attachment(self) -> AsyncMock:
        """Create a mock ScriptAttachment."""
        attachment = AsyncMock()
        attachment.get_id.return_value = "test-attachment-id"
        attachment.get_description.return_value = "Test image"
        attachment.get_mime_type.return_value = "image/png"

        # Create test image content using mock backend
        mock_backend = MockImageBackend()
        test_image_bytes = await mock_backend.generate_image(
            "test attachment image", "auto"
        )
        attachment.get_content_async.return_value = test_image_bytes

        return attachment

    @pytest.mark.asyncio
    async def test_generate_image_tool_basic(self, mock_exec_context: Mock) -> None:
        """Test basic image generation."""
        result = await generate_image_tool(
            mock_exec_context, prompt="a beautiful landscape", style="auto"
        )

        assert isinstance(result, ToolResult)
        assert "Generated image for: a beautiful landscape" in result.text
        assert result.attachments and len(result.attachments) > 0
        assert isinstance(result.attachments[0], ToolAttachment)
        assert result.attachments[0].mime_type == "image/png"
        assert result.attachments[0].content is not None
        assert len(result.attachments[0].content) > 0

    @pytest.mark.asyncio
    async def test_generate_image_tool_photorealistic(
        self, mock_exec_context: Mock
    ) -> None:
        """Test photorealistic image generation."""
        result = await generate_image_tool(
            mock_exec_context, prompt="a city skyline", style="photorealistic"
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        assert "Generated image: a city skyline" in result.attachments[0].description

    @pytest.mark.asyncio
    async def test_generate_image_tool_artistic(self, mock_exec_context: Mock) -> None:
        """Test artistic image generation."""
        result = await generate_image_tool(
            mock_exec_context, prompt="abstract art", style="artistic"
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_generate_image_tool_long_prompt(
        self, mock_exec_context: Mock
    ) -> None:
        """Test generation with a long prompt."""
        long_prompt = "a very detailed and elaborate description of a fantasy landscape with mountains, rivers, castles, and magical creatures"

        result = await generate_image_tool(
            mock_exec_context, prompt=long_prompt, style="auto"
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        # Description should be truncated with ellipsis
        assert (
            len(result.attachments[0].description) <= 70
        )  # "Generated image: " + truncated prompt + "..."

    @pytest.mark.asyncio
    async def test_transform_image_tool_basic(
        self, mock_exec_context: Mock, mock_script_attachment: AsyncMock
    ) -> None:
        """Test basic image transformation."""
        result = await transform_image_tool(
            mock_exec_context,
            image=mock_script_attachment,
            instruction="make it brighter",
        )

        assert isinstance(result, ToolResult)
        assert "Transformed image: make it brighter" in result.text
        assert result.attachments and len(result.attachments) > 0
        assert isinstance(result.attachments[0], ToolAttachment)
        assert result.attachments[0].mime_type == "image/png"
        assert result.attachments[0].content is not None

        # Verify attachment was accessed
        mock_script_attachment.get_content_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_transform_image_tool_grayscale(
        self, mock_exec_context: Mock, mock_script_attachment: AsyncMock
    ) -> None:
        """Test grayscale transformation."""
        result = await transform_image_tool(
            mock_exec_context,
            image=mock_script_attachment,
            instruction="convert to black and white",
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        assert (
            "Transformed: convert to black and white"
            in result.attachments[0].description
        )

    @pytest.mark.asyncio
    async def test_transform_image_tool_remove_object(
        self, mock_exec_context: Mock, mock_script_attachment: AsyncMock
    ) -> None:
        """Test object removal transformation."""
        result = await transform_image_tool(
            mock_exec_context,
            image=mock_script_attachment,
            instruction="remove the car from the image",
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_add_object(
        self, mock_exec_context: Mock, mock_script_attachment: AsyncMock
    ) -> None:
        """Test object addition transformation."""
        result = await transform_image_tool(
            mock_exec_context,
            image=mock_script_attachment,
            instruction="add clouds to the sky",
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_style_change(
        self, mock_exec_context: Mock, mock_script_attachment: AsyncMock
    ) -> None:
        """Test style transformation."""
        result = await transform_image_tool(
            mock_exec_context,
            image=mock_script_attachment,
            instruction="make it look like a watercolor painting",
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_no_content(
        self, mock_exec_context: Mock
    ) -> None:
        """Test transformation when attachment has no content."""
        attachment = AsyncMock()
        attachment.get_content_async.return_value = None
        attachment.get_id.return_value = "empty-attachment"

        result = await transform_image_tool(
            mock_exec_context, image=attachment, instruction="transform this"
        )

        assert isinstance(result, ToolResult)
        assert "Could not access the image content" in result.text
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_long_instruction(
        self, mock_exec_context: Mock, mock_script_attachment: AsyncMock
    ) -> None:
        """Test transformation with long instruction."""
        long_instruction = "transform this image into a magnificent fantasy landscape with dragons, castles, magic, and beautiful colors"

        result = await transform_image_tool(
            mock_exec_context,
            image=mock_script_attachment,
            instruction=long_instruction,
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        # Description should be truncated
        assert (
            len(result.attachments[0].description) <= 70
        )  # "Transformed: " + truncated instruction + "..."

    @pytest.mark.asyncio
    async def test_generate_image_tool_error_handling(
        self, mock_exec_context: Mock
    ) -> None:
        """Test error handling in image generation."""
        # Mock an exception during image generation
        mock_backend = Mock()
        mock_backend.generate_image = AsyncMock(side_effect=Exception("Test error"))
        mock_exec_context.image_backend = mock_backend

        result = await generate_image_tool(
            mock_exec_context, prompt="test prompt", style="auto"
        )

        assert isinstance(result, ToolResult)
        assert "Error generating image: Test error" in result.text
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_error_handling(
        self, mock_exec_context: Mock
    ) -> None:
        """Test error handling in image transformation."""
        # Mock an exception during attachment access
        attachment = AsyncMock()
        attachment.get_content_async.side_effect = Exception("Attachment error")
        attachment.get_id.return_value = "error-attachment"

        result = await transform_image_tool(
            mock_exec_context, image=attachment, instruction="transform this"
        )

        assert isinstance(result, ToolResult)
        assert "Error transforming image: Attachment error" in result.text
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_backend_error_handling(
        self, mock_exec_context: Mock
    ) -> None:
        """Test error handling in backend transformation."""
        # Mock backend that throws during transformation
        mock_backend = Mock()
        mock_backend.transform_image = AsyncMock(side_effect=Exception("Backend error"))
        mock_exec_context.image_backend = mock_backend

        # Mock attachment that works
        attachment = AsyncMock()
        attachment.get_content_async.return_value = b"fake image content"
        attachment.get_id.return_value = "test-attachment"

        result = await transform_image_tool(
            mock_exec_context, image=attachment, instruction="transform this"
        )

        assert isinstance(result, ToolResult)
        assert "Error transforming image: Backend error" in result.text
        assert not result.attachments or len(result.attachments) == 0
