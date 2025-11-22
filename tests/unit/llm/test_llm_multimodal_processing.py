"""
Unit tests for LLM provider multimodal message processing.
"""

import base64
import json
from typing import Any, cast
from unittest.mock import patch

from family_assistant.llm import BaseLLMClient, LiteLLMClient
from family_assistant.llm.messages import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
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
            assert result.parts is not None
            assert len(result.parts) == 2

            # First part should be system message
            assert (
                result.parts[0]["text"] == "[System: File from previous tool response]"
            )

            # Second part should be a types.Part object with inline_data
            part = result.parts[1]
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

            result = client.create_attachment_injection(attachment)

            assert result.role == "user"
            assert result.parts is not None
            assert len(result.parts) == 2

            # First part should be system message
            assert (
                result.parts[0]["text"] == "[System: File from previous tool response]"
            )

            # Second part should be a types.Part object with inline_data for PDF
            part = result.parts[1]
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

            result = client.create_attachment_injection(attachment)

            assert result.role == "user"
            assert result.parts is not None
            assert len(result.parts) == 2

            # Should describe the non-PDF binary content
            text_part = result.parts[1]["text"]
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
            assert result.parts is not None
            assert len(result.parts) == 2

            text_part = result.parts[1]["text"]
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
        """Test OpenAI attachment injection for PDF content"""
        client = OpenAIClient(api_key="test", model="gpt-4")

        fake_data = b"x" * 2048  # 2KB
        attachment = ToolAttachment(
            mime_type="application/pdf", content=fake_data, description="Test document"
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, list)
        assert len(result.content) == 2

        # First part should be system message
        assert result.content[0].type == "text"
        assert "File from previous tool response" in result.content[0].text

        # Second part should be text description for PDF (fallback approach)
        text_part = result.content[1]
        assert text_part.type == "text"
        assert "PDF Document" in text_part.text
        assert "Test document" in text_part.text
        assert "0.0MB" in text_part.text
        assert "Content cannot be displayed" in text_part.text

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

        # First part should be text
        content_list: list[Any] = result[0].content  # type: ignore[assignment]
        assert content_list[0]["type"] == "text"  # type: ignore[index]
        assert content_list[0]["text"] == "Generated an image"  # type: ignore[index]

        # Second part should be image
        assert content_list[1]["type"] == "image"  # type: ignore[index]
        assert content_list[1]["source"]["type"] == "base64"  # type: ignore[index]
        assert content_list[1]["source"]["media_type"] == "image/png"  # type: ignore[index]

    def test_process_tool_messages_with_pdf_attachment(self) -> None:
        """Test processing tool messages with PDF attachments for Claude"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

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

        # First part should be text
        content_list: list[Any] = result[0].content  # type: ignore[assignment]
        assert content_list[0]["type"] == "text"  # type: ignore[index]
        assert content_list[0]["text"] == "Retrieved a PDF document"  # type: ignore[index]

        # Second part should be document
        assert content_list[1]["type"] == "document"  # type: ignore[index]
        assert content_list[1]["source"]["type"] == "base64"  # type: ignore[index]
        assert content_list[1]["source"]["media_type"] == "application/pdf"  # type: ignore[index]

    def test_process_tool_messages_with_unsupported_content_type(self) -> None:
        """Test processing tool messages with unsupported content types"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

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

        # Should have 2 messages: the tool message and the injected user message
        assert len(result) == 2
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[1], UserMessage)
        assert "File from previous tool response" in result[1].content

    def test_process_tool_messages_with_file_path_only_attachment(self) -> None:
        """Test processing tool messages with file-path-only attachments"""
        client = LiteLLMClient(model="claude-3-sonnet-20240229")

        attachment = ToolAttachment(
            mime_type="application/pdf",
            file_path="/path/to/document.pdf",
            description="External PDF file",
        )

        messages: list = [
            ToolMessage(
                tool_call_id="call_999",
                content="Found an external document",
                name="test_tool",
                _attachments=[attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: the tool message and the injected user message
        assert len(result) == 2
        assert isinstance(result[0], ToolMessage)
        assert isinstance(result[1], UserMessage)
        assert "File from previous tool response" in result[1].content
        assert (
            "External PDF file" in result[1].content
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

        messages: list = [
            ToolMessage(
                tool_call_id="call_000",
                content="Generated an image",
                name="test_tool",
                _attachments=[attachment],
            )
        ]

        result = client._process_tool_messages(messages)

        # Should have 2 messages: modified tool message + injected user message
        assert len(result) == 2
        assert isinstance(result[0], ToolMessage)
        assert "[File content in following message]" in result[0].content
        assert isinstance(result[1], UserMessage)

    def test_create_attachment_injection_small_json(self) -> None:
        """Test small JSON files (≤10KiB) are inlined fully"""
        client = BaseLLMClient()

        # Small JSON object (< 10KB)
        json_data = {"items": [{"id": i, "value": f"item_{i}"} for i in range(100)]}
        json_bytes = json.dumps(json_data).encode("utf-8")
        assert len(json_bytes) < 10 * 1024  # Verify it's under threshold

        attachment = ToolAttachment(
            mime_type="application/json",
            content=json_bytes,
            description="Small dataset",
            attachment_id="test-123",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        assert "[System: File from previous tool response]" in content
        assert "[Description: Small dataset]" in content
        assert "[Attachment ID: test-123]" in content
        assert f"[Content ({len(json_bytes)} bytes)]:" in content
        # Full JSON should be present
        assert '"items"' in content
        assert '"id"' in content
        # Should NOT have schema or jq note
        assert "JSON Schema" not in content
        assert "jq" not in content

    def test_create_attachment_injection_large_json(self) -> None:
        """Test large JSON files (>10KiB) get schema injection with jq note"""
        client = BaseLLMClient()

        # Large JSON object (> 10KB)
        json_data = {
            "items": [
                {
                    "id": i,
                    "name": f"Item {i}",
                    "description": f"Description for item {i} with some padding text to make it larger",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "metadata": {"category": "test", "priority": i % 10},
                }
                for i in range(200)
            ]
        }
        json_bytes = json.dumps(json_data).encode("utf-8")
        assert len(json_bytes) > 10 * 1024  # Verify it's over threshold

        attachment = ToolAttachment(
            mime_type="application/json",
            content=json_bytes,
            description="Large dataset",
            attachment_id="test-456",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        assert "[System: Large data attachment from previous tool response]" in content
        assert "[Description: Large dataset]" in content
        assert f"[Size: {len(json_bytes)} bytes" in content
        assert "[Attachment ID: test-456]" in content
        assert "Data structure (JSON Schema):" in content
        # Should have schema structure indicators
        assert '"type"' in content
        assert '"properties"' in content or '"items"' in content
        # Should have jq usage note
        assert "jq" in content
        assert "test-456" in content  # Reference to attachment ID in jq note
        # Should NOT have full JSON data
        assert "Description for item" not in content

    def test_create_attachment_injection_small_text(self) -> None:
        """Test small text files (≤10KiB) are inlined fully"""
        client = BaseLLMClient()

        # Small text content
        text_content = "This is a small text file.\n" * 100
        text_bytes = text_content.encode("utf-8")
        assert len(text_bytes) < 10 * 1024

        attachment = ToolAttachment(
            mime_type="text/plain",
            content=text_bytes,
            description="Small notes",
            attachment_id="text-123",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        assert "[System: File from previous tool response]" in content
        assert "[Description: Small notes]" in content
        assert "[Attachment ID: text-123]" in content
        assert f"[Content ({len(text_bytes)} bytes)]:" in content
        # Full text should be present
        assert "This is a small text file." in content

    def test_create_attachment_injection_large_text(self) -> None:
        """Test large text files (>10KiB) get summary without inline content"""
        client = BaseLLMClient()

        # Large text content
        text_content = "This is a large text file with lots of content.\n" * 300
        text_bytes = text_content.encode("utf-8")
        assert len(text_bytes) > 10 * 1024

        attachment = ToolAttachment(
            mime_type="text/plain",
            content=text_bytes,
            description="Large document",
            attachment_id="text-456",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        assert "[System: Large text file from previous tool response]" in content
        assert "[Description: Large document]" in content
        assert f"[Size: {len(text_bytes)} bytes" in content
        assert "[Attachment ID: text-456]" in content
        assert "[MIME type: text/plain]" in content
        assert "Content too large for inline display" in content
        # Should NOT have full text content
        assert "This is a large text file with lots of content." not in content

    def test_create_attachment_injection_small_csv(self) -> None:
        """Test small CSV files (≤10KiB) are inlined fully"""
        client = BaseLLMClient()

        # Small CSV content
        csv_content = "id,name,value\n" + "\n".join([
            f"{i},Item{i},{i * 10}" for i in range(100)
        ])
        csv_bytes = csv_content.encode("utf-8")
        assert len(csv_bytes) < 10 * 1024

        attachment = ToolAttachment(
            mime_type="text/csv",
            content=csv_bytes,
            description="Small CSV",
            attachment_id="csv-123",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        assert "[System: File from previous tool response]" in content
        assert "[Description: Small CSV]" in content
        assert "id,name,value" in content

    def test_create_attachment_injection_large_csv(self) -> None:
        """Test large CSV files (>10KiB) get summary"""
        client = BaseLLMClient()

        # Large CSV content
        csv_content = "id,name,value,description\n" + "\n".join([
            f"{i},Item{i},{i * 10},Description for item {i}" for i in range(500)
        ])
        csv_bytes = csv_content.encode("utf-8")
        assert len(csv_bytes) > 10 * 1024

        attachment = ToolAttachment(
            mime_type="text/csv",
            content=csv_bytes,
            description="Large CSV",
            attachment_id="csv-456",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        assert "[System: Large text file from previous tool response]" in content
        assert "[Description: Large CSV]" in content
        assert f"[Size: {len(csv_bytes)} bytes" in content
        assert "[MIME type: text/csv]" in content
        assert "Content too large for inline display" in content

    def test_create_attachment_injection_invalid_json(self) -> None:
        """Test malformed JSON large files fall back to text summary"""
        client = BaseLLMClient()

        # Invalid JSON (> 10KB)
        invalid_json = '{"broken": invalid json' * 1000
        json_bytes = invalid_json.encode("utf-8")
        assert len(json_bytes) > 10 * 1024

        attachment = ToolAttachment(
            mime_type="application/json",
            content=json_bytes,
            description="Malformed JSON",
            attachment_id="broken-123",
        )

        result = client.create_attachment_injection(attachment)

        assert result.role == "user"
        assert isinstance(result.content, str)
        content = result.content
        # Should fall back to text summary (not schema)
        assert "[System: Large text file from previous tool response]" in content
        assert "Content too large for inline display" in content
        assert "JSON Schema" not in content
