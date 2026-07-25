"""Unit tests for the Anthropic provider client."""

import base64
import os
import tempfile
from types import SimpleNamespace

import pytest
from anthropic.types import TextBlockParam

from family_assistant.llm.messages import LLMMessage, SystemMessage, UserMessage
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


@pytest.mark.no_db
class TestAnthropicPromptCacheBreakpoint:
    """Placement of the prompt-cache breakpoint in the top-level system value."""

    @staticmethod
    def _convert(
        messages: list[LLMMessage],
    ) -> str | list[TextBlockParam] | None:
        client = AnthropicClient(api_key="test", model="claude-sonnet-4-6")
        system_value, _ = client._convert_messages_to_anthropic_format(messages)
        return system_value

    def test_splits_at_stable_prefix_and_marks_only_the_prefix_cacheable(self) -> None:
        stable = "You are a helpful assistant."
        volatile = "\n\nCurrent time: 2026-07-25 10:00:00 UTC"
        system_value = self._convert([
            SystemMessage(content=stable + volatile, stable_prefix_len=len(stable)),
            UserMessage(content="hi"),
        ])

        assert system_value == [
            {
                "type": "text",
                "text": stable,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": volatile},
        ]

    def test_split_preserves_the_exact_prompt_text(self) -> None:
        """The model must see identical bytes whether or not we split."""
        content = "stable part\n\nCurrent time: 2026-07-25"
        system_value = self._convert([
            SystemMessage(content=content, stable_prefix_len=len("stable part")),
            UserMessage(content="hi"),
        ])

        assert isinstance(system_value, list)
        assert "".join(block["text"] for block in system_value) == content

    def test_falls_back_to_plain_string_without_a_boundary(self) -> None:
        """No boundary means nothing cacheable, so keep the pre-existing shape."""
        system_value = self._convert([
            SystemMessage(content="You are a helpful assistant."),
            UserMessage(content="hi"),
        ])

        assert system_value == "You are a helpful assistant."

    @pytest.mark.parametrize("offset", [0, 28, 99])
    def test_out_of_range_offsets_do_not_split(self, offset: int) -> None:
        """A degenerate offset must not produce an empty or truncated block."""
        content = "You are a helpful assistant."
        system_value = self._convert([
            SystemMessage(content=content, stable_prefix_len=offset),
            UserMessage(content="hi"),
        ])

        assert system_value == content

    def test_hoisted_mid_conversation_system_message_lands_past_the_breakpoint(
        self,
    ) -> None:
        """Per-turn system triggers must not end up inside the cached block."""
        stable = "You are a helpful assistant."
        system_value = self._convert([
            SystemMessage(content=stable, stable_prefix_len=len(stable)),
            UserMessage(content="hi"),
            SystemMessage(content="System: Reminder triggered at 10:00:00"),
        ])

        assert isinstance(system_value, list)
        assert system_value[0]["text"] == stable
        assert system_value[0].get("cache_control") == {"type": "ephemeral"}
        assert "Reminder triggered" in system_value[1]["text"]
        assert "cache_control" not in system_value[1]

    def test_no_system_messages_yields_none(self) -> None:
        assert self._convert([UserMessage(content="hi")]) is None


@pytest.mark.no_db
class TestAnthropicCacheUsageReporting:
    """Anthropic reports cache tokens as buckets disjoint from input_tokens."""

    @staticmethod
    def _usage(**kwargs: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            input_tokens=kwargs.get("input_tokens", 0),
            output_tokens=kwargs.get("output_tokens", 0),
            cache_read_input_tokens=kwargs.get("cache_read_input_tokens"),
            cache_creation_input_tokens=kwargs.get("cache_creation_input_tokens"),
        )

    def test_cache_read_is_reported_and_added_to_the_total(self) -> None:
        info = AnthropicClient._reasoning_info_from_usage(
            self._usage(
                input_tokens=100, output_tokens=20, cache_read_input_tokens=4000
            )
        )

        assert info.get("cached_prompt_tokens") == 4000
        assert info.get("prompt_tokens") == 100
        # Without adding the cache bucket back, a cached turn would report a
        # prompt ~40x smaller than the one actually sent.
        assert info.get("total_tokens") == 4120

    def test_cache_write_is_reported_and_added_to_the_total(self) -> None:
        info = AnthropicClient._reasoning_info_from_usage(
            self._usage(
                input_tokens=100, output_tokens=20, cache_creation_input_tokens=4000
            )
        )

        assert info.get("cache_write_tokens") == 4000
        assert info.get("total_tokens") == 4120

    def test_absent_cache_fields_are_omitted_not_zeroed(self) -> None:
        """Absent must stay distinguishable from a genuine zero-hit turn."""
        info = AnthropicClient._reasoning_info_from_usage(
            self._usage(input_tokens=100, output_tokens=20)
        )

        assert "cached_prompt_tokens" not in info
        assert "cache_write_tokens" not in info
        assert info.get("total_tokens") == 120
