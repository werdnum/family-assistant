"""
Unit tests for LLM provider multimodal message processing.
"""

import base64
from unittest.mock import patch

from family_assistant.llm import BaseLLMClient, LiteLLMClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.tools.types import ToolAttachment


class TestBaseLLMClient:
    """Test BaseLLMClient multimodal functionality"""

    def test_supports_multimodal_tools_default(self) -> None:
        """Test default multimodal support is False"""
        client = BaseLLMClient()
        assert client._supports_multimodal_tools() is False

    def test_create_attachment_injection_default(self) -> None:
        """Test default attachment injection"""
        client = BaseLLMClient()
        attachment = ToolAttachment(
            mime_type="image/png", content=b"fake image data", description="Test image"
        )

        result = client._create_attachment_injection(attachment)

        assert result["role"] == "user"
        assert "Test image" in result["content"]
        assert "File from previous tool response" in result["content"]

    def test_process_tool_messages_no_attachments(self) -> None:
        """Test processing messages without attachments"""
        client = BaseLLMClient()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "tool", "tool_call_id": "123", "content": "Tool result"},
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 3
        assert result == messages

    def test_process_tool_messages_with_attachment_no_native_support(self) -> None:
        """Test processing messages with attachments (no native support)"""
        client = BaseLLMClient()
        attachment = ToolAttachment(
            mime_type="image/png", content=b"fake data", description="Test image"
        )

        messages = [
            {"role": "user", "content": "Process this image"},
            {
                "role": "tool",
                "tool_call_id": "123",
                "content": "Image processed",
                "_attachment": attachment,
            },
        ]

        result = client._process_tool_messages(messages)

        # Should have 3 messages: user, modified tool, injected user
        assert len(result) == 3

        # First message unchanged
        assert result[0] == {"role": "user", "content": "Process this image"}

        # Tool message modified (no _attachment, content updated)
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "123"
        assert "Image processed" in result[1]["content"]
        assert "[File content in following message]" in result[1]["content"]
        assert "_attachment" not in result[1]

        # Injected user message
        assert result[2]["role"] == "user"
        assert "File from previous tool response" in result[2]["content"]

    def test_process_tool_messages_preserves_original(self) -> None:
        """Test that original messages are not modified (no side effects)"""
        client = BaseLLMClient()
        attachment = ToolAttachment(mime_type="text/plain", content=b"data")

        original_messages = [
            {
                "role": "tool",
                "tool_call_id": "123",
                "content": "Original content",
                "_attachment": attachment,
            }
        ]

        # Keep reference to original for comparison
        original_tool_msg = original_messages[0]

        result = client._process_tool_messages(original_messages)

        # Original message should be unchanged
        assert "_attachment" in original_tool_msg
        assert original_tool_msg["content"] == "Original content"

        # Result should be different
        assert len(result) == 2  # Tool + injected user message
        assert "_attachment" not in result[0]


