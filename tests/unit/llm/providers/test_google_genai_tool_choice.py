"""Unit tests for Google GenAI tool_choice parameter handling.

Tests the tool_choice parameter implementation in GoogleGenAIClient, verifying
that different tool_choice values correctly configure the FunctionCallingConfig.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from family_assistant.llm import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


class TestGoogleGenAIToolChoice:
    """Tests for tool_choice parameter handling in Google GenAI client."""

    @pytest.fixture
    def google_client(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for testing."""
        return GoogleGenAIClient(
            api_key="test_key_for_unit_tests", model="gemini-2.0-flash"
        )

    @pytest.fixture
    # ast-grep-ignore: no-dict-any - Test fixtures use dict for mock tool definitions
    def sample_tools(self) -> list[dict[str, Any]]:
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
    # ast-grep-ignore-block: no-dict-any - Test parameters use dict[str, Any] for flexible mock data
    async def test_tool_choice_required_sets_any_mode(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='required' sets FunctionCallingConfigMode.ANY."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Tool response"
            mock_generate.return_value = mock_response

            # Call generate_response with tool_choice="required"
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tool_config is set with mode ANY
            assert config is not None
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that specific tool_choice restricts to allowed_function_names."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Tool response"
            mock_generate.return_value = mock_response

            # Call generate_response with specific tool name
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="attach_to_response",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tool_config is set with mode ANY and allowed_function_names
            assert config is not None
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='auto' doesn't set restrictive tool config."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Tool response"
            mock_generate.return_value = mock_response

            # Call generate_response with tool_choice="auto" (default)
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="auto",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tool_config is not set for "auto" mode
            # In "auto" mode, we rely on the model's default behavior
            assert config is not None
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='none' sets FunctionCallingConfigMode.NONE."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Text only response"
            mock_generate.return_value = mock_response

            # Call generate_response with tool_choice="none"
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="none",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tool_config is set with mode NONE
            assert config is not None
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='none' prevents tools from being included in config."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Text only response"
            mock_generate.return_value = mock_response

            # Call generate_response with tool_choice="none" and tools provided
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="none",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tools are not included in generation config
            # When tool_choice="none", _prepare_all_tools should return empty list
            assert config is not None
            assert config.tools is None or len(config.tools) == 0

    @pytest.mark.asyncio
    async def test_tool_choice_required_includes_tools(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='required' includes tools in config."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Tool response"
            mock_generate.return_value = mock_response

            # Call generate_response with tool_choice="required"
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tools are included in generation config
            assert config is not None
            assert config.tools is not None
            assert len(config.tools) > 0

    @pytest.mark.asyncio
    async def test_tool_choice_specific_tool_includes_tools(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that specific tool_choice includes tools in config."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Tool response"
            mock_generate.return_value = mock_response

            # Call generate_response with specific tool name
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="search_calendar",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify tools are included in generation config
            assert config is not None
            assert config.tools is not None
            assert len(config.tools) > 0

    @pytest.mark.asyncio
    async def test_tool_choice_parameter_passed_to_generate_response(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice parameter is properly passed through."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Response"
            mock_generate.return_value = mock_response

            # Test with different tool_choice values
            tool_choice_values = ["auto", "none", "required", "attach_to_response"]

            for tool_choice in tool_choice_values:
                await google_client.generate_response(
                    sample_messages,
                    tools=sample_tools,
                    tool_choice=tool_choice,
                )

                # Verify each call was made
                assert mock_generate.called

    @pytest.mark.asyncio
    async def test_automatic_function_calling_disabled(
        self,
        google_client: GoogleGenAIClient,
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that automatic function calling is disabled when tools are provided."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Response"
            mock_generate.return_value = mock_response

            # Call generate_response with tools
            await google_client.generate_response(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            )

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # Verify automatic_function_calling is disabled
            assert config is not None
            assert config.automatic_function_calling is not None
            assert config.automatic_function_calling.disable is True

    @pytest.mark.asyncio
    async def test_tool_choice_with_empty_tools_list(
        self,
        google_client: GoogleGenAIClient,
        sample_messages: list[Any],
    ) -> None:
        """Test tool_choice behavior when no tools are provided."""
        with patch.object(
            google_client.client.aio.models, "generate_content", new_callable=AsyncMock
        ) as mock_generate:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.text = "Response without tools"
            mock_generate.return_value = mock_response

            # Call generate_response without tools
            await google_client.generate_response(
                sample_messages,
                tools=[],
                tool_choice="required",
            )

            # Verify generate_content was called
            assert mock_generate.called

            # Get the config argument
            call_args = mock_generate.call_args
            config = call_args.kwargs.get("config")

            # When tools are empty, config.tools should be empty/None
            assert config is not None
            assert config.tools is None or len(config.tools) == 0


class TestGoogleGenAIToolChoiceStreaming:
    """Tests for tool_choice parameter in streaming mode."""

    @pytest.fixture
    def google_client(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for testing."""
        return GoogleGenAIClient(
            api_key="test_key_for_unit_tests", model="gemini-2.0-flash"
        )

    # ast-grep-ignore: no-dict-any - Test fixtures use dict for mock tool definitions
    @pytest.fixture
    def sample_tools(self) -> list[dict[str, Any]]:
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='required' works in streaming mode."""
        with patch.object(
            google_client.client.aio.models,
            "generate_content_stream",
            new_callable=AsyncMock,
        ) as mock_stream:
            # Setup mock streaming response
            mock_response = AsyncMock()
            mock_response.__aiter__.return_value = AsyncMock()
            mock_response.__aiter__.return_value.__anext__.side_effect = (
                StopAsyncIteration()
            )
            mock_stream.return_value = mock_response

            # Consume the stream
            async for _ in google_client.generate_response_stream(
                sample_messages,
                tools=sample_tools,
                tool_choice="required",
            ):
                pass

            # Verify stream was called
            assert mock_stream.called

            # Get the config argument
            call_args = mock_stream.call_args
            config = call_args.kwargs.get("config")

            # Verify tool_config is set
            assert config is not None
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that specific tool_choice works in streaming mode."""
        with patch.object(
            google_client.client.aio.models,
            "generate_content_stream",
            new_callable=AsyncMock,
        ) as mock_stream:
            # Setup mock streaming response
            mock_response = AsyncMock()
            mock_response.__aiter__.return_value = AsyncMock()
            mock_response.__aiter__.return_value.__anext__.side_effect = (
                StopAsyncIteration()
            )
            mock_stream.return_value = mock_response

            # Consume the stream
            async for _ in google_client.generate_response_stream(
                sample_messages,
                tools=sample_tools,
                tool_choice="test_tool",
            ):
                pass

            # Verify stream was called
            assert mock_stream.called

            # Get the config argument
            call_args = mock_stream.call_args
            config = call_args.kwargs.get("config")

            # Verify allowed_function_names is set
            assert config is not None
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
        sample_tools: list[dict[str, Any]],
        sample_messages: list[Any],
    ) -> None:
        """Test that tool_choice='none' works in streaming mode."""
        with patch.object(
            google_client.client.aio.models,
            "generate_content_stream",
            new_callable=AsyncMock,
        ) as mock_stream:
            # Setup mock streaming response
            mock_response = AsyncMock()
            mock_response.__aiter__.return_value = AsyncMock()
            mock_response.__aiter__.return_value.__anext__.side_effect = (
                StopAsyncIteration()
            )
            mock_stream.return_value = mock_response

            # Consume the stream
            async for _ in google_client.generate_response_stream(
                sample_messages,
                tools=sample_tools,
                tool_choice="none",
            ):
                pass

            # Verify stream was called
            assert mock_stream.called

            # Get the config argument
            call_args = mock_stream.call_args
            config = call_args.kwargs.get("config")

            # Verify mode is NONE
            assert config is not None
            assert config.tool_config is not None
            assert config.tool_config.function_calling_config is not None

            assert (
                config.tool_config.function_calling_config.mode
                == types.FunctionCallingConfigMode.NONE
            )
