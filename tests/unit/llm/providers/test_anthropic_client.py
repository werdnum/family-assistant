"""Unit tests for the Anthropic provider client."""

import base64
import os
import tempfile
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from anthropic.types import TextBlockParam

from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from family_assistant.llm import LLMStreamEvent


@pytest.mark.no_db
async def test_closing_stream_immediately_exits_provider_context() -> None:
    class TrackingStream:
        def __init__(self) -> None:
            self.closed = False
            self._yielded = False

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            self.closed = True

        def __aiter__(self) -> Self:
            return self

        async def __anext__(self) -> Mock:
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return Mock(
                type="content_block_delta",
                delta=Mock(type="text_delta", text="partial"),
            )

    provider_stream = TrackingStream()
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-6")
    client.client = cast(
        "Any",
        Mock(messages=Mock(stream=lambda **_kwargs: provider_stream)),
    )

    with patch.object(
        client,
        "_maybe_parse_vcr_stream",
        new=AsyncMock(return_value=None),
    ):
        stream = cast(
            "AsyncGenerator[LLMStreamEvent]",
            client.generate_response_stream([UserMessage(content="hello")]),
        )
        event = await anext(stream)
        assert event.content == "partial"
        await stream.aclose()

    assert provider_stream.closed


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

    def test_zero_offset_does_not_split(self) -> None:
        """Nothing stable to cache, so keep the pre-existing shape."""
        content = "You are a helpful assistant."
        system_value = self._convert([
            SystemMessage(content=content, stable_prefix_len=0),
            UserMessage(content="hi"),
        ])

        assert system_value == content

    @pytest.mark.parametrize("offset", [28, 99])
    def test_fully_stable_prompt_is_cached_as_one_block(self, offset: int) -> None:
        """A prompt stable to the end is the ideal case and must still cache.

        `offset == len(content)` is what a template with no volatile
        placeholders produces; a larger offset can only mean the content was
        trimmed after the boundary was computed. Neither is a reason to skip
        caching the whole thing.
        """
        content = "You are a helpful assistant."
        assert offset >= len(content)

        system_value = self._convert([
            SystemMessage(content=content, stable_prefix_len=offset),
            UserMessage(content="hi"),
        ])

        assert system_value == [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

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
    def _usage(**kwargs: int | None) -> Mock:
        return Mock(
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

    def test_reported_zero_cache_tokens_are_preserved(self) -> None:
        """Anthropic always reports these fields, so 0 is a known miss."""
        info = AnthropicClient._reasoning_info_from_usage(
            self._usage(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
        )

        assert info.get("cached_prompt_tokens") == 0
        assert info.get("cache_write_tokens") == 0
        assert info.get("total_tokens") == 120


def _without_markers(
    api_messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """The same messages with every cache_control directive removed."""
    stripped: list[dict[str, object]] = []
    for message in api_messages:
        content = message["content"]
        if isinstance(content, list):
            content = [
                {k: v for k, v in block.items() if k != "cache_control"}
                if isinstance(block, dict)
                else block
                for block in content
            ]
        stripped.append({**message, "content": content})
    return stripped


def _breakpoint_positions(
    api_messages: list[dict[str, object]],
) -> list[tuple[int, int]]:
    """(message index, block index) of every cache_control marker."""
    found: list[tuple[int, int]] = []
    for message_index, message in enumerate(api_messages):
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and "cache_control" in block:
                found.append((message_index, block_index))
    return found


@pytest.mark.no_db
class TestConversationCacheBreakpoints:
    """Anthropic caches only to an explicit breakpoint.

    Without these the history stays uncached no matter how prefix-stable it is,
    which is what the turn-context move made it.
    """

    def _client(self) -> AnthropicClient:
        return AnthropicClient(api_key="test", model="claude-sonnet-5")

    def test_breakpoint_skips_back_past_messages_that_merge_with_the_block(
        self,
    ) -> None:
        """It must land on a message whose serialization does not depend on the
        block, so the same history goes out identically on the next turn."""
        client = self._client()
        messages: list[LLMMessage] = [
            SystemMessage(content="You are helpful.", stable_prefix_len=16),
            UserMessage(content="Earlier question"),
            AssistantMessage(content="Earlier answer"),
            UserMessage(content="What is on my calendar?"),
            UserMessage(
                content="<turn_context>\nCurrent time: now\n</turn_context>",
                is_turn_scaffolding=True,
            ),
        ]

        _, api_messages = client._convert_messages_to_anthropic_format(messages)

        # api_messages: [user earlier][assistant][user trigger + block merged].
        # The breakpoint is on the assistant turn, not on the merged user turn.
        assert [index for index, _ in _breakpoint_positions(api_messages)] == [1, 2]

    def test_cached_prefix_is_byte_identical_on_the_next_turn(self) -> None:
        """The property the whole change exists for, at the API boundary."""
        client = self._client()
        history: list[LLMMessage] = [
            SystemMessage(content="You are helpful.", stable_prefix_len=16),
            UserMessage(content="Earlier question"),
            AssistantMessage(content="Earlier answer"),
        ]

        def block(text: str) -> UserMessage:
            return UserMessage(content=text, is_turn_scaffolding=True)

        _, turn_one = client._convert_messages_to_anthropic_format([
            *history,
            UserMessage(content="What is on my calendar?"),
            block("<turn_context>\nCurrent time: 10:00\n</turn_context>"),
        ])
        _, turn_two = client._convert_messages_to_anthropic_format([
            *history,
            UserMessage(content="What is on my calendar?"),
            AssistantMessage(content="Nothing today."),
            UserMessage(content="Thanks"),
            block("<turn_context>\nCurrent time: 10:05\n</turn_context>"),
        ])

        # Everything up to and including turn one's first breakpoint must be
        # reproduced exactly, or turn two writes a fresh entry instead of
        # reading it. The marker itself is a directive rather than cached
        # content, and sits at a different place on each turn, so it is stripped
        # before comparing.
        cut = _breakpoint_positions(turn_one)[0][0]
        assert _without_markers(turn_one[: cut + 1]) == _without_markers(
            turn_two[: cut + 1]
        )

    def test_trailing_breakpoint_follows_the_tool_loop(self) -> None:
        """Without it each iteration re-reads every result accumulated so far."""
        client = self._client()
        messages: list[LLMMessage] = [
            SystemMessage(content="You are helpful.", stable_prefix_len=16),
            UserMessage(content="Check the weather"),
            UserMessage(
                content="<turn_context>\nnow\n</turn_context>", is_turn_scaffolding=True
            ),
            AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(name="weather", arguments="{}"),
                    )
                ],
            ),
            ToolMessage(content="sunny", tool_call_id="call_1", name="weather"),
        ]

        _, api_messages = client._convert_messages_to_anthropic_format(messages)

        positions = _breakpoint_positions(api_messages)
        last_message_index = len(api_messages) - 1
        assert any(index == last_message_index for index, _ in positions)

    def test_plain_string_content_is_left_unshaped(self) -> None:
        """One-shot callers build message lists by hand and carry no block.

        Wrapping their string content into a block list to carry a breakpoint
        would change the request shape for no gain -- there is no conversation
        here to cache -- and a message that goes out as a string on one turn and
        a list on another cannot be prefix-matched.
        """
        client = self._client()
        messages: list[LLMMessage] = [
            SystemMessage(content="You are helpful."),
            UserMessage(content="Summarize this."),
        ]

        _, api_messages = client._convert_messages_to_anthropic_format(messages)

        assert api_messages == [{"role": "user", "content": "Summarize this."}]

    def test_thinking_blocks_are_never_annotated(self) -> None:
        """Their signatures are validated against exactly what came back."""
        client = self._client()
        messages: list[LLMMessage] = [
            SystemMessage(content="You are helpful."),
            UserMessage(content="Think about it"),
            AssistantMessage(
                content=None,
                provider_metadata={
                    "thinking_blocks": [
                        {
                            "type": "thinking",
                            "thinking": "reasoning",
                            "signature": "sig",
                        }
                    ]
                },
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(name="noop", arguments="{}"),
                    )
                ],
            ),
        ]

        _, api_messages = client._convert_messages_to_anthropic_format(messages)

        for message in api_messages:
            content = message["content"]
            if not isinstance(content, list):
                continue
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") in {
                    "thinking",
                    "redacted_thinking",
                }:
                    assert "cache_control" not in blk

    def test_total_breakpoints_stay_within_anthropic_limit(self) -> None:
        """System block plus the two conversation ones must not exceed four."""
        client = self._client()
        messages: list[LLMMessage] = [
            SystemMessage(content="You are helpful.", stable_prefix_len=8),
            UserMessage(content="Do a thing"),
            UserMessage(
                content="<turn_context>\nnow\n</turn_context>", is_turn_scaffolding=True
            ),
            AssistantMessage(content="Working"),
            UserMessage(content="[SYSTEM: final iteration]", is_turn_scaffolding=True),
        ]

        system_value, api_messages = client._convert_messages_to_anthropic_format(
            messages
        )

        system_breakpoints = (
            sum(1 for blk in system_value if "cache_control" in blk)
            if isinstance(system_value, list)
            else 0
        )
        assert system_breakpoints + len(_breakpoint_positions(api_messages)) <= 4