class TestGoogleGenAIClient:
    """Test Google GenAI client multimodal handling"""

    def test_supports_multimodal_tools(self) -> None:
        """Test Gemini doesn't support multimodal tool responses"""
        with patch("family_assistant.llm.providers.google_genai_client.genai"):
            client = GoogleGenAIClient(api_key="test", model="gemini-pro")
            assert client._supports_multimodal_tools() is False

    def test_create_attachment_injection_image(self) -> None:
        """Test Gemini attachment injection for images"""
        with patch("family_assistant.llm.providers.google_genai_client.genai"):
            client = GoogleGenAIClient(api_key="test", model="gemini-pro")

            attachment = ToolAttachment(
                mime_type="image/png", content=b"fake png data", description="Test PNG"
            )

            result = client._create_attachment_injection(attachment)

            assert result["role"] == "user"
            assert "parts" in result
            assert len(result["parts"]) == 2

            # First part should be system message
            assert (
                result["parts"][0]["text"]
                == "[System: File from previous tool response]"
            )

            # Second part should be a types.Part object with inline_data
            part = result["parts"][1]
            assert hasattr(part, "inline_data")
            assert part.inline_data.mime_type == "image/png"
            assert part.inline_data.data == b"fake png data"

    def test_create_attachment_injection_pdf_with_content(self) -> None:
        """Test Gemini attachment injection for PDF content"""
        with patch("family_assistant.llm.providers.google_genai_client.genai"):
            client = GoogleGenAIClient(api_key="test", model="gemini-pro")

            # 1KB of fake PDF data
            fake_data = b"x" * 1024
            attachment = ToolAttachment(
                mime_type="application/pdf",
                content=fake_data,
                description="Test PDF document",
            )

            result = client._create_attachment_injection(attachment)

            assert result["role"] == "user"
            assert len(result["parts"]) == 2

            # First part should be system message
            assert (
                result["parts"][0]["text"]
                == "[System: File from previous tool response]"
            )

            # Second part should be a types.Part object with inline_data for PDF
            part = result["parts"][1]
            assert hasattr(part, "inline_data")
            assert part.inline_data.mime_type == "application/pdf"
            assert part.inline_data.data == fake_data

    def test_create_attachment_injection_non_pdf_binary_content(self) -> None:
        """Test Gemini attachment injection for non-PDF binary content"""
        with patch("family_assistant.llm.providers.google_genai_client.genai"):
            client = GoogleGenAIClient(api_key="test", model="gemini-pro")

            # 1KB of fake ZIP data
            fake_data = b"x" * 1024
            attachment = ToolAttachment(
                mime_type="application/zip",
                content=fake_data,
                description="Test ZIP archive",
            )

            result = client._create_attachment_injection(attachment)

            assert result["role"] == "user"
            assert len(result["parts"]) == 2

            # Should describe the non-PDF binary content
            text_part = result["parts"][1]["text"]
            assert "application/zip" in text_part
            assert "0.0MB" in text_part  # 1KB shows as 0.0MB
            assert "Test ZIP archive" in text_part
            assert "Binary content not accessible" in text_part

    def test_create_attachment_injection_file_path_only(self) -> None:
        """Test Gemini attachment injection with file path only"""
        with patch("family_assistant.llm.providers.google_genai_client.genai"):
            client = GoogleGenAIClient(api_key="test", model="gemini-pro")

            attachment = ToolAttachment(
                mime_type="application/pdf",
                file_path="/path/to/document.pdf",
                description="File reference",
            )

            result = client._create_attachment_injection(attachment)

            assert result["role"] == "user"
            assert len(result["parts"]) == 2

            text_part = result["parts"][1]["text"]
            assert "/path/to/document.pdf" in text_part
            assert "File not found or inaccessible" in text_part


class TestOpenAIClient:
    """Test OpenAI client multimodal handling"""

    def test_supports_multimodal_tools(self) -> None:
        """Test OpenAI doesn't support multimodal tool responses"""
        client = OpenAIClient(api_key="test", model="gpt-4")
        assert client._supports_multimodal_tools() is False

    def test_create_attachment_injection_image(self) -> None:
        """Test OpenAI attachment injection for images"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        attachment = ToolAttachment(
            mime_type="image/jpeg", content=b"fake jpeg data", description="Test JPEG"
        )

        result = client._create_attachment_injection(attachment)

        assert result["role"] == "user"
        assert isinstance(result["content"], list)
        assert len(result["content"]) == 2

        # First part should be system message
        assert result["content"][0]["type"] == "text"
        assert "File from previous tool response" in result["content"][0]["text"]

        # Second part should be image_url
        assert result["content"][1]["type"] == "image_url"
        image_url = result["content"][1]["image_url"]["url"]
        assert image_url.startswith("data:image/jpeg;base64,")

        # Verify base64 decoding works

        b64_data = image_url.split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        assert decoded == b"fake jpeg data"

    def test_create_attachment_injection_pdf_with_content(self) -> None:
        """Test OpenAI attachment injection for PDF content"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        fake_data = b"x" * 2048  # 2KB
        attachment = ToolAttachment(
            mime_type="application/pdf", content=fake_data, description="Test document"
        )

        result = client._create_attachment_injection(attachment)

        assert result["role"] == "user"
        assert len(result["content"]) == 2

        # First part should be system message
        assert result["content"][0]["type"] == "text"
        assert "File from previous tool response" in result["content"][0]["text"]

        # Second part should be text description for PDF (fallback approach)
        text_part = result["content"][1]
        assert text_part["type"] == "text"
        assert "PDF Document" in text_part["text"]
        assert "Test document" in text_part["text"]
        assert "0.0MB" in text_part["text"]
        assert "Content cannot be displayed" in text_part["text"]

    def test_create_attachment_injection_non_pdf_binary_content(self) -> None:
        """Test OpenAI attachment injection for non-PDF binary content"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        fake_data = b"x" * 1024  # 1KB
        attachment = ToolAttachment(
            mime_type="application/zip", content=fake_data, description="Test archive"
        )

        result = client._create_attachment_injection(attachment)

        assert result["role"] == "user"
        assert len(result["content"]) == 2

        # Should have descriptive text for non-PDF binary content
        desc_part = result["content"][1]
        assert desc_part["type"] == "text"
        assert "application/zip" in desc_part["text"]
        assert "0.0MB" in desc_part["text"]  # 1KB shows as 0.0MB
        assert "Test archive" in desc_part["text"]
        assert "Binary content not accessible" in desc_part["text"]

    def test_create_attachment_injection_file_path_only(self) -> None:
        """Test OpenAI attachment injection with file path only"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        attachment = ToolAttachment(
            mime_type="text/plain", file_path="/path/to/file.txt"
        )

        result = client._create_attachment_injection(attachment)

        assert result["role"] == "user"
        assert len(result["content"]) == 2

        file_part = result["content"][1]
        assert file_part["type"] == "text"
        assert "/path/to/file.txt" in file_part["text"]


