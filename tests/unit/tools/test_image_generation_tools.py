"""Tests for image generation tools."""

import base64
import io
from typing import Literal
from unittest.mock import AsyncMock, Mock, patch

import pytest
from PIL import Image

from family_assistant.config_models import (
    AppConfig,
    OpenAIImageConfig,
    OpenAIImageRequestConfig,
)
from family_assistant.tools.image_backends import (
    MockImageBackend,
    OpenAIImageBackend,
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
        assert "Generated image for: a beautiful landscape" in result.get_text()
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
        assert "Transformed image: make it brighter" in result.get_text()
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
        assert "Could not access the image content" in result.get_text()
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
        assert "Error generating image: Test error" in result.get_text()
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
        assert "Error transforming image: Attachment error" in result.get_text()
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
        assert "Error transforming image: Backend error" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0


def _make_png_b64() -> str:
    """Create a minimal valid PNG and return as base64."""
    img = Image.new("RGB", (64, 64), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _make_app_config(
    *,
    image_generation_backend: Literal["openai", "gemini", "mock"] | None,
    openai_api_key: str | None = None,
    gemini_api_key: str | None = None,
    openai_image: OpenAIImageConfig | None = None,
) -> AppConfig:
    return AppConfig(
        image_generation_backend=image_generation_backend,
        openai_api_key=openai_api_key,
        gemini_api_key=gemini_api_key,
        openai_image=openai_image or OpenAIImageConfig(),
    )


class TestOpenAIImageBackend:
    """Test the OpenAI image backend."""

    @pytest.fixture
    def openai_backend(self) -> OpenAIImageBackend:
        """Create an OpenAI backend with a dummy key."""
        return OpenAIImageBackend(
            api_key="test-key",
            generate_config=OpenAIImageRequestConfig(),
            edit_config=OpenAIImageRequestConfig(),
        )

    @pytest.mark.asyncio
    async def test_generate_image(self, openai_backend: OpenAIImageBackend) -> None:
        """Test image generation calls the OpenAI API correctly."""
        png_b64 = _make_png_b64()
        mock_response = Mock()
        mock_response.data = [Mock(b64_json=png_b64)]
        mock_response.usage = Mock(input_tokens=10, output_tokens=100)

        openai_backend.client.images.generate = AsyncMock(return_value=mock_response)

        result = await openai_backend.generate_image("a red square", "photorealistic")

        assert isinstance(result, bytes)
        assert len(result) > 0
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"

        call_kwargs = openai_backend.client.images.generate.call_args.kwargs
        assert call_kwargs["model"] == "gpt-image-2"
        assert "photorealistic" in call_kwargs["prompt"]
        assert "a red square" in call_kwargs["prompt"]
        assert call_kwargs["quality"] == "high"

    @pytest.mark.asyncio
    async def test_generate_image_auto_style_passes_prompt_through(
        self, openai_backend: OpenAIImageBackend
    ) -> None:
        """Test that auto style passes the prompt through unchanged."""
        png_b64 = _make_png_b64()
        mock_response = Mock()
        mock_response.data = [Mock(b64_json=png_b64)]
        mock_response.usage = None

        openai_backend.client.images.generate = AsyncMock(return_value=mock_response)

        await openai_backend.generate_image("test", "auto")

        call_kwargs = openai_backend.client.images.generate.call_args.kwargs
        assert call_kwargs["prompt"] == "test"

    @pytest.mark.asyncio
    async def test_generate_image_no_data_raises(
        self, openai_backend: OpenAIImageBackend
    ) -> None:
        """Test error when API returns no data."""
        mock_response = Mock()
        mock_response.data = None

        openai_backend.client.images.generate = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.generate_image("test")

    @pytest.mark.asyncio
    async def test_generate_image_no_b64_raises(
        self, openai_backend: OpenAIImageBackend
    ) -> None:
        """Test error when API returns empty b64_json."""
        mock_response = Mock()
        mock_response.data = [Mock(b64_json=None)]

        openai_backend.client.images.generate = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.generate_image("test")

    @pytest.mark.asyncio
    async def test_transform_image(self, openai_backend: OpenAIImageBackend) -> None:
        """Test image transformation calls the edit endpoint."""
        png_b64 = _make_png_b64()
        mock_response = Mock()
        mock_response.data = [Mock(b64_json=png_b64)]

        openai_backend.client.images.edit = AsyncMock(return_value=mock_response)

        # Create source image bytes
        src_img = Image.new("RGB", (64, 64), color=(0, 0, 255))
        buf = io.BytesIO()
        src_img.save(buf, format="PNG")
        src_bytes = buf.getvalue()

        result = await openai_backend.transform_image(src_bytes, "make it red")

        assert isinstance(result, bytes)
        assert len(result) > 0

        call_kwargs = openai_backend.client.images.edit.call_args.kwargs
        assert call_kwargs["model"] == "gpt-image-2"
        assert call_kwargs["prompt"] == "make it red"
        assert call_kwargs["quality"] == "high"
        assert call_kwargs["input_fidelity"] == "high"
        assert call_kwargs["output_format"] == "png"

    @pytest.mark.asyncio
    async def test_transform_image_no_data_raises(
        self, openai_backend: OpenAIImageBackend
    ) -> None:
        """Test error when transform API returns no data."""
        mock_response = Mock()
        mock_response.data = None

        openai_backend.client.images.edit = AsyncMock(return_value=mock_response)

        # Create dummy image bytes
        src_img = Image.new("RGB", (64, 64), color=(0, 0, 255))
        buf = io.BytesIO()
        src_img.save(buf, format="PNG")
        src_bytes = buf.getvalue()

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.transform_image(src_bytes, "make it red")

    @pytest.mark.asyncio
    async def test_transform_image_no_b64_raises(
        self, openai_backend: OpenAIImageBackend
    ) -> None:
        """Test error when transform API returns empty b64_json."""
        mock_response = Mock()
        mock_response.data = [Mock(b64_json=None)]

        openai_backend.client.images.edit = AsyncMock(return_value=mock_response)

        # Create dummy image bytes
        src_img = Image.new("RGB", (64, 64), color=(0, 0, 255))
        buf = io.BytesIO()
        src_img.save(buf, format="PNG")
        src_bytes = buf.getvalue()

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.transform_image(src_bytes, "make it red")

    def test_style_prompt_injection(self) -> None:
        """Test style is injected into prompt text."""
        photo = OpenAIImageBackend._apply_style_to_prompt("a cat", "photorealistic")
        assert "photorealistic" in photo
        assert "a cat" in photo

        artistic = OpenAIImageBackend._apply_style_to_prompt("a cat", "artistic")
        assert "artistic" in artistic
        assert "a cat" in artistic

        auto = OpenAIImageBackend._apply_style_to_prompt("a cat", "auto")
        assert auto == "a cat"


@pytest.mark.asyncio
async def test_openai_image_config_applied_to_backend_selection() -> None:
    """Configured OpenAI image options are forwarded to backend."""
    app_config = _make_app_config(
        image_generation_backend="openai",
        openai_api_key="test-openai-key",
        openai_image=OpenAIImageConfig(
            model="gpt-image-2",
            default_generate=OpenAIImageRequestConfig(
                size="1024x1024",
                quality="medium",
                input_fidelity="low",
                output_format="jpeg",
                output_compression=75,
            ),
            default_edit=OpenAIImageRequestConfig(
                size="auto",
                quality="high",
                input_fidelity="high",
                output_format="png",
                output_compression=None,
            ),
        ),
    )

    context = Mock()
    context.processing_service = Mock()
    context.processing_service.app_config = app_config
    del context.image_backend

    with patch(
        "family_assistant.tools.image_generation.OpenAIImageBackend"
    ) as mock_cls:
        mock_backend = Mock()
        mock_backend.generate_image = AsyncMock(return_value=_make_png_bytes())
        mock_cls.return_value = mock_backend

        await generate_image_tool(context, prompt="test")

        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["generate_config"] == OpenAIImageRequestConfig(
            size="1024x1024",
            quality="medium",
            input_fidelity="low",
            output_format="jpeg",
            output_compression=75,
        )
        assert call_kwargs["edit_config"] == OpenAIImageRequestConfig(
            size="auto",
            quality="high",
            input_fidelity="high",
            output_format="png",
            output_compression=None,
        )


class TestBackendSelection:
    """Test that the correct backend is selected based on configuration."""

    @pytest.mark.asyncio
    async def test_openai_backend_selected(self) -> None:
        """When image_generation_backend is 'openai', OpenAI backend is used."""
        app_config = _make_app_config(
            image_generation_backend="openai",
            openai_api_key="test-openai-key",
        )

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        with patch(
            "family_assistant.tools.image_generation.OpenAIImageBackend"
        ) as mock_cls:
            mock_backend = Mock()
            mock_backend.generate_image = AsyncMock(return_value=_make_png_bytes())
            mock_cls.return_value = mock_backend

            await generate_image_tool(context, prompt="test")
            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs["model"] == "gpt-image-2"
            assert call_kwargs["generate_config"].quality == "high"
            assert call_kwargs["edit_config"].quality == "high"
            assert call_kwargs["edit_config"].input_fidelity == "high"

    @pytest.mark.asyncio
    async def test_gemini_backend_selected(self) -> None:
        """When image_generation_backend is 'gemini', Gemini backend is used."""
        app_config = _make_app_config(
            image_generation_backend="gemini",
            gemini_api_key="test-gemini-key",
        )

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        with patch(
            "family_assistant.tools.image_generation.GeminiImageBackend"
        ) as mock_cls:
            mock_backend = Mock()
            mock_backend.generate_image = AsyncMock(return_value=_make_png_bytes())
            mock_cls.return_value = mock_backend

            await generate_image_tool(context, prompt="test")
            mock_cls.assert_called_once_with("test-gemini-key")

    @pytest.mark.asyncio
    async def test_mock_backend_selected(self) -> None:
        """When image_generation_backend is 'mock', mock backend is used."""
        app_config = _make_app_config(image_generation_backend="mock")

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        result = await generate_image_tool(context, prompt="test landscape")

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_no_backend_configured_autodetects_openai(self) -> None:
        """When image_generation_backend is None and OpenAI key exists, use OpenAI."""
        app_config = _make_app_config(
            image_generation_backend=None,
            openai_api_key="test-key",
        )

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        with patch(
            "family_assistant.tools.image_generation.OpenAIImageBackend"
        ) as mock_cls:
            mock_backend = Mock()
            mock_backend.generate_image = AsyncMock(return_value=_make_png_bytes())
            mock_cls.return_value = mock_backend

            await generate_image_tool(context, prompt="test")
            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs["model"] == "gpt-image-2"
            assert call_kwargs["generate_config"].output_format == "png"
            assert call_kwargs["edit_config"].output_format == "png"

    @pytest.mark.asyncio
    async def test_no_backend_configured_autodetects_gemini(self) -> None:
        """When image_generation_backend is None and Gemini key exists, use Gemini."""
        app_config = _make_app_config(
            image_generation_backend=None,
            gemini_api_key="test-key",
        )

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        with patch(
            "family_assistant.tools.image_generation.GeminiImageBackend"
        ) as mock_cls:
            mock_backend = Mock()
            mock_backend.generate_image = AsyncMock(return_value=_make_png_bytes())
            mock_cls.return_value = mock_backend

            await generate_image_tool(context, prompt="test")
            mock_cls.assert_called_once_with("test-key")

    @pytest.mark.asyncio
    async def test_no_backend_configured_uses_mock(self) -> None:
        """When image_generation_backend is None and no API keys, use mock."""
        app_config = _make_app_config(image_generation_backend=None)

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        result = await generate_image_tool(context, prompt="test landscape")

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_openai_backend_without_key_raises(self) -> None:
        """When backend is 'openai' but no key, error is returned."""
        app_config = _make_app_config(image_generation_backend="openai")

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        result = await generate_image_tool(context, prompt="test")

        assert isinstance(result, ToolResult)
        assert "OPENAI_API_KEY is not set" in result.get_text()

    @pytest.mark.asyncio
    async def test_gemini_backend_without_key_raises(self) -> None:
        """When backend is 'gemini' but no key, error is returned."""
        app_config = _make_app_config(image_generation_backend="gemini")

        context = Mock()
        context.processing_service = Mock()
        context.processing_service.app_config = app_config
        del context.image_backend

        result = await generate_image_tool(context, prompt="test")

        assert isinstance(result, ToolResult)
        assert "GEMINI_API_KEY is not set" in result.get_text()


def _make_png_bytes() -> bytes:
    """Create minimal valid PNG bytes."""
    img = Image.new("RGB", (64, 64), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
