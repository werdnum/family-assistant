"""
Unit tests for LLM provider multimodal message processing.
"""

import base64
from typing import Any, cast
from unittest.mock import patch

import pytest

from family_assistant.llm import BaseLLMClient
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    AssistantMessage,
    ImageUrlContentPart,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
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

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert "Test image" in result.content
        assert "File from previous tool response" in result.content

    def test_process_tool_messages_no_attachments(self) -> None:
        """Test processing messages without attachments"""
        client = BaseLLMClient()
        messages = [
            UserMessage(content="Hello"),
            AssistantMessage(content="Hi there"),
            ToolMessage(tool_call_id="123", content="Tool result", name="test_tool"),
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 3
        assert isinstance(result[0], UserMessage)
        assert result[0].content == "Hello"
        assert isinstance(result[1], AssistantMessage)
        assert result[1].content == "Hi there"
        assert isinstance(result[2], ToolMessage)
        assert result[2].content == "Tool result"

    def test_process_tool_messages_with_attachment_no_native_support(self) -> None:
        """Test processing messages with attachments (no native support)"""
        client = BaseLLMClient()
        attachment = ToolAttachment(
            mime_type="image/png", content=b"fake data", description="Test image"
        )

        messages = [
            UserMessage(content="Process this image"),
            ToolMessage(
                tool_call_id="123",
                content="Image processed",
                name="test_tool",
                _attachments=[attachment],
            ),
        ]

        result = client._process_tool_messages(messages)

        # Should have 3 messages: user, modified tool, injected user
        assert len(result) == 3

        # First message unchanged
        assert isinstance(result[0], UserMessage)
        assert result[0].content == "Process this image"

        # Tool message modified (no transient_attachments, content updated)
        assert isinstance(result[1], ToolMessage)
        assert result[1].tool_call_id == "123"
        assert "Image processed" in result[1].content
        assert "[File content in following message]" in result[1].content
        assert (
            not hasattr(result[1], "transient_attachments")
            or result[1].transient_attachments is None
        )

        # Injected user message
        assert isinstance(result[2], UserMessage)
        assert "File from previous tool response" in result[2].content

    def test_process_tool_messages_preserves_original(self) -> None:
        """Test that original messages are not modified (no side effects)"""
        client = BaseLLMClient()
        attachment = ToolAttachment(mime_type="text/plain", content=b"data")

        original_tool_msg = ToolMessage(
            tool_call_id="123",
            content="Original content",
            name="test_tool",
            _attachments=[attachment],
        )
        original_messages = [original_tool_msg]

        result = client._process_tool_messages(cast("list[Any]", original_messages))

        # Original message should be unchanged
        assert original_tool_msg.transient_attachments is not None
        assert original_tool_msg.content == "Original content"

        # Result should be different
        assert len(result) == 2  # Tool + injected user message
        assert isinstance(result[0], ToolMessage)
        assert result[0].transient_attachments is None


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

            result = client.create_attachment_injection(attachment)

            assert result.role == "user"
            assert isinstance(result.content, list)
            assert len(result.content) == 2

            prelude = result.content[0]
            assert isinstance(prelude, TextContentPart)
            assert prelude.text == "[System: File from previous tool response]"

            media = result.content[1]
            assert isinstance(media, ImageUrlContentPart)
            assert media.image_url["url"] == (
                "data:image/png;base64," + base64.b64encode(b"fake png data").decode()
            )

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

            result = client.create_attachment_injection(attachment)

            assert result.role == "user"
            assert isinstance(result.content, list)
            assert len(result.content) == 2

            # A PDF may be dropped by whichever adapter renders this next, so the
            # prelude describes it rather than staying generic.
            prelude = result.content[0]
            assert isinstance(prelude, TextContentPart)
            assert "application/pdf" in prelude.text

            media = result.content[1]
            assert isinstance(media, ImageUrlContentPart)
            assert media.image_url["url"] == (
                "data:application/pdf;base64," + base64.b64encode(fake_data).decode()
            )

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

            result = client.create_attachment_injection(attachment)

            assert result.role == "user"
            assert isinstance(result.content, list)
            assert len(result.content) == 2

            # Should describe the non-PDF binary content
            described = result.content[1]
            assert isinstance(described, TextContentPart)
            text_part = described.text
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

            result = client.create_attachment_injection(attachment)

            assert result.role == "user"
            assert isinstance(result.content, list)
            assert len(result.content) == 2

            described = result.content[1]
            assert isinstance(described, TextContentPart)
            text_part = described.text
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

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, list)
        assert len(result.content) == 2

        # First part should be system message
        assert result.content[0].type == "text"
        assert "File from previous tool response" in result.content[0].text

        # Second part should be image_url
        assert result.content[1].type == "image_url"
        image_url = result.content[1].image_url["url"]
        assert image_url.startswith("data:image/jpeg;base64,")

        # Verify base64 decoding works

        b64_data = image_url.split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        assert decoded == b"fake jpeg data"

    def test_create_attachment_injection_pdf_with_content(self) -> None:
        """The Responses API reads PDFs, so the bytes are kept rather than described.

        Web chat routes every non-image upload through this path, so describing a
        PDF as `[PDF Document: ...]` here discarded a document the model could
        have read. The description is still correct on a Chat Completions
        endpoint, which has no file input -- covered separately.
        """
        client = OpenAIClient(api_key="test", model="gpt-4")

        fake_data = b"x" * 2048  # 2KB
        attachment = ToolAttachment(
            mime_type="application/pdf",
            content=fake_data,
            attachment_id="att-pdf",
            description="Test document",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, list)
        assert len(result.content) == 2

        # First part should be system message
        assert result.content[0].type == "text"
        assert "File from previous tool response" in result.content[0].text

        file_part = result.content[1]
        assert file_part.type == "image_url"
        assert file_part.image_url["url"].startswith("data:application/pdf;base64,")
        assert file_part.attachment_id == "att-pdf"

    def test_create_attachment_injection_non_pdf_binary_content(self) -> None:
        """Test OpenAI attachment injection for non-PDF binary content"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        fake_data = b"x" * 1024  # 1KB
        attachment = ToolAttachment(
            mime_type="application/zip", content=fake_data, description="Test archive"
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, list)
        assert len(result.content) == 2

        # Should have descriptive text for non-PDF binary content
        desc_part = result.content[1]
        assert desc_part.type == "text"
        assert "application/zip" in desc_part.text
        assert "0.0MB" in desc_part.text  # 1KB shows as 0.0MB
        assert "Test archive" in desc_part.text
        assert "Binary content not accessible" in desc_part.text

    def test_create_attachment_injection_file_path_only(self) -> None:
        """Test OpenAI attachment injection with file path only"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        attachment = ToolAttachment(
            mime_type="text/plain", file_path="/path/to/file.txt"
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, list)
        assert len(result.content) == 2

        file_part = result.content[1]
        assert file_part.type == "text"
        assert "/path/to/file.txt" in file_part.text