class TestLiteLLMClient:
    """Test LiteLLMClient multimodal functionality"""

    def test_supports_multimodal_tools_claude_model(self) -> None:
        """Test Claude models support multimodal tools"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")
        assert client._supports_multimodal_tools() is True

    def test_supports_multimodal_tools_non_claude_model(self) -> None:
        """Test non-Claude models don't support multimodal tools"""
        client = LiteLLMClient(model="gpt-4")
        assert client._supports_multimodal_tools() is False

    def test_process_tool_messages_with_image_attachment(self) -> None:
        """Test processing tool messages with image attachments for Claude"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

        fake_image_data = b"fake image data"
        attachment = ToolAttachment(
            mime_type="image/png", content=fake_image_data, description="Test image"
        )

        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "Generated an image",
                "_attachment": attachment,
            }
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert isinstance(result[0]["content"], list)
        assert len(result[0]["content"]) == 2

        # First part should be text
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][0]["text"] == "Generated an image"

        # Second part should be image
        assert result[0]["content"][1]["type"] == "image"
        assert result[0]["content"][1]["source"]["type"] == "base64"
        assert result[0]["content"][1]["source"]["media_type"] == "image/png"

    def test_process_tool_messages_with_pdf_attachment(self) -> None:
        """Test processing tool messages with PDF attachments for Claude"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

        fake_pdf_data = b"fake pdf data"
        attachment = ToolAttachment(
            mime_type="application/pdf", content=fake_pdf_data, description="Test PDF"
        )

        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_456",
                "content": "Retrieved a PDF document",
                "_attachment": attachment,
            }
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert isinstance(result[0]["content"], list)
        assert len(result[0]["content"]) == 2

        # First part should be text
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][0]["text"] == "Retrieved a PDF document"

        # Second part should be document
        assert result[0]["content"][1]["type"] == "document"
        assert result[0]["content"][1]["source"]["type"] == "base64"
        assert result[0]["content"][1]["source"]["media_type"] == "application/pdf"

    def test_process_tool_messages_with_unsupported_content_type(self) -> None:
        """Test processing tool messages with unsupported content types"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

        fake_data = b"fake zip data"
        attachment = ToolAttachment(
            mime_type="application/zip", content=fake_data, description="Test ZIP"
        )

        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_789",
                "content": "Created a ZIP file",
                "_attachment": attachment,
            }
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: the tool message and the injected user message
        assert len(result) == 2
        assert result[0]["role"] == "tool"
        assert result[1]["role"] == "user"  # Injected message
        assert "File from previous tool response" in result[1]["content"]

    def test_process_tool_messages_with_file_path_only_attachment(self) -> None:
        """Test processing tool messages with file-path-only attachments"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

        attachment = ToolAttachment(
            mime_type="application/pdf",
            file_path="/path/to/document.pdf",
            description="External PDF file",
        )

        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_999",
                "content": "Found an external document",
                "_attachment": attachment,
            }
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: the tool message and the injected user message
        assert len(result) == 2
        assert result[0]["role"] == "tool"
        assert result[1]["role"] == "user"  # Injected message
        assert "File from previous tool response" in result[1]["content"]
        assert (
            "External PDF file" in result[1]["content"]
        )  # Uses description, not file_path

    def test_process_tool_messages_falls_back_to_base_class_for_non_claude(
        self,
    ) -> None:
        """Test that non-Claude models fall back to base class behavior"""
        client = LiteLLMClient(model="gpt-4")

        fake_image_data = b"fake image data"
        attachment = ToolAttachment(
            mime_type="image/png", content=fake_image_data, description="Test image"
        )

        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_000",
                "content": "Generated an image",
                "_attachment": attachment,
            }
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: modified tool message + injected user message
        assert len(result) == 2
        assert result[0]["role"] == "tool"
        assert "[File content in following message]" in result[0]["content"]
        assert result[1]["role"] == "user"  # Injected message
