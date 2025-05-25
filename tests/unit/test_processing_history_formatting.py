"""
Unit tests for the history formatting logic in ProcessingService.
"""

import json
from typing import Any
from unittest.mock import Mock

import pytest

from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.tools.types import ToolExecutionContext


# Mock interfaces required by ProcessingService constructor
class MockLLMClient:
    async def generate_response(self, *args: Any, **kwargs: Any) -> Mock:
        return Mock()  # Not used in the tested method

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # Add any other params from the protocol if necessary, though not used here
        **_kwargs: Any,  # Capture other potential arguments
    ) -> dict[str, Any]:
        # Return a simple dict structure, content not important for these tests
        return {"role": "user", "content": prompt_text or ""}


class MockToolsProvider:
    async def get_tool_definitions(
        self, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return []  # Not used

    async def execute_tool(
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        # Actual execution logic not needed for these tests, just conform to signature
        # The protocol expects a string return.
        return f"Executed tool {name} with args {arguments} in context {context}"

    async def close(self) -> None:
        pass  # Not used


# --- Test Setup ---


@pytest.fixture
def processing_service() -> ProcessingService:
    """Provides a ProcessingService instance with mock dependencies."""
    mock_service_config = ProcessingServiceConfig(
        prompts={},  # Not used by _format_history_for_llm
        calendar_config={},  # Not used
        timezone_str="UTC",  # Not used
        max_history_messages=10,  # Not used
        history_max_age_hours=1,  # Not used
    )
    return ProcessingService(
        llm_client=MockLLMClient(),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,  # Use the config object
        context_providers=[],
        server_url="http://test.com",  # Not used
        app_config={},  # Add dummy app_config
    )


# --- Test Cases ---


def test_format_simple_history(processing_service: ProcessingService) -> None:
    """Test formatting a simple user-assistant conversation."""
    history_messages = [
        {"role": "user", "content": "Hello", "tool_calls_info_raw": None},
        {"role": "assistant", "content": "Hi there!", "tool_calls_info_raw": None},
    ]
    expected_output = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    actual_output = processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output  # Marked line 120


def test_format_history_with_tool_call(processing_service: ProcessingService) -> None:
    """Test formatting history including an assistant message with a tool call."""
    tool_call_id = "call_123"
    tool_name = "get_weather"
    tool_args = {"location": "London"}
    tool_response = "Weather in London is sunny."

    history_messages = [
        {
            "role": "user",
            "content": "What's the weather like?",
            "tool_calls_info_raw": None,
        },
        {
            "role": "assistant",
            "content": None,  # Assistant might not provide text when calling tools
            "tool_calls": [  # Use the new key 'tool_calls' and OpenAI-like structure
                {
                    "id": tool_call_id,  # Corresponds to OpenAI tool_call 'id'
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(
                            tool_args
                        ),  # Arguments should be a JSON string in this format
                    },
                }
            ],
        },
        # This should represent the stored 'tool' response message
        {
            "role": "tool",
            "tool_call_id": tool_call_id,  # Required for tool messages
            "content": tool_response,  # The actual tool response content
        },
    ]

    expected_output = [
        {"role": "user", "content": "What's the weather like?"},
        # Assistant message requesting the tool
        {
            "role": "assistant",
            "tool_calls": [  # This should be passed through directly
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args),  # Arguments are stringified
                    },
                }
            ],
        },
        # Tool response message
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": tool_response,
        },
    ]

    actual_output = processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


def test_format_history_preserves_leading_tool_and_assistant_tool_calls(
    processing_service: ProcessingService,
) -> None:
    """
    Test that _format_history_for_llm correctly formats and preserves
    leading 'tool' messages and 'assistant' messages with 'tool_calls'.
    The actual pruning of such leading messages happens later in
    generate_llm_response_for_chat, which consumes the output of this method.
    """
    tool_call_id1 = "call_t1"
    tool_call_id2 = "call_a2"
    tool_name2 = "another_tool"
    tool_args2 = {"param": "value"}

    history_messages = [
        {
            "role": "tool",
            "tool_call_id": tool_call_id1,
            "content": "Response from first tool",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id2,
                    "type": "function",
                    "function": {
                        "name": tool_name2,
                        "arguments": json.dumps(tool_args2),
                    },
                }
            ],
        },
        {"role": "user", "content": "Follow-up user message"},
    ]

    expected_output = [
        {
            "role": "tool",
            "tool_call_id": tool_call_id1,
            "content": "Response from first tool",
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id2,
                    "type": "function",
                    "function": {
                        "name": tool_name2,
                        "arguments": json.dumps(tool_args2),
                    },
                }
            ],
        },
        {"role": "user", "content": "Follow-up user message"},
    ]

    actual_output = processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


def test_format_history_filters_errors(processing_service: ProcessingService) -> None:
    """Test that messages with role 'error' are filtered out."""
    history_messages = [
        {"role": "user", "content": "Try something", "tool_calls_info_raw": None},
        {
            "role": "error",
            "content": "Something went wrong",
            "error_traceback": "Traceback...",
            "tool_calls_info_raw": None,
        },
        {"role": "assistant", "content": "Okay", "tool_calls_info_raw": None},
    ]
    expected_output = [
        {"role": "user", "content": "Try something"},
        {"role": "assistant", "content": "Okay"},
    ]
    actual_output = processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


def test_format_history_handles_empty_tool_calls(
    processing_service: ProcessingService,
) -> None:
    """Test formatting an assistant message where tool_calls_info is explicitly empty."""
    history_messages = [
        {"role": "user", "content": "User message", "tool_calls_info_raw": None},
        {
            "role": "assistant",
            "content": "Assistant message",
            "tool_calls_info_raw": [],
        },  # Empty list
    ]
    expected_output = [
        {"role": "user", "content": "User message"},
        {
            "role": "assistant",
            "content": "Assistant message",
        },  # Should be treated as simple message
    ]
    actual_output = processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output
