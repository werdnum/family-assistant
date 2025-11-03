"""
Unit tests for multimodal tool results functionality.
"""

import base64

from family_assistant.tools.types import ToolAttachment, ToolResult


class TestToolAttachment:
    """Test ToolAttachment functionality"""

    def test_tool_attachment_creation(self) -> None:
        """Test creating a ToolAttachment"""
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"test image data",
            description="A test image",
        )

        assert attachment.mime_type == "image/png"
        assert attachment.content == b"test image data"
        assert attachment.file_path is None
        assert attachment.description == "A test image"

    def test_tool_attachment_with_file_path(self) -> None:
        """Test ToolAttachment with file path"""
        attachment = ToolAttachment(
            mime_type="application/pdf",
            file_path="/path/to/document.pdf",
            description="A test PDF",
        )

        assert attachment.mime_type == "application/pdf"
        assert attachment.content is None
        assert attachment.file_path == "/path/to/document.pdf"
        assert attachment.description == "A test PDF"

    def test_get_content_as_base64_with_content(self) -> None:
        """Test base64 encoding of attachment content"""
        test_data = b"Hello, World!"
        expected_b64 = base64.b64encode(test_data).decode()

        attachment = ToolAttachment(mime_type="text/plain", content=test_data)

        result = attachment.get_content_as_base64()
        assert result == expected_b64

    def test_get_content_as_base64_without_content(self) -> None:
        """Test base64 encoding returns None when no content"""
        attachment = ToolAttachment(
            mime_type="text/plain", file_path="/path/to/file.txt"
        )

        result = attachment.get_content_as_base64()
        assert result is None

    def test_get_content_as_base64_empty_content(self) -> None:
        """Test base64 encoding with empty content"""
        attachment = ToolAttachment(mime_type="text/plain", content=b"")

        result = attachment.get_content_as_base64()
        assert not result  # Base64 of empty bytes is empty string


class TestToolResult:
    """Test ToolResult functionality"""

    def test_tool_result_text_only(self) -> None:
        """Test creating a ToolResult with text only"""
        result = ToolResult(text="Operation completed successfully")

        assert result.text == "Operation completed successfully"
        assert result.attachments is None

    def test_tool_result_with_attachment(self) -> None:
        """Test creating a ToolResult with attachment"""
        attachment = ToolAttachment(
            mime_type="image/jpeg",
            content=b"fake jpeg data",
            description="Test image",
        )
        result = ToolResult(
            text="Image processed successfully", attachments=[attachment]
        )

        assert result.text == "Image processed successfully"
        assert result.attachments == [attachment]
        assert result.attachments is not None
        assert len(result.attachments) > 0
        assert result.attachments[0].mime_type == "image/jpeg"

    def test_to_string_without_attachment(self) -> None:
        """Test to_string method without attachment"""
        result = ToolResult(text="Simple text result")

        assert result.to_string() == "Simple text result"

    def test_to_string_with_attachment(self) -> None:
        """Test to_string method with attachment"""
        attachment = ToolAttachment(
            mime_type="application/pdf", content=b"fake pdf data"
        )
        result = ToolResult(text="Document retrieved", attachments=[attachment])

        # Should return just the text (message injection handled by providers)
        assert result.to_string() == "Document retrieved"


class TestToolResultIntegration:
    """Integration tests for ToolResult and ToolAttachment together"""

    def test_image_tool_result(self) -> None:
        """Test creating a realistic image tool result"""
        # Simulate a small PNG image
        png_header = b"\x89PNG\r\n\x1a\n"
        fake_png_data = png_header + b"fake png data"

        attachment = ToolAttachment(
            mime_type="image/png",
            content=fake_png_data,
            description="Generated chart showing sales data",
        )

        result = ToolResult(
            text="Chart generated successfully showing Q3 sales trends",
            attachments=[attachment],
        )

        assert result.text == "Chart generated successfully showing Q3 sales trends"
        assert result.attachments is not None
        assert len(result.attachments) > 0
        assert result.attachments[0].mime_type == "image/png"
        assert result.attachments[0].content is not None
        assert result.attachments[0].content.startswith(png_header)

        # Test base64 encoding works
        b64_content = result.attachments[0].get_content_as_base64()
        assert b64_content is not None
        assert base64.b64decode(b64_content) == fake_png_data

    def test_pdf_tool_result(self) -> None:
        """Test creating a realistic PDF tool result"""
        attachment = ToolAttachment(
            mime_type="application/pdf",
            file_path="/tmp/document_123.pdf",
            description="Financial report Q3 2024",
        )

        result = ToolResult(
            text="Retrieved financial report for Q3 2024", attachments=[attachment]
        )

        assert result.text == "Retrieved financial report for Q3 2024"
        assert result.attachments is not None
        assert len(result.attachments) > 0
        assert result.attachments[0].mime_type == "application/pdf"
        assert result.attachments[0].file_path == "/tmp/document_123.pdf"
        assert result.attachments[0].content is None  # No content, just file path

    def test_large_attachment_handling(self) -> None:
        """Test handling of larger attachments"""
        # Create 1MB of fake data
        large_data = b"x" * (1024 * 1024)

        attachment = ToolAttachment(
            mime_type="application/octet-stream",
            content=large_data,
            description="Large binary file",
        )

        result = ToolResult(text="Large file processed", attachments=[attachment])

        assert result.attachments is not None
        assert len(result.attachments) > 0
        assert result.attachments[0].content is not None
        assert len(result.attachments[0].content) == 1024 * 1024

        # Test base64 encoding still works (but would be large)
        b64_content = result.attachments[0].get_content_as_base64()
        assert b64_content is not None
        assert len(b64_content) > len(large_data)  # Base64 is larger than original
