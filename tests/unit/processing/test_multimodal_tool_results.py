"""
Unit tests for multimodal tool results functionality.
"""

import base64

from family_assistant.tools.types import ToolAttachment, ToolResult


class TestToolAttachment:
    """Test ToolAttachment functionality"""

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
