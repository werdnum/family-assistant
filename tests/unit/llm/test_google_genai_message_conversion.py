"""Unit tests for Google GenAI message format conversion.

Tests the internal message conversion logic, particularly for thought signatures
and their association with function calls.
"""

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from google.genai import types
    from google.genai import types as genai_types

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.google_genai_client import (
    GeminiProviderMetadata,
    GeminiThoughtSignature,
    GoogleGenAIClient,
)


class TestThoughtSignatureConversion:
    """Tests for thought signature handling in message conversion."""

    @pytest.fixture
    def google_client(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for testing."""
        return GoogleGenAIClient(
            api_key="test_key_for_unit_tests", model="gemini-2.5-pro"
        )

    def test_thought_signature_attached_to_function_call_part(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that thought signatures are correctly attached to function call parts.

        This is a regression test for the bug where thought signatures were tracked
        globally with part indices, causing them to be lost or applied to wrong parts
        when reconstructing messages for the API.
        """
        # Create an assistant message with a tool call that has a thought signature
        thought_sig = GeminiThoughtSignature(b"test_signature_bytes")
        provider_metadata = GeminiProviderMetadata(thought_signature=thought_sig)

        messages: list[LLMMessage] = [
            AssistantMessage(
                role="assistant",
                content="",  # Empty content when only tool calls present
                tool_calls=[
                    ToolCallItem(
                        id="call_abc123",
                        type="function",
                        function=ToolCallFunction(
                            name="search_calendar_events",
                            arguments='{"query": "meetings today"}',
                        ),
                        provider_metadata=provider_metadata,
                    )
                ],
            )
        ]

        # Convert to GenAI format
        contents = google_client._convert_messages_to_genai_format(messages)

        # Should have exactly one content with role="model"
        assert len(contents) == 1
        content = cast("types.Content", contents[0])
        assert content.role == "model"

        # Should have exactly one part (the function call)
        assert content.parts is not None, "Content must have parts"
        assert len(content.parts) == 1
        part = cast("types.Part", content.parts[0])

        # CRITICAL: The part must have BOTH function_call AND thought_signature
        assert hasattr(part, "function_call"), "Part must have function_call attribute"
        assert part.function_call is not None, "Part.function_call must not be None"
        assert part.function_call.name == "search_calendar_events", (
            "Function call name must match"
        )

        assert hasattr(part, "thought_signature"), (
            "Part must have thought_signature attribute"
        )
        assert part.thought_signature is not None, (
            "Part.thought_signature must not be None"
        )
        assert isinstance(part.thought_signature, bytes), (
            "Thought signature must be bytes"
        )
        assert part.thought_signature == b"test_signature_bytes", (
            "Thought signature must be reconstituted as bytes"
        )

    def test_multiple_function_calls_preserve_individual_signatures(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that multiple function calls each preserve their own thought signatures.

        This catches the bug where all thought signatures were globally shared,
        causing all tool calls to get the same metadata.
        """
        # Create signatures for two different function calls
        sig1 = GeminiThoughtSignature(b"signature_for_call_1")
        sig2 = GeminiThoughtSignature(b"signature_for_call_2")

        messages: list[LLMMessage] = [
            AssistantMessage(
                role="assistant",
                content="I'll search the calendar and check the weather.",
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(
                            name="search_calendar",
                            arguments='{"query": "meetings"}',
                        ),
                        provider_metadata=GeminiProviderMetadata(
                            thought_signature=sig1
                        ),
                    ),
                    ToolCallItem(
                        id="call_2",
                        type="function",
                        function=ToolCallFunction(
                            name="get_weather",
                            arguments='{"location": "Paris"}',
                        ),
                        provider_metadata=GeminiProviderMetadata(
                            thought_signature=sig2
                        ),
                    ),
                ],
            )
        ]

        # Convert to GenAI format
        contents = google_client._convert_messages_to_genai_format(messages)

        assert len(contents) == 1
        content = cast("types.Content", contents[0])
        assert content.role == "model"

        # Should have 3 parts: text + 2 function calls
        assert content.parts is not None, "Content must have parts"
        parts = content.parts
        assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}"

        # Part 0: text (no signature)
        part0 = cast("types.Part", parts[0])
        assert hasattr(part0, "text")
        assert part0.text == "I'll search the calendar and check the weather."
        # May or may not have thought_signature attribute, but if present should be None
        if hasattr(part0, "thought_signature"):
            assert part0.thought_signature is None

        # Part 1: first function call with sig1
        part1 = cast("types.Part", parts[1])
        assert hasattr(part1, "function_call")
        assert part1.function_call is not None
        assert part1.function_call.name == "search_calendar"
        assert hasattr(part1, "thought_signature")
        assert part1.thought_signature == b"signature_for_call_1", (
            "First call should have sig1"
        )

        # Part 2: second function call with sig2
        part2 = cast("types.Part", parts[2])
        assert hasattr(part2, "function_call")
        assert part2.function_call is not None
        assert part2.function_call.name == "get_weather"
        assert hasattr(part2, "thought_signature")
        assert part2.thought_signature == b"signature_for_call_2", (
            "Second call should have sig2"
        )

    def test_function_call_without_signature_still_works(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that function calls without thought signatures work correctly.

        This ensures backward compatibility with models/scenarios that don't
        use thought signatures.
        """
        messages: list[LLMMessage] = [
            AssistantMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_xyz",
                        type="function",
                        function=ToolCallFunction(
                            name="get_weather",
                            arguments='{"location": "Tokyo"}',
                        ),
                        provider_metadata=None,
                    )
                ],
            )
        ]

        # Should not raise an exception
        contents = google_client._convert_messages_to_genai_format(messages)

        assert len(contents) == 1
        content = cast("types.Content", contents[0])
        assert content.parts is not None
        assert len(content.parts) == 1

        part = cast("types.Part", content.parts[0])
        assert hasattr(part, "function_call")
        assert part.function_call is not None
        assert part.function_call.name == "get_weather"

        # Should have the dummy thought signature as a workaround
        assert hasattr(part, "thought_signature")
        assert part.thought_signature == b"skip_thought_signature_validator", (
            "Should have dummy thought signature when none provided"
        )

    @pytest.mark.skip(
        reason="Text-only thought signatures not currently supported. "
        "Thought signatures are only attached to function calls in the current implementation. "
        "If needed in the future, implement support in _generate_response_stream and "
        "_convert_messages_to_genai_format for text-only messages with thought signatures."
    )
    def test_text_content_with_thought_signature(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that thought signatures can be attached to text parts too.

        While less common, text responses from thinking models can also have
        thought signatures for context preservation.

        NOTE: This test is currently skipped because we don't support thought
        signatures on text-only messages. The current implementation only
        associates thought signatures with function calls.
        """
        test_signature = b"thought_for_text_response"

        messages: list[LLMMessage] = [
            AssistantMessage(
                role="assistant",
                content="Based on my analysis, the answer is 42.",
            )
        ]

        contents = google_client._convert_messages_to_genai_format(messages)

        assert len(contents) == 1
        content = cast("types.Content", contents[0])
        assert content.parts is not None
        assert len(content.parts) == 1

        part = cast("types.Part", content.parts[0])
        assert hasattr(part, "text")
        assert part.text == "Based on my analysis, the answer is 42."
        assert hasattr(part, "thought_signature")
        assert part.thought_signature == test_signature

    def test_empty_provider_metadata_handled_gracefully(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that empty or invalid provider_metadata doesn't break conversion."""
        messages: list[LLMMessage] = [
            AssistantMessage(
                role="assistant",
                content="Hello",
            ),
            AssistantMessage(
                role="assistant",
                content="World",
            ),
            AssistantMessage(
                role="assistant",
                content="Test",
            ),
        ]

        # Should not raise exceptions
        contents = google_client._convert_messages_to_genai_format(messages)

        # Should create 3 content objects, all with text parts
        assert len(contents) == 3
        for content_union in contents:
            content = cast("types.Content", content_union)
            assert content.parts is not None
            assert len(content.parts) == 1
            part = cast("types.Part", content.parts[0])
            assert hasattr(part, "text")

    async def test_end_to_end_thought_signature_workflow(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """End-to-end test: streaming response → tool call → reconstruction.

        This test simulates the actual workflow:
        1. Model returns response with thought_signature and function_call
        2. We process it through generate_response_stream()
        3. We build a conversation with the tool call and response
        4. We convert it back for the next API call
        5. We verify thought_signature is correctly attached to function_call

        This catches the bug where thought_signatures are collected globally
        and attached to all tool calls with wrong part indices.

        Response structure that causes the bug:
        - Part 0: thought (reasoning, has signature but not a function call)
        - Part 1: text "I'll check the weather"
        - Part 2: function_call with signature

        When we emit this, ALL thought signatures (0 and 2) get attached to the tool call.
        When we reconstruct, we try to apply signatures at indices 0 and 2 to a
        2-part structure [text, function_call], causing index mismatch.
        """

        # Create a mock streaming response that mimics Google's API
        test_reasoning_sig = b"signature_for_reasoning"
        test_function_sig = b"signature_for_weather_call"

        # Part 0: Thought/reasoning part (has signature but no function call)
        thought_part = MagicMock()
        thought_part.text = "Let me think about this..."
        thought_part.thought = True  # This is a thought/reasoning part
        thought_part.thought_signature = test_reasoning_sig
        thought_part.function_call = None

        # Part 1: Text part (no signature, no function call)
        text_part = MagicMock()
        text_part.text = "I'll check the weather for you."
        text_part.thought = False
        text_part.thought_signature = None
        text_part.function_call = None

        # Part 2: Function call part (has its own signature)
        function_call_part = MagicMock()
        function_call_part.text = None
        function_call_part.thought = False
        function_call_part.thought_signature = test_function_sig
        function_call_part.function_call = MagicMock()
        function_call_part.function_call.name = "get_weather"
        function_call_part.function_call.args = {"location": "Paris"}

        # Create mock candidate and chunk
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [thought_part, text_part, function_call_part]

        mock_chunk = MagicMock()
        mock_chunk.text = None
        mock_chunk.candidates = [mock_candidate]

        # Mock the streaming API call
        async def mock_stream() -> "AsyncIterator[MagicMock]":
            yield mock_chunk

        google_client.client.aio.models.generate_content_stream = AsyncMock(
            return_value=mock_stream()
        )

        # Step 1: Process streaming response
        events = []
        async for event in google_client.generate_response_stream(
            messages=[UserMessage(role="user", content="What's the weather in Paris?")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                        },
                    },
                }
            ],
        ):
            events.append(event)

        # Extract the tool call event
        tool_call_events = [e for e in events if e.type == "tool_call"]
        assert len(tool_call_events) == 1, "Should have exactly one tool call event"

        tool_call = tool_call_events[0].tool_call
        assert tool_call is not None

        # Verify provider_metadata was created
        assert tool_call.provider_metadata is not None, (
            "Tool call must have provider_metadata"
        )

        # Step 2: Build conversation history like the real application does
        messages = [
            UserMessage(
                role="user",
                content="What's the weather in Paris?",
            ),
            AssistantMessage(
                role="assistant",
                content="I'll check the weather for you.",
                tool_calls=[
                    ToolCallItem(
                        id=tool_call.id,
                        type="function",
                        function=ToolCallFunction(
                            name="get_weather",
                            arguments='{"location": "Paris"}',
                        ),
                        provider_metadata=tool_call.provider_metadata,
                    )
                ],
            ),
            ToolMessage(
                role="tool",
                tool_call_id=tool_call.id,
                name="get_weather",
                content='{"temperature": 72, "condition": "sunny"}',
            ),
        ]

        # Step 3: Convert back for next API call (this is where the bug manifests)
        contents = google_client._convert_messages_to_genai_format(messages)

        # Step 4: Verify the assistant message has thought_signature on function_call
        # Find the assistant (model) content
        assistant_contents = [
            c for c in contents if cast("genai_types.Content", c).role == "model"
        ]  # type: ignore[attr-defined]
        assert len(assistant_contents) > 0, "Should have assistant content"

        assistant_content = cast("genai_types.Content", assistant_contents[0])
        assert assistant_content.parts is not None, "Assistant content must have parts"

        # Find the function_call part
        function_call_parts = [
            p
            for p in assistant_content.parts
            if hasattr(cast("genai_types.Part", p), "function_call")
            and cast("genai_types.Part", p).function_call is not None
        ]
        assert len(function_call_parts) == 1, (
            "Should have exactly one function_call part"
        )

        fc_part = cast("genai_types.Part", function_call_parts[0])

        # CRITICAL ASSERTION: The function_call part MUST have thought_signature
        assert hasattr(fc_part, "thought_signature"), (
            "Function call part must have thought_signature attribute"
        )
        assert fc_part.thought_signature is not None, (
            "Function call part must have non-None thought_signature"
        )
        assert fc_part.thought_signature == test_function_sig, (
            f"Thought signature mismatch: expected {test_function_sig}, "
            f"got {fc_part.thought_signature}"
        )
