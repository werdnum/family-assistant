"""Integration tests for the Gemini Computer Use profile."""

import os
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


@pytest.fixture
async def gemini_client() -> AsyncGenerator[GoogleGenAIClient]:
    """Create a GoogleGenAIClient instance for testing."""
    api_key = os.getenv("GEMINI_API_KEY", "dummy_key")
    # Use the computer use model to trigger the specific logic
    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.5-computer-use-preview-10-2025",
        enable_url_context=False,
        enable_google_search=False,
    )
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_computer_use_tool_injection(gemini_client: GoogleGenAIClient) -> None:
    """Test that the Computer Use tool is automatically injected for the correct model."""

    # Verify the model detection logic
    assert gemini_client._is_computer_use_model("gemini-2.5-computer-use-preview-10-2025")
    assert not gemini_client._is_computer_use_model("gemini-1.5-pro")

    # Mock the underlying SDK client generate_content method
    # We want to verify that `tools` in the config contains the ComputerUse tool
    with patch.object(gemini_client.client.aio.models, "generate_content") as mock_generate:
        # Mock response to avoid actual API call failure if key is dummy
        part_mock = MagicMock(text="Response")
        # Ensure thought_signature is None so it doesn't trigger processing
        part_mock.thought_signature = None
        part_mock.function_call = None

        mock_response = MagicMock()
        mock_response.candidates = [
            MagicMock(content=MagicMock(parts=[part_mock]))
        ]
        mock_generate.return_value = mock_response

        # Call generate_response with some dummy messages
        messages = [UserMessage(content="Navigate to google.com")]

        # We pass some dummy tools to verify filtering logic too
        dummy_tools = [
            {
                "type": "function",
                "function": {
                    "name": "click_at",  # Should be filtered out
                    "description": "Manual click_at",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "other_tool",  # Should be kept
                    "description": "Some other tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

        await gemini_client.generate_response(messages, tools=dummy_tools)

        # Verify arguments passed to generate_content
        call_args = mock_generate.call_args
        assert call_args is not None
        _, kwargs = call_args

        config = kwargs.get("config")
        assert config is not None
        assert isinstance(config, types.GenerateContentConfig)

        # Check tools
        tools_passed = config.tools
        assert tools_passed is not None

        # Verify ComputerUse tool is present
        has_computer_use = False
        for tool in tools_passed:
            # Check if tool has computer_use attribute (SDK object) or key (dict)
            if hasattr(tool, "computer_use") and tool.computer_use:
                has_computer_use = True
                assert tool.computer_use.environment == types.Environment.ENVIRONMENT_BROWSER
                break
            # Fallback for if it's a dict or other structure (though SDK usually uses objects)
            elif isinstance(tool, dict) and "computer_use" in tool:
                 has_computer_use = True
                 break

        if not has_computer_use:
            print(f"\nDEBUG: Tools passed: {tools_passed}")
            for t in tools_passed:
                print(f"Tool type: {type(t)}")
                print(f"Tool vars: {vars(t) if hasattr(t, '__dict__') else 'no dict'}")

        assert has_computer_use, "Computer Use tool was not injected"

        # Verify manual 'click_at' was filtered out, but 'other_tool' remains
        has_click_at = False
        has_other_tool = False

        for tool in tools_passed:
            if hasattr(tool, "function_declarations") and tool.function_declarations:
                for func in tool.function_declarations:
                    if func.name == "click_at":
                        has_click_at = True
                    if func.name == "other_tool":
                        has_other_tool = True

        assert not has_click_at, "Manual 'click_at' tool definition should be filtered out"
        assert has_other_tool, "'other_tool' should be preserved"


@pytest.mark.asyncio
async def test_computer_use_end_to_end_flow(gemini_client: GoogleGenAIClient) -> None:
    """Test the end-to-end flow of tool calling and response handling with Computer Use."""
    # This test verifies that the client correctly processes a Computer Use function call
    # and prepares the next request.

    # 1. Mock the model returning a 'click_at' call
    with patch.object(gemini_client.client.aio.models, "generate_content") as mock_generate:
        # Construct a response with a function call
        mock_response = MagicMock()

        # Mock Part with function_call
        function_call_part = MagicMock()
        function_call_part.text = None
        function_call_part.function_call = MagicMock()
        function_call_part.function_call.name = "click_at"
        function_call_part.function_call.args = {"x": 500, "y": 300}
        function_call_part.function_call.id = "call_123"
        function_call_part.thought_signature = None  # Optional thought signature

        # Mock Candidate
        candidate = MagicMock()
        candidate.content.parts = [function_call_part]
        mock_response.candidates = [candidate]
        mock_response.usage_metadata = MagicMock()

        mock_generate.return_value = mock_response

        # Execute
        messages = [UserMessage(content="Click the button")]
        response = await gemini_client.generate_response(messages)

        # Verify we got a tool call
        assert response.tool_calls is not None
        assert len(response.tool_calls) == 1
        tool_call = response.tool_calls[0]
        assert tool_call.function.name == "click_at"
        assert tool_call.function.arguments == {"x": 500, "y": 300}


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("GEMINI_API_KEY") is None, reason="Requires GEMINI_API_KEY")
@pytest.mark.asyncio
async def test_real_gemini_computer_use_protocol() -> None:
    """
    Test against the real Gemini API to verify the protocol works.

    This test sends a request to the real model (if key is present) and verifies
    it doesn't crash when configured with Computer Use.
    """
    api_key = os.environ["GEMINI_API_KEY"]
    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.5-computer-use-preview-10-2025",
    )

    try:
        # We don't need to actually execute a browser action here (that's heavy),
        # but we want to verify the model accepts our tool configuration.
        # We'll ask it something that *might* trigger a tool call or at least a text response.
        messages = [UserMessage(content="What is the current time?")]

        # We pass no extra tools, relying on the auto-injection
        response = await client.generate_response(messages)

        # We just want to ensure we got a valid response (text or tool call)
        # and no API error about invalid tool configuration.
        assert response.content is not None or response.tool_calls is not None

    finally:
        await client.close()
