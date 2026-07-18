"""Unit tests for Gemini native computer use client configuration."""

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES


class TestComputerUseClientConfiguration:
    """Tests for GoogleGenAIClient computer use configuration."""

    @pytest.mark.asyncio
    async def test_client_with_enable_computer_use_true(self) -> None:
        """Test client initialization with enable_computer_use=True."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=True,
        )

        assert client.enable_computer_use is True
        assert client.computer_use_excluded_functions is None

        await client.close()

    @pytest.mark.asyncio
    async def test_client_with_excluded_functions(self) -> None:
        """Test client initialization with excluded functions."""
        excluded = ["double_click", "triple_click"]
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=True,
            computer_use_excluded_functions=excluded,
        )

        assert client.enable_computer_use is True
        assert client.computer_use_excluded_functions == excluded

        await client.close()

    @pytest.mark.asyncio
    async def test_client_without_computer_use(self) -> None:
        """Test client without computer use enabled."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=False,
        )

        assert client.enable_computer_use is False
        assert client.computer_use_excluded_functions is None

        await client.close()

    @pytest.mark.asyncio
    async def test_computer_use_tool_injection(self) -> None:
        """Test that ComputerUse tool is injected when enabled."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=True,
        )

        with patch.object(
            client.client.aio.models, "generate_content"
        ) as mock_generate:
            # Mock response
            part_mock = MagicMock(text="Response")
            part_mock.thought_signature = None
            part_mock.function_call = None

            mock_response = MagicMock()
            mock_response.candidates = [MagicMock(content=MagicMock(parts=[part_mock]))]
            mock_generate.return_value = mock_response

            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages)

            # Verify the call included the ComputerUse tool
            call_args = mock_generate.call_args
            assert call_args is not None
            _, kwargs = call_args

            config = kwargs.get("config")
            assert config is not None
            assert isinstance(config, types.GenerateContentConfig)

            tools_passed = config.tools
            assert tools_passed is not None

            # Find the ComputerUse tool
            has_computer_use = False
            for tool in tools_passed:
                if isinstance(tool, types.Tool) and tool.computer_use:
                    has_computer_use = True
                    assert (
                        tool.computer_use.environment
                        == types.Environment.ENVIRONMENT_BROWSER
                    )
                    assert tool.computer_use.enable_prompt_injection_detection is True
                    break

            assert has_computer_use

        await client.close()

    @pytest.mark.asyncio
    async def test_computer_use_excluded_functions_passed(self) -> None:
        """Test that excluded functions are passed to the ComputerUse tool."""
        excluded = ["double_click", "triple_click"]
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=True,
            computer_use_excluded_functions=excluded,
        )

        with patch.object(
            client.client.aio.models, "generate_content"
        ) as mock_generate:
            # Mock response
            part_mock = MagicMock(text="Response")
            part_mock.thought_signature = None
            part_mock.function_call = None

            mock_response = MagicMock()
            mock_response.candidates = [MagicMock(content=MagicMock(parts=[part_mock]))]
            mock_generate.return_value = mock_response

            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages)

            # Verify excluded functions are passed
            call_args = mock_generate.call_args
            assert call_args is not None
            _, kwargs = call_args

            config = kwargs.get("config")
            tools_passed = config.tools
            assert tools_passed is not None

            for tool in tools_passed:
                if isinstance(tool, types.Tool) and tool.computer_use:
                    assert tool.computer_use.excluded_predefined_functions == excluded
                    break

        await client.close()

    @pytest.mark.asyncio
    async def test_computer_use_function_names_filtered(self) -> None:
        """Test that computer use function names are filtered from manual tools."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=True,
        )

        with patch.object(
            client.client.aio.models, "generate_content"
        ) as mock_generate:
            # Mock response
            part_mock = MagicMock(text="Response")
            part_mock.thought_signature = None
            part_mock.function_call = None

            mock_response = MagicMock()
            mock_response.candidates = [MagicMock(content=MagicMock(parts=[part_mock]))]
            mock_generate.return_value = mock_response

            # Create test tools with both computer use names and regular names
            test_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "click",  # Computer use name - should be filtered
                        "description": "Manual click",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "type",  # Computer use name - should be filtered
                        "description": "Manual type",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "regular_tool",  # Not a computer use name - should be kept
                        "description": "Some other tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ]

            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages, tools=cast("Any", test_tools))

            # Verify filtering
            call_args = mock_generate.call_args
            assert call_args is not None
            _, kwargs = call_args

            config = kwargs.get("config")
            tools_passed = config.tools
            assert tools_passed is not None

            # Find function declarations
            found_click = False
            found_type = False
            found_regular = False

            for tool in tools_passed:
                if isinstance(tool, types.Tool) and tool.function_declarations:
                    for func in tool.function_declarations:
                        if func.name == "click":
                            found_click = True
                        if func.name == "type":
                            found_type = True
                        if func.name == "regular_tool":
                            found_regular = True

            assert not found_click, "Computer use 'click' should be filtered"
            assert not found_type, "Computer use 'type' should be filtered"
            assert found_regular, "Regular tool should be kept"

        await client.close()

    @pytest.mark.asyncio
    async def test_no_computer_use_tool_when_disabled(self) -> None:
        """Test that ComputerUse tool is not injected when disabled."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.5-flash",
            enable_computer_use=False,
        )

        with patch.object(
            client.client.aio.models, "generate_content"
        ) as mock_generate:
            # Mock response
            part_mock = MagicMock(text="Response")
            part_mock.thought_signature = None
            part_mock.function_call = None

            mock_response = MagicMock()
            mock_response.candidates = [MagicMock(content=MagicMock(parts=[part_mock]))]
            mock_generate.return_value = mock_response

            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages)

            # Verify no ComputerUse tool
            call_args = mock_generate.call_args
            assert call_args is not None
            _, kwargs = call_args

            config = kwargs.get("config")
            tools_passed = config.tools

            # Should be None if no tools provided
            assert tools_passed is None or len(tools_passed) == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_computer_use_function_names_constant_used(self) -> None:
        """Test that COMPUTER_USE_FUNCTION_NAMES constant is used for filtering."""
        # Verify the constant contains the expected Gemini 3.5 function names
        expected_names = {
            "click",
            "double_click",
            "triple_click",
            "middle_click",
            "right_click",
            "mouse_down",
            "mouse_up",
            "move",
            "type",
            "drag_and_drop",
            "press_key",
            "key_down",
            "key_up",
            "hotkey",
            "scroll",
            "navigate",
            "go_back",
            "go_forward",
            "take_screenshot",
            "wait",
        }

        assert expected_names == COMPUTER_USE_FUNCTION_NAMES
