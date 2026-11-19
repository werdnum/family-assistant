"""Unit tests for Gemini native computer use client configuration."""

from typing import Any, cast
from unittest.mock import patch

import pytest
from google.genai import types

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from tests.mocks.recording_async_call import RecordingAsyncCall


def text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)])
            )
        ]
    )


class TestComputerUseClientConfiguration:
    """Tests for GoogleGenAIClient computer use configuration."""

    @pytest.mark.asyncio
    async def test_computer_use_tool_injection(self) -> None:
        """Test that ComputerUse tool is injected when enabled."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.8-flash",
            enable_computer_use=True,
        )

        generate = RecordingAsyncCall(text_response("Response"))
        with patch.object(client.client.aio.models, "generate_content", new=generate):
            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages)

            # Verify the call included the ComputerUse tool
            config = generate.last_call.kwargs["config"]
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
            model="gemini-3.8-flash",
            enable_computer_use=True,
            computer_use_excluded_functions=excluded,
        )

        generate = RecordingAsyncCall(text_response("Response"))
        with patch.object(client.client.aio.models, "generate_content", new=generate):
            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages)

            # Verify excluded functions are passed
            config = generate.last_call.kwargs["config"]
            assert isinstance(config, types.GenerateContentConfig)
            tools_passed = config.tools
            assert tools_passed is not None

            computer_use_tools = [
                tool.computer_use
                for tool in tools_passed
                if isinstance(tool, types.Tool) and tool.computer_use is not None
            ]
            assert len(computer_use_tools) == 1
            assert computer_use_tools[0].excluded_predefined_functions == excluded

        await client.close()

    @pytest.mark.asyncio
    async def test_computer_use_function_names_filtered(self) -> None:
        """Test that computer use function names are filtered from manual tools."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.8-flash",
            enable_computer_use=True,
        )

        generate = RecordingAsyncCall(text_response("Response"))
        with patch.object(client.client.aio.models, "generate_content", new=generate):
            computer_use_names = (
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
            )
            test_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": f"Manual {name}",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
                for name in (*computer_use_names, "regular_tool")
            ]

            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages, tools=cast("Any", test_tools))

            # Verify filtering
            config = generate.last_call.kwargs["config"]
            assert isinstance(config, types.GenerateContentConfig)
            tools_passed = config.tools
            assert tools_passed is not None

            function_names = [
                declaration.name
                for tool in tools_passed
                if isinstance(tool, types.Tool) and tool.function_declarations
                for declaration in tool.function_declarations
            ]
            assert function_names == ["regular_tool"]

        await client.close()

    @pytest.mark.asyncio
    async def test_no_computer_use_tool_when_disabled(self) -> None:
        """Test that ComputerUse tool is not injected when disabled."""
        client = GoogleGenAIClient(
            api_key="test_key",
            model="gemini-3.8-flash",
            enable_computer_use=False,
            use_interactions_api=False,
        )

        generate = RecordingAsyncCall(text_response("Response"))
        with patch.object(client.client.aio.models, "generate_content", new=generate):
            messages = [UserMessage(content="Test message")]
            await client.generate_response(messages)

            # Verify no ComputerUse tool
            config = generate.last_call.kwargs["config"]
            assert isinstance(config, types.GenerateContentConfig)
            tools_passed = config.tools

            # Should be None if no tools provided
            assert tools_passed is None or len(tools_passed) == 0

        await client.close()
