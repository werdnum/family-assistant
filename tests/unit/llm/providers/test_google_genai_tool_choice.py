"""Unit tests for Google GenAI tool_choice parameter handling.

Tests the tool_choice parameter implementation in GoogleGenAIClient, verifying
that different tool_choice values correctly configure the FunctionCallingConfig.
These cover the generateContent path (``use_interactions_api: false``); the
Interactions mapping is tested in ``test_gemini_interactions.py``.
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from google.genai import types

from family_assistant.llm import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolDefinition
from tests.mocks.recording_async_call import RecordedCall, RecordingAsyncCall


def text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)])
            )
        ]
    )


async def stream_of(
    *responses: types.GenerateContentResponse,
) -> AsyncIterator[types.GenerateContentResponse]:
    for response in responses:
        yield response


def sent_config(calls: list[RecordedCall]) -> types.GenerateContentConfig:
    """The generation config of the single call made to the SDK."""
    assert len(calls) == 1
    config = calls[0].kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    return config


class TestGoogleGenAIToolChoice:
    """Tests for tool_choice parameter handling in Google GenAI client."""

    @pytest.fixture
    def google_client(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for testing."""
        return GoogleGenAIClient(
            api_key="test_key_for_unit_tests",
            model="gemini-2.0-flash",
            use_interactions_api=False,
        )

    @pytest.fixture
    def sample_tools(self) -> list[ToolDefinition]:
        """Create sample tool definitions for testing."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "attach_to_response",
                    "description": "Attach file to response",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "Name of file",
                            }
                        },
                        "required": ["filename"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_calendar",
                    "description": "Search calendar events",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"}
                        },
                        "required": ["query"],
                    },
                },
            },
        ]

    # ast-grep-ignore: no-dict-any - Sample messages use generic list[Any] for test flexibility
    @pytest.fixture
    def sample_messages(self) -> list[Any]:
        """Create sample messages for testing."""
        return [UserMessage(content="Hello, can you help me?")]

    @pytest.mark.asyncio
    async def test_tool_choice_required_sets_any_mode(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='required' sets FunctionCallingConfigMode.ANY."""
        generate = RecordingAsyncCall(text_response("Tool response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with tool_choice="required"
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            )

            config = sent_config(generate.calls)

            # Verify tool_config is set with mode ANY
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None

            assert (
                config.tool_config.function_calling_config.mode
                == types.FunctionCallingConfigMode.ANY
            )

            # Verify no allowed_function_names restriction when tool_choice="required"
            assert (
                config.tool_config.function_calling_config.allowed_function_names
                is None
                or len(
                    config.tool_config.function_calling_config.allowed_function_names
                )
                == 0
            )

    @pytest.mark.asyncio
    async def test_tool_choice_specific_tool_sets_allowed_names(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that specific tool_choice restricts to allowed_function_names."""
        generate = RecordingAsyncCall(text_response("Tool response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with specific tool name
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="attach_to_response",
            )

            config = sent_config(generate.calls)

            # Verify tool_config is set with mode ANY and allowed_function_names
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None

            assert (
                config.tool_config.function_calling_config.mode
                == types.FunctionCallingConfigMode.ANY
            )

            # Verify allowed_function_names is set to the specific tool
            assert (
                config.tool_config.function_calling_config.allowed_function_names
                == ["attach_to_response"]
            )

    @pytest.mark.asyncio
    async def test_tool_choice_auto_no_restrictive_config(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='auto' doesn't set restrictive tool config."""
        generate = RecordingAsyncCall(text_response("Tool response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with tool_choice="auto" (default)
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="auto",
            )

            config = sent_config(generate.calls)

            # Verify tool_config is not set for "auto" mode
            # In "auto" mode, we rely on the model's default behavior
            # tool_config should be None or not contain function_calling_config
            if config.tool_config is not None:
                # If tool_config exists, it shouldn't have function_calling_config set
                assert (
                    config.tool_config.function_calling_config is None
                    or not hasattr(config.tool_config, "function_calling_config")
                )

    @pytest.mark.asyncio
    async def test_tool_choice_none_sets_none_mode(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='none' sets FunctionCallingConfigMode.NONE."""
        generate = RecordingAsyncCall(text_response("Text only response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with tool_choice="none"
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="none",
            )

            config = sent_config(generate.calls)

            # Verify tool_config is set with mode NONE
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None

            assert (
                config.tool_config.function_calling_config.mode
                == types.FunctionCallingConfigMode.NONE
            )

    @pytest.mark.asyncio
    async def test_tool_choice_none_prevents_tool_inclusion(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='none' prevents tools from being included in config."""
        generate = RecordingAsyncCall(text_response("Text only response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with tool_choice="none" and tools provided
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="none",
            )

            config = sent_config(generate.calls)

            # Verify tools are not included in generation config
            # When tool_choice="none", _prepare_all_tools should return empty list
            assert config.tools is None or len(config.tools) == 0

    @pytest.mark.asyncio
    async def test_tool_choice_required_includes_tools(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='required' includes tools in config."""
        generate = RecordingAsyncCall(text_response("Tool response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with tool_choice="required"
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            )

            config = sent_config(generate.calls)

            # Verify tools are included in generation config
            assert config.tools is not None
            assert len(config.tools) > 0

    @pytest.mark.asyncio
    async def test_tool_choice_specific_tool_includes_tools(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that specific tool_choice includes tools in config."""
        generate = RecordingAsyncCall(text_response("Tool response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with specific tool name
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="search_calendar",
            )

            config = sent_config(generate.calls)

            # Verify tools are included in generation config
            assert config.tools is not None
            assert len(config.tools) > 0

    @pytest.mark.asyncio
    async def test_automatic_function_calling_disabled(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that automatic function calling is disabled when tools are provided."""
        generate = RecordingAsyncCall(text_response("Response"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response with tools
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            )

            config = sent_config(generate.calls)

            # Verify automatic_function_calling is disabled
            assert config.automatic_function_calling is not None
            assert config.automatic_function_calling.disable is True

    @pytest.mark.asyncio
    async def test_tool_choice_with_empty_tools_list(
        self,
        google_client: GoogleGenAIClient,
        sample_messages: list[Any],
    ) -> None:
        """Test tool_choice behavior when no tools are provided."""
        generate = RecordingAsyncCall(text_response("Response without tools"))
        with patch.object(
            google_client.client.aio.models, "generate_content", new=generate
        ):
            # Call generate_response without tools
            await google_client.generate_response(
                sample_messages,
                tools=[],
                tool_choice="required",
            )

            config = sent_config(generate.calls)

            # When tools are empty, config.tools should be empty/None
            assert config.tools is None or len(config.tools) == 0


class TestGoogleGenAIToolChoiceStreaming:
    """Tests for tool_choice parameter in streaming mode."""

    @pytest.fixture
    def google_client(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for testing."""
        return GoogleGenAIClient(
            api_key="test_key_for_unit_tests",
            model="gemini-2.0-flash",
            use_interactions_api=False,
        )

    @pytest.fixture
    def sample_tools(self) -> list[ToolDefinition]:
        """Create sample tool definitions for testing."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "param": {"type": "string", "description": "A parameter"}
                        },
                        "required": ["param"],
                    },
                },
            }
        ]

    @pytest.fixture
    def sample_messages(self) -> list[Any]:
        """Create sample messages for testing."""
        return [UserMessage(content="Hello")]

    @pytest.mark.asyncio
    async def test_stream_tool_choice_required(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='required' works in streaming mode."""
        stream = RecordingAsyncCall(stream_of(text_response("Streamed")))
        with patch.object(
            google_client.client.aio.models, "generate_content_stream", new=stream
        ):
            # Consume the stream
            async for _ in google_client.generate_response_stream(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            ):
                pass

            config = sent_config(stream.calls)

            # Verify tool_config is set
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None

            assert (
                config.tool_config.function_calling_config.mode
                == types.FunctionCallingConfigMode.ANY
            )

    @pytest.mark.asyncio
    async def test_stream_tool_choice_specific_tool(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that specific tool_choice works in streaming mode."""
        stream = RecordingAsyncCall(stream_of(text_response("Streamed")))
        with patch.object(
            google_client.client.aio.models, "generate_content_stream", new=stream
        ):
            # Consume the stream
            async for _ in google_client.generate_response_stream(
                sample_messages,
                tools=sample_tools,
                tool_choice="test_tool",
            ):
                pass

            config = sent_config(stream.calls)

            # Verify allowed_function_names is set
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None
            assert (
                config.tool_config.function_calling_config.allowed_function_names
                == ["test_tool"]
            )

    @pytest.mark.asyncio
    async def test_stream_tool_choice_none(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[ToolDefinition],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='none' works in streaming mode."""
        stream = RecordingAsyncCall(stream_of(text_response("Streamed")))
        with patch.object(
            google_client.client.aio.models, "generate_content_stream", new=stream
        ):
            # Consume the stream
            async for _ in google_client.generate_response_stream(
                sample_messages,
                tools=sample_tools,
                tool_choice="none",
            ):
                pass

            config = sent_config(stream.calls)

            # Verify mode is NONE
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None

            assert (
                config.tool_config.function_calling_config.mode
                == types.FunctionCallingConfigMode.NONE
            )
