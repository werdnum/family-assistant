"""Unit tests for the Anthropic provider client."""

import base64
import os
import tempfile

import pytest

from family_assistant.llm.providers.anthropic_client import AnthropicClient


@pytest.mark.no_db
class TestAnthropicFormatUserMessageWithFile:
    """Test format_user_message_with_file for various file types."""

    async def test_no_file_returns_text_only(self) -> None:
        """Test message with no file returns plain text."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet")
        result = await client.format_user_message_with_file(
            prompt_text="Hello", file_path=None, mime_type=None, max_text_length=None
        )
        assert result == {"role": "user", "content": "Hello"}

    async def test_image_file_returns_base64_content(self) -> None:
        """Test image file is encoded as base64 in image block."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"fake png data")
            tmp_path = f.name

        try:
            result = await client.format_user_message_with_file(
                prompt_text="Describe this",
                file_path=tmp_path,
                mime_type="image/png",
                max_text_length=None,
            )
            assert result["role"] == "user"
            content = result["content"]
            assert isinstance(content, list)
            assert content[0] == {"type": "text", "text": "Describe this"}
            assert content[1]["type"] == "image"
            assert content[1]["source"]["type"] == "base64"
            assert content[1]["source"]["media_type"] == "image/png"
            decoded = base64.b64decode(content[1]["source"]["data"])
            assert decoded == b"fake png data"
        finally:
            os.unlink(tmp_path)

    async def test_pdf_file_returns_document_block(self) -> None:
        """Test PDF file uses native document block instead of crashing."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake pdf content")
            tmp_path = f.name

        try:
            result = await client.format_user_message_with_file(
                prompt_text="Analyze this",
                file_path=tmp_path,
                mime_type="application/pdf",
                max_text_length=None,
            )
            assert result["role"] == "user"
            content = result["content"]
            assert isinstance(content, list)
            assert content[0] == {"type": "text", "text": "Analyze this"}
            assert content[1]["type"] == "document"
            assert content[1]["source"]["type"] == "base64"
            assert content[1]["source"]["media_type"] == "application/pdf"
            decoded = base64.b64decode(content[1]["source"]["data"])
            assert decoded == b"%PDF-1.4 fake pdf content"
        finally:
            os.unlink(tmp_path)

    async def test_text_file_returns_inline_content(self) -> None:
        """Test text file is read and inlined."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet")

        with tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("Hello world")
            tmp_path = f.name

        try:
            result = await client.format_user_message_with_file(
                prompt_text="Read this",
                file_path=tmp_path,
                mime_type="text/plain",
                max_text_length=None,
            )
            assert result["role"] == "user"
            content_str = str(result["content"])
            assert "Hello world" in content_str
            assert "Read this" in content_str
        finally:
            os.unlink(tmp_path)

    async def test_binary_file_does_not_crash(self) -> None:
        """Test binary file with non-UTF8 content doesn't crash."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet")

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01\x02\xff\xfe\xfd")
            tmp_path = f.name

        try:
            result = await client.format_user_message_with_file(
                prompt_text="What is this?",
                file_path=tmp_path,
                mime_type="application/octet-stream",
                max_text_length=None,
            )
            assert result["role"] == "user"
            content = result["content"]
            assert isinstance(content, str)
            assert "Binary file" in content
            assert "What is this?" in content
        finally:
            os.unlink(tmp_path)

    async def test_text_file_truncation(self) -> None:
        """Test text file content is truncated when max_text_length is set."""
        client = AnthropicClient(api_key="test", model="claude-3-sonnet")

        with tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("A" * 1000)
            tmp_path = f.name

        try:
            result = await client.format_user_message_with_file(
                prompt_text=None,
                file_path=tmp_path,
                mime_type="text/plain",
                max_text_length=100,
            )
            assert result["role"] == "user"
            content_str = str(result["content"])
            assert len(content_str) == 100
        finally:
            os.unlink(tmp_path)
