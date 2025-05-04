"""
Unit tests for the history formatting logic in ProcessingService.
"""

import json
import pytest
from unittest.mock import Mock

from family_assistant.processing import ProcessingService


# Mock interfaces required by ProcessingService constructor
class MockLLMClient:
    async def generate_response(self, *args, **kwargs):
        return Mock()  # Not used in the tested method


class MockToolsProvider:
    async def get_tool_definitions(self, *args, **kwargs):
        return []  # Not used

    async def execute_tool(self, *args, **kwargs):
        pass  # Not used


# --- Test Setup ---


@pytest.fixture
def processing_service() -> ProcessingService:
    """Provides a ProcessingService instance with mock dependencies."""
    return ProcessingService(
        llm_client=MockLLMClient(),
        tools_provider=MockToolsProvider(),
        prompts={},  # Not used by _format_history_for_llm
        calendar_config={},  # Not used
        timezone_str="UTC",  # Not used
        max_history_messages=10,  # Not used
        server_url="http://test.com",  # Not used
        history_max_age_hours=1,  # Not used
    )


# --- Test Cases ---


def test_format_simple_history(processing_service: ProcessingService):
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
    assert actual_output == expected_output


def test_format_history_with_tool_call(processing_service: ProcessingService):
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
            "tool_calls_info_raw": [  # List containing info for each call
                {
                    "tool_call_id": tool_call_id, # FIX: Use the key 'tool_call_id' as stored in DB
                    "function_name": tool_name,
                    "arguments": tool_args,
                    "response_content": tool_response,
                }
            ],
        },
        # This should represent the stored 'tool' response message
        {
            "role": "tool",
            "tool_call_id": tool_call_id, # Required for tool messages
            "content": tool_response,     # The actual tool response content
        },
    ]

    expected_output = [
        {"role": "user", "content": "What's the weather like?"},
        # Assistant message requesting the tool
        {
            "role": "assistant",
            "content": None, # Keep None as content might be None
            "tool_calls": [
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


def test_format_history_filters_errors(processing_service: ProcessingService):
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


def test_format_history_handles_empty_tool_calls(processing_service: ProcessingService):
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
