"""Unit tests for Google GenAI multimodal function responses."""

from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from google.genai import types

from family_assistant.llm.messages import ToolMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolAttachment


class TestMultimodalFunctionResponses:
    """Tests for multimodal function response handling in GoogleGenAIClient."""

    @pytest.fixture
    def client_v2(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for Gemini 2.0 (no multimodal tool support)."""
        return GoogleGenAIClient(api_key="test_key", model="gemini-2.0-flash-exp")

    @pytest.fixture
    def client_v3(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for Gemini 3.0 (multimodal tool support)."""
        return GoogleGenAIClient(api_key="test_key", model="gemini-3-flash-preview")

    def test_supports_multimodal_tools(
        self, client_v2: GoogleGenAIClient, client_v3: GoogleGenAIClient
    ) -> None:
        """Test detection of multimodal tool support based on model name."""
        assert client_v2._supports_multimodal_tools() is False
        assert client_v3._supports_multimodal_tools() is True

    def test_convert_tool_message_with_attachment_v2(
        self, client_v2: GoogleGenAIClient
    ) -> None:
        """Test conversion of tool message with attachment for Gemini 2.0."""
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake_image_bytes",
            description="A fake image",
        )
        tool_msg = ToolMessage(
            tool_call_id="call_123",
            content="Image generated",
            name="generate_image",
            _attachments=[attachment],
        )

        contents = client_v2._convert_messages_to_genai_format([tool_msg])

        assert len(contents) == 1
        content = cast("types.Content", contents[0])
        assert content.role == "function"
        assert content.parts is not None
        assert len(content.parts) == 1
        part = cast("types.Part", content.parts[0])
        assert part.function_response is not None

        # Verify no multimodal parts are attached to function_response
        fr = part.function_response
        assert fr.parts is None or len(fr.parts) == 0

    def test_convert_tool_message_with_attachment_v3(
        self, client_v3: GoogleGenAIClient
    ) -> None:
        """Test conversion of tool message with attachment for Gemini 3.0."""
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake_image_bytes",
            description="A fake image",
            attachment_id="img_123",
        )
        tool_msg = ToolMessage(
            tool_call_id="call_123",
            content="Image generated",
            name="generate_image",
            _attachments=[attachment],
        )

        contents = client_v3._convert_messages_to_genai_format([tool_msg])

        assert len(contents) == 1
        content = cast("types.Content", contents[0])
        assert content.role == "function"
        assert content.parts is not None
        assert len(content.parts) == 1
        part = cast("types.Part", content.parts[0])

        fr = part.function_response
        assert fr is not None
        assert fr.parts is not None
        assert len(fr.parts) == 1

        fr_part = fr.parts[0]
        # Depending on SDK version, it might be inline_data or file_data
        # Our implementation uses inline_data for content bytes
        assert fr_part.inline_data is not None
        assert fr_part.inline_data.mime_type == "image/png"
        assert fr_part.inline_data.data == b"fake_image_bytes"
        # We used description or attachment_id for display_name
        assert fr_part.inline_data.display_name == "A fake image"

    def test_convert_tool_message_with_multiple_attachments_v3(
        self, client_v3: GoogleGenAIClient
    ) -> None:
        """Test conversion of tool message with multiple attachments for Gemini 3.0."""
        att1 = ToolAttachment(
            mime_type="image/png", content=b"img1", description="Image 1"
        )
        att2 = ToolAttachment(
            mime_type="application/pdf", content=b"pdf1", description="PDF 1"
        )
        tool_msg = ToolMessage(
            tool_call_id="call_multi",
            content="Multiple files",
            name="generate_files",
            _attachments=[att1, att2],
        )

        contents = client_v3._convert_messages_to_genai_format([tool_msg])

        assert len(contents) == 1
        # Need to safely access parts for type checking
        content = cast("types.Content", contents[0])
        assert content.parts is not None
        part = cast("types.Part", content.parts[0])
        fr = part.function_response

        assert fr is not None
        assert fr.parts is not None
        assert len(fr.parts) == 2

        assert fr.parts[0].inline_data is not None
        assert fr.parts[0].inline_data.data == b"img1"
        assert fr.parts[0].inline_data.mime_type == "image/png"

        assert fr.parts[1].inline_data is not None
        assert fr.parts[1].inline_data.data == b"pdf1"
        assert fr.parts[1].inline_data.mime_type == "application/pdf"