class TestAnthropicClient:
    """Test AnthropicClient multimodal functionality"""

    def test_supports_multimodal_tools(self) -> None:
        """Test Anthropic supports multimodal tool responses"""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")
        assert client._supports_multimodal_tools() is True

    def test_process_tool_messages_with_image_attachment(self) -> None:
        """Test processing tool messages with image attachments"""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        fake_image_data = b"fake image data"
        attachment = ToolAttachment(
            mime_type="image/png", content=fake_image_data, description="Test image"
        )

        messages: list = [
            ToolMessage(
                tool_call_id="call_123",
                content="Generated an image",
                name="test_tool",
                _attachments=[attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 1
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[0].content, list)
        assert len(result[0].content) == 2

        content_list: list[Any] = result[0].content  # type: ignore[assignment]
        assert content_list[0]["type"] == "text"  # type: ignore[index]
        assert content_list[0]["text"] == "Generated an image"  # type: ignore[index]

        assert content_list[1]["type"] == "image"  # type: ignore[index]
        assert content_list[1]["source"]["type"] == "base64"  # type: ignore[index]
        assert content_list[1]["source"]["media_type"] == "image/png"  # type: ignore[index]

        # Verify base64 content
        decoded = base64.b64decode(content_list[1]["source"]["data"])  # type: ignore[index]
        assert decoded == fake_image_data

    def test_process_tool_messages_with_pdf_attachment(self) -> None:
        """Test processing tool messages with PDF attachments uses document blocks"""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        fake_pdf_data = b"fake pdf data"
        attachment = ToolAttachment(
            mime_type="application/pdf", content=fake_pdf_data, description="Test PDF"
        )

        messages: list = [
            ToolMessage(
                tool_call_id="call_456",
                content="Retrieved a PDF document",
                name="test_tool",
                _attachments=[attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 1
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[0].content, list)
        assert len(result[0].content) == 2

        content_list: list[Any] = result[0].content  # type: ignore[assignment]
        assert content_list[0]["type"] == "text"  # type: ignore[index]
        assert content_list[0]["text"] == "Retrieved a PDF document"  # type: ignore[index]

        assert content_list[1]["type"] == "document"  # type: ignore[index]
        assert content_list[1]["source"]["type"] == "base64"  # type: ignore[index]
        assert content_list[1]["source"]["media_type"] == "application/pdf"  # type: ignore[index]

    def test_process_tool_messages_with_unsupported_type_falls_back(self) -> None:
        """Test unsupported attachment types fall back to injection"""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        fake_data = b"fake zip data"
        attachment = ToolAttachment(
            mime_type="application/zip", content=fake_data, description="Test ZIP"
        )

        messages: list = [
            ToolMessage(
                tool_call_id="call_789",
                content="Created a ZIP file",
                name="test_tool",
                _attachments=[attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: tool message + injected user message
        assert len(result) == 2
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[1], UserMessage)
        assert "File from previous tool response" in str(result[1].content)

    def test_process_tool_messages_with_file_path_only(self) -> None:
        """Test file-path-only attachments fall back to injection"""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        attachment = ToolAttachment(
            mime_type="application/pdf",
            file_path="/path/to/document.pdf",
            description="External PDF",
        )

        messages: list = [
            ToolMessage(
                tool_call_id="call_999",
                content="Found external document",
                name="test_tool",
                _attachments=[attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 2
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[1], UserMessage)
        assert "File from previous tool response" in str(result[1].content)

    def test_process_tool_messages_without_attachments_passes_through(self) -> None:
        """Test messages without attachments pass through unchanged"""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        messages: list = [
            UserMessage(content="Hello"),
            ToolMessage(
                tool_call_id="call_000",
                content="Tool result",
                name="test_tool",
            ),
        ]

        result = client._process_tool_messages(messages)

        assert len(result) == 2
        assert result[0].content == "Hello"
        assert result[1].content == "Tool result"

    def test_process_tool_messages_multimodal_content_in_tool_result(self) -> None:
        """Test that multimodal tool content is preserved in message conversion."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        fake_image_data = b"fake image data"
        attachment = ToolAttachment(
            mime_type="image/png", content=fake_image_data, description="Screenshot"
        )

        messages: list = [
            UserMessage(content="Take a screenshot"),
            AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(name="screenshot", arguments="{}"),
                    )
                ],
            ),
            ToolMessage(
                tool_call_id="call_123",
                content="Screenshot captured",
                name="screenshot",
                _attachments=[attachment],
            ),
        ]

        processed = client._process_tool_messages(messages)
        _system, api_messages = client._convert_messages_to_anthropic_format(processed)

        # The tool_result should contain the multimodal content list
        # Find the tool_result block
        tool_result_found = False
        for msg in api_messages:
            if msg["role"] == "user":
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_result_found = True
                        # Content should be a list with text + image blocks
                        assert isinstance(block["content"], list)
                        assert block["content"][0]["type"] == "text"
                        assert block["content"][1]["type"] == "image"
                        break

        assert tool_result_found, "tool_result block not found in converted messages"

    def test_process_tool_messages_mixed_attachments(self) -> None:
        """Test tool result with mix of supported (image) and unsupported (zip) attachments."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        image_attachment = ToolAttachment(
            mime_type="image/png", content=b"fake image", description="Screenshot"
        )
        zip_attachment = ToolAttachment(
            mime_type="application/zip", content=b"fake zip", description="Archive"
        )

        messages: list = [
            ToolMessage(
                tool_call_id="call_mix",
                content="Files ready",
                name="test_tool",
                _attachments=[image_attachment, zip_attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: tool (with image inline) + injected user (for zip)
        assert len(result) == 2
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[1], UserMessage)

        # Tool message content should have text + image blocks
        content_list: list[Any] = result[0].content  # type: ignore[assignment]
        assert isinstance(content_list, list)
        assert content_list[0]["type"] == "text"  # type: ignore[index]
        assert content_list[1]["type"] == "image"  # type: ignore[index]

        # Injected user message for unsupported zip
        assert "File from previous tool response" in str(result[1].content)

    def test_convert_messages_malformed_tool_arguments_raises_error(self) -> None:
        """Test that malformed JSON in tool call arguments raises InvalidRequestError."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet-20240229")

        messages: list = [
            AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call_bad",
                        type="function",
                        function=ToolCallFunction(
                            name="broken_tool",
                            arguments="{invalid json!!!",
                        ),
                    )
                ],
            ),
        ]

        with pytest.raises(InvalidRequestError, match="Malformed tool call arguments"):
            client._convert_messages_to_anthropic_format(messages)
